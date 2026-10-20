"""Renders queued tiles and builds queued summaries, highest priority first,
until stopped.

Each worker thread claims a job: a tile from map data (reading it, building
its summary at the base level, and running tilemaker-core), a tile from
summaries, or a summary. The work happens in tilemaker and the databases, so
threads are enough to render several tiles at once; run more processes to
render more.
"""

import logging
import os
import signal
import socket
import threading
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


def wait_for_summaries(job, missing):
	"""Queues jobs for the summaries a job needs, one deeper than it and ahead
	of it, and puts it back to wait. The job fails for good if they would be
	deeper than SUMMARY_MAX_DEPTH (it makes no jobs then), if one of them has
	failed, or if it has waited too long."""
	depth = job.depth + 1
	if depth > settings.SUMMARY_MAX_DEPTH:
		raise FinalError("summaries it needs aren't built, and jobs to build them would be deeper "
			"than SUMMARY_MAX_DEPTH ({}): {}".format(settings.SUMMARY_MAX_DEPTH, _tiles(missing)))
	failed = queue.queue_summaries(missing, job.priority + 1, depth)
	if failed:
		raise FinalError("summaries it needs failed: " + _tiles(failed))
	try:
		queue.defer(job)
	except queue.WaitedTooLong as e:
		raise FinalError("{}, still missing {}".format(e, _tiles(missing)))
	return None


def render_tile_job(job):
	"""Renders a tile. Returns the tile and its data version, or None if it is
	waiting for summaries."""
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
		return wait_for_summaries(job, missing)
	loaded = [summaries.load(*tile) for tile in needed]
	return render_data(summaries.merge(loaded), job.zoom, job.x, job.y), max(v for _, v in loaded)


def build_summary_job(job):
	"""Builds a summary. Returns its data version, or None if it is waiting for
	the finer summaries it is built from."""
	if job.zoom == summaries.base_level():
		data, version = read_tile_data(job.zoom, job.x, job.y)
		summaries.build_from_data(job.zoom, job.x, job.y, data, version)
		return version
	missing = summaries.build_from_finer(job.zoom, job.x, job.y)
	if missing:
		return wait_for_summaries(job, missing)
	return summaries.load(job.zoom, job.x, job.y)[1]


def render_one(worker):
	"""Does the next queued job, if there is one. Returns whether there was.
	Jobs queued by idle renderers are done only when working ahead
	(RENDER_IDLE_WORK)."""
	job = queue.claim_next(worker, idle=settings.RENDER_IDLE_WORK)
	if job is None:
		return False
	name = "{} {}/{}/{}".format(job.kind, job.zoom, job.x, job.y)
	started = time.monotonic()
	try:
		if job.kind == TileState.SUMMARY:
			version = build_summary_job(job)
			if version is not None:
				queue.finish_summary(job, version, time.monotonic() - started)
				logger.info("Built %s: version %s, %.2fs", name, version, time.monotonic() - started)
		else:
			result = render_tile_job(job)
			if result is not None:
				tile, version = result
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
		parser.add_argument("--workers", type=int, default=settings.RENDER_WORKERS,
			help="tiles rendered at once (default %(default)s)")
		parser.add_argument("--poll", type=float, default=0.2,
			help="seconds between looks at an empty queue (default %(default)s)")
		parser.add_argument("--once", action="store_true",
			help="stop when the queue is empty, rather than waiting for more")

	def handle(self, *args, workers, poll, once, **options):
		stop = threading.Event()
		for sig in (signal.SIGINT, signal.SIGTERM):
			signal.signal(sig, lambda *_: stop.set())

		name = "{}:{}".format(socket.gethostname(), os.getpid())
		queue.requeue_abandoned()

		idle_work = settings.RENDER_IDLE_WORK and not once

		def work(number):
			worker = "{}:{}".format(name, number)
			try:
				while not stop.is_set():
					close_old_connections()
					if render_one(worker):
						continue
					if once:
						return
					wait = poll
					if number == 0:
						queue.requeue_abandoned()
						# One worker plans idle work, for them all
						if idle_work and idle.queue_idle_work(workers) == 0:
							wait = max(poll, settings.IDLE_POLL)
					stop.wait(wait)
			finally:
				connections.close_all()

		threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(workers)]
		for thread in threads:
			thread.start()
		self.stdout.write("Rendering with {} workers{}".format(workers,
			", working ahead when idle" if idle_work else ""))
		while any(t.is_alive() for t in threads):
			for thread in threads:
				thread.join(0.5)
		self.stdout.write("Stopped")
