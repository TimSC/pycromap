"""Renders queued tiles, highest priority first, until stopped.

Each worker thread claims a tile, reads its map data, runs tilemaker-core and
records the result. The work happens in tilemaker and the databases, so
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

from tiles import queue
from tiles.render import render_tile

logger = logging.getLogger("tiles")


def render_one(worker):
	"""Renders the next queued tile, if there is one. Returns whether there was."""
	tile = queue.claim_next(worker)
	if tile is None:
		return False
	started = time.monotonic()
	try:
		data, version = render_tile(tile.zoom, tile.x, tile.y)
	except Exception as e:
		logger.warning("Rendering %s/%s/%s failed: %s", tile.zoom, tile.x, tile.y, e)
		queue.fail(tile, e)
	else:
		queue.finish(tile, data, version, time.monotonic() - started)
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

		def work(number):
			worker = "{}:{}".format(name, number)
			try:
				while not stop.is_set():
					close_old_connections()
					if not render_one(worker):
						if once:
							return
						stop.wait(poll)
						if number == 0:
							queue.requeue_abandoned()
			finally:
				connections.close_all()

		threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(workers)]
		for thread in threads:
			thread.start()
		self.stdout.write("Rendering with {} workers".format(workers))
		while any(t.is_alive() for t in threads):
			for thread in threads:
				thread.join(0.5)
		self.stdout.write("Stopped")
