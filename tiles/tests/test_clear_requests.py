from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from tiles.models import TileRequest


class ClearRequestsTests(TestCase):
	def test_clears_every_request(self):
		TileRequest.objects.create(zoom=8, x=1, y=2, status=TileRequest.WAITING)
		TileRequest.objects.create(zoom=8, x=1, y=3, status=TileRequest.CLAIMED, claimed_by="old:1")
		TileRequest.objects.create(zoom=8, x=1, y=4, cancelled=True)
		out = StringIO()
		call_command("clear_requests", stdout=out)
		self.assertFalse(TileRequest.objects.exists())
		self.assertIn("Cleared 3 requests", out.getvalue())
