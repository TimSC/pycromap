import threading
from unittest import mock

import pyo5m
from django.test import TestCase, override_settings

from summaries import build
from tiles import queue
from tiles.management.commands.render_tiles import render_one
from tiles.models import Job, Tile, TileRequest

ZOOMS = {12: {"source": "data", "cache": False, "summary": True},
	11: {"source": "summary", "level": 12, "cache": False},
	10: {"source": "summary", "level": 10, "cache": False, "summary": True}}
RENDER = "tiles.management.commands.render_tiles.render_data"
READ = "tiles.management.commands.render_tiles.read_tile_data"


def want(zoom, x, y):
	"""A request waiting for a tile to be rendered."""
	return TileRequest.objects.create(zoom=zoom, x=x, y=y, status=TileRequest.WAITING)


def town():
	data = pyo5m.OsmData()
	data.nodes = [pyo5m.Node(1, 50.0, -1.0, {"place": "town", "name": "T"})]
	return data


@override_settings(TILE_ZOOMS=ZOOMS)
class JobTests(TestCase):
	def test_a_tile_from_summaries_all_built(self):
		for level, x, y in build.tiles_beneath(11, 3, 4, 12):
			build.store(level, x, y, town(), 8)
		request = want(11, 3, 4)
		with mock.patch(RENDER, return_value=b"tile") as render:
			render_one("w")
		self.assertEqual(len(render.call_args[0][0].nodes), 1)  # four summaries, merged
		tile = Tile.objects.get()
		request.refresh_from_db()
		self.assertEqual((tile.data_version, request.status), (8, TileRequest.DONE))
		self.assertFalse(Job.objects.exists())

	def test_a_claimed_tile_has_its_missing_summaries_built_first(self):
		build.store(12, 6, 8, town(), 8)  # one of the four
		request = want(11, 3, 4)
		with mock.patch(READ, return_value=(town(), 9)) as read, mock.patch(RENDER, return_value=b"tile") as render:
			render_one("w")
		self.assertEqual(read.call_count, 3)  # the three missing, from the map data
		self.assertEqual(build.missing(build.tiles_beneath(11, 3, 4, 12)), [])
		render.assert_called_once()
		# Each a job, claimed, and deleted once built, as the tile's
		self.assertFalse(Job.objects.exists())
		self.assertEqual(Tile.objects.count(), 1)
		request.refresh_from_db()
		self.assertEqual(request.status, TileRequest.DONE)

	def test_the_whole_cascade_finest_first(self):
		# z10 is made from its level-10 summary, built from 16 level-12 ones
		want(10, 0, 0)
		built = []
		original = build.store

		def storing(level, x, y, data, version):
			built.append(level)
			return original(level, x, y, data, version)
		with mock.patch(READ, return_value=(town(), 9)) as read, mock.patch(RENDER, return_value=b"tile") as render, \
				mock.patch("summaries.build.store", side_effect=storing):
			render_one("w")
		self.assertEqual(read.call_count, 16)
		self.assertEqual(built, [12] * 16 + [10])
		render.assert_called_once()
		self.assertTrue(Tile.objects.exists())
		self.assertFalse(Job.objects.exists())

	def test_idle_work_builds_nothing_it_needs(self):
		Job.objects.create(kind=Job.TILE, zoom=11, x=3, y=4, status=Job.QUEUED, idle=True, priority=-1)
		with mock.patch(READ) as read, mock.patch(RENDER):
			with override_settings(RENDER_IDLE_WORK=True):
				render_one("w")
		read.assert_not_called()
		self.assertEqual(Job.objects.get(kind=Job.TILE).status, Job.FAILED)

	def test_a_summary_that_fails_fails_the_tile(self):
		request = want(11, 3, 4)
		with mock.patch(READ, side_effect=RuntimeError("no map")), mock.patch(RENDER) as render:
			render_one("w")
		render.assert_not_called()
		tile = Job.objects.get(kind=Job.TILE)
		request.refresh_from_db()
		self.assertEqual((tile.status, request.status), (Job.FAILED, TileRequest.FAILED))
		self.assertIn("it needs failed: no map", tile.error)
		self.assertEqual(Job.objects.filter(kind=Job.SUMMARY, status=Job.FAILED).count(), 1)

	def test_building_stops_once_nobody_wants_the_tile(self):
		from tiles.management.commands.render_tiles import FinalError, build_dependencies
		job = Job.objects.create(kind=Job.TILE, zoom=11, x=3, y=4, status=Job.RENDERING,
			claimed_by="w", priority=1)
		TileRequest.objects.create(zoom=11, x=3, y=4, status=TileRequest.CLAIMED, claimed_by="w", cancelled=True)
		with mock.patch(READ) as read:
			with self.assertRaises(FinalError):
				build_dependencies(job, build.tiles_beneath(11, 3, 4, 12), "w")
		read.assert_not_called()

	def test_a_stopping_renderer_breaks_off_a_cascade_and_leaves_its_claim(self):
		from tiles.management.commands.render_tiles import Stopped, build_dependencies
		job = Job.objects.create(kind=Job.TILE, zoom=11, x=3, y=4, status=Job.RENDERING,
			claimed_by="w", priority=1)
		want(11, 3, 4)
		stop = threading.Event()
		stop.set()
		with mock.patch(READ) as read:
			with self.assertRaises(Stopped):
				build_dependencies(job, build.tiles_beneath(11, 3, 4, 12), "w", stop=stop)
		read.assert_not_called()
		# Through render_one: the job isn't failed, but left claimed, to be released
		request = want(10, 0, 0)
		with mock.patch(READ), mock.patch(RENDER) as render:
			render_one("v", stop)
		render.assert_not_called()
		tile = Job.objects.get(kind=Job.TILE, zoom=10)
		request.refresh_from_db()
		self.assertEqual((tile.status, tile.claimed_by, request.status), (Job.RENDERING, "v", TileRequest.CLAIMED))

	def test_waits_for_a_summary_another_renderer_is_building(self):
		from tiles.management.commands.render_tiles import build_dependencies
		for x, y in ((6, 8), (6, 9), (7, 8)):
			build.store(12, x, y, town(), 8)
		other = Job.objects.create(kind=Job.SUMMARY, zoom=12, x=7, y=9, status=Job.RENDERING,
			claimed_by="other", priority=1)
		job = Job.objects.create(kind=Job.TILE, zoom=11, x=3, y=4, status=Job.RENDERING,
			claimed_by="w", priority=1)
		want(11, 3, 4)

		def other_finishes(seconds):
			build.store(12, 7, 9, town(), 8)
			Job.objects.filter(pk=other.pk).delete()
		with mock.patch(READ) as read, mock.patch("tiles.management.commands.render_tiles.time.sleep",
				side_effect=other_finishes) as sleep:
			build_dependencies(job, build.tiles_beneath(11, 3, 4, 12), "w")
		sleep.assert_called_once()
		read.assert_not_called()  # it built none: it waited

	def test_rendering_from_map_data_builds_the_base_summary(self):
		want(12, 5, 6)
		with mock.patch(READ, return_value=(pyo5m.OsmData(), 8)), mock.patch(RENDER, return_value=b"tile"):
			render_one("w")
		self.assertEqual(build.missing([(12, 5, 6)]), [])

	@override_settings(TILE_ZOOMS={12: {"source": "data", "cache": False}})
	def test_a_zoom_without_summary_builds_none(self):
		want(12, 5, 6)
		with mock.patch(READ, return_value=(pyo5m.OsmData(), 8)), mock.patch(RENDER, return_value=b"tile"):
			render_one("w")
		self.assertEqual(build.missing([(12, 5, 6)]), [(12, 5, 6)])

	def test_a_summary_job_needing_finer_summaries_fails_without_queueing_them(self):
		# As the idle renderers queue them
		Job.objects.create(kind=Job.SUMMARY, zoom=10, x=0, y=0, status=Job.QUEUED, idle=True, priority=-1)
		with mock.patch(RENDER):
			render_one("w")
		job = Job.objects.get(kind=Job.SUMMARY)
		self.assertEqual(job.status, Job.FAILED)
		self.assertIn("finer summaries", job.error)
		self.assertEqual(Job.objects.count(), 1)

	def test_a_summary_job_from_finer_summaries(self):
		for level, x, y in build.tiles_beneath(10, 0, 0, 12):
			build.store(level, x, y, town(), 5)
		Job.objects.create(kind=Job.SUMMARY, zoom=10, x=0, y=0, status=Job.QUEUED, idle=True, priority=-1)
		render_one("w")
		self.assertFalse(Job.objects.exists())
		self.assertEqual(build.missing([(10, 0, 0)]), [])


@override_settings(TILE_ZOOMS=ZOOMS, TILE_REQUEST_WAIT=0.2, CASCADE_MIN_ZOOM=10)
class PlaceholderTests(TestCase):
	def queued(self, path):
		with mock.patch("tiles.views.queue.wait_for_render", wraps=queue.wait_for_render) as wait:
			response = self.client.get(path)
		return response, wait.called

	def test_at_or_above_cascade_min_zoom_a_tile_is_queued_with_its_summaries_missing(self):
		response, queued = self.queued("/tiles/10/0/0.pbf")
		self.assertEqual(response.status_code, 503)  # waited; no renderer in the test
		self.assertTrue(queued)
		self.assertFalse(Job.objects.filter(kind=Job.SUMMARY).exists())  # the renderer builds them

	@override_settings(CASCADE_MIN_ZOOM=11)
	def test_below_it_queued_once_its_summaries_are_built(self):
		build.store(10, 0, 0, pyo5m.OsmData(), 3)
		response, queued = self.queued("/tiles/10/0/0.pbf")
		self.assertEqual(response.status_code, 503)  # waited; no renderer in the test
		self.assertTrue(queued)

	@override_settings(CASCADE_MIN_ZOOM=11)
	def test_below_it_a_placeholder_while_a_summary_cant_be_built(self):
		# z10 is made from its level-10 summary, which is made from 16
		# level-12 ones: 15 are built, so it can't be built straight away
		for _, x, y in build.tiles_beneath(10, 0, 0, 12)[:15]:
			build.store(12, x, y, pyo5m.OsmData(), 3)
		response, queued = self.queued("/tiles/10/0/0.pbf")
		self.assertEqual((response.status_code, response["X-Tile-Status"]), (204, "placeholder"))
		self.assertEqual(response["Cache-Control"], "no-store")
		self.assertFalse(queued)
		# Nothing recorded: the tile isn't queued
		self.assertFalse(Job.objects.exists())
		self.assertFalse(Tile.objects.exists())
		self.assertFalse(TileRequest.objects.exists())

	@override_settings(CASCADE_MIN_ZOOM=11)
	def test_below_it_the_summaries_it_is_made_from_are_built(self):
		for _, x, y in build.tiles_beneath(10, 0, 0, 12):
			build.store(12, x, y, town(), 8)
		response, queued = self.queued("/tiles/10/0/0.pbf")
		self.assertTrue(queued)  # its level-10 summary can be built straight away
		request = want(10, 0, 0)
		with mock.patch(READ) as read, mock.patch(RENDER, return_value=b"tile") as render:
			render_one("w")
		read.assert_not_called()  # from the level-12 summaries, not the map data
		render.assert_called_once()
		self.assertEqual(build.missing([(10, 0, 0)]), [])
		request.refresh_from_db()
		self.assertEqual(request.status, TileRequest.DONE)

	@override_settings(CASCADE_MIN_ZOOM=11)
	def test_below_it_one_level_only(self):
		# Queued regardless (the web server wouldn't have): its level-10
		# summary can't be built, as the level-12 ones aren't, and those
		# aren't built for it
		request = want(10, 0, 0)
		with mock.patch(READ) as read, mock.patch(RENDER) as render:
			render_one("w")
		read.assert_not_called(); render.assert_not_called()
		request.refresh_from_db()
		self.assertEqual(request.status, TileRequest.FAILED)

	@override_settings(CASCADE_MIN_ZOOM=12)
	def test_below_it_base_summaries_are_built_from_the_map_data(self):
		# One level down from z11 is the base level, built from the map data
		build.store(12, 6, 8, town(), 8)
		response, queued = self.queued("/tiles/11/3/4.pbf")
		self.assertTrue(queued)
		request = want(11, 3, 4)
		with mock.patch(READ, return_value=(town(), 9)) as read, mock.patch(RENDER, return_value=b"tile"):
			render_one("w")
		self.assertEqual(read.call_count, 3)  # the three missing
		request.refresh_from_db()
		self.assertEqual(request.status, TileRequest.DONE)

	@override_settings(CASCADE_MIN_ZOOM=12)
	def test_below_it_rendered_without_building_anything(self):
		for _, x, y in build.tiles_beneath(11, 3, 4, 12):
			build.store(12, x, y, town(), 8)
		request = want(11, 3, 4)
		with mock.patch(READ) as read, mock.patch(RENDER, return_value=b"tile"):
			render_one("w")
		read.assert_not_called()
		request.refresh_from_db()
		self.assertEqual(request.status, TileRequest.DONE)
