import json
import os
import tempfile
import unittest

import pyo5m
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from summaries.build import RING_ID_BASE
from tiles.render import TILEMAKER_RING_IDS_FROM, _tilemaker_ids, run_tilemaker
from tiles.tilegeo import tile_area, tile_bounds


class TilemakerIdTests(SimpleTestCase):
	def test_rings_are_numbered_afresh_below_tilemakers_limit(self):
		data = pyo5m.OsmData()
		real = 2000000000005  # a way made since pycrocosm
		data.ways = [pyo5m.Way(7, [1, 2], {}), pyo5m.Way(real, [2, 3], {}), pyo5m.Way(RING_ID_BASE + 9, [1, 2, 3, 1], {}),
			pyo5m.Way(RING_ID_BASE + 42, [3, 4, 5, 3], {})]
		data.relations = [pyo5m.Relation(20, [pyo5m.Member("way", RING_ID_BASE + 42, "outer"),
			pyo5m.Member("way", real, "outer")], {"type": "multipolygon"})]
		out = _tilemaker_ids(data)  # FOSM's ranges, by default
		start = TILEMAKER_RING_IDS_FROM["fosm"]
		self.assertEqual([w.id for w in out.ways], [7, real, start, start + 1])
		self.assertEqual([m.ref for m in out.relations[0].members], [start + 1, real])
		for ranges, start in TILEMAKER_RING_IDS_FROM.items():
			self.assertLess(start + 10 ** 9, 1 << 42)  # room below tilemaker's limit
		with override_settings(MAP_ID_RANGES="osm"):
			self.assertEqual(_tilemaker_ids(data).ways[2].id, TILEMAKER_RING_IDS_FROM["osm"])
		self.assertEqual(data.ways[2].id, RING_ID_BASE + 9)  # left as it was
		# Turned off, the data itself, rings and all
		with override_settings(TILEMAKER={**settings.TILEMAKER, "renumber_rings": False}):
			self.assertIs(_tilemaker_ids(data), data)
		# Without rings, the data itself
		data.ways, data.relations = data.ways[:2], []
		self.assertIs(_tilemaker_ids(data), data)


class TileGeoTests(SimpleTestCase):
	def test_bounds(self):
		west, south, east, north = tile_bounds(0, 0, 0)
		self.assertAlmostEqual(west, -180)
		self.assertAlmostEqual(east, 180)
		self.assertAlmostEqual(north, 85.0511287798, places=6)
		self.assertAlmostEqual(south, -85.0511287798, places=6)

	def test_area_has_a_margin(self):
		west, south, east, north = tile_bounds(12, 2035, 1374)
		a_west, a_south, a_east, a_north = tile_area(12, 2035, 1374)
		self.assertAlmostEqual(west - a_west, (east - west) / 8)
		self.assertAlmostEqual(a_north - north, (north - south) / 8)


@unittest.skipUnless(os.path.exists(settings.TILEMAKER["program"]), "tilemaker-core is not built")
class TilemakerTests(SimpleTestCase):
	"""pyo5m's output, through tilemaker-core"""

	def render(self, format):
		west, south, east, north = tile_bounds(12, 2035, 1374)
		lat, lon = (south + north) / 2, (west + east) / 2
		data = pyo5m.OsmData()
		data.nodes = [pyo5m.Node(1, lat, lon - 0.01), pyo5m.Node(2, lat, lon + 0.01),
			pyo5m.Node(3, lat + 0.001, lon, {"amenity": "pub", "name": "The Test"})]
		data.ways = [pyo5m.Way(10, [1, 2], {"highway": "residential", "name": "Test Street"})]
		with tempfile.TemporaryDirectory() as work:
			path = os.path.join(work, "area." + format)
			pyo5m.save(data, path)
			return run_tilemaker(path, 12, 2035, 1374, os.path.join(work, "tiles"))

	def test_o5m(self):
		self.assertGreater(len(self.render("o5m")), 0)

	def test_pbf(self):
		self.assertGreater(len(self.render("pbf")), 0)

	def test_empty_area(self):
		# Without the config's shapefile layers, which would draw the sea here
		with open(settings.TILEMAKER["config"]) as f:
			config = json.load(f)
		for layer in config["layers"].values():
			layer.pop("source", None)
		with tempfile.TemporaryDirectory() as work:
			config_path = os.path.join(work, "config.json")
			with open(config_path, "w") as f:
				json.dump(config, f)
			path = os.path.join(work, "area.o5m")
			pyo5m.save(pyo5m.OsmData(), path)
			with override_settings(TILEMAKER={**settings.TILEMAKER, "config": config_path}):
				self.assertEqual(run_tilemaker(path, 12, 2035, 1374, os.path.join(work, "tiles")), b"")
