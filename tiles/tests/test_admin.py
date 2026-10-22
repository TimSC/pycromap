from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from tiles.admin import TileStateAdmin
from tiles.models import TileState


class AdminTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
		self.client.login(username="admin", password="password")

	def test_changelist_shows_the_jobs_and_their_counts(self):
		TileState.objects.create(kind=TileState.TILE, zoom=12, x=1, y=2, status=TileState.DONE,
			request_count=3, data_version=9, render_seconds=1.5)
		TileState.objects.create(kind=TileState.SUMMARY, zoom=12, x=3, y=4, status=TileState.QUEUED,
			idle=True, priority=-1)
		response = self.client.get("/admin/tiles/tilestate/")
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "queued: 1")
		self.assertContains(response, "idle queued: 1")
		self.assertContains(response, "done: 1")
		self.assertContains(response, "summary 12/3/4 (idle)")
		self.assertContains(response, "v9")

	def test_change_page_is_read_only(self):
		tile = TileState.objects.create(kind=TileState.TILE, zoom=12, x=1, y=2, status=TileState.DONE)
		response = self.client.get("/admin/tiles/tilestate/{}/change/".format(tile.pk))
		self.assertEqual(response.status_code, 200)

	def test_jobs_cannot_be_added_or_deleted(self):
		admin = TileStateAdmin(TileState, None)
		request = RequestFactory().get("/")
		self.assertFalse(admin.has_add_permission(request))
		self.assertFalse(admin.has_delete_permission(request))
		self.assertFalse(admin.has_delete_permission(request, TileState()))
