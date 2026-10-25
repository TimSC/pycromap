from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from tiles.admin import JobAdmin, TileAdmin
from tiles.models import Job, Tile


class AdminTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
		self.client.login(username="admin", password="password")

	def test_changelist_shows_the_jobs_and_their_counts(self):
		Job.objects.create(kind=Job.TILE, zoom=12, x=1, y=2, status=Job.FAILED, attempts=3)
		Job.objects.create(kind=Job.SUMMARY, zoom=12, x=3, y=4, status=Job.QUEUED,
			idle=True, priority=-1)
		response = self.client.get("/admin/tiles/job/")
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "queued: 1")
		self.assertContains(response, "idle queued: 1")
		self.assertContains(response, "failed: 1")
		self.assertContains(response, "summary 12/3/4 (idle)")

	def test_tiles_are_listed(self):
		Tile.objects.create(zoom=12, x=1, y=2, rendered_at=timezone.now(), data_version=9, render_seconds=1.5)
		response = self.client.get("/admin/tiles/tile/")
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "tile 12/1/2")

	def test_change_page_is_read_only(self):
		job = Job.objects.create(kind=Job.TILE, zoom=12, x=1, y=2)
		response = self.client.get("/admin/tiles/job/{}/change/".format(job.pk))
		self.assertEqual(response.status_code, 200)

	def test_jobs_and_tiles_cannot_be_added_or_deleted(self):
		request = RequestFactory().get("/")
		for admin, obj in ((JobAdmin(Job, None), Job()), (TileAdmin(Tile, None), Tile())):
			self.assertFalse(admin.has_add_permission(request))
			self.assertFalse(admin.has_delete_permission(request))
			self.assertFalse(admin.has_delete_permission(request, obj))
