import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from tiles import queue
from tiles.models import TileState


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

	def test_empty_tile_has_neither(self):
		queue.request_tile(13, 1, 2, rerender=True)
		queue.finish(queue.claim_next("a"), b"", 7, 0.5, cached=True)
		tile = TileState.objects.get()
		self.assertEqual((tile.file, tile.data), ("", None))

	@override_settings(RENDER_LEASE=60)
	def test_abandoned_claims_are_requeued(self):
		queue.request_tile(13, 1, 2, rerender=True)
		queue.claim_next("a")
		self.assertEqual(queue.requeue_abandoned(), 0)
		TileState.objects.update(claimed_at=timezone.now() - timedelta(seconds=61))
		self.assertEqual(queue.requeue_abandoned(), 1)
		self.assertEqual(TileState.objects.get().status, TileState.QUEUED)
