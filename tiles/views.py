import json
import logging
import os
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils import timezone
from django.utils._os import safe_join
from django.views.decorators.http import require_GET

from mapdata import changes
from summaries import build as summaries

from . import queue
from .crop import crop
from .models import TileState
from .tilegeo import tile_area, valid_tile

logger = logging.getLogger(__name__)

MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"


def _no_store(response):
	# A pending or uncached answer: a later request may get something else
	response["Cache-Control"] = "no-store"
	return response


def _placeholder():
	"""Stands in for a tile whose summaries won't be built soon: empty, and
	not to be remembered."""
	response = _no_store(HttpResponse(status=204))
	response["X-Tile-Status"] = "placeholder"
	return response


def _can_render(zoom, x, y, config):
	"""Whether a tile can be rendered soon: from map data, or from summaries
	that exist or can be built within SUMMARY_MAX_DEPTH (see
	summaries.build.obtainable). The tile's job is depth 0, so the jobs for
	its summaries would be depth 1."""
	if config["source"] != "summary":
		return True
	needed = summaries.tiles_beneath(zoom, x, y, config["level"])
	return all(summaries.obtainable(*tile, 1) for tile in summaries.missing(needed))


def _gzipped_tile(response):
	response["Content-Encoding"] = "gzip"
	return response


def _respond(tile, cached):
	"""The response for a rendered tile."""
	if cached:
		if not tile.file:
			return _no_store(HttpResponse(status=204))
		# Each rendering has its own file, so the redirect must not be
		# remembered, but the file can be
		return _no_store(HttpResponseRedirect(settings.TILE_CACHE_URL + tile.file))
	if not tile.data:
		return _no_store(HttpResponse(status=204))
	return _no_store(_gzipped_tile(HttpResponse(bytes(tile.data), content_type=MVT_CONTENT_TYPE)))


def _edited_since(tile):
	"""Whether the map in a tile's area has been edited since the newest edit
	of what it was made from (see mapdata.changes). The map database is asked
	at most once every EDIT_CHECK_INTERVAL seconds for a tile, counting from
	when it was rendered or last found unedited; in between, it is taken to
	be as it was, as it is if the map database can't say."""
	now = timezone.now()
	last = max(t for t in (tile.rendered_at, tile.edits_checked_at) if t is not None)
	if (now - last).total_seconds() < settings.EDIT_CHECK_INTERVAL:
		return False
	try:
		edited = changes.changed_since(tile.data_version or 0, *tile_area(tile.zoom, tile.x, tile.y))
	except DatabaseError as e:
		logger.warning("Couldn't tell whether %d/%d/%d has been edited: %s", tile.zoom, tile.x, tile.y, e)
		return False
	if not edited:
		# Rendering it again starts the wait afresh, from rendered_at
		TileState.objects.filter(pk=tile.pk).update(edits_checked_at=now)
	return edited


def _needs_rendering(existing, config):
	"""Whether a tile needs rendering (again) for a request: an uncached zoom's
	always; a cached zoom's if it hasn't been, its file is gone, it was
	rendered while the zoom wasn't cached (its data is in the database, not a
	file), it is older than the zoom's max_age (seconds), or, for one made
	from the map data, the map in its area has been edited since (unless the
	zoom's check_edits is False)."""
	if not config.get("cache", False):
		return True
	if existing is None or existing.status != TileState.DONE or existing.rendered_at is None:
		return True
	if existing.file and not (Path(settings.TILE_CACHE_DIR) / existing.file).exists():
		return True
	if not existing.file and existing.data:
		return True
	max_age = config.get("max_age")
	if max_age and (timezone.now() - existing.rendered_at).total_seconds() > max_age:
		return True
	if config["source"] == "data" and config.get("check_edits", True):
		return _edited_since(existing)
	return False


def _obtain(zoom, x, y, config):
	"""A tile, rendered if need be: (its state, None), or (None, the response
	to give instead: a placeholder, or that it isn't ready yet)."""
	existing = TileState.objects.filter(kind=TileState.TILE, zoom=zoom, x=x, y=y).first()
	rerender = _needs_rendering(existing, config)
	if rerender and not _can_render(zoom, x, y, config):
		queue.log_request(zoom, x, y)
		return None, _placeholder()
	tile, queued_at = queue.request_tile(zoom, x, y, rerender=rerender)
	if queued_at is not None:
		tile = queue.wait_for_tile(tile.pk, queued_at, settings.TILE_REQUEST_WAIT)
		if tile is None:
			response = _no_store(HttpResponse("The tile is not ready", status=503, content_type="text/plain"))
			response["Retry-After"] = "2"
			return None, response
	return tile, None


def _tile_data(tile, cached):
	"""A rendered tile's gzipped data (b"" if it is empty)."""
	if cached:
		if not tile.file:
			return b""
		with open(Path(settings.TILE_CACHE_DIR) / tile.file, "rb") as f:
			return f.read()
	return bytes(tile.data or b"")


@require_GET
def tile(request, zoom, x, y):
	config = queue.zoom_config(zoom)
	if config is None or not valid_tile(zoom, x, y):
		raise Http404("Zoom {} is not served".format(zoom))

	if config["source"] == "crop":
		# Cut from the tile covering it at a lower zoom, which is obtained as
		# a request for that tile would be
		parent_zoom = config["from"]
		parent_config = queue.zoom_config(parent_zoom)
		scale = 1 << (zoom - parent_zoom)
		parent_x, parent_y = x // scale, y // scale
		queue.log_request(zoom, x, y)
		parent, response = _obtain(parent_zoom, parent_x, parent_y, parent_config)
		if response is not None:
			return response
		data = crop(_tile_data(parent, parent_config.get("cache", False)), parent_zoom, parent_x, parent_y, zoom, x, y)
		if not data:
			return _no_store(HttpResponse(status=204))
		return _no_store(_gzipped_tile(HttpResponse(data, content_type=MVT_CONTENT_TYPE)))

	tile, response = _obtain(zoom, x, y, config)
	if response is not None:
		return response
	return _respond(tile, config.get("cache", False))


@require_GET
def cached_tile(request, path):
	"""A cached tile's file, while DEBUG is on; in production the web server
	serves TILE_CACHE_DIR, with the same headers (see README.md)."""
	if not path.endswith(".pbf.gz"):
		raise Http404()
	full = safe_join(settings.TILE_CACHE_DIR, path)
	if not os.path.isfile(full):
		raise Http404()
	response = _gzipped_tile(FileResponse(open(full, "rb"), content_type=MVT_CONTENT_TYPE))
	first, name = path.split("/")[0], path.split("/")[-1][:-len(".pbf.gz")]
	if first == queue.SHARED_DIR or (first == queue.COMMON_DIR and len(name) == 32):
		# Named by its content, so a name never has other content: kept for good
		response["Cache-Control"] = "public, max-age=31536000, immutable"
		return response
	if first == queue.COMMON_DIR:
		# A common tile named for what it shows, which a new tilemaker profile
		# could change: kept a day
		response["Cache-Control"] = "public, max-age=86400"
		return response
	# The name has the version in it, so a rendering of a given version never
	# changes; but a re-rendering at the same version (a new tilemaker profile)
	# must reach the client, so the client's hold on it is the zoom's max_age
	# (a day for a zoom without one, or one no longer served)
	config = queue.zoom_config(int(first)) if first.isdecimal() else None
	max_age = (config or {}).get("max_age") or 86400
	response["Cache-Control"] = "public, max-age={}".format(max_age)
	return response


@require_GET
def tilejson(request):
	"""TileJSON for the tiles served, as fresh as the style it is fetched with."""
	zooms = sorted(settings.TILE_ZOOMS)
	west, south, east, north = (float(v) for v in settings.MAP_INITIAL_BBOX.split(","))
	body = {
		"tilejson": "2.1.0",
		"name": "pycromap",
		"scheme": "xyz",
		"tiles": ["/tiles/{z}/{x}/{y}.pbf"],
		"minzoom": zooms[0],
		"maxzoom": zooms[-1],
		"bounds": [-180, -85.05112877980659, 180, 85.0511287798066],
		"center": [(west + east) / 2, (south + north) / 2, zooms[-1]],
	}
	return _no_store(JsonResponse(body))


@require_GET
def style(request):
	"""The map style, with its glyphs served from the static files."""
	with open(finders.find("tiles/style.json")) as f:
		style = json.load(f)
	style["sources"]["openmaptiles"] = {"type": "vector", "url": "/spec.json"}
	style["glyphs"] = static("tiles/fonts/") + "{fontstack}/{range}.pbf"
	# The style changes with the code, so it must not be heuristically cached
	return _no_store(JsonResponse(style))


@require_GET
def metadata(request):
	"""Where the map page opens."""
	zooms = sorted(settings.TILE_ZOOMS)
	return _no_store(JsonResponse({"name": "pycromap", "format": "pbf", "bounds": settings.MAP_INITIAL_BBOX,
		"minzoom": str(zooms[0]), "maxzoom": str(zooms[-1]), "viewer_maxzoom": settings.VIEWER_MAX_ZOOM}))


@require_GET
def index(request):
	return render(request, "tiles/index.html")
