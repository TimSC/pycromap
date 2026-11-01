import sqlite3
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from tiles.models import Tile

ZOOMS = {3: {"source": "summary", "level": 4, "cache": True},
	4: {"source": "data", "cache": True, "summary": True},
	5: {"source": "data", "cache": False}}
METADATA = [("name", "pycromap"), ("json", '{"vector_layers":[]}'), ("minzoom", "12"), ("bounds", "1,2,3,4")]


@override_settings(TILE_ZOOMS=ZOOMS, MAP_INITIAL_BBOX="-2,50,0,52")
class ExportMbtilesTests(TestCase):
	def setUp(self):
		self.work = tempfile.TemporaryDirectory()
		self.cache = Path(self.work.name) / "cache"
		self.output = Path(self.work.name) / "map.mbtiles"
		self.settings = override_settings(TILE_CACHE_DIR=self.cache)
		self.settings.enable()
		patcher = patch("tiles.management.commands.export_mbtiles.tilemaker_metadata", return_value=METADATA)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		self.settings.disable()
		self.work.cleanup()

	def tile(self, zoom, x, y, file="", data=None, contents=None):
		if contents is not None:
			(self.cache / file).parent.mkdir(parents=True, exist_ok=True)
			(self.cache / file).write_bytes(contents)
		Tile.objects.create(zoom=zoom, x=x, y=y, rendered_at=timezone.now(), file=file, data=data)

	def export(self, *args):
		out, err = StringIO(), StringIO()
		call_command("export_mbtiles", str(self.output), *args, stdout=out, stderr=err)
		return out.getvalue(), err.getvalue()

	def read(self, query):
		connection = sqlite3.connect(self.output)
		try:
			return connection.execute(query).fetchall()
		finally:
			connection.close()

	def test_writes_tiles_with_rows_counted_from_the_south(self):
		self.tile(3, 1, 2, "3/1/2.7.pbf.gz", contents=b"own")
		self.tile(4, 5, 6, "common/sea.pbf.gz", contents=b"sea")
		self.tile(4, 5, 7, "common/sea.pbf.gz")
		self.tile(5, 1, 0, data=b"stored")
		self.tile(3, 0, 0)  # Nothing in it
		out, err = self.export()
		self.assertEqual(self.read("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles ORDER BY 1, 2, 3"), [
			(3, 1, 5, b"own"), (4, 5, 8, b"sea"), (4, 5, 9, b"sea"), (5, 1, 31, b"stored")])
		self.assertIn("z4: 2 tiles", out)
		self.assertEqual(err, "")
		self.assertFalse(self.output.with_name("map.mbtiles.tmp").exists())

	def test_metadata_is_tilemakers_with_what_describes_these_tiles(self):
		self.tile(4, 5, 6, data=b"a")
		self.tile(5, 1, 0, data=b"b")
		self.export()
		self.assertEqual(dict(self.read("SELECT name, value FROM metadata")), {"name": "pycromap",
			"json": '{"vector_layers":[]}', "format": "pbf", "minzoom": "4", "maxzoom": "5",
			"bounds": "-180,-85.0511287798066,180,85.0511287798066", "center": "-1.0,51.0,5"})

	def test_only_the_zooms_given(self):
		self.tile(4, 5, 6, data=b"a")
		self.tile(5, 1, 0, data=b"b")
		self.export("--zooms", "5")
		self.assertEqual(self.read("SELECT zoom_level FROM tiles"), [(5,)])
		with self.assertRaises(CommandError):
			self.export("--zooms", "6", "--force")

	def test_a_missing_file_is_reported_and_left_out(self):
		self.tile(4, 5, 6, "4/5/6.1.pbf.gz")
		self.tile(4, 5, 7, data=b"a")
		_, err = self.export()
		self.assertIn("1 tiles left out, their files missing: 4/5/6", err)
		self.assertEqual(self.read("SELECT tile_row FROM tiles"), [(8,)])

	def test_an_existing_file_is_kept_unless_forced(self):
		self.tile(4, 5, 6, data=b"a")
		self.output.write_bytes(b"precious")
		with self.assertRaises(CommandError):
			self.export()
		self.assertEqual(self.output.read_bytes(), b"precious")
		self.export("--force")
		self.assertEqual(self.read("SELECT count(*) FROM tiles"), [(1,)])

	def test_nothing_to_export_leaves_no_file(self):
		self.tile(3, 0, 0)
		with self.assertRaises(CommandError):
			self.export()
		self.assertFalse(self.output.exists())
		self.assertFalse(self.output.with_name("map.mbtiles.tmp").exists())
