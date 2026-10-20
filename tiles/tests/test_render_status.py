from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from tiles.models import TileState


class RenderStatusTests(TestCase):
	def test_shows_the_workers_and_the_queue(self):
		TileState.objects.create(kind=TileState.TILE, zoom=12, x=1, y=2, status=TileState.RENDERING,
			claimed_by="host:1:0", claimed_at=timezone.now())
		TileState.objects.create(kind=TileState.SUMMARY, zoom=12, x=3, y=4, status=TileState.QUEUED,
			depth=1, priority=-1.0, queued_at=timezone.now())
		TileState.objects.create(kind=TileState.TILE, zoom=10, x=5, y=6, status=TileState.FAILED,
			attempts=3, error="boom", requested_at=timezone.now())
		out = StringIO()
		call_command("render_status", stdout=out)
		text = out.getvalue()
		self.assertIn("renderers: 1", text)
		self.assertIn("host:1:0", text)
		self.assertIn("tile 12/1/2", text)
		self.assertIn("queued: 1 (1 idle)", text)
		self.assertIn("summary 12/3/4", text)
		self.assertIn("failed: 1", text)
		self.assertIn("3 tries: boom", text)
