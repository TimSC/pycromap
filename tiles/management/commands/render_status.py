"""Reports on the render queue: what each renderer is working on, what is
queued, waiting for summaries, and failed.

    manage.py render_status            one report
    manage.py render_status --watch 5  a report every 5 seconds
"""

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from tiles import queue
from tiles.models import TileState


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

		# The queue, in the order the jobs will be taken
		ready = TileState.objects.filter(Q(status=TileState.QUEUED) &
			(Q(not_before__isnull=True) | Q(not_before__lte=now)))
		self.stdout.write("")
		self.stdout.write("queued: {} ({} idle)".format(ready.count(), ready.filter(priority__lt=0).count()))
		for job in ready.order_by("-priority", "queued_at")[:lines]:
			markers = []
			if job.depth:
				markers.append("depth {}".format(job.depth))
			if job.priority < 0:
				markers.append("idle")
			self.stdout.write("  " + _row(job, queue.span(job.queued_at), *markers))

		# Waiting for the summaries they need
		waiting = TileState.objects.filter(status=TileState.QUEUED, not_before__gt=now).order_by("not_before")
		self.stdout.write("")
		self.stdout.write("waiting for summaries: {}".format(waiting.count()))
		for job in waiting[:lines]:
			self.stdout.write("  " + _row(job, "since " + queue.span(job.waiting_since),
				"again " + queue.span(job.not_before)))

		# Failed
		failed = TileState.objects.filter(status=TileState.FAILED).order_by("-requested_at")
		self.stdout.write("")
		self.stdout.write("failed: {}".format(failed.count()))
		for job in failed[:lines]:
			self.stdout.write("  " + _row(job, "{} tries: {}".format(job.attempts, job.error[:120])))
