"""Clears out the requests table (TileRequest), as the web server starts.

A request is only kept while the web server is answering it, so any left when
the web server starts went with the one that stopped: their clients ask
again. Run before the web server takes any request (as docker-compose.yml
does): run while it is answering, the requests it is waiting on would be
answered as not ready. Renderers working on tiles for them stop once no
request wants their tiles.

    manage.py clear_requests
"""

from django.core.management.base import BaseCommand

from tiles.models import TileRequest


class Command(BaseCommand):
	help = "Deletes every tile request: run as the web server starts, before it takes any"

	def handle(self, *args, **options):
		deleted, _ = TileRequest.objects.all().delete()
		self.stdout.write("Cleared {} requests".format(deleted))
