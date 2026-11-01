"""Writes the rendered tiles to one MBTiles file, which go-pmtiles can make a
PMTiles archive of (pmtiles convert). Tiles with nothing in them are left out,
as are those not rendered yet: the file holds what has been rendered so far,
as it is read, while renderers may go on working.

The file's description comes from tilemaker, for the profile in use (see
tilemaker_metadata). It is written beside the output, as OUTPUT.tmp, and takes
its name only once it is complete.

    manage.py export_mbtiles map.mbtiles              every zoom
    manage.py export_mbtiles map.mbtiles --zooms 0-8  those zooms only
    manage.py export_mbtiles map.mbtiles --force      replacing the file
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pyo5m
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tiles import queue
from tiles.management.commands.import_tiles import _zooms
from tiles.models import Tile
from tiles.render import run_tilemaker

BATCH = 5000
# The whole map: that of the tiles served (tiles.views.tilejson)
BOUNDS = "-180,-85.0511287798066,180,85.0511287798066"
MISSING_SHOWN = 10


def tilemaker_metadata():
	"""The metadata tilemaker writes to an MBTiles file, as (name, value) pairs:
	the map's name and its layers, from the profile. It is got by rendering a
	tile with nothing in it, so what depends on that tile (the zooms and the
	area) isn't meant to be kept."""
	with tempfile.TemporaryDirectory(prefix="pycromap-") as work:
		input_path = os.path.join(work, "empty." + settings.TILEMAKER["input_format"])
		pyo5m.save(pyo5m.OsmData(), input_path)
		output = os.path.join(work, "metadata.mbtiles")
		run_tilemaker(input_path, max(settings.TILE_ZOOMS), 0, 0, output)
		connection = sqlite3.connect(output)
		try:
			return connection.execute("SELECT name, value FROM metadata").fetchall()
		finally:
			connection.close()


def tile_rows(wanted):
	"""The tiles of some zooms, in key order, as (zoom, x, y, file, data), in
	batches: no read of the database is held open for the whole export, which
	would keep its write-ahead log from being cleared."""
	after = -1
	while True:
		batch = list(Tile.objects.filter(pk__gt=after, zoom__in=wanted).order_by("pk")
			.values_list("pk", "zoom", "x", "y", "file", "data")[:BATCH])
		if not batch:
			return
		after = batch[-1][0]
		yield [row[1:] for row in batch]


class Command(BaseCommand):
	help = "Writes the rendered tiles to one MBTiles file"

	def add_arguments(self, parser):
		parser.add_argument("output", help="the MBTiles file to write")
		parser.add_argument("--zooms", help="the zooms to export, e.g. 0-8 (default: every zoom served)")
		parser.add_argument("--force", action="store_true", help="replace the file if it exists")

	def handle(self, *args, output, zooms, force, **options):
		wanted = _zooms(zooms) if zooms else set(settings.TILE_ZOOMS)
		unserved = sorted(wanted - set(settings.TILE_ZOOMS))
		if unserved:
			raise CommandError("zooms not served (TILE_ZOOMS): {}".format(unserved))
		output = Path(output)
		if output.exists() and not force:
			raise CommandError("{} exists (--force to replace it)".format(output))

		metadata = tilemaker_metadata()
		partial = output.with_name(output.name + ".tmp")
		partial.unlink(missing_ok=True)
		try:
			self.write_file(partial, wanted, metadata)
			os.replace(partial, output)
		finally:
			partial.unlink(missing_ok=True)
		self.stdout.write("Wrote {}".format(output))

	def write_file(self, path, wanted, metadata):
		connection = sqlite3.connect(path)
		try:
			# Nothing to recover if this stops: the file isn't used until complete
			connection.execute("PRAGMA journal_mode=OFF")
			connection.execute("PRAGMA synchronous=OFF")
			connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
			connection.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, "
				"tile_data BLOB)")
			counts, missing = self.write_tiles(connection, wanted)
			if not counts:
				raise CommandError("There are no rendered tiles to export")
			connection.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
			connection.execute("CREATE UNIQUE INDEX metadata_index ON metadata (name)")
			connection.executemany("INSERT INTO metadata VALUES (?, ?)", self.file_metadata(metadata, counts))
			connection.commit()
		finally:
			connection.close()
		for zoom, count in sorted(counts.items()):
			self.stdout.write("z{}: {} tiles".format(zoom, count))
		if missing:
			self.stderr.write("{} tiles left out, their files missing: {}".format(len(missing),
				", ".join(missing[:MISSING_SHOWN])))

	def write_tiles(self, connection, wanted):
		"""Writes the tiles. Returns the number at each zoom, and the names of
		those whose files are missing."""
		cache = Path(settings.TILE_CACHE_DIR)
		common = {}  # A common file's contents: millions of tiles may point at one
		counts, missing = {}, []
		for batch in tile_rows(wanted):
			rows = []
			for zoom, x, y, file, data in batch:
				if file:
					shared = file.startswith(queue.COMMON_DIR + "/")
					data = common.get(file) if shared else None
					if data is None:
						try:
							data = (cache / file).read_bytes()
						except FileNotFoundError:
							missing.append("{}/{}/{}".format(zoom, x, y))
							continue
						if shared:
							common[file] = data
				elif not data:
					continue  # Nothing in it
				# MBTiles counts its rows up from the south
				rows.append((zoom, x, (1 << zoom) - 1 - y, bytes(data)))
				counts[zoom] = counts.get(zoom, 0) + 1
			connection.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", rows)
		return counts, missing

	def file_metadata(self, metadata, counts):
		"""tilemaker's metadata, with what describes the tiles of this file."""
		west, south, east, north = (float(v) for v in settings.MAP_INITIAL_BBOX.split(","))
		ours = {"format": "pbf", "minzoom": str(min(counts)), "maxzoom": str(max(counts)), "bounds": BOUNDS,
			"center": "{},{},{}".format((west + east) / 2, (south + north) / 2, max(counts))}
		return list({**dict(metadata), **ours}.items())
