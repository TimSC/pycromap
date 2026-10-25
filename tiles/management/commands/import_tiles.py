"""Records the tile files already in TILE_CACHE_DIR as rendered tiles (Tile
rows), as after the database was recreated: so they are served, and the idle
sweep takes them for done, rather than rendering them again.

Each file zoom/x/y.version.pbf.gz (see queue.tile_file) is a tile of a cached
zoom, rendered from that version of the map data, at the file's time. Of
several versions of a tile, the newest is taken. A tile that already has a
row is left alone. Tiles that pointed at common files, or were rendered empty,
had no file of their own, so aren't found: they are rendered again.

    manage.py import_tiles                every cached zoom
    manage.py import_tiles --zooms 0-8    those zooms only
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tiles.models import Tile
from tiles.tilegeo import tile_key, valid_tile

FILE_NAME = re.compile(r"^(\d+)\.(\d+)\.pbf\.gz$")
BATCH = 5000


def _zooms(spec):
	"""The zooms of "0-8", "3", or "0-4,6"."""
	zooms = set()
	for part in spec.split(","):
		first, _, last = part.partition("-")
		zooms.update(range(int(first), int(last or first) + 1))
	return zooms


class Command(BaseCommand):
	help = "Records the tile files in the tile cache as rendered tiles"

	def add_arguments(self, parser):
		parser.add_argument("--zooms", help="the zooms to import, e.g. 0-8 (default: every cached zoom)")

	def handle(self, *args, zooms, **options):
		cached = {zoom for zoom, config in settings.TILE_ZOOMS.items() if config.get("cache")}
		wanted = _zooms(zooms) if zooms else cached
		uncached = sorted(wanted - cached)
		if uncached:
			raise CommandError("not cached zooms (TILE_ZOOMS): {}".format(uncached))
		cache = Path(settings.TILE_CACHE_DIR)
		for zoom in sorted(wanted):
			found, skipped = self.import_zoom(cache, zoom)
			self.stdout.write("z{}: {} tiles recorded, {} files skipped".format(zoom, found, skipped))

	def import_zoom(self, cache, zoom):
		"""Records a zoom's files. Returns (tiles recorded, files skipped)."""
		newest, skipped = {}, 0  # (x, y): (version, path)
		folder = cache / str(zoom)
		for directory, _, names in os.walk(folder):
			column = Path(directory).relative_to(folder)
			for name in names:
				match = FILE_NAME.match(name)
				if len(column.parts) != 1 or not column.parts[0].isdecimal() or not match:
					skipped += 1
					continue
				x, y, version = int(column.parts[0]), int(match[1]), int(match[2])
				if not valid_tile(zoom, x, y):
					skipped += 1
					continue
				if (x, y) in newest:
					skipped += 1
					if newest[(x, y)][0] >= version:
						continue
				newest[(x, y)] = (version, Path(directory) / name)
		rows = []
		for (x, y), (version, path) in sorted(newest.items(), key=lambda item: tile_key(zoom, *item[0])):
			rendered = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
			rows.append(Tile(id=tile_key(zoom, x, y), zoom=zoom, x=x, y=y, rendered_at=rendered,
				data_version=version, file=path.relative_to(cache).as_posix()))
		before = Tile.objects.filter(zoom=zoom).count()
		for i in range(0, len(rows), BATCH):
			Tile.objects.bulk_create(rows[i:i + BATCH], ignore_conflicts=True)
		return Tile.objects.filter(zoom=zoom).count() - before, skipped
