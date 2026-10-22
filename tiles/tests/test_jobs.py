from unittest import mock

import pyo5m
from django.test import TestCase, override_settings

from summaries import build
from tiles import queue
from tiles.management.commands.render_tiles import render_one
from tiles.models import TileRequest, TileState

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
		tile = TileState.objects.get(kind=TileState.TILE)
		request.refresh_from_db()
		self.assertEqual((tile.status, tile.data_version, request.status), (TileState.DONE, 8, TileRequest.DONE))

	def test_a_tile_from_summaries_not_all_built_fails_without_queueing_them(self):
		build.store(12, 6, 8, town(), 8)  # one of the four
		request = want(11, 3, 4)
		with mock.patch(RENDER) as render:
			render_one("w")
		render.assert_not_called()
		tile = TileState.objects.get(kind=TileState.TILE)
		request.refresh_from_db()
		self.assertEqual((tile.status, request.status), (TileState.FAILED, TileRequest.FAILED))
		self.assertIn("aren't built: 12/6/9", tile.error)
		self.assertFalse(TileState.objects.filter(kind=TileState.SUMMARY).exists())

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
		TileState.objects.create(kind=TileState.SUMMARY, zoom=10, x=0, y=0, status=TileState.QUEUED, priority=-1)
		with mock.patch(RENDER):
			render_one("w")
		job = TileState.objects.get(kind=TileState.SUMMARY)
		self.assertEqual(job.status, TileState.FAILED)
		self.assertIn("finer summaries", job.error)
		self.assertEqual(TileState.objects.count(), 1)

	def test_a_summary_job_from_finer_summaries(self):
		for level, x, y in build.tiles_beneath(10, 0, 0, 12):
			build.store(level, x, y, town(), 5)
		TileState.objects.create(kind=TileState.SUMMARY, zoom=10, x=0, y=0, status=TileState.QUEUED, priority=-1)
		render_one("w")
		self.assertEqual(TileState.objects.get().status, TileState.DONE)
		self.assertEqual(build.missing([(10, 0, 0)]), [])


@override_settings(TILE_ZOOMS=ZOOMS, TILE_REQUEST_WAIT=0.2)
class PlaceholderTests(TestCase):
	def test_coarse_tile_without_its_summaries_is_a_placeholder(self):
		response = self.client.get("/tiles/10/0/0.pbf")
		self.assertEqual((response.status_code, response["X-Tile-Status"]), (204, "placeholder"))
		self.assertEqual(response["Cache-Control"], "no-store")
		tile = TileState.objects.get()
		self.assertEqual((tile.kind, tile.status, tile.request_count), (TileState.TILE, TileState.DONE, 1))
		# Nothing queued: no summary jobs, and no request left waiting
		self.assertFalse(TileState.objects.filter(kind=TileState.SUMMARY).exists())
		self.assertFalse(TileRequest.objects.exists())

	def test_a_tile_from_base_summaries_not_all_built_is_a_placeholder(self):
		response = self.client.get("/tiles/11/3/4.pbf")
		self.assertEqual((response.status_code, response["X-Tile-Status"]), (204, "placeholder"))

	def test_coarse_tile_waits_for_a_renderer_once_its_summaries_are_built(self):
		build.store(10, 0, 0, pyo5m.OsmData(), 3)  # z10 is drawn from its own level-10 summary
		with mock.patch("tiles.views.queue.wait_for_render", wraps=queue.wait_for_render) as wait:
			response = self.client.get("/tiles/10/0/0.pbf")
		self.assertEqual(response.status_code, 503)  # waited; no renderer in the test
		wait.assert_called_once()
