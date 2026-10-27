"""Deletes rendered tiles: their Tile rows, and the zooms' folders in
TILE_CACHE_DIR (the tiles' files, and any left over). The tile jobs and the
idle sweep's tile rows for those zooms go too, so the sweep starts over. The
common files (TILE_CACHE_DIR/common) are kept: they are managed by hand. The
summaries are kept. Tiles are rendered again as they are asked for, or by the
idle renderers for the zooms marked "idle_tile_gen".

    manage.py clear_tiles                every zoom
    manage.py clear_tiles --zooms 9-12   those zooms only
    manage.py clear_tiles --noinput      without asking first
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tiles.management.commands.import_tiles import _zooms
from tiles.models import Job, Tile, TileSweep


class Command(BaseCommand):
	help = "Deletes rendered tiles, their files, and the tile sweep, for some zooms or all"

	def add_arguments(self, parser):
		parser.add_argument("--zooms", help="the zooms to clear, e.g. 9-12 (default: every zoom)")
		parser.add_argument("--noinput", "--no-input", action="store_false", dest="interactive",
			help="don't ask for confirmation")

	def handle(self, *args, zooms, interactive, **options):
		wanted = sorted(_zooms(zooms)) if zooms else list(range(23))
		named = "zooms {}".format(zooms) if zooms else "every zoom"
		if interactive:
			answer = input("Delete the rendered tiles of {}, and their files? [y/N] ".format(named))
			if answer.strip().lower() not in ("y", "yes"):
				raise CommandError("Nothing deleted")
		with transaction.atomic():
			tiles = Tile.objects.filter(zoom__in=wanted).delete()[0]
			jobs = Job.objects.filter(kind=Job.TILE, zoom__in=wanted).delete()[0]
			rows = TileSweep.objects.filter(kind=Job.TILE, zoom__in=wanted).delete()[0]
		cache = Path(settings.TILE_CACHE_DIR)
		folders = [cache / str(zoom) for zoom in wanted if (cache / str(zoom)).is_dir()]
		for folder in folders:
			shutil.rmtree(folder, ignore_errors=True)
		self.stdout.write("Deleted {} tiles, {} tile jobs and {} sweep rows; removed folders: {}".format(
			tiles, jobs, rows, ", ".join(folder.name for folder in folders) or "none"))
