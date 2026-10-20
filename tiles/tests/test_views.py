import tempfile
import threading
from pathlib import Path
from unittest import mock

import pyo5m
from datetime import timedelta

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from tiles.management.commands.render_tiles import render_one
from tiles.models import TileState
from tiles.render import EMPTY_TILE


class TileViewTests(TransactionTestCase):
	def setUp(self):
		self.cache = tempfile.TemporaryDirectory()
		self.settings = override_settings(TILE_CACHE_DIR=Path(self.cache.name), TILE_CACHE_URL="/tilecache/",
			TILE_ZOOMS={13: {"source": "data", "cache": False}, 12: {"source": "data", "cache": True, "max_age": 3600},
				14: {"source": "crop", "from": 12}},
			TILE_REQUEST_WAIT=5)
		self.settings.enable()

	def tearDown(self):
		self.settings.disable()
		self.cache.cleanup()

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
		response = self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content, EMPTY_TILE)
		self.assertEqual((response["Content-Type"], response["Content-Encoding"]),
			("application/vnd.mapbox-vector-tile", "gzip"))

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_tile_rendered_before_its_zoom_was_cached_is_rendered_again(self):
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), data=b"tile")
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 503)
		self.assertEqual(TileState.objects.get().status, TileState.QUEUED)

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_cached_tile_older_than_max_age_is_rendered_again(self):
		(Path(self.cache.name) / "12/1").mkdir(parents=True)
		(Path(self.cache.name) / "12/1/2.5.pbf.gz").write_bytes(b"tile")
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE,
			rendered_at=timezone.now() - timedelta(seconds=3601), file="12/1/2.5.pbf.gz")
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 503)
		self.assertEqual(TileState.objects.get().status, TileState.QUEUED)

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
		self.assertEqual(TileState.objects.get(zoom=12).status, TileState.QUEUED)

	@override_settings(TILE_REQUEST_WAIT=0.2)
	def test_times_out_without_a_renderer(self):
		response = self.client.get("/tiles/13/1/2.pbf")
		self.assertEqual(response.status_code, 503)
		self.assertEqual(TileState.objects.get().status, TileState.QUEUED)

	def test_uncached_tile_is_rendered_and_returned(self):
		stop = threading.Event()

		def renderer():
			while not stop.is_set():
				if not render_one("test"):
					stop.wait(0.02)

		data = pyo5m.OsmData()
		data.nodes.append(pyo5m.Node(1, 0.0, 0.0, {}))
		with mock.patch("tiles.management.commands.render_tiles.read_tile_data", return_value=(data, 9)), \
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
