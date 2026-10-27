import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from summaries.models import Recipe, TileSummary
from tiles.models import Job, Tile, TileSweep


class ClearTilesTests(TestCase):
	def setUp(self):
		self.cache = tempfile.TemporaryDirectory()
		self.settings = override_settings(TILE_CACHE_DIR=Path(self.cache.name))
		self.settings.enable()
		for path in ("3/1/2.7.pbf.gz", "9/5/6.3.pbf.gz", "9/5/7.3.pbf.gz", "common/sea.pbf.gz"):
			(Path(self.cache.name) / path).parent.mkdir(parents=True, exist_ok=True)
			(Path(self.cache.name) / path).write_bytes(b"tile")
		now = timezone.now()
		Tile.objects.create(zoom=3, x=1, y=2, rendered_at=now, file="3/1/2.7.pbf.gz")
		Tile.objects.create(zoom=9, x=5, y=6, rendered_at=now, file="9/5/6.3.pbf.gz")
		Tile.objects.create(zoom=9, x=5, y=8, rendered_at=now, file="common/sea.pbf.gz")
		Job.objects.create(kind=Job.TILE, zoom=9, x=1, y=1, status=Job.FAILED)
		Job.objects.create(kind=Job.SUMMARY, zoom=10, x=1, y=1, status=Job.QUEUED)
		TileSweep.objects.create(kind=Job.TILE, zoom=3, y=0, last_checked=now)
		TileSweep.objects.create(kind=Job.SUMMARY, zoom=12, y=0)
		recipe = Recipe.objects.create(hash="r", settings="{}", created_at=now)
		TileSummary.objects.create(zoom=12, x=0, y=0, recipe=recipe, version=1, built_at=now, data=b"")

	def tearDown(self):
		self.settings.disable()
		self.cache.cleanup()

	def test_clears_every_zoom_but_keeps_common_files_and_summaries(self):
		out = StringIO()
		call_command("clear_tiles", "--noinput", stdout=out)
		self.assertIn("Deleted 3 tiles, 1 tile jobs and 1 sweep rows; removed folders: 3, 9", out.getvalue())
		self.assertFalse(Tile.objects.exists())
		self.assertEqual(sorted(p.name for p in Path(self.cache.name).iterdir()), ["common"])
		# The summary work, and the summaries, are left alone
		self.assertEqual(list(Job.objects.values_list("kind", flat=True)), [Job.SUMMARY])
		self.assertEqual(list(TileSweep.objects.values_list("kind", flat=True)), [Job.SUMMARY])
		self.assertTrue(TileSummary.objects.exists())

	def test_clears_only_the_zooms_given(self):
		call_command("clear_tiles", "--zooms", "9-12", "--noinput", stdout=StringIO())
		self.assertEqual(list(Tile.objects.values_list("zoom", flat=True)), [3])
		self.assertTrue((Path(self.cache.name) / "3/1/2.7.pbf.gz").exists())
		self.assertFalse((Path(self.cache.name) / "9").exists())
		self.assertTrue(TileSweep.objects.filter(kind=Job.TILE, zoom=3).exists())
