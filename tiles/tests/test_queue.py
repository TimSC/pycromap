import gzip
import tempfile
from datetime import timedelta
from pathlib import Path

import mapbox_vector_tile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from tiles import queue
from tiles.models import TileState
from tiles.common import NAME_LENGTH, content_hash, describe, file_name


class QueueTests(TestCase):
	def test_new_tile_is_queued(self):
		tile, queued_at = queue.request_tile(13, 1, 2, rerender=False)
		self.assertEqual(tile.status, TileState.QUEUED)
		self.assertIsNotNone(queued_at)
		self.assertEqual(tile.request_count, 1)

	def test_request_while_queued_waits_for_that_rendering(self):
		_, first = queue.request_tile(13, 1, 2, rerender=True)
		tile, second = queue.request_tile(13, 1, 2, rerender=True)
		self.assertEqual(first, second)
		self.assertEqual(tile.request_count, 2)

	def test_rendered_tile_is_not_queued_unless_asked(self):
		TileState.objects.create(zoom=13, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now())
		tile, queued_at = queue.request_tile(13, 1, 2, rerender=False)
		self.assertIsNone(queued_at)
		self.assertEqual(tile.status, TileState.DONE)
		tile, queued_at = queue.request_tile(13, 1, 2, rerender=True)
		self.assertIsNotNone(queued_at)
		self.assertEqual(tile.status, TileState.QUEUED)

	def test_claims_highest_priority_first(self):
		queue.request_tile(13, 1, 1, rerender=True)
		queue.request_tile(13, 2, 2, rerender=True)
		TileState.objects.filter(x=1).update(priority=100)
		TileState.objects.filter(x=2).update(priority=200)
		self.assertEqual(queue.claim_next("a").x, 2)
		claimed = queue.claim_next("b")
		self.assertEqual((claimed.x, claimed.status, claimed.claimed_by, claimed.attempts), (1, TileState.RENDERING, "b", 1))
		self.assertIsNone(queue.claim_next("c"))

	@override_settings(RENDER_ATTEMPTS=2)
	def test_failures_are_retried_then_marked(self):
		queue.request_tile(13, 1, 1, rerender=True)
		queue.fail(queue.claim_next("a"), "boom")
		self.assertEqual(TileState.objects.get().status, TileState.QUEUED)
		queue.fail(queue.claim_next("a"), "boom again")
		tile = TileState.objects.get()
		self.assertEqual((tile.status, tile.error), (TileState.FAILED, "boom again"))

	def test_cached_tile_is_a_file_per_version(self):
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			queue.request_tile(13, 1, 2, rerender=True)
			queue.finish(queue.claim_next("a"), b"tile", 7, 0.5, cached=True)
			tile = TileState.objects.get()
			self.assertEqual((tile.status, tile.data_version, tile.file, tile.data), (TileState.DONE, 7, "13/1/2.7.pbf.gz", None))
			self.assertEqual((Path(cache) / tile.file).read_bytes(), b"tile")
			# A newer version replaces the file
			queue.request_tile(13, 1, 2, rerender=True)
			queue.finish(queue.claim_next("a"), b"newer", 8, 0.5, cached=True)
			self.assertEqual(sorted(p.name for p in (Path(cache) / "13/1").iterdir()), ["2.8.pbf.gz"])

	def test_uncached_tile_is_kept_with_its_state(self):
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			queue.request_tile(13, 1, 2, rerender=True)
			queue.finish(queue.claim_next("a"), b"tile", 7, 0.5, cached=False)
			tile = TileState.objects.get()
			self.assertEqual((tile.file, bytes(tile.data)), ("", b"tile"))
			self.assertEqual(list(Path(cache).iterdir()), [])

	def test_uncached_empty_tile_has_neither(self):
		queue.request_tile(13, 1, 2, rerender=True)
		queue.finish(queue.claim_next("a"), b"", 7, 0.5, cached=False)
		tile = TileState.objects.get()
		self.assertEqual((tile.file, tile.data), ("", None))

	def test_common_contents_point_at_their_file(self):
		sea = vector_tile([{"geometry": CommonTileTests.whole, "properties": {"class": "ocean"}}], "water")
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			# Chosen by hand: a file in the common folder
			(Path(cache) / "common").mkdir()
			(Path(cache) / "common" / "sea.pbf.gz").write_bytes(gzip.compress(gzip.decompress(sea), mtime=0))
			(Path(cache) / "common" / "land.pbf.gz").write_bytes(gzip.compress(b""))
			for x, data in ((1, sea), (2, gzip.compress(gzip.decompress(sea))), (3, b""), (4, b"tile")):
				queue.request_tile(12, x, 2, rerender=True)
				queue.finish(queue.claim_next("a"), data, 7, 0.5, cached=True)
			files = dict(TileState.objects.values_list("x", "file"))
			# Sea, gzipped at different times: its common file; nothing: land;
			# anything else: a file of its own
			self.assertEqual((files[1], files[2], files[3], files[4]),
				("common/sea.pbf.gz", "common/sea.pbf.gz", "common/land.pbf.gz", "12/4/2.7.pbf.gz"))
			# A tile that changes from sea to its own content leaves the common file
			queue.request_tile(12, 1, 2, rerender=True)
			queue.finish(queue.claim_next("a"), b"other", 8, 0.5, cached=True)
			self.assertTrue((Path(cache) / "common" / "sea.pbf.gz").exists())
			# Nothing is common without a common folder
			(Path(cache) / "common" / "sea.pbf.gz").unlink()
			(Path(cache) / "common" / "land.pbf.gz").unlink()
			queue.request_tile(12, 1, 2, rerender=True)
			queue.finish(queue.claim_next("a"), sea, 9, 0.5, cached=True)
			self.assertEqual(TileState.objects.get(x=1).file, "12/1/2.9.pbf.gz")

	def test_an_empty_tile_with_no_common_land_has_no_file(self):
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			queue.request_tile(12, 1, 2, rerender=True)
			queue.finish(queue.claim_next("a"), b"", 7, 0.5, cached=True)
			self.assertEqual(TileState.objects.get().file, "")

	@override_settings(RENDER_LEASE=60)
	def test_abandoned_claims_are_requeued(self):
		queue.request_tile(13, 1, 2, rerender=True)
		queue.claim_next("a")
		self.assertEqual(queue.requeue_abandoned(), 0)
		TileState.objects.update(claimed_at=timezone.now() - timedelta(seconds=61))
		self.assertEqual(queue.requeue_abandoned(), 1)
		self.assertEqual(TileState.objects.get().status, TileState.QUEUED)


def vector_tile(features, layer="water"):
	return gzip.compress(mapbox_vector_tile.encode([{"name": layer, "features": features, "extent": 4096}],
		default_options={"y_coord_down": True, "extents": 4096}))


class CommonTileTests(SimpleTestCase):
	whole = "POLYGON ((-64 -64, 4160 -64, 4160 4160, -64 4160, -64 -64))"

	def name(self, features, layer="water"):
		return describe(gzip.decompress(vector_tile(features, layer)))

	def test_names(self):
		ocean = {"class": "ocean"}
		top = "POLYGON ((-64 -64, 4160 -64, 4160 1500, -64 1500, -64 -64))"
		bottom = "POLYGON ((-64 1400, 4160 1400, 4160 4160, -64 4160, -64 1400))"
		self.assertEqual(describe(b""), "land")
		self.assertEqual(self.name([]), "land")
		self.assertEqual(self.name([{"geometry": self.whole, "properties": ocean}]), "sea")
		self.assertEqual(self.name([{"geometry": top, "properties": ocean}, {"geometry": bottom, "properties": ocean}]),
			"sea-2poly")
		self.assertEqual(self.name([{"geometry": top, "properties": ocean}]), "sea-part")
		self.assertEqual(self.name([{"geometry": self.whole, "properties": {"class": "national_park"}}], "park"), "park")
		self.assertEqual(self.name([{"geometry": "LINESTRING (0 0, 0 4096)", "properties": {"admin_level": 2}}],
			"boundary"), "boundary")
		# Anything else has no name: the ocean with something on it, or a lake
		self.assertIsNone(self.name([{"geometry": self.whole, "properties": ocean},
			{"geometry": "POINT (10 10)", "properties": {"name": "buoy"}}]))
		self.assertIsNone(self.name([{"geometry": self.whole, "properties": {"class": "lake"}}]))
		self.assertIsNone(describe(b"tile"))

	def test_file_names(self):
		sea = gzip.decompress(vector_tile([{"geometry": self.whole, "properties": {"class": "ocean"}}]))
		self.assertEqual(file_name(sea), "sea.pbf.gz")
		# A name already taken, or none to be had: the hash
		self.assertEqual(file_name(sea, {"sea"}), content_hash(sea) + ".pbf.gz")
		self.assertEqual(len(content_hash(b"tile")), 32)
		self.assertEqual(file_name(b"tile"), content_hash(b"tile") + ".pbf.gz")
		self.assertTrue(all(len(describe(x) or "") <= NAME_LENGTH for x in (b"", sea)))
