import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from tiles.management.commands.render_tiles import render_one
from tiles.models import TileState


class TileViewTests(TransactionTestCase):
	def setUp(self):
		self.cache = tempfile.TemporaryDirectory()
		self.settings = override_settings(TILE_CACHE_DIR=Path(self.cache.name), TILE_CACHE_URL="/tilecache/",
			TILE_ZOOMS={13: {"source": "data", "cache": False}, 12: {"source": "data", "cache": True}},
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
		(Path(self.cache.name) / "12/1/2.5.pbf").write_bytes(b"tile")
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), file="12/1/2.5.pbf")
		response = self.client.get("/tiles/12/1/2.pbf")
		self.assertEqual((response.status_code, response["Location"]), (302, "/tilecache/12/1/2.5.pbf"))
		self.assertEqual(TileState.objects.get().request_count, 1)

	def test_empty_cached_tile(self):
		TileState.objects.create(zoom=12, x=1, y=2, status=TileState.DONE, rendered_at=timezone.now(), file="")
		self.assertEqual(self.client.get("/tiles/12/1/2.pbf").status_code, 204)

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

		with mock.patch("tiles.management.commands.render_tiles.render_tile", return_value=(b"vector tile", 9)):
			thread = threading.Thread(target=renderer)
			thread.start()
			try:
				response = self.client.get("/tiles/13/1/2.pbf")
				again = self.client.get("/tiles/13/1/2.pbf")
			finally:
				stop.set()
				thread.join()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(b"".join(response.streaming_content), b"vector tile")
		self.assertEqual(response["Content-Type"], "application/vnd.mapbox-vector-tile")
		self.assertEqual(again.status_code, 200)
		tile = TileState.objects.get()
		self.assertEqual((tile.request_count, tile.data_version), (2, 9))


class PageTests(TransactionTestCase):
	def test_pages(self):
		self.assertEqual(self.client.get("/").status_code, 200)
		spec = self.client.get("/spec.json").json()
		self.assertEqual(spec["tiles"], ["/tiles/{z}/{x}/{y}.pbf"])
		style = self.client.get("/style.json").json()
		self.assertEqual(style["sources"]["openmaptiles"]["url"], "/spec.json")
		self.assertTrue(style["glyphs"].startswith("/static/tiles/fonts/"))
