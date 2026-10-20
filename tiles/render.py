"""Rendering a tile: the map data of its area, given to tilemaker-core."""

import gzip
import logging
import os
import subprocess
import tempfile
import time

import pyo5m
from django.conf import settings

from mapdata.reader import read_area

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


def render_tile(zoom, x, y):
	"""Renders a tile from the map database. Returns the tile, as an
	uncompressed vector tile (b"" if there was nothing to draw), and the
	version of the map data it is from."""
	started = time.monotonic()
	data, version = read_area(*tile_area(zoom, x, y))
	read = time.monotonic()

	with tempfile.TemporaryDirectory(prefix="pycromap-") as work:
		input_path = os.path.join(work, "area." + settings.TILEMAKER["input_format"])
		pyo5m.save(data, input_path)
		tile = run_tilemaker(input_path, zoom, x, y, os.path.join(work, "tiles"))

	# Kept uncompressed, so that a static file server can serve it as it is
	if tile[:2] == b"\x1f\x8b":
		tile = gzip.decompress(tile)
	logger.info("Rendered %s/%s/%s: %d bytes, version %s (read %.2fs, render %.2fs)",
		zoom, x, y, len(tile), version, read - started, time.monotonic() - read)
	return tile, version
