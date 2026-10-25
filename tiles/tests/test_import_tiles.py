import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from tiles.models import Tile
from tiles.tilegeo import tile_key

ZOOMS = {3: {"source": "summary", "level": 4, "cache": True},
	4: {"source": "data", "cache": True, "summary": True},
	5: {"source": "data", "cache": False}}


@override_settings(TILE_ZOOMS=ZOOMS)
class ImportTilesTests(TestCase):
	def setUp(self):
		self.cache = tempfile.TemporaryDirectory()
		self.settings = override_settings(TILE_CACHE_DIR=Path(self.cache.name))
		self.settings.enable()

	def tearDown(self):
		self.settings.disable()
		self.cache.cleanup()

	def file(self, path):
		full = Path(self.cache.name) / path
		full.parent.mkdir(parents=True, exist_ok=True)
		full.write_bytes(b"tile")

	def test_records_the_files_as_rendered_tiles(self):
		for path in ("3/1/2.7.pbf.gz", "4/5/6.3.pbf.gz", "4/5/6.9.pbf.gz", "4/5/6.9.pbf.gz.tmp12",
				"4/99/0.1.pbf.gz", "common/sea.pbf.gz"):
			self.file(path)
		out = StringIO()
		call_command("import_tiles", stdout=out)
		self.assertIn("z3: 1 tiles recorded, 0 files skipped", out.getvalue())
		# The newest of two versions; not a temporary file, or a tile off the grid
		self.assertIn("z4: 1 tiles recorded, 3 files skipped", out.getvalue())
		self.assertEqual(set(Tile.objects.values_list("pk", "zoom", "x", "y", "data_version", "file")),
			{(tile_key(3, 1, 2), 3, 1, 2, 7, "3/1/2.7.pbf.gz"), (tile_key(4, 5, 6), 4, 5, 6, 9, "4/5/6.9.pbf.gz")})

	def test_a_tile_already_recorded_is_left_alone(self):
		Tile.objects.create(zoom=3, x=1, y=2, rendered_at=timezone.now(), data_version=8, file="3/1/2.8.pbf.gz")
		self.file("3/1/2.7.pbf.gz")
		call_command("import_tiles", "--zooms", "3", stdout=StringIO())
		self.assertEqual(Tile.objects.get().data_version, 8)

	def test_only_cached_zooms(self):
		with self.assertRaises(CommandError):
			call_command("import_tiles", "--zooms", "4-5", stdout=StringIO())
