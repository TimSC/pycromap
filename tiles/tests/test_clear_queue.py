from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tiles.models import TileState


class ClearQueueTests(TestCase):
	def test_clears_the_queued_jobs(self):
		TileState.objects.create(kind=TileState.TILE, zoom=12, x=1, y=2, status=TileState.QUEUED,
			queued_at=timezone.now(), attempts=1, error="boom")
		TileState.objects.create(kind=TileState.SUMMARY, zoom=12, x=3, y=4, status=TileState.QUEUED,
			priority=-1.0, queued_at=timezone.now())
		kept = TileState.objects.create(kind=TileState.TILE, zoom=10, x=5, y=6, status=TileState.RENDERING,
			claimed_by="host:1", claimed_at=timezone.now())
		out = StringIO()
		call_command("clear_queue", stdout=out)
		self.assertIn("Cleared 2", out.getvalue())
		self.assertFalse(TileState.objects.filter(status=TileState.QUEUED).exists())
		kept.refresh_from_db()
		self.assertEqual(kept.status, TileState.RENDERING)
		cleared = TileState.objects.get(kind=TileState.TILE, zoom=12, x=1, y=2)
		self.assertEqual(cleared.status, TileState.DONE)
		self.assertEqual(cleared.attempts, 0)
		self.assertEqual(cleared.error, "")

	def test_all_clears_the_rendering_jobs_too(self):
		TileState.objects.create(kind=TileState.TILE, zoom=10, x=5, y=6, status=TileState.RENDERING,
			claimed_by="host:1", claimed_at=timezone.now())
		out = StringIO()
		call_command("clear_queue", "--all", stdout=out)
		self.assertIn("Cleared 1", out.getvalue())
		self.assertFalse(TileState.objects.exclude(status=TileState.DONE).exists())
