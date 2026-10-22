from datetime import timedelta

import pyo5m
from django.test import TestCase, override_settings
from django.utils import timezone

from summaries import build
from summaries.models import TileSummary
from tiles import idle, queue
from tiles.models import TileRequest, TileState, TileSweep

ZOOMS = {12: {"source": "data", "cache": False, "summary": True},
	11: {"source": "summary", "level": 12, "cache": False},
	10: {"source": "summary", "level": 10, "cache": False, "summary": True}}

# A tiny pyramid, so the sweeps are a handful of tiles: the grid at 4 is
# 16 by 16
TZOOMS = {4: {"source": "data", "cache": False, "summary": True},
	3: {"source": "summary", "level": 4, "cache": False, "summary": True}}


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_QUEUE=8)
class PlanTests(TestCase):
	def test_the_sweep_begins_at_the_corner(self):
		# Nobody has asked for anything: the fleet starts at the front of the
		# base level's summaries, several along the first row, and never
		# renders tiles ahead
		planned = idle.plan(7, "w")
		self.assertEqual(planned, [(TileState.SUMMARY, 12, x, 0) for x in range(7)])

	def test_skips_what_exists_or_is_under_way(self):
		build.store(12, 0, 0, pyo5m.OsmData(), 1)
		TileState.objects.create(kind=TileState.SUMMARY, zoom=12, x=1, y=0, status=TileState.QUEUED, priority=10)
		self.assertEqual(idle.plan(5, "w"), [(TileState.SUMMARY, 12, x, 0) for x in range(2, 7)])


@override_settings(TILE_ZOOMS=TZOOMS)
class SweepTests(TestCase):
	def test_a_stored_summary_advances_the_frontier(self):
		planned = idle.plan(10000, "w")
		self.assertIn((TileState.SUMMARY, 4, 0, 0), planned)
		self.assertFalse([job for job in planned if job[0] == TileState.TILE])
		build.store(4, 0, 0, pyo5m.OsmData(), 1)
		planned = idle.plan(10000, "w")
		self.assertNotIn((TileState.SUMMARY, 4, 0, 0), planned)
		# The frontier of row 0 moves on to the next missing summary
		self.assertIn((TileState.SUMMARY, 4, 0, 1), planned)

	def test_a_row_stays_claimed_until_it_is_full(self):
		idle.plan(1, "w")
		row = TileSweep.objects.get(kind=TileState.SUMMARY, zoom=4, y=0)
		self.assertIsNone(row.last_checked)  # not yet verified
		self.assertEqual(row.claimed_by, "w")  # stays claimed while it has gaps

	@override_settings(TILE_ZOOMS={**TZOOMS, 4: {**TZOOMS[4], "cache": True}})
	def test_the_sweep_builds_only_summaries(self):
		self.assertEqual({kind for kind, _, _, _ in idle.plan(10, "w")}, {TileState.SUMMARY})
		# The base level's rows; the coarser level's are made when its turn
		# comes, and none for tiles, even of a cached zoom
		self.assertEqual(TileSweep.objects.filter(zoom=4, kind=TileState.SUMMARY).count(), 1 << 4)
		self.assertEqual(TileSweep.objects.filter(kind=TileState.TILE).count(), 0)
		self.assertEqual(TileSweep.objects.filter(zoom=3).count(), 0)

	def test_a_coarser_summary_once_those_beneath_are_built(self):
		# The base level's first two rows: the coarser level's first row can
		# be built from them, and nothing further down
		for x in range(1 << 4):
			for y in (0, 1):
				build.store(4, x, y, pyo5m.OsmData(), 1)
		planned = idle.plan(10000, "w")
		self.assertEqual([job for job in planned if job[1] == 3], [(TileState.SUMMARY, 3, x, 0) for x in range(8)])

	def test_an_out_of_date_summary_is_a_gap(self):
		build.store(4, 0, 0, pyo5m.OsmData(), 1)
		TileSummary.objects.filter(zoom=4, x=0, y=0).update(recipe="older")
		self.assertIn((TileState.SUMMARY, 4, 0, 0), idle.plan(10000, "w"))

	@override_settings(IDLE_QUEUE=64, RENDER_ATTEMPTS=3)
	def test_a_failed_job_is_tried_again_until_given_up(self):
		job = TileState.objects.create(kind=TileState.SUMMARY, zoom=4, x=0, y=0, status=TileState.FAILED,
			attempts=1, error="finer summaries it is built from aren't built")
		idle.queue_idle_work("w")
		job.refresh_from_db()
		self.assertEqual((job.status, job.idle, job.attempts), (TileState.QUEUED, True, 1))
		# Given up: the row moves on past it (the rest of the row, queued
		# meanwhile, is let go of, to be handed out again)
		TileState.objects.filter(pk=job.pk).update(status=TileState.FAILED, attempts=3)
		TileState.objects.filter(status=TileState.QUEUED).delete()
		planned = idle.plan(10000, "w")
		self.assertNotIn((TileState.SUMMARY, 4, 0, 0), planned)
		self.assertIn((TileState.SUMMARY, 4, 1, 0), planned)


@override_settings(TILE_ZOOMS=TZOOMS)
class ClaimTests(TestCase):
	def test_a_claimed_row_is_claimed_again(self):
		idle.plan(1, "w1")
		row = TileSweep.objects.filter(kind=TileState.SUMMARY, zoom=4, y=0).first()
		# A renderer that went quiet: its row is given back, and a new
		# renderer claims it (and keeps it, as the row has gaps)
		row.claimed_by, row.claimed_at = "dead", timezone.now() - timedelta(hours=1)
		row.save()
		self.assertEqual(idle.reclaim_sweeps(), 1)
		idle.plan(1, "w2")
		self.assertEqual(TileSweep.objects.get(pk=row.pk).claimed_by, "w2")

	def test_a_stopping_renderer_releases_its_rows(self):
		idle.plan(1, "w1")
		self.assertEqual(idle.release_sweeps("w1"), 1)
		self.assertFalse(TileSweep.objects.exclude(claimed_by="").exists())
		idle.plan(1, "w2")
		self.assertEqual(TileSweep.objects.get(kind=TileState.SUMMARY, zoom=4, y=0).claimed_by, "w2")

	def test_idle_jobs_are_left_unless_working_ahead(self):
		idle.queue_idle_work("w")
		self.assertIsNone(queue.claim_next("w", idle=False))
		self.assertLess(queue.claim_next("w", idle=True).priority, 0)


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_QUEUE=4)
class QueueIdleTests(TestCase):
	def test_fills_up_to_the_renderers_below_requests(self):
		self.assertEqual(idle.queue_idle_work("w"), 4)  # IDLE_QUEUE of them
		jobs = TileState.objects.filter(idle=True)
		self.assertEqual(jobs.count(), 4)
		self.assertTrue(all(j.status == TileState.QUEUED and j.idle for j in jobs))
		self.assertEqual(idle.queue_idle_work("w"), 0)  # already enough queued
		# A request comes first
		TileRequest.objects.create(zoom=12, x=50, y=50, status=TileRequest.WAITING)
		self.assertEqual((queue.claim_next("w").x), 50)
		self.assertGreaterEqual(TileState.objects.get(kind=TileState.TILE, x=50).priority, 0)

	def test_requested_jobs_do_not_count_towards_the_idle_queue(self):
		TileRequest.objects.create(zoom=12, x=50, y=50, status=TileRequest.WAITING)
		# The request is queued, but the idle renderers still get their share
		self.assertEqual(idle.queue_idle_work("w"), 4)

	def test_tops_up_once_down_to_half(self):
		idle.queue_idle_work("w")
		queue.claim_next("w")
		self.assertEqual(idle.queue_idle_work("w"), 0)  # three left: more than half
		queue.claim_next("w")
		self.assertEqual(idle.queue_idle_work("w"), 2)  # two left: back up to four

	def test_one_renderer_plans_at_a_time(self):
		with idle._planning() as planning:
			self.assertTrue(planning)
			self.assertTrue(idle.planning_now())
			self.assertEqual(idle.queue_idle_work("w"), 0)  # another is planning
		self.assertFalse(idle.planning_now())
		self.assertEqual(idle.queue_idle_work("w"), 4)
