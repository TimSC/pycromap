"""Renders requested tiles, and the work queued for idle renderers, until
stopped (see tiles.queue).

The process claims jobs one at a time: a tile from map data (reading it,
building its summary if the zoom marks one, and running tilemaker-core), a
tile from summaries that are all built already, or a summary from finer
summaries that are. A job doesn't queue the summaries it needs, for now: a
request for a tile whose summaries aren't all built gets a placeholder
(tiles.views), and the idle renderers build the pyramid from the bottom. The work is CPU-bound Python, and threads
in one process share the interpreter's lock, so one process renders one job
at a time: run `RENDER_WORKERS` (8) of them to render more.
"""

import logging
import os
import signal
import socket
import threading  # for the stop event, nothing else
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections

from summaries import build as summaries
from tiles import idle, queue
from tiles.models import TileState
from tiles.render import read_tile_data, render_data

logger = logging.getLogger("tiles")


class RenderError(Exception):
	pass


class FinalError(RenderError):
	"""A failure that trying again won't mend"""


def _tiles(tiles):
	return ", ".join("{}/{}/{}".format(*tile) for tile in tiles[:5]) + (" ..." if len(tiles) > 5 else "")


def render_tile_job(job):
	"""Renders a tile. Returns the tile and its data version."""
	config = queue.zoom_config(job.zoom)
	if config is None:
		raise RenderError("zoom {} is not served".format(job.zoom))

	if config["source"] == "data":
		data, version = read_tile_data(job.zoom, job.x, job.y)
		# Its summary, while the map data is here: only for the zooms marked
		# "summary" in TILE_ZOOMS, the finest of which the pyramid is built from
		if config.get("summary"):
			summaries.build_from_data(job.zoom, job.x, job.y, data, version)
		return render_data(data, job.zoom, job.x, job.y), version

	# From summaries: those beneath it at the configured level, merged
	needed = summaries.tiles_beneath(job.zoom, job.x, job.y, config["level"])
	missing = summaries.missing(needed)
	if missing:
		raise FinalError("summaries it is made from aren't built: " + _tiles(missing))
	loaded = [summaries.load(*tile) for tile in needed]
	return render_data(summaries.merge(loaded), job.zoom, job.x, job.y), max(v for _, v in loaded)


def build_summary_job(job):
	"""Builds a summary. Returns its data version."""
	if job.zoom == summaries.base_level():
		data, version = read_tile_data(job.zoom, job.x, job.y)
		summaries.build_from_data(job.zoom, job.x, job.y, data, version)
		return version
	missing = summaries.build_from_finer(job.zoom, job.x, job.y)
	if missing:
		raise FinalError("finer summaries it is built from aren't built: " + _tiles(missing))
	return summaries.load(job.zoom, job.x, job.y)[1]


def render_one(process):
	"""Does the next queued job, if there is one. Returns whether there was.
	Jobs queued by idle renderers are done only when working ahead
	(RENDER_IDLE_WORK)."""
	job = queue.claim_next(process, idle=settings.RENDER_IDLE_WORK)
	if job is None:
		return False
	name = "{} {}/{}/{}".format(job.kind, job.zoom, job.x, job.y)
	started = time.monotonic()
	try:
		if job.kind == TileState.SUMMARY:
			version = build_summary_job(job)
			queue.finish_summary(job, version, time.monotonic() - started)
			logger.info("Built %s: version %s, %.2fs", name, version, time.monotonic() - started)
		else:
			tile, version = render_tile_job(job)
			cached = (queue.zoom_config(job.zoom) or {}).get("cache", False)
			queue.finish(job, tile, version, time.monotonic() - started, cached)
			logger.info("Rendered %s: %d bytes, version %s, %.2fs", name, len(tile), version, time.monotonic() - started)
	except Exception as e:
		logger.warning("%s failed: %s", name, e)
		queue.fail(job, e, final=isinstance(e, FinalError))
	return True


class Command(BaseCommand):
	help = "Renders queued tiles, highest priority first, until stopped"

	def add_arguments(self, parser):
		parser.add_argument("--poll", type=float, default=0.2,
			help="seconds between looks at an empty queue (default %(default)s)")
		parser.add_argument("--once", action="store_true",
			help="stop when the queue is empty, rather than waiting for more")

	def handle(self, *args, poll, once, **options):
		stop = threading.Event()
		for sig in (signal.SIGINT, signal.SIGTERM):
			signal.signal(sig, lambda *_: stop.set())

		name = "{}:{}".format(socket.gethostname(), os.getpid())
		queue.requeue_abandoned()
		idle_work = settings.RENDER_IDLE_WORK and not once
		self.stdout.write("Rendering as {}{}".format(name,
			", working ahead when idle" if idle_work else ""))
		forgotten = 0.0  # when cancelled requests were last cleared out
		try:
			while not stop.is_set():
				close_old_connections()
				# Requests whose clients went away are kept a while, and
				# those left unanswered too long are dropped; cleared out at
				# most once a minute, busy or not
				if time.monotonic() - forgotten > 60:
					queue.forget_requests()
					forgotten = time.monotonic()
				if render_one(name):
					# Top up the idle work while there is still some, so that
					# the fleet renders on while one of them plans
					if idle_work:
						idle.queue_idle_work(name)
					continue
				if once:
					break
				wait = poll
				queue.requeue_abandoned()
				if idle_work:
					idle.queue_idle_work(name)
					# Only when there is nothing at all to do does it rest
					# longer; while another plans, it looks again soon
					if idle.idle_queued() == 0 and not idle.planning_now():
						wait = max(poll, settings.IDLE_POLL)
				stop.wait(wait)
		finally:
			connections.close_all()
		self.stdout.write("Stopped")
