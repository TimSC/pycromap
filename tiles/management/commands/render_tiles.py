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
from tiles.render import RenderError, read_tile_data, render_data

logger = logging.getLogger("tiles")


class FinalError(RenderError):
	"""A failure that trying again won't mend"""


def _tiles(tiles):
	return ", ".join("{}/{}/{}".format(*tile) for tile in tiles[:5]) + (" ..." if len(tiles) > 5 else "")


# Seconds between looks at the summaries other renderers are building
DEPENDENCY_POLL = 1.0


def missing_tree(needed):
	"""Every summary missing under the summaries needed (level, x, y): those
	missing, the missing ones those are built from, and so on, down to the
	base level, built from the map data."""
	tree, frontier, seen = [], summaries.missing(needed), set()
	while frontier:
		frontier = [tile for tile in frontier if tile not in seen]
		seen.update(frontier)
		tree.extend(frontier)
		finer = []
		for level, x, y in frontier:
			beneath = summaries.finer_level(level)
			if beneath is not None:
				finer.extend(summaries.missing(summaries.tiles_beneath(level, x, y, beneath)))
		frontier = finer
	return tree


def build_dependencies(job, needed, worker, wait=DEPENDENCY_POLL):
	"""Builds every summary missing under those a claimed tile is made from
	(needed), so it can be rendered: the claiming renderer is responsible for
	them all (CASCADE_MIN_ZOOM). Again and again, of those missing and not
	underway, it takes one at the highest zoom (the finest, so what it is
	built from is there), adds it to TileState as a job, claims it, and builds
	it. Those other renderers are building, it waits for. It keeps its claim
	on the tile fresh as it goes, and stops (FinalError) if no request wants
	the tile any more, or a summary it needs can't be built."""
	while True:
		tree = missing_tree(needed)
		if not tree:
			return
		if not queue.still_wanted(job):
			raise FinalError("stopped: no request wants it any more; {} summaries it needs aren't built".format(len(tree)))
		# Ready: not underway, and with nothing it is built from missing
		missing, busy = set(tree), queue.underway(tree)

		def buildable(tile):
			level, x, y = tile
			finer = summaries.finer_level(level)
			return finer is None or not any(beneath in missing for beneath in summaries.tiles_beneath(level, x, y, finer))
		ready = [tile for tile in tree if tile not in busy and buildable(tile)]
		if not ready:
			# What is left waits for what other renderers are building
			queue.refresh_claim(job, worker)
			time.sleep(wait)
			continue
		level, x, y = max(ready, key=lambda tile: tile[0])
		dependency = queue.claim_dependency(level, x, y, worker, job.priority)
		if dependency is None:
			continue  # another renderer took it meanwhile
		started = time.monotonic()
		try:
			version = build_summary_job(dependency)
		except Exception as e:
			queue.fail(dependency, e, final=True)
			raise FinalError("summary {}/{}/{} it needs failed: {}".format(level, x, y, e))
		queue.finish_summary(dependency, version, time.monotonic() - started)
		logger.info("Built summary %d/%d/%d for %s %d/%d/%d: version %s, %.2fs", level, x, y, job.kind, job.zoom,
			job.x, job.y, version, time.monotonic() - started)
		queue.refresh_claim(job, worker)


def render_tile_job(job, worker=""):
	"""Renders a tile. Returns the tile and its data version. For a tile
	claimed for requests at or above CASCADE_MIN_ZOOM, made from summaries,
	the summaries it needs are built first (build_dependencies)."""
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
	if not job.idle and job.zoom >= settings.CASCADE_MIN_ZOOM:
		build_dependencies(job, needed, worker)
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
			tile, version = render_tile_job(job, process)
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
