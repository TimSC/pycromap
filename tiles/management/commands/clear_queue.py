"""Clears the render queue: requests waiting for a renderer are answered as
failed, and queued jobs are deleted, without renderings. The tiles
come back organically, re-rendered when requested. The renderers queue their
idle work ahead again as the queue empties.

    manage.py clear_queue        queued jobs only
    manage.py clear_queue --all  also the jobs renderers are mid-rendering on,
                                 whose results are abandoned
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from tiles.models import Job, TileRequest


class Command(BaseCommand):
	help = "Clears the render queue: waiting requests are failed, queued jobs deleted"

	def add_arguments(self, parser):
		parser.add_argument("--all", action="store_true",
			help="also clear jobs renderers are mid-rendering on (their results are abandoned)")

	def handle(self, *args, all, **options):
		statuses = [Job.QUEUED] + ([Job.RENDERING] if all else [])
		requests = [TileRequest.WAITING] + ([TileRequest.CLAIMED] if all else [])
		with transaction.atomic():
			answered = TileRequest.objects.filter(status__in=requests).update(status=TileRequest.FAILED)
			cleared = Job.objects.filter(status__in=statuses).delete()[0]
		self.stdout.write("Cleared {} requests and {} queued jobs".format(answered, cleared))
