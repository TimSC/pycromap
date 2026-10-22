import gzip
import tempfile
from datetime import timedelta
from pathlib import Path

import mapbox_vector_tile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from tiles import queue
from tiles.models import TileRequest, TileState
from tiles.common import NAME_LENGTH, content_hash, describe, file_name


def want(x, y=2, zoom=13, **fields):
	"""A request waiting for a tile to be rendered."""
	return TileRequest.objects.create(zoom=zoom, x=x, y=y, status=TileRequest.WAITING, **fields)


class QueueTests(TestCase):
	def test_claims_the_newest_request_with_every_other_for_its_tile(self):
		older, newest, same = want(1), want(2), want(1)
		TileRequest.objects.filter(pk=older.pk).update(started_at=timezone.now() - timedelta(seconds=10))
		TileRequest.objects.filter(pk=same.pk).update(started_at=timezone.now() - timedelta(seconds=5))
		TileRequest.objects.filter(pk=newest.pk).update(started_at=timezone.now())
		tile = queue.claim_next("a")
		self.assertEqual((tile.x, tile.status, tile.claimed_by, tile.attempts), (2, TileState.RENDERING, "a", 1))
		# The next, for the other tile, with both its requests
		tile = queue.claim_next("b")
		self.assertEqual(tile.x, 1)
		self.assertEqual(set(TileRequest.objects.filter(x=1).values_list("status", "claimed_by")),
			{(TileRequest.CLAIMED, "b")})
		self.assertIsNone(queue.claim_next("c"))

	def test_the_tile_is_added_to_tile_state_as_it_is_claimed(self):
		want(1)
		self.assertFalse(TileState.objects.exists())
		queue.claim_next("a")
		tile = TileState.objects.get()
		self.assertEqual((tile.kind, tile.zoom, tile.x, tile.y, tile.status), (TileState.TILE, 13, 1, 2, TileState.RENDERING))
		self.assertGreater(tile.priority, 0)

	def test_cancelled_requests_are_not_claimed(self):
		want(1, cancelled=True, cancelled_at=timezone.now())
		self.assertIsNone(queue.claim_next("a"))

	def test_requests_answered_by_a_cache_redirect_are_not_claimed(self):
		want(1, cache_redirect=True)
		self.assertIsNone(queue.claim_next("a"))

	def test_requests_for_a_tile_being_rendered_wait_for_that_rendering(self):
		want(1)
		job = queue.claim_next("a")
		later = want(1)
		self.assertIsNone(queue.claim_next("b"))
		queue.finish(job, b"", 7, 0.5, cached=False)
		# Answered with it, the request that came meanwhile too
		self.assertEqual(set(TileRequest.objects.values_list("status", flat=True)), {TileRequest.DONE})
		later.refresh_from_db()
		self.assertEqual(later.status, TileRequest.DONE)

	def test_requests_come_before_idle_work(self):
		TileState.objects.create(zoom=13, x=5, y=5, status=TileState.QUEUED, idle=True, priority=-1)
		want(1)
		self.assertEqual(queue.claim_next("a", idle=False).x, 1)
		# Idle work only for a renderer working ahead
		self.assertIsNone(queue.claim_next("a", idle=False))
		self.assertEqual(queue.claim_next("a", idle=True).x, 5)

	def test_a_request_for_a_tile_queued_as_idle_work_takes_it(self):
		TileState.objects.create(zoom=13, x=1, y=2, status=TileState.QUEUED, idle=True, priority=-1)
		want(1)
		tile = queue.claim_next("a", idle=False)
		self.assertEqual((tile.x, tile.status), (1, TileState.RENDERING))
		self.assertGreater(tile.priority, 0)

	@override_settings(RENDER_ATTEMPTS=2)
	def test_failures_are_retried_then_marked(self):
		request = want(1)
		queue.fail(queue.claim_next("a"), "boom")
		request.refresh_from_db()
		# The tile marked failed; its request waits again, keeping count
		self.assertEqual((TileState.objects.get().status, request.status, request.attempts),
			(TileState.FAILED, TileRequest.WAITING, 1))
		tile = queue.claim_next("a")
		self.assertEqual(tile.attempts, 2)
		queue.fail(tile, "boom again")
		tile.refresh_from_db(); request.refresh_from_db()
		self.assertEqual((tile.status, tile.error, request.status), (TileState.FAILED, "boom again", TileRequest.FAILED))

	def test_a_final_failure_answers_at_once(self):
		request = want(1)
		queue.fail(queue.claim_next("a"), "no", final=True)
		request.refresh_from_db()
		self.assertEqual(request.status, TileRequest.FAILED)

	def test_cached_tile_is_a_file_per_version(self):
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			want(1, 2, 13)
			queue.finish(queue.claim_next("a"), b"tile", 7, 0.5, cached=True)
			tile = TileState.objects.get()
			self.assertEqual((tile.status, tile.data_version, tile.file, tile.data), (TileState.DONE, 7, "13/1/2.7.pbf.gz", None))
			self.assertEqual((Path(cache) / tile.file).read_bytes(), b"tile")
			# A newer version replaces the file
			want(1, 2, 13)
			queue.finish(queue.claim_next("a"), b"newer", 8, 0.5, cached=True)
			self.assertEqual(sorted(p.name for p in (Path(cache) / "13/1").iterdir()), ["2.8.pbf.gz"])

	def test_uncached_tile_is_kept_with_its_state(self):
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			want(1, 2, 13)
			queue.finish(queue.claim_next("a"), b"tile", 7, 0.5, cached=False)
			tile = TileState.objects.get()
			self.assertEqual((tile.file, bytes(tile.data)), ("", b"tile"))
			self.assertEqual(list(Path(cache).iterdir()), [])

	def test_uncached_empty_tile_has_neither(self):
		want(1, 2, 13)
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
				want(x, 2, 12)
				queue.finish(queue.claim_next("a"), data, 7, 0.5, cached=True)
			files = dict(TileState.objects.values_list("x", "file"))
			# Sea, gzipped at different times: its common file; nothing: land;
			# anything else: a file of its own
			self.assertEqual((files[1], files[2], files[3], files[4]),
				("common/sea.pbf.gz", "common/sea.pbf.gz", "common/land.pbf.gz", "12/4/2.7.pbf.gz"))
			# A tile that changes from sea to its own content leaves the common file
			want(1, 2, 12)
			queue.finish(queue.claim_next("a"), b"other", 8, 0.5, cached=True)
			self.assertTrue((Path(cache) / "common" / "sea.pbf.gz").exists())
			# Nothing is common without a common folder
			(Path(cache) / "common" / "sea.pbf.gz").unlink()
			(Path(cache) / "common" / "land.pbf.gz").unlink()
			want(1, 2, 12)
			queue.finish(queue.claim_next("a"), sea, 9, 0.5, cached=True)
			self.assertEqual(TileState.objects.get(x=1).file, "12/1/2.9.pbf.gz")

	def test_an_empty_tile_with_no_common_land_has_no_file(self):
		with tempfile.TemporaryDirectory() as cache, override_settings(TILE_CACHE_DIR=Path(cache)):
			want(1, 2, 12)
			queue.finish(queue.claim_next("a"), b"", 7, 0.5, cached=True)
			self.assertEqual(TileState.objects.get().file, "")

	@override_settings(RENDER_LEASE=60)
	def test_abandoned_claims_are_requeued(self):
		request = want(1, 2, 13)
		queue.claim_next("a")
		idle = TileState.objects.create(zoom=13, x=5, y=5, status=TileState.QUEUED, idle=True, priority=-1)
		queue.claim_next("b", idle=True)
		self.assertEqual(queue.requeue_abandoned(), 0)
		TileState.objects.update(claimed_at=timezone.now() - timedelta(seconds=61))
		TileRequest.objects.update(claimed_at=timezone.now() - timedelta(seconds=61))
		self.assertEqual(queue.requeue_abandoned(), 2)
		request.refresh_from_db(); idle.refresh_from_db()
		# The request waits again, its tile marked failed; the idle work is queued again
		self.assertEqual((TileState.objects.get(x=1).status, request.status, request.claimed_by),
			(TileState.FAILED, TileRequest.WAITING, ""))
		self.assertEqual(idle.status, TileState.QUEUED)

	def test_a_stopping_renderer_releases_its_claims_at_once(self):
		request = want(1, 2, 13)
		queue.claim_next("a")
		idle = TileState.objects.create(zoom=13, x=5, y=5, status=TileState.QUEUED, idle=True, priority=-1)
		queue.claim_next("a", idle=True)
		other = want(2, 2, 13)
		queue.claim_next("b")
		self.assertEqual(queue.release_claims("a"), 2)
		request.refresh_from_db(); idle.refresh_from_db(); other.refresh_from_db()
		tile = TileState.objects.get(x=1)
		self.assertEqual((tile.status, tile.error, request.status, request.claimed_by),
			(TileState.FAILED, "its renderer stopped", TileRequest.WAITING, ""))
		self.assertEqual((idle.status, idle.claimed_by), (TileState.QUEUED, ""))
		# Another renderer's claims are left alone
		self.assertEqual((TileState.objects.get(x=2).status, other.status), (TileState.RENDERING, TileRequest.CLAIMED))
		# and the released request is claimed again
		self.assertEqual(queue.claim_next("c").x, 1)


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
