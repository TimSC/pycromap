import tempfile
from pathlib import Path

import pyo5m
from django.conf import settings
from django.test import TestCase, override_settings

from summaries import build
from summaries.lua import summary_filter
from summaries.models import TileSummary


def osm(nodes=(), ways=(), relations=()):
	data = pyo5m.OsmData()
	data.nodes, data.ways, data.relations = list(nodes), list(ways), list(relations)
	return data


class FilterTests(TestCase):
	def test_roads_by_level(self):
		keep = summary_filter()
		self.assertIsNone(keep("way", {"highway": "residential"}, 12, False))
		self.assertIsNotNone(keep("way", {"highway": "tertiary"}, 12, False))
		self.assertIsNone(keep("way", {"highway": "tertiary"}, 10, False))
		self.assertIsNotNone(keep("way", {"highway": "secondary"}, 10, False))
		self.assertIsNone(keep("way", {"highway": "secondary"}, 8, False))
		self.assertIsNotNone(keep("way", {"highway": "motorway"}, 6, False))

	def test_tags_are_trimmed(self):
		tags = summary_filter()("way", {"highway": "primary", "name": "High Street", "surveyed": "2019"}, 12, False)
		self.assertEqual(tags, {"highway": "primary", "name": "High Street"})

	def test_places_and_areas(self):
		keep = summary_filter()
		self.assertIsNotNone(keep("node", {"place": "town", "name": "Fareham"}, 8))
		self.assertIsNone(keep("node", {"amenity": "pub"}, 12))
		self.assertIsNotNone(keep("way", {"natural": "water"}, 10, True))
		self.assertIsNone(keep("way", {"natural": "water"}, 10, False))
		self.assertIsNotNone(keep("relation", {"type": "multipolygon", "landuse": "forest"}, 6))


class SimplifyTests(TestCase):
	def test_straight_line_keeps_its_ends(self):
		coords = {i: (i * 0.001, 0.0) for i in range(1, 6)}
		self.assertEqual(build.simplify_refs([1, 2, 3, 4, 5], coords, 0.0001), [1, 5])

	def test_corners_are_kept(self):
		coords = {1: (0, 0), 2: (0.5, 0.0), 3: (1, 0), 4: (1, 1)}
		self.assertEqual(build.simplify_refs([1, 2, 3, 4], coords, 0.0001), [1, 3, 4])

	def test_ring_stays_closed(self):
		coords = {1: (0, 0), 2: (1, 0), 3: (1, 1), 4: (0, 1)}
		refs = build.simplify_refs([1, 2, 3, 4, 1], coords, 0.0001)
		self.assertEqual(refs[0], refs[-1])
		self.assertEqual(len(refs), 5)


class SummarizeTests(TestCase):
	def test_keeps_what_the_filter_picks_and_what_it_needs(self):
		data = osm(
			nodes=[pyo5m.Node(1, 50.0, -1.0), pyo5m.Node(2, 50.0, -0.9), pyo5m.Node(3, 50.1, -0.9),
				pyo5m.Node(4, 50.05, -0.95, {"amenity": "pub"}), pyo5m.Node(5, 50.05, -0.96, {"place": "town", "name": "T"})],
			ways=[pyo5m.Way(10, [1, 2], {"highway": "primary"}), pyo5m.Way(11, [2, 3], {"highway": "footway"}),
				pyo5m.Way(12, [1, 2, 3, 1], {})],
			relations=[pyo5m.Relation(20, [pyo5m.Member("way", 12, "outer")], {"type": "multipolygon", "natural": "water"})])
		out = build.summarize(data, 12)
		self.assertEqual([w.id for w in out.ways], [10, 12])
		self.assertEqual(out.ways[1].tags, {})  # in the relation for its geometry only
		self.assertEqual([r.id for r in out.relations], [20])
		self.assertEqual([n.id for n in out.nodes], [1, 2, 3, 5])

	def test_encodes_and_decodes(self):
		data = osm(nodes=[pyo5m.Node(5, 50.05, -0.96, {"place": "town"})])
		blob = build.encode(data)
		self.assertEqual(blob[:2], b"\x1f\x8b")  # gzipped
		self.assertEqual(build.decode(blob).nodes[0].tags, {"place": "town"})
		self.assertEqual(build.decode(blob, "o5m.gz").nodes[0].id, 5)


class MergeTests(TestCase):
	def test_newest_copy_wins(self):
		old = osm(nodes=[pyo5m.Node(1, 50.0, -1.0, {"name": "old"}), pyo5m.Node(2, 51.0, -1.0)])
		new = osm(nodes=[pyo5m.Node(1, 50.0, -1.0, {"name": "new"}), pyo5m.Node(3, 52.0, -1.0)])
		merged = build.merge([(old, 5), (new, 7)])
		self.assertEqual([n.id for n in merged.nodes], [1, 2, 3])
		self.assertEqual(merged.nodes[0].tags, {"name": "new"})


class PyramidTests(TestCase):
	def test_tiles_beneath(self):
		self.assertEqual(build.tiles_beneath(11, 3, 4, 12), [(12, 6, 8), (12, 6, 9), (12, 7, 8), (12, 7, 9)])
		self.assertEqual(len(build.tiles_beneath(10, 0, 0, 12)), 16)
		self.assertEqual(build.finer_level(10), 12)
		self.assertIsNone(build.finer_level(12))

	def test_old_recipe_counts_as_missing(self):
		build.store(12, 1, 2, osm(), 5)
		self.assertEqual(build.missing([(12, 1, 2), (12, 1, 3)]), [(12, 1, 3)])
		with override_settings(SUMMARY_SIMPLIFY_PIXELS=settings.SUMMARY_SIMPLIFY_PIXELS * 2):
			self.assertEqual(build.missing([(12, 1, 2)]), [(12, 1, 2)])
			self.assertIsNone(build.load(12, 1, 2))

	def test_changing_the_lua_changes_the_recipe(self):
		with tempfile.TemporaryDirectory() as work:
			lua = Path(work) / "summary.lua"
			lua.write_bytes(Path(settings.SUMMARY_LUA).read_bytes())
			with override_settings(SUMMARY_LUA=lua):
				before = build.recipe()
				lua.write_text(lua.read_text() + "\n-- changed\n")
				self.assertNotEqual(build.recipe(), before)

	def test_builds_from_the_finer_level(self):
		road = osm(nodes=[pyo5m.Node(1, 50.0, -1.0), pyo5m.Node(2, 50.0, -0.9)],
			ways=[pyo5m.Way(10, [1, 2], {"highway": "primary"})])
		beneath = build.tiles_beneath(10, 0, 0, 12)
		for level, x, y in beneath[:-1]:
			build.store(level, x, y, road, 3)
		self.assertEqual(build.build_from_finer(10, 0, 0), [beneath[-1]])
		build.store(*beneath[-1], road, 4)
		self.assertEqual(build.build_from_finer(10, 0, 0), [])
		summary = TileSummary.objects.get(zoom=10)
		self.assertEqual((summary.version, summary.ways), (4, 1))
