"""Rendering a tile: the map data of its area, given to tilemaker-core."""

import gzip
import logging
import os
import subprocess
import tempfile

import pyo5m
from django.conf import settings

from mapdata.source import read_area

from .tilegeo import tile_area

logger = logging.getLogger(__name__)


class RenderError(Exception):
	pass


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
