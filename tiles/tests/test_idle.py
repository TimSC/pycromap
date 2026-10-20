import time

import pyo5m
from django.test import TestCase, override_settings

from summaries import build
from tiles import idle, queue
from tiles.models import TileState

ZOOMS = {12: {"source": "data", "cache": False, "summary": True},
	11: {"source": "summary", "level": 12, "cache": False},
	10: {"source": "summary", "level": 10, "cache": False, "summary": True}}


def request(zoom, x, y):
	TileState.objects.create(kind=TileState.TILE, zoom=zoom, x=x, y=y, status=TileState.DONE,
		request_count=1, requested_at=None)


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class PlanTests(TestCase):
	def test_nothing_without_requests(self):
		TileState.objects.create(kind=TileState.TILE, zoom=12, x=5, y=5, status=TileState.DONE)
		self.assertEqual(idle.plan(5), [])

	def test_base_summaries_nearest_first(self):
		request(12, 100, 100)
		planned = idle.plan(10)
		self.assertEqual(planned[0], (TileState.SUMMARY, 12, 100, 100))
		self.assertEqual(len(planned), 9)  # the tile and the ring around it
		self.assertTrue(all(z == 12 for _, z, _, _ in planned))

	def test_skips_what_exists_or_is_under_way(self):
		request(12, 100, 100)
		build.store(12, 100, 100, pyo5m.OsmData(), 1)
		queue.queue_summaries([(12, 101, 100)], 10, 1)
		planned = idle.plan(10)
		self.assertNotIn((TileState.SUMMARY, 12, 100, 100), planned)
		self.assertNotIn((TileState.SUMMARY, 12, 101, 100), planned)
		self.assertEqual(len(planned), 7)

	def test_coarser_summary_once_those_beneath_exist(self):
		request(11, 0, 0)
		beneath = build.tiles_beneath(10, 0, 0, 12)
		for level, x, y in beneath:
			build.store(level, x, y, pyo5m.OsmData(), 1)
		planned = idle.plan(50)
		self.assertIn((TileState.SUMMARY, 10, 0, 0), planned)
		# after the level-12 work, which is higher
		levels = [z for _, z, _, _ in planned]
		self.assertEqual(levels, sorted(levels, reverse=True))

	def test_idle_work_does_not_seed_more(self):
		request(12, 100, 100)
		idle.queue_idle_work(20)
		# Only the requested tile seeds: everything planned is within a tile of it
		for job in TileState.objects.filter(idle=True):
			self.assertLessEqual(max(abs(job.x - 100), abs(job.y - 100)), 1)


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_RADIUS=2, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class QueueIdleTests(TestCase):
	def test_fills_up_to_the_workers_below_requests(self):
		request(12, 100, 100)
		self.assertEqual(idle.queue_idle_work(4), 4)
		jobs = TileState.objects.filter(idle=True)
		self.assertEqual(jobs.count(), 4)
		self.assertTrue(all(j.status == TileState.QUEUED and j.priority < 0 and j.depth == 1 for j in jobs))
		self.assertEqual(idle.queue_idle_work(4), 0)  # already enough queued
		# A request comes first
		queue.request_tile(12, 50, 50, rerender=True)
		self.assertEqual(queue.claim_next("w").zoom, 12)
		self.assertFalse(TileState.objects.get(kind=TileState.TILE, x=50).idle)

	def test_request_takes_over_an_idle_job(self):
		request(12, 100, 100)
		idle.queue_idle_work(1)
		job = TileState.objects.get(idle=True)
		queue.queue_summaries([(job.zoom, job.x, job.y)], time.time(), 1)
		job.refresh_from_db()
		self.assertFalse(job.idle)
		self.assertGreater(job.priority, 0)


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class ClaimTests(TestCase):
	def test_idle_jobs_are_left_unless_working_ahead(self):
		request(12, 100, 100)
		idle.queue_idle_work(1)
		self.assertIsNone(queue.claim_next("w", idle=False))
		self.assertTrue(queue.claim_next("w", idle=True).idle)


@override_settings(TILE_ZOOMS={12: {"source": "data", "cache": True}, 13: {"source": "crop", "from": 12}},
	IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class CachedBaseTests(TestCase):
	def test_tiles_rather_than_their_summaries(self):
		request(13, 200, 200)
		planned = idle.plan(20)
		# Rendering a base tile builds its summary, so it isn't planned as well
		self.assertEqual(planned[0], (TileState.TILE, 12, 100, 100))
		self.assertEqual({k for k, z, _, _ in planned if z == 12}, {TileState.TILE})
		self.assertFalse(any(z == 13 for _, z, _, _ in planned))
