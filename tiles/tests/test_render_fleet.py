import subprocess
import sys
import threading
import time

from django.test import TestCase

from tiles.management.commands.render_fleet import supervise


def dying(spawns):
	def spawn():
		child = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
		child.started = time.monotonic()
		spawns.append(child)
		return child
	return spawn


class SuperviseTests(TestCase):
	def test_a_dying_renderer_is_replaced_after_growing_delays(self):
		spawns = []
		stops = []
		stop = threading.Event()
		def log(line):
			stops.append((time.monotonic(), line))
		def stop_soon():
			time.sleep(4.5)
			stop.set()
		threading.Thread(target=stop_soon, daemon=True).start()
		supervise(1, dying(spawns), stop, log)
		# The first two deaths are immediate; the third waits ~4s
		self.assertGreaterEqual(len(spawns), 3)
		self.assertLess(len(spawns), 6)
		self.assertIn("restarting in 1s", stops[0][1])
		self.assertIn("restarting in 2s", stops[1][1])

	def test_a_renderer_that_lived_is_replaced_at_once(self):
		spawns = []
		logs = []
		stop = threading.Event()
		def spawn():
			child = subprocess.Popen([sys.executable, "-c", "pass"])
			child.started = time.monotonic() - 31  # an old process
			spawns.append(child)
			return child
		def stop_soon():
			time.sleep(1)
			stop.set()
		threading.Thread(target=stop_soon, daemon=True).start()
		supervise(1, spawn, stop, logs.append)
		self.assertGreaterEqual(len(spawns), 2)
		self.assertTrue(any("replacing it" in line for line in logs))
