import tempfile
import threading
from pathlib import Path
from unittest import mock

import pyo5m
from datetime import timedelta

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from tiles.management.commands.render_tiles import render_one
from tiles.models import TileRequest, TileState


class TileViewTests(TransactionTestCase):
	def setUp(self):
		self.cache = tempfile.TemporaryDirectory()
		self.settings = override_settings(TILE_CACHE_DIR=Path(self.cache.name), TILE_CACHE_URL="/tilecache/",
			TILE_ZOOMS={13: {"source": "data", "cache": False}, 12: {"source": "data", "cache": True, "max_age": 3600},
				14: {"source": "crop", "from": 12}},
			TILE_REQUEST_WAIT=5, EDIT_CHECK_INTERVAL=0)
		self.settings.enable()
		# Whether the map has been edited: it hasn't, unless a test says so
		self.edited = mock.patch("tiles.views.changes.changed_since", return_value=False)
		self.changed_since = self.edited.start()
		# The tiles requests asked to have rendered (queue.wait_for_render)
		from tiles import queue
		wait_for_render, self.asked = queue.wait_for_render, []

		def asking(request_id):
			request = TileRequest.objects.get(pk=request_id)
			self.asked.append((request.zoom, request.x, request.y))
			wait_for_render(request_id)
		self.asking = mock.patch("tiles.views.queue.wait_for_render", side_effect=asking)
		self.asking.start()

	def tearDown(self):
		self.asking.stop()
		self.edited.stop()
		self.settings.disable()
		self.cache.cleanup()

	def cached(self, zoom=12, x=1, y=2, version=5):
		"""A cached tile, rendered just now from the map as at edit version."""
		path = "{}/{}/{}.{}.pbf.gz".format(zoom, x, y, version)
		(Path(self.cache.name) / path).parent.mkdir(parents=True, exist_ok=True)
		(Path(self.cache.name) / path).write_bytes(b"tile")
		return TileState.objects.create(zoom=zoom, x=x, y=y, status=TileState.DONE, rendered_at=timezone.now(),
			file=path, data_version=version)

	def flags(self):
		"""Patches the flagging of cache redirects to record, for each request
		flagged, whether its row was flagged (rows are deleted once answered)."""
		from tiles import queue
		original, seen = queue.answered_by_cache, []

		async def flagging(request_id):
			await original(request_id)
			seen.append(await TileRequest.objects.filter(pk=request_id).values_list("cache_redirect", flat=True).afirst())
		return mock.patch("tiles.views.queue.answered_by_cache", flagging), seen

	def test_a_cache_redirect_is_flagged_in_the_request_table(self):
		self.cached()
		patch, seen = self.flags()
		with patch:
			response = self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual(response.status_code, 302)
		self.assertEqual(seen, [True])
		self.assertFalse(TileRequest.objects.exists())  # answered: deleted all the same

	def test_other_answers_are_not_flagged(self):
		# An empty cached tile (no file) and a placeholder answer otherwise
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), file="")
		patch, seen = self.flags()
		with patch:
			self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 204)
		self.assertEqual(seen, [])

	def test_cached_tile_is_kept_while_its_area_is_unedited(self):
		from tiles.tilegeo import tile_area
		self.cached()
		response = self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual(response.status_code, 302)
		# Asked about the tile's area, since the edit it was made at
		self.changed_since.assert_called_once_with(5, *tile_area(12, 1, 2))

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_cached_tile_is_rendered_again_once_its_area_is_edited(self):
		self.cached()
		self.changed_since.return_value = True
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 503)  # waited; no renderer here
		self.assertEqual(self.asked, [(12, 1, 2)])

	def test_cached_tile_is_served_if_the_map_cant_say(self):
		from django.db import OperationalError
		self.cached()
		self.changed_since.side_effect = OperationalError("the map database is down")
		with self.assertLogs("tiles.views", "WARNING"):
			self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 302)

	@override_settings(EDIT_CHECK_INTERVAL=60)
	def test_edits_are_checked_at_most_once_a_minute(self):
		tile = self.cached()
		# Rendered just now: not asked
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 302)
		self.changed_since.assert_not_called()
		# A minute on, asked, and found unedited: not asked for another minute
		TileState.objects.filter(pk=tile.pk).update(rendered_at=timezone.now() - timedelta(seconds=61))
		self.client.get("/tiles/12/1/2.pbf")
		self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual(self.changed_since.call_count, 1)
		tile.refresh_from_db()
		self.assertIsNotNone(tile.edits_checked_at)
		# Another minute on, asked again
		TileState.objects.filter(pk=tile.pk).update(edits_checked_at=timezone.now() - timedelta(seconds=61))
		self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual(self.changed_since.call_count, 2)

	@override_settings(EDIT_CHECK_INTERVAL=60, TILE_REQUEST_WAIT=0.2)
	def test_an_edited_tile_is_not_marked_checked(self):
		tile = self.cached()
		TileState.objects.filter(pk=tile.pk).update(rendered_at=timezone.now() - timedelta(seconds=61))
		self.changed_since.return_value = True
		self.client.get("/tiles/12/1/2.pbf")
		tile.refresh_from_db()
		self.assertIsNone(tile.edits_checked_at)
		self.assertEqual(self.asked, [(12, 1, 2)])

	def test_a_failed_check_is_not_marked_checked(self):
		from django.db import OperationalError
		tile = self.cached()
		self.changed_since.side_effect = OperationalError("the map database is down")
		with self.assertLogs("tiles.views", "WARNING"):
			self.client.get("/tiles/12/1/2.pbf")
		tile.refresh_from_db()
		self.assertIsNone(tile.edits_checked_at)

	def test_checking_edits_can_be_turned_off(self):
		self.cached()
		self.changed_since.return_value = True
		with override_settings(TILE_ZOOMS={12: {"source": "data", "cache": True, "check_edits": False}}):
			self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 302)
		self.changed_since.assert_not_called()

	def test_tiles_made_from_summaries_are_not_checked(self):
		self.cached(zoom=11)
		with override_settings(TILE_ZOOMS={11: {"source": "summary", "level": 12, "cache": True}}):
			self.assertEqual(self.client.get("/tiles/11/1/2.pbf").status_code, 302)
		self.changed_since.assert_not_called()

	def test_unserved_zoom_and_bad_tiles(self):
		self.assertEqual(self.client.get("/tiles/11/0/0.pbf").status_code, 404)
		self.assertEqual(self.client.get("/tiles/13/8192/0.pbf").status_code, 404)

	def test_cached_tile_redirects_to_its_file(self):
		(Path(self.cache.name) / "12/1").mkdir(parents=True)
		(Path(self.cache.name) / "12/1/2.5.pbf.gz").write_bytes(b"tile")
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), file="12/1/2.5.pbf.gz")
		response = self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual((response.status_code, response["Location"]), (302, "/tilecache/12/1/2.5.pbf.gz"))
		self.assertEqual(TileState.objects.get().request_count, 1)

		# The file itself, while DEBUG is on (it is in production)
		with override_settings(DEBUG=True):
			from tiles.views import cached_tile
			from django.test import RequestFactory
			response = cached_tile(RequestFactory().get("/"), "12/1/2.5.pbf.gz")
		self.assertEqual(b"".join(response.streaming_content), b"tile")
		self.assertEqual((response["Content-Encoding"], response["Content-Type"]), ("gzip", "application/vnd.mapbox-vector-tile"))
		self.assertIn("max-age=3600", response["Cache-Control"])

	def test_cache_view_of_common_files_and_unserved_zooms(self):
		from django.test import RequestFactory
		from tiles.views import cached_tile
		for path in ("5/1/2.0.pbf.gz", "common/sea.pbf.gz", "common/7c402ade53ede63ca7ee6d5120fd40ab.pbf.gz"):
			(Path(self.cache.name) / path).parent.mkdir(parents=True, exist_ok=True)
			(Path(self.cache.name) / path).write_bytes(b"tile")
		with override_settings(DEBUG=True):
			unserved = cached_tile(RequestFactory().get("/"), "5/1/2.0.pbf.gz")
			named = cached_tile(RequestFactory().get("/"), "common/sea.pbf.gz")
			hashed = cached_tile(RequestFactory().get("/"), "common/7c402ade53ede63ca7ee6d5120fd40ab.pbf.gz")
		# A common file named for what it shows may be made again; one named
		# by its content can't change
		self.assertEqual(named["Cache-Control"], "public, max-age=86400")
		self.assertEqual(hashed["Cache-Control"], "public, max-age=31536000, immutable")
		self.assertEqual(b"".join(hashed.streaming_content), b"tile")
		self.assertEqual(unserved["Cache-Control"], "public, max-age=86400")

	def test_cache_view_stays_in_the_cache(self):
		from django.core.exceptions import SuspiciousFileOperation
		from django.http import Http404
		from django.test import RequestFactory
		from tiles.views import cached_tile
		with self.assertRaises(SuspiciousFileOperation):
			cached_tile(RequestFactory().get("/"), "../../etc/passwd.pbf.gz")
		with self.assertRaises(Http404):
			cached_tile(RequestFactory().get("/"), "12/1/9.pbf")

	def test_empty_cached_tile(self):
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), file="")
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 204)

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_tile_rendered_before_its_zoom_was_cached_is_rendered_again(self):
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), data=b"tile")
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 503)
		self.assertEqual(self.asked, [(12, 1, 2)])

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_cached_tile_older_than_max_age_is_rendered_again(self):
		(Path(self.cache.name) / "12/1").mkdir(parents=True)
		(Path(self.cache.name) / "12/1/2.5.pbf.gz").write_bytes(b"tile")
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE,
			rendered_at=timezone.now() - timedelta(seconds=3601), file="12/1/2.5.pbf.gz")
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 503)
		self.assertEqual(self.asked, [(12, 1, 2)])

	def test_cropped_from_the_cached_tile(self):
		from tiles.tests.test_crop import decoded, vector_tile
		(Path(self.cache.name) / "12/1").mkdir(parents=True)
		(Path(self.cache.name) / "12/1/2.5.pbf.gz").write_bytes(
			vector_tile([{"geometry": "POINT (3000 1000)", "properties": {"name": "here"}}]))
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), file="12/1/2.5.pbf.gz")
		# The third of four across, the first down
		response = self.client.get("/tiles/14/6/8.pbf")
		self.assertEqual(response.status_code, 200)
		self.assertEqual((response["Content-Encoding"], response["Cache-Control"]), ("gzip", "no-store"))
		self.assertEqual(decoded(response.content)["place"]["features"][0]["geometry"]["coordinates"], [(3000 - 2048) * 4, 1000 * 4])
		self.assertEqual(self.client.get("/tiles/14/4/8.pbf").status_code, 204)
		# Logged as asked for, as is the tile it came from
		self.assertEqual(TileState.objects.get(zoom=14, x=6, y=8).request_count, 1)
		self.assertEqual(TileState.objects.get(zoom=12).request_count, 2)

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_crop_waits_for_the_tile_it_comes_from(self):
		self.assertEqual(self.client.get("/tiles/14/5/8.pbf").status_code, 503)
		self.assertEqual(self.asked, [(12, 1, 2)])  # the tile it is cut from

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_times_out_without_a_renderer(self):
		response = self.client.get("/tiles/13/1/2.pbf")
		self.assertEqual(response.status_code, 503)
		self.assertEqual(self.asked, [(13, 1, 2)])
		# Answered (not ready), so no longer waiting
		self.assertFalse(TileRequest.objects.exists())

	def test_uncached_tile_is_rendered_and_returned(self):
		stop = threading.Event()

		def renderer():
			while not stop.is_set():
				if not render_one("test"):
					stop.wait(0.02)

		with mock.patch("tiles.management.commands.render_tiles.read_tile_data", return_value=(pyo5m.OsmData(), 9)), \
				mock.patch("tiles.management.commands.render_tiles.render_data", return_value=b"vector tile"):
			thread = threading.Thread(target=renderer)
			thread.start()
			try:
				response = self.client.get("/tiles/13/1/2.pbf")
				again = self.client.get("/tiles/13/1/2.pbf")
			finally:
				stop.set()
				thread.join()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content, b"vector tile")
		self.assertEqual((response["Content-Type"], response["Content-Encoding"]), ("application/vnd.mapbox-vector-tile", "gzip"))
		self.assertEqual(again.status_code, 200)
		tile = TileState.objects.get()
		self.assertEqual((tile.request_count, tile.data_version), (2, 9))


class PageTests(TransactionTestCase):
	def test_pages(self):
		self.assertEqual(self.client.get("/").status_code, 200)
		spec = self.client.get("/spec.json").json()
		self.assertEqual(spec["tiles"], ["/tiles/{z}/{x}/{y}.pbf"])
		with override_settings(VIEWER_MAX_ZOOM=16):
			self.assertEqual(self.client.get("/metadata").json()["viewer_maxzoom"], 16)
		style = self.client.get("/style.json").json()
		self.assertEqual(style["sources"]["openmaptiles"]["url"], "/spec.json")
		self.assertTrue(style["glyphs"].startswith("/static/tiles/fonts/"))


@override_settings(TILE_ZOOMS={12: {"source": "data", "cache": True}}, TILE_REQUEST_WAIT=30, EDIT_CHECK_INTERVAL=0)
class TileRequestTests(TransactionTestCase):
	"""Requests are kept while they are answered; one whose client goes away
	(Django cancels the view) is flagged, and kept a while."""

	def view(self, *args):
		from django.test import RequestFactory
		from tiles import views
		return views.tile(RequestFactory().get("/tiles/12/1/2.pbf"), *args)

	def test_an_answered_request_is_not_kept(self):
		import asyncio
		with override_settings(TILE_REQUEST_WAIT=0.1):
			response = asyncio.run(self.view(12, 1, 2))
		self.assertEqual(response.status_code, 503)  # not rendered in time: answered all the same
		self.assertFalse(TileRequest.objects.exists())

	def test_a_request_whose_client_goes_is_flagged(self):
		import asyncio

		async def ask_then_go():
			task = asyncio.create_task(self.view(12, 1, 2))
			# Waiting for a renderer when the client goes
			for _ in range(100):
				await asyncio.sleep(0.05)
				if await TileRequest.objects.filter(status=TileRequest.WAITING).aexists():
					break
			self.assertTrue(await TileRequest.objects.filter(cancelled=False).aexists())
			task.cancel()
			with self.assertRaises(asyncio.CancelledError):
				await task

		asyncio.run(ask_then_go())
		request = TileRequest.objects.get()
		self.assertEqual((request.zoom, request.x, request.y, request.cancelled), (12, 1, 2, True))
		self.assertIsNotNone(request.cancelled_at)
		# It no longer wants its tile rendered: a renderer doesn't claim it
		from tiles import queue
		self.assertEqual(request.status, TileRequest.WAITING)
		self.assertIsNone(queue.claim_next("w"))

	def test_a_request_that_fails_is_not_kept(self):
		import asyncio
		with mock.patch("tiles.views._answer", side_effect=RuntimeError("broken")):
			with self.assertRaises(RuntimeError):
				asyncio.run(self.view(12, 1, 2))
		self.assertFalse(TileRequest.objects.exists())

	@override_settings(CANCELLED_REQUEST_KEEP=600, TILE_REQUEST_WAIT=300)
	def test_requests_are_forgotten_after_a_while(self):
		from tiles import queue
		now = timezone.now()

		def request(y, started, cancelled=None):
			made = TileRequest.objects.create(zoom=12, x=1, y=y, cancelled=cancelled is not None,
				cancelled_at=None if cancelled is None else now - timedelta(seconds=cancelled))
			# started_at is set as it is made
			TileRequest.objects.filter(pk=made.pk).update(started_at=now - timedelta(seconds=started))
			return made.pk

		request(1, 700, cancelled=601)                  # cancelled too long ago
		recent = request(2, 700, cancelled=599)         # cancelled, kept a while yet
		request(3, 301)                                 # unanswered for longer than a request waits
		answering = request(4, 299)                     # still being answered
		self.assertEqual(queue.forget_requests(), 2)
		self.assertEqual(set(TileRequest.objects.values_list("pk", flat=True)), {recent, answering})
