from datetime import timedelta

import pyo5m
from django.test import TestCase, override_settings
from django.utils import timezone

from summaries import build
from tiles import idle, queue
from tiles.models import TileRequest, TileState, TileSweep

ZOOMS = {12: {"source": "data", "cache": False, "summary": True},
	11: {"source": "summary", "level": 12, "cache": False},
	10: {"source": "summary", "level": 10, "cache": False, "summary": True}}

# A tiny pyramid, so the sweeps are a handful of tiles: the grid at 4 is
# 16 by 16
TZOOMS = {4: {"source": "data", "cache": False, "summary": True},
	3: {"source": "summary", "level": 4, "cache": False, "summary": True}}


def request(zoom, x, y, when=None):
	TileState.objects.create(kind=TileState.TILE, zoom=zoom, x=x, y=y, status=TileState.DONE,
		request_count=1, requested_at=when)


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1, IDLE_QUEUE=8)
class PlanTests(TestCase):
	def test_the_sweep_begins_at_the_corner(self):
		# Nobody has asked for anything: the fleet starts at the front of the
		# highest zoom, tiles first (rendering one builds its summary); the
		# summaries beneath catch up once the tiles have filled the budget
		planned = idle.plan(7, "w")
		self.assertEqual(planned, [(TileState.TILE, 12, 0, x) for x in range(7)])

	def test_base_summaries_nearest_first(self):
		request(12, 100, 100)
		planned = idle.plan(10, "w")
		self.assertEqual(planned[0], (TileState.SUMMARY, 12, 100, 100))
		self.assertTrue(all(z == 12 for _, z, _, _ in planned[:9]))  # the ring
		self.assertEqual(planned[9], (TileState.TILE, 12, 0, 0))  # then the sweep

	def test_skips_what_exists_or_is_under_way(self):
		request(12, 100, 100)
		build.store(12, 100, 100, pyo5m.OsmData(), 1)
		TileState.objects.create(kind=TileState.SUMMARY, zoom=12, x=101, y=100, status=TileState.QUEUED, priority=10)
		planned = idle.plan(10, "w")
		self.assertNotIn((TileState.SUMMARY, 12, 100, 100), planned)
		self.assertNotIn((TileState.SUMMARY, 12, 101, 100), planned)
		self.assertEqual(len(planned), 10)  # the ring, less two, and the sweep

	def test_coarser_summary_once_those_beneath_exist(self):
		request(11, 0, 0)
		beneath = build.tiles_beneath(10, 0, 0, 12)
		for level, x, y in beneath:
			build.store(level, x, y, pyo5m.OsmData(), 1)
		planned = idle.plan(50, "w")
		# Before the sweep: the summaries beneath it all exist
		self.assertEqual(planned[0], (TileState.SUMMARY, 10, 0, 0))

	def test_idle_work_does_not_seed_more(self):
		request(12, 100, 100)
		idle.queue_idle_work("w")
		# Only the requested tile seeds: everything planned is within a tile
		# of it, or is the frontier of a row
		for job in TileState.objects.filter(priority__lt=0):
			self.assertTrue(max(abs(job.x - 100), abs(job.y - 100)) <= 1
				or (job.x, job.y) == (0, 0))


@override_settings(TILE_ZOOMS=TZOOMS, IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class SweepTests(TestCase):
	def test_the_newest_seed_comes_first(self):
		t0 = timezone.now() - timedelta(hours=1)
		request(4, 1, 1, t0)
		request(4, 8, 8, t0 + timedelta(minutes=2))
		planned = idle.plan(100, "w")
		# The newer (centre) seed comes first, with its ring around it: the base
		# summaries, the cheapest way to have the pyramid there
		self.assertEqual(planned[0], (TileState.SUMMARY, 4, 8, 8))
		self.assertEqual(len([j for j in planned if max(abs(j[2] - 8), abs(j[3] - 8)) <= 1]), 9)
		# ...and then the rest of the planet, row by row, from the frontiers
		self.assertEqual(planned[-8:], [(TileState.SUMMARY, 3, 0, x) for x in range(8)])

	def test_a_rendered_tile_advances_the_frontier(self):
		planned = idle.plan(10000, "w")
		self.assertIn((TileState.TILE, 4, 0, 0), planned)
		# Rendering the corner tile builds its summary: the frontier of row 0
		# moves on
		TileState.objects.create(kind=TileState.TILE, zoom=4, x=0, y=0, status=TileState.DONE,
			rendered_at=timezone.now())
		build.store(4, 0, 0, pyo5m.OsmData(), 1)
		planned = idle.plan(10000, "w")
		self.assertNotIn((TileState.TILE, 4, 0, 0), planned)
		# The frontier of row 0 moves on to the next unrendered tile
		self.assertIn((TileState.TILE, 4, 0, 1), planned)

	def test_a_row_stays_claimed_until_it_is_full(self):
		idle.plan(1, "w")
		row = TileSweep.objects.get(kind=TileState.TILE, zoom=4, y=0)
		self.assertIsNone(row.last_checked)  # not yet verified
		self.assertEqual(row.claimed_by, "w")  # stays claimed while it has gaps

	def test_the_sweep_skips_uncached_zooms(self):
		request(4, 8, 8)
		idle.plan(10, "w")
		# The base zoom's tiles, whose rendering builds their summary; the
		# coarser zoom's rows are made when its turn comes, and none for
		# uncached zooms (the base's tile job renders those)
		self.assertEqual(TileSweep.objects.filter(zoom=4, kind=TileState.TILE).count(), 1 << 4)
		self.assertEqual(TileSweep.objects.filter(kind=TileState.TILE, zoom__gt=4).count(), 0)
		self.assertEqual(TileSweep.objects.filter(kind=TileState.SUMMARY, zoom__gt=3).count(), 0)
		# The tile frontier fills the whole budget, so the summary rows are only
		# made once the tiles are done: the summaries' turn comes later
		for x in range(1 << 4):
			TileState.objects.create(kind=TileState.TILE, zoom=4, x=x, y=0,
				status=TileState.DONE, rendered_at=timezone.now())
		idle.plan(20, "w")
		self.assertEqual(TileSweep.objects.filter(zoom=3, kind=TileState.SUMMARY).count(), 1 << 3)


@override_settings(TILE_ZOOMS=TZOOMS, IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class ClaimTests(TestCase):
	def test_a_claimed_row_is_claimed_again(self):
		idle.plan(1, "w1")
		row = TileSweep.objects.filter(kind=TileState.TILE, zoom=4, y=0).first()
		# A renderer that went quiet: its row is given back, and a new
		# renderer claims it (and keeps it, as the row has gaps)
		row.claimed_by, row.claimed_at = "dead", timezone.now() - timedelta(hours=1)
		row.save()
		self.assertEqual(idle.reclaim_sweeps(), 1)
		idle.plan(1, "w2")
		self.assertEqual(TileSweep.objects.get(pk=row.pk).claimed_by, "w2")

	def test_idle_jobs_are_left_unless_working_ahead(self):
		request(4, 8, 8)
		idle.queue_idle_work("w")
		self.assertIsNone(queue.claim_next("w", idle=False))
		self.assertLess(queue.claim_next("w", idle=True).priority, 0)


@override_settings(TILE_ZOOMS=ZOOMS, IDLE_RADIUS=2, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1, IDLE_QUEUE=4)
class QueueIdleTests(TestCase):
	def test_fills_up_to_the_renderers_below_requests(self):
		request(12, 100, 100)
		self.assertEqual(idle.queue_idle_work("w"), 4)  # IDLE_QUEUE of them
		jobs = TileState.objects.filter(priority__lt=0)
		self.assertEqual(jobs.count(), 4)
		self.assertTrue(all(j.status == TileState.QUEUED and j.priority < 0 for j in jobs))
		self.assertEqual(idle.queue_idle_work("w"), 0)  # already enough queued
		# A request comes first
		TileRequest.objects.create(zoom=12, x=50, y=50, status=TileRequest.WAITING)
		self.assertEqual((queue.claim_next("w").x), 50)
		self.assertGreaterEqual(TileState.objects.get(kind=TileState.TILE, x=50).priority, 0)

	def test_requested_jobs_do_not_count_towards_the_idle_queue(self):
		request(12, 100, 100)
		TileRequest.objects.create(zoom=12, x=50, y=50, status=TileRequest.WAITING)
		# The request is queued, but the idle renderers still get their share
		self.assertEqual(idle.queue_idle_work("w"), 4)

	def test_tops_up_once_down_to_half(self):
		request(12, 100, 100)
		idle.queue_idle_work("w")
		queue.claim_next("w")
		self.assertEqual(idle.queue_idle_work("w"), 0)  # three left: more than half
		queue.claim_next("w")
		self.assertEqual(idle.queue_idle_work("w"), 2)  # two left: back up to four

	def test_one_renderer_plans_at_a_time(self):
		request(12, 100, 100)
		with idle._planning() as planning:
			self.assertTrue(planning)
			self.assertTrue(idle.planning_now())
			self.assertEqual(idle.queue_idle_work("w"), 0)  # another is planning
		self.assertFalse(idle.planning_now())
		self.assertEqual(idle.queue_idle_work("w"), 4)


@override_settings(TILE_ZOOMS={12: {"source": "data", "cache": True}, 13: {"source": "crop", "from": 12}},
	IDLE_RADIUS=1, IDLE_SEEDS=200, SUMMARY_MAX_DEPTH=1)
class CachedBaseTests(TestCase):
	def test_tiles_rather_than_their_summaries(self):
		request(13, 200, 200)
		planned = idle.plan(20, "w")
		# Rendering a base tile builds its summary, so it isn't planned as well
		self.assertEqual(planned[0], (TileState.TILE, 12, 100, 100))
		self.assertEqual({k for k, z, _, _ in planned if z == 12}, {TileState.TILE})
		self.assertFalse(any(z == 13 for _, z, _, _ in planned))
