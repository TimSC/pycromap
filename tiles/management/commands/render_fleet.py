"""Runs `RENDER_WORKERS` (8) render_tiles processes, restarting any that die.

Rendering is CPU-bound Python, and threads in one process share the
interpreter's lock, so the renderers run in separate processes: this one
supervises them, replacing a renderer that exits (waiting longer after each
quick death, so a broken job can't spin the fleet) and stopping them all on
SIGINT or SIGTERM.
"""

import signal
import subprocess
import sys
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand


def supervise(count, spawn, stop, log):
	"""Runs count processes, made by spawn(), restarting any that dies. A
	process that dies within 30 seconds of starting is replaced after a
	growing delay (1s, 2s, ..., 30s); one that lived longer, at once. Returns
	the current processes when stop is set."""
	children = [spawn() for _ in range(count)]
	delays = [0.0] * count
	while not stop.is_set():
		for i, child in enumerate(children):
			code = child.poll()
			if code is None:
				continue
			young = time.monotonic() - child.started < 30
			delays[i] = min(delays[i] * 2 or 1, 30) if young else 0.0
			if delays[i]:
				log("renderer {} exited (code {}) after {:.0f}s; restarting in {:.0f}s".format(
					child.pid, code, time.monotonic() - child.started, delays[i]))
				if stop.wait(delays[i]):
					return children
			else:
				log("renderer {} exited (code {}); replacing it".format(child.pid, code))
			children[i] = spawn()
		stop.wait(0.2)
	return children


class Command(BaseCommand):
	help = "Runs RENDER_WORKERS render_tiles processes, restarting any that die"

	def add_arguments(self, parser):
		parser.add_argument("--workers", type=int, default=settings.RENDER_WORKERS,
			help="how many render_tiles processes to run (default %(default)s)")

	def spawn(self):
		child = subprocess.Popen([sys.executable, "manage.py", "render_tiles"])
		child.started = time.monotonic()
		return child

	def handle(self, *args, workers, **options):
		stop = threading.Event()
		for sig in (signal.SIGINT, signal.SIGTERM):
			signal.signal(sig, lambda *_: stop.set())

		self.stdout.write("Running {} renderers".format(workers))
		try:
			children = supervise(workers, self.spawn, stop, self.stdout.write)
		finally:
			for child in children:
				if child.poll() is None:
					child.terminate()
			for child in children:
				child.wait()
		self.stdout.write("Stopped")
