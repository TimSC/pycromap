import asyncio
import json
import logging
import os
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils import timezone
from django.utils._os import safe_join
from django.views.decorators.http import require_GET

from mapdata import source
from summaries import build as summaries

from . import queue
from .models import Tile, TileRequest
from .tilegeo import tile_area, tile_key, valid_tile

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
	"""Whether a tile is rendered on demand. At or above CASCADE_MIN_ZOOM,
	always, as the renderer that claims it builds whatever summaries it needs
	that are missing, all the way down. Below it, only if each summary it is
	made from is built, or can be built straight away (what that is made from
	is all built, or it is built from the map data): the renderer then builds
	those missing, one level, not what they are made from, after the requests
	at or above it (see queue._claim_requested). One that can't gets a
	placeholder at once, rather than wait in the queue for summaries that may
	be long in coming, so the renderers never look at it."""
	if zoom >= settings.CASCADE_MIN_ZOOM or config["source"] != "summary":
		return True
	# Two queries: the summaries it is made from, and those beneath them
	level = config["level"]
	scale = 1 << (level - zoom)
	x0, y0 = x * scale, y * scale
	built = summaries.built_in(level, x0, x0 + scale - 1, y0, y0 + scale - 1)
	missing = [(sx, sy) for sx in range(x0, x0 + scale) for sy in range(y0, y0 + scale) if (sx, sy) not in built]
	finer = summaries.finer_level(level)
	if not missing or finer is None:
		return True
	f = 1 << (finer - level)
	beneath = summaries.built_in(finer, x0 * f, (x0 + scale) * f - 1, y0 * f, (y0 + scale) * f - 1)
	return all((fx, fy) in beneath for sx, sy in missing
		for fx in range(sx * f, (sx + 1) * f) for fy in range(sy * f, (sy + 1) * f))


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
	of what it was made from (see mapdata.source). The map is asked at most
	once every EDIT_CHECK_INTERVAL seconds for a tile, counting from when it
	was rendered or last found unedited; in between, it is taken to be as it
	was, as it is if the map can't say."""
	now = timezone.now()
	last = max(t for t in (tile.rendered_at, tile.edits_checked_at) if t is not None)
	if (now - last).total_seconds() < settings.EDIT_CHECK_INTERVAL:
		return False
	try:
		edited = source.changed_since(tile.data_version or 0, *tile_area(tile.zoom, tile.x, tile.y))
	except source.UNAVAILABLE as e:
		logger.warning("Couldn't tell whether %d/%d/%d has been edited: %s", tile.zoom, tile.x, tile.y, e)
		return False
	if not edited:
		# Rendering it again starts the wait afresh, from rendered_at
		Tile.objects.filter(pk=tile.pk).update(edits_checked_at=now)
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
	if existing is None:
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


@sync_to_async
def _ask_for(zoom, x, y, config, request_id):
	"""If a tile needs rendering, queues the request for it
	(queue.wait_for_render): (its Tile if it needn't be rendered, whether
	the request waits for a renderer, None), or (None, False, the response to
	give instead: a placeholder)."""
	existing = Tile.objects.filter(pk=tile_key(zoom, x, y)).first()
	if not _needs_rendering(existing, config):
		return existing, False, None
	if not _can_render(zoom, x, y, config):
		return None, False, _placeholder()
	queue.wait_for_render(request_id)
	return None, True, None


async def _obtain(zoom, x, y, config, request_id):
	"""A tile, rendered if need be: (its state, None), or (None, the response
	to give instead: a placeholder, or that it isn't ready yet). The request
	(request_id) is the queue's entry for it; waiting for a renderer to
	render it holds no thread."""
	tile, waiting, response = await _ask_for(zoom, x, y, config, request_id)
	if response is not None:
		return None, response
	if not waiting:
		return tile, None
	status = await queue.await_render(request_id, settings.TILE_REQUEST_WAIT)
	if status == TileRequest.DONE:
		return await Tile.objects.aget(pk=tile_key(zoom, x, y)), None
	response = _no_store(HttpResponse("The tile couldn't be rendered" if status == TileRequest.FAILED else
		"The tile is not ready", status=503, content_type="text/plain"))
	response["Retry-After"] = "2"
	return None, response


@require_GET
async def tile(request, zoom, x, y):
	"""A tile. Asynchronous, so that waiting for the renderer holds no thread,
	and so that Django can cancel it when the client goes away, as a map does
	with the tiles it no longer shows; under ASGI, that is.

	Each request is kept as a TileRequest while it is answered: the queue's
	entry, if its tile must be rendered. It is deleted once the
	request is answered (whatever the answer), or flagged cancelled if the
	client goes away first, not to be rendered for it, and deleted later
	(queue.forget_requests)."""
	config = queue.zoom_config(zoom)
	if config is None or not valid_tile(zoom, x, y):
		raise Http404("Zoom {} is not served".format(zoom))

	tracked = await TileRequest.objects.acreate(zoom=zoom, x=x, y=y)
	try:
		response = await _answer(config, zoom, x, y, tracked.pk)
	except asyncio.CancelledError:
		# Shielded, so that it is done though the view is being cancelled
		await asyncio.shield(TileRequest.objects.filter(pk=tracked.pk).aupdate(
			cancelled=True, cancelled_at=timezone.now()))
		raise
	except BaseException:
		await asyncio.shield(TileRequest.objects.filter(pk=tracked.pk).adelete())
		raise
	await TileRequest.objects.filter(pk=tracked.pk).adelete()
	return response


async def _answer(config, zoom, x, y, request_id):
	"""The response to a request for a tile of a served zoom."""
	tile, response = await _obtain(zoom, x, y, config, request_id)
	if response is not None:
		return response
	cached = config.get("cache", False)
	if cached and tile.file:
		# A redirect to the cached file: flagged, as nothing needs rendering
		await queue.answered_by_cache(request_id)
	return _respond(tile, cached)


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
	if first == queue.COMMON_DIR and len(name) == 32:
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
