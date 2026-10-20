from datetime import timedelta
from unittest import mock

import pyo5m
from django.test import TestCase, override_settings
from django.utils import timezone

from summaries import build
from tiles import queue
from tiles.management.commands.render_tiles import render_one
from tiles.models import TileState

ZOOMS = {12: {"source": "data", "cache": False, "summary": True},
	11: {"source": "summary", "level": 12, "cache": False},
	10: {"source": "summary", "level": 10, "cache": False, "summary": True}}
RENDER = "tiles.management.commands.render_tiles.render_data"
READ = "tiles.management.commands.render_tiles.read_tile_data"


@override_settings(TILE_ZOOMS=ZOOMS, SUMMARY_WAIT=1, SUMMARY_WAIT_MAX=60, SUMMARY_WAIT_LIMIT=1800)
class SummaryJobTests(TestCase):
	def test_tile_from_summaries_waits_for_them(self):
		queue.request_tile(11, 3, 4, rerender=True)
		with mock.patch(RENDER) as render:
			render_one("w")
		render.assert_not_called()
		tile = TileState.objects.get(kind=TileState.TILE)
		self.assertEqual((tile.status, tile.attempts), (TileState.QUEUED, 0))
		self.assertIsNotNone(tile.waiting_since)
		self.assertGreater(tile.not_before, timezone.now())
		summaries = TileState.objects.filter(kind=TileState.SUMMARY).order_by("x", "y")
		self.assertEqual([(s.zoom, s.x, s.y) for s in summaries], build.tiles_beneath(11, 3, 4, 12))
		self.assertTrue(all(s.priority > tile.priority for s in summaries))

	def test_summary_jobs_read_the_map_data_and_the_tile_then_renders(self):
		queue.request_tile(11, 3, 4, rerender=True)
		data = pyo5m.OsmData()
		data.nodes = [pyo5m.Node(1, 50.0, -1.0, {"place": "town", "name": "T"})]
		with mock.patch(READ, return_value=(data, 8)) as read, mock.patch(RENDER, return_value=b"tile") as render:
			render_one("w")
			for _ in range(4):
				render_one("w")
			self.assertEqual(read.call_count, 4)
			TileState.objects.filter(kind=TileState.TILE).update(not_before=None)
			render_one("w")
		render.assert_called_once()
		self.assertEqual(len(render.call_args[0][0].nodes), 1)  # four summaries, merged
		tile = TileState.objects.get(kind=TileState.TILE)
		self.assertEqual((tile.status, tile.data_version), (TileState.DONE, 8))

	def test_rendering_from_map_data_builds_the_base_summary(self):
		queue.request_tile(12, 5, 6, rerender=True)
		with mock.patch(READ, return_value=(pyo5m.OsmData(), 8)), mock.patch(RENDER, return_value=b"tile"):
			render_one("w")
		self.assertEqual(build.missing([(12, 5, 6)]), [])

	@override_settings(TILE_ZOOMS={12: {"source": "data", "cache": False}})
	def test_a_zoom_without_summary_builds_none(self):
		queue.request_tile(12, 5, 6, rerender=True)
		with mock.patch(READ, return_value=(pyo5m.OsmData(), 8)), mock.patch(RENDER, return_value=b"tile"):
			render_one("w")
		self.assertEqual(build.missing([(12, 5, 6)]), [(12, 5, 6)])

	def test_waiting_backs_off(self):
		queue.request_tile(11, 3, 4, rerender=True)
		TileState.objects.filter(kind=TileState.SUMMARY).delete()
		with mock.patch(RENDER):
			render_one("w")
			tile = TileState.objects.get(kind=TileState.TILE)
			TileState.objects.filter(pk=tile.pk).update(not_before=None,
				waiting_since=timezone.now() - timedelta(seconds=400))
			TileState.objects.filter(kind=TileState.SUMMARY).update(status=TileState.RENDERING, claimed_at=timezone.now())
			render_one("w")
		tile.refresh_from_db()
		delay = (tile.not_before - timezone.now()).total_seconds()
		self.assertTrue(55 < delay <= 60)  # capped at SUMMARY_WAIT_MAX

	def test_waiting_too_long_fails_for_good(self):
		queue.request_tile(11, 3, 4, rerender=True)
		with mock.patch(RENDER):
			render_one("w")
			TileState.objects.filter(kind=TileState.TILE).update(not_before=None,
				waiting_since=timezone.now() - timedelta(seconds=1801))
			TileState.objects.filter(kind=TileState.SUMMARY).update(status=TileState.RENDERING, claimed_at=timezone.now())
			render_one("w")
		tile = TileState.objects.get(kind=TileState.TILE)
		self.assertEqual(tile.status, TileState.FAILED)
		self.assertIn("waited", tile.error)
		self.assertIn("still missing 12/6/8", tile.error)

	def test_a_failed_summary_fails_what_needs_it(self):
		queue.request_tile(11, 3, 4, rerender=True)
		with mock.patch(RENDER):
			render_one("w")
			TileState.objects.filter(kind=TileState.SUMMARY).update(status=TileState.FAILED)
			TileState.objects.filter(kind=TileState.TILE).update(not_before=None)
			render_one("w")
		tile = TileState.objects.get(kind=TileState.TILE)
		self.assertEqual(tile.status, TileState.FAILED)
		self.assertIn("summaries it needs failed", tile.error)

	def test_summary_jobs_are_one_deeper(self):
		queue.request_tile(11, 3, 4, rerender=True)
		with mock.patch(RENDER):
			render_one("w")
		self.assertEqual(set(TileState.objects.filter(kind=TileState.SUMMARY).values_list("depth", flat=True)), {1})

	@override_settings(SUMMARY_MAX_DEPTH=1)
	def test_summary_job_makes_no_jobs_beyond_the_depth(self):
		queue.queue_summaries([(10, 0, 0)], 10, 1)
		render_one("w")
		job = TileState.objects.get(kind=TileState.SUMMARY, zoom=10)
		self.assertEqual(job.status, TileState.FAILED)
		self.assertIn("deeper than SUMMARY_MAX_DEPTH (1)", job.error)
		self.assertFalse(TileState.objects.filter(zoom=12).exists())

	@override_settings(SUMMARY_MAX_DEPTH=2)
	def test_deeper_limit_queues_the_finer_summaries(self):
		queue.queue_summaries([(10, 0, 0)], 10, 1)
		render_one("w")
		finer = TileState.objects.filter(kind=TileState.SUMMARY, zoom=12)
		self.assertEqual((finer.count(), set(finer.values_list("depth", flat=True))), (16, {2}))
		self.assertEqual(TileState.objects.get(kind=TileState.SUMMARY, zoom=10).status, TileState.QUEUED)

	def test_lower_depth_supersedes(self):
		queue.queue_summaries([(12, 1, 2)], 10, 2)
		queue.queue_summaries([(12, 1, 2)], 5, 1)
		job = TileState.objects.get()
		self.assertEqual((job.depth, job.priority, job.status), (1, 10, TileState.QUEUED))
		# A job that failed for being too deep is tried again at the lower depth
		TileState.objects.update(status=TileState.FAILED, depth=2)
		self.assertEqual(queue.queue_summaries([(12, 1, 2)], 20, 1), [])
		job.refresh_from_db()
		self.assertEqual((job.depth, job.status), (1, TileState.QUEUED))
		# but not at the same depth, or deeper
		TileState.objects.update(status=TileState.FAILED)
		self.assertEqual(queue.queue_summaries([(12, 1, 2)], 20, 1), [(12, 1, 2)])


@override_settings(TILE_ZOOMS=ZOOMS, TILE_REQUEST_WAIT=0.2)
class PlaceholderTests(TestCase):
	def test_coarse_tile_without_its_summaries_is_a_placeholder(self):
		response = self.client.get("/tiles/10/0/0.pbf")
		self.assertEqual((response.status_code, response["X-Tile-Status"]), (204, "placeholder"))
		self.assertEqual(response["Cache-Control"], "no-store")
		tile = TileState.objects.get()
		self.assertEqual((tile.kind, tile.status, tile.request_count), (TileState.TILE, TileState.DONE, 1))
		self.assertFalse(TileState.objects.filter(kind=TileState.SUMMARY).exists())

	def test_coarse_tile_is_queued_once_its_summaries_can_be_built(self):
		for level, x, y in build.tiles_beneath(10, 0, 0, 12):
			build.store(level, x, y, pyo5m.OsmData(), 3)
		response = self.client.get("/tiles/10/0/0.pbf")
		self.assertEqual(response.status_code, 503)  # queued; no renderer in the test
		self.assertEqual(TileState.objects.get(kind=TileState.TILE).status, TileState.QUEUED)

	def test_tile_from_base_summaries_is_always_queued(self):
		self.client.get("/tiles/11/3/4.pbf")
		self.assertEqual(TileState.objects.get(kind=TileState.TILE).status, TileState.QUEUED)

	def test_renderer_does_not_queue_beyond_the_depth(self):
		# The tile's summary job is depth 1, which may not make jobs at depth 2
		queue.request_tile(10, 0, 0, rerender=True)
		with mock.patch(RENDER):
			render_one("w")
			render_one("w")
		summary = TileState.objects.get(kind=TileState.SUMMARY)
		self.assertEqual(((summary.zoom, summary.depth), summary.status), ((10, 1), TileState.FAILED))
		self.assertFalse(TileState.objects.filter(zoom=12).exists())

	@override_settings(SUMMARY_MAX_DEPTH=2)
	def test_deeper_limit_accepts_the_coarse_tile(self):
		response = self.client.get("/tiles/10/0/0.pbf")
		self.assertEqual(response.status_code, 503)  # queued; no renderer in the test
		self.assertEqual(TileState.objects.get(kind=TileState.TILE).status, TileState.QUEUED)
