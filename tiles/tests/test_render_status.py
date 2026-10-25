from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tiles.models import TileRequest, Job


class RenderStatusTests(TestCase):
	def test_shows_the_workers_and_the_queue(self):
		Job.objects.create(kind=Job.TILE, zoom=12, x=1, y=2, status=Job.RENDERING,
			claimed_by="host:1:0", claimed_at=timezone.now())
		Job.objects.create(kind=Job.SUMMARY, zoom=12, x=3, y=4, status=Job.QUEUED,
			idle=True, priority=-1, queued_at=timezone.now())
		TileRequest.objects.create(zoom=12, x=7, y=8, status=TileRequest.WAITING, attempts=1)
		Job.objects.create(kind=Job.TILE, zoom=10, x=5, y=6, status=Job.FAILED,
			attempts=3, error="boom", queued_at=timezone.now())
		out = StringIO()
		call_command("render_status", stdout=out)
		text = out.getvalue()
		self.assertIn("renderers: 1", text)
		self.assertIn("host:1:0", text)
		self.assertIn("tile 12/1/2", text)
		self.assertIn("requests waiting: 1 (0 claimed, 0 cancelled)", text)
		self.assertIn("tile 12/7/8", text)
		self.assertIn("tried 1 times", text)
		self.assertIn("idle work queued: 1", text)
		self.assertIn("summary 12/3/4", text)
		self.assertIn("failed: 1", text)
		self.assertIn("3 tries: boom", text)
