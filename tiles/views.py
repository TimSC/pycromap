import json
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET

from . import queue
from .models import TileState
from .tilegeo import valid_tile

MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"


def _no_store(response):
	# A pending or uncached answer: a later request may get something else
	response["Cache-Control"] = "no-store"
	return response


def _respond(tile, cached):
	"""The response for a rendered tile."""
	if not tile.file:
		return _no_store(HttpResponse(status=204))
	if cached:
		# Each rendering has its own file, so the redirect must not be
		# remembered, but the file can be
		return _no_store(HttpResponseRedirect(settings.TILE_CACHE_URL + tile.file))
	path = Path(settings.TILE_CACHE_DIR) / tile.file
	return _no_store(FileResponse(open(path, "rb"), content_type=MVT_CONTENT_TYPE))


@require_GET
def tile(request, zoom, x, y):
	config = queue.zoom_config(zoom)
	if config is None or not valid_tile(zoom, x, y):
		raise Http404("Zoom {} is not served".format(zoom))
	cached = config.get("cache", False)

	# A cached zoom needs rendering only if it hasn't been, or its file is gone
	existing = TileState.objects.filter(zoom=zoom, x=x, y=y).first()
	missing = (existing is None or existing.status != TileState.DONE
		or (existing.file and not (Path(settings.TILE_CACHE_DIR) / existing.file).exists()))
	tile, queued_at = queue.request_tile(zoom, x, y, rerender=not cached or missing)

	if queued_at is not None:
		tile = queue.wait_for_tile(tile.pk, queued_at, settings.TILE_REQUEST_WAIT)
		if tile is None:
			response = _no_store(HttpResponse("The tile is not ready", status=503, content_type="text/plain"))
			response["Retry-After"] = "2"
			return response
	return _respond(tile, cached)


@require_GET
def tilejson(request):
	"""TileJSON for the tiles served."""
	zooms = sorted(settings.TILE_ZOOMS)
	west, south, east, north = (float(v) for v in settings.MAP_INITIAL_BBOX.split(","))
	return JsonResponse({
		"tilejson": "2.1.0",
		"name": "pycromap",
		"scheme": "xyz",
		"tiles": ["/tiles/{z}/{x}/{y}.pbf"],
		"minzoom": zooms[0],
		"maxzoom": zooms[-1],
		"bounds": [-180, -85.05112877980659, 180, 85.0511287798066],
		"center": [(west + east) / 2, (south + north) / 2, zooms[-1]],
	})


@require_GET
def style(request):
	"""The map style, with its glyphs served from the static files."""
	with open(finders.find("tiles/style.json")) as f:
		style = json.load(f)
	style["sources"]["openmaptiles"] = {"type": "vector", "url": "/spec.json"}
	style["glyphs"] = static("tiles/fonts/") + "{fontstack}/{range}.pbf"
	return JsonResponse(style)


@require_GET
def metadata(request):
	"""Where the map page opens."""
	zooms = sorted(settings.TILE_ZOOMS)
	return JsonResponse({"name": "pycromap", "format": "pbf", "bounds": settings.MAP_INITIAL_BBOX,
		"minzoom": str(zooms[0]), "maxzoom": str(zooms[-1])})


@require_GET
def index(request):
	return render(request, "tiles/index.html")
