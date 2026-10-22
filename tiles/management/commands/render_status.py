"""Reports on the render queue: what each renderer is working on, the
requests waiting for a renderer, the idle work queued, and what failed.

    manage.py render_status            one report
    manage.py render_status --watch 5  a report every 5 seconds
"""

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from tiles import queue
from tiles.models import TileRequest, TileState


def _name(job):
	return "{} {}/{}/{}".format(job.kind, job.zoom, job.x, job.y)


def _row(job, *after):
	"""A job and its markers, on one line, aligned."""
	return "{}  {}".format(_name(job).ljust(20), "  ".join(part for part in after if part)).rstrip()


class Command(BaseCommand):
	help = "Shows what the render workers are doing, and the state of the queue"

	def add_arguments(self, parser):
		parser.add_argument("--lines", type=int, default=10,
			help="jobs shown per renderer and section (default %(default)s)")
		parser.add_argument("--watch", type=float, default=0,
			help="refresh the report every N seconds (0: once)")

	def handle(self, *args, lines, watch, **options):
		while True:
			self.report(lines)
			if not watch:
				return
			time.sleep(watch)
			self.stdout.write("\x1b[2J\x1b[H", ending="")  # clear the screen

	def report(self, lines):
		now = timezone.now()
		self.stdout.write("pycromap render status, {}".format(now.strftime("%H:%M:%S")))

		# The renderers, and what each is working on
		by_worker = {}
		for job in TileState.objects.filter(status=TileState.RENDERING).order_by("-claimed_at"):
			by_worker.setdefault(job.claimed_by or "(unknown)", []).append(job)
		self.stdout.write("")
		self.stdout.write("renderers: {}".format(len(by_worker)))
		if not by_worker:
			self.stdout.write("  (none)")
		for worker, jobs in sorted(by_worker.items()):
			self.stdout.write("  {} ({} jobs)".format(worker, len(jobs)))
			for job in jobs[:lines]:
				stale = "(past the lease, will be requeued)" if now - job.claimed_at > timedelta(
					seconds=settings.RENDER_LEASE) else ""
				self.stdout.write("    " + _row(job, "claimed " + queue.span(job.claimed_at), stale))

		# Requested work, newest first, as the renderers take it
		waiting = TileRequest.objects.filter(status=TileRequest.WAITING, cancelled=False)
		self.stdout.write("")
		self.stdout.write("requests waiting: {} ({} claimed, {} cancelled)".format(waiting.count(),
			TileRequest.objects.filter(status=TileRequest.CLAIMED).count(),
			TileRequest.objects.filter(cancelled=True).count()))
		for request in waiting.order_by("-started_at")[:lines]:
			tries = " (tried {} times)".format(request.attempts) if request.attempts else ""
			self.stdout.write("  {:<24} {}{}".format("tile {}/{}/{}".format(request.zoom, request.x, request.y),
				queue.span(request.started_at), tries))

		# Idle work, in the order it will be taken
		ready = TileState.objects.filter(status=TileState.QUEUED, priority__lt=0)
		self.stdout.write("")
		self.stdout.write("idle work queued: {}".format(ready.count()))
		for job in ready.order_by("-priority", "queued_at")[:lines]:
			self.stdout.write("  " + _row(job, queue.span(job.queued_at)))

		# Failed
		failed = TileState.objects.filter(status=TileState.FAILED).order_by("-requested_at")
		self.stdout.write("")
		self.stdout.write("failed: {}".format(failed.count()))
		for job in failed[:lines]:
			self.stdout.write("  " + _row(job, "{} tries: {}".format(job.attempts, job.error[:120])))
