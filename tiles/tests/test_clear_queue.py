from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tiles import queue
from tiles.models import Job, TileRequest


class ClearQueueTests(TestCase):
	def test_clears_the_queued_jobs(self):
		waiting = TileRequest.objects.create(zoom=12, x=9, y=9, status=TileRequest.WAITING)
		claimed = TileRequest.objects.create(zoom=10, x=5, y=6, status=TileRequest.CLAIMED, claimed_by="host:1")
		Job.objects.create(kind=Job.TILE, zoom=12, x=1, y=2, status=Job.QUEUED,
			queued_at=timezone.now(), attempts=1, error="boom")
		Job.objects.create(kind=Job.SUMMARY, zoom=12, x=3, y=4, status=Job.QUEUED,
			idle=True, priority=-1, queued_at=timezone.now())
		kept = Job.objects.create(kind=Job.TILE, zoom=10, x=5, y=6, status=Job.RENDERING,
			claimed_by="host:1", claimed_at=timezone.now())
		out = StringIO()
		call_command("clear_queue", stdout=out)
		self.assertIn("Cleared 1 requests and 2 queued jobs", out.getvalue())
		# The waiting request is answered, as failed; the one being rendered for is left
		waiting.refresh_from_db(); claimed.refresh_from_db()
		self.assertEqual((waiting.status, claimed.status), (TileRequest.FAILED, TileRequest.CLAIMED))
		# The queued jobs are deleted; the one being rendered is left
		self.assertEqual(list(Job.objects.values_list("pk", "status")), [(kept.pk, Job.RENDERING)])

	def test_all_clears_the_rendering_jobs_too(self):
		job = Job.objects.create(kind=Job.TILE, zoom=10, x=5, y=6, status=Job.RENDERING,
			claimed_by="host:1", claimed_at=timezone.now())
		out = StringIO()
		call_command("clear_queue", "--all", stdout=out)
		self.assertIn("Cleared 0 requests and 1 queued jobs", out.getvalue())
		self.assertFalse(Job.objects.exists())
		# Its renderer failing it afterwards leaves it deleted
		queue.fail(job, "boom")
		self.assertFalse(Job.objects.exists())
