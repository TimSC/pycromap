"""Rendering a tile: the map data of its area, given to tilemaker-core."""

import gzip
import logging
import os
import subprocess
import tempfile

import mapbox_vector_tile
import pyo5m
from django.conf import settings
from shapely.geometry import box, shape

from mapdata.reader import read_area

from .tilegeo import tile_area

logger = logging.getLogger(__name__)


class RenderError(Exception):
	pass


def shared_kind(tile):
	"""What a rendered tile (gzipped or not) is, if it is the same as many
	others are: "empty", with nothing drawn, as over land with nothing mapped,
	or "uniform", one feature covering the whole of it, as over open sea. None
	for anything else, or what can't be read as a vector tile. Tiles of a
	kind with the same content can share one file (see queue.finish)."""
	if not tile:
		return "empty"
	try:
		raw = gzip.decompress(tile) if tile[:2] == b"\x1f\x8b" else tile
		layers = mapbox_vector_tile.decode(raw, default_options={"y_coord_down": True})
	except Exception:
		return None
	features = [(layer["extent"], f) for layer in layers.values() for f in layer["features"]]
	if not features:
		return "empty"
	if len(features) != 1:
		return None
	extent, feature = features[0]
	try:
		geometry = shape(feature["geometry"])
		if geometry.geom_type in ("Polygon", "MultiPolygon") and geometry.covers(box(0, 0, extent, extent)):
			return "uniform"
	except Exception:
		pass
	return None


def run_tilemaker(input_path, zoom, x, y, output_dir):
	"""Renders one tile from a file of map data into output_dir. Returns the
	tile's bytes (gzipped or not, as the config says), or b"" if there was
	nothing to draw."""
	tm = settings.TILEMAKER
	args = [tm["program"], "--input", str(input_path), "--tile", "{}/{}/{}".format(zoom, x, y),
		"--output", str(output_dir), "--config", tm["config"], "--process", tm["process"], "--quiet"]
	try:
		result = subprocess.run(args, cwd=tm["directory"], capture_output=True, text=True, timeout=tm["timeout"])
	except subprocess.TimeoutExpired:
		raise RenderError("tilemaker took over {} seconds".format(tm["timeout"]))
	except OSError as e:
		raise RenderError("Couldn't run tilemaker: {}".format(e))
	if result.returncode != 0:
		# The last lines of its output say why; drop the noise about
		# shapefiles that aren't there
		lines = [l for l in result.stderr.splitlines() if not l.startswith("Unable to open")]
		raise RenderError("tilemaker failed ({}): {}".format(result.returncode, " / ".join(lines[-5:])))

	path = os.path.join(output_dir, str(zoom), str(x), "{}.pbf".format(y))
	if not os.path.exists(path):
		return b""
	with open(path, "rb") as f:
		return f.read()


def read_tile_data(zoom, x, y):
	"""The map data of a tile's area, and the version of the map data."""
	return read_area(*tile_area(zoom, x, y))


def render_data(data, zoom, x, y):
	"""Renders a tile from map data (a pyo5m OsmData). Returns it as a gzipped
	vector tile, or b"" if there was nothing to draw."""
	with tempfile.TemporaryDirectory(prefix="pycromap-") as work:
		input_path = os.path.join(work, "area." + settings.TILEMAKER["input_format"])
		pyo5m.save(data, input_path)
		tile = run_tilemaker(input_path, zoom, x, y, os.path.join(work, "tiles"))
	# Gzipped, whatever tilemaker's config says, as tiles are kept and sent
	if tile and tile[:2] != b"\x1f\x8b":
		tile = gzip.compress(tile)
	return tile
