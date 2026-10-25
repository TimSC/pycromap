import tempfile
from pathlib import Path

import pyo5m
from shapely.geometry import LineString, Polygon
from django.conf import settings
from django.test import TestCase, override_settings

from summaries import build
from summaries.lua import summary_filter
from summaries.models import TileSummary
from tiles.tilegeo import tile_bounds


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

	def test_every_summary_level_has_rules(self):
		# A level in TILE_ZOOMS without entries in summary.lua's tables would
		# fail on every summary built at it
		keep = summary_filter()
		for level in build.levels():
			keep("way", {"highway": "motorway"}, level, False)
			keep("way", {"waterway": "river"}, level, False)
			keep("way", {"boundary": "administrative", "admin_level": "2"}, level, False)
			keep("node", {"place": "country", "name": "X"}, level)
			keep("relation", {"type": "boundary", "boundary": "administrative", "admin_level": "2"}, level)

	def test_coastlines_are_not_kept(self):
		# The sea is drawn from the coastline shapefile, so OSM's coastline
		# ways, open or closed, are left out
		keep = summary_filter()
		self.assertIsNone(keep("way", {"natural": "coastline"}, 12, False))
		self.assertIsNone(keep("way", {"natural": "coastline"}, 12, True))


class SimplifyTests(TestCase):
	def test_straight_line_keeps_its_ends(self):
		coords = {i: (i * 0.001, 0.0) for i in range(1, 6)}
		self.assertEqual(build.simplify_refs([1, 2, 3, 4, 5], coords, 0.0001), [1, 5])

	def test_corners_are_kept(self):
		coords = {1: (0, 0), 2: (0.5, 0.0), 3: (1, 0), 4: (1, 1)}
		self.assertEqual(build.simplify_refs([1, 2, 3, 4], coords, 0.0001), [1, 3, 4])

	def test_ring_stays_valid(self):
		# A square whose bottom edge dips, with a narrow notch from the top
		# reaching into the dip: simplified as a plain line, the dip goes and
		# the notch's tip pokes through the bottom edge
		points = [(0, 0), (4, 0), (5, -0.4), (6, 0), (10, 0), (10, 10), (5.2, 10), (5.05, -0.2),
			(4.95, -0.2), (4.8, 10), (0, 10), (0, 0)]
		coords = {i: p for i, p in enumerate(points[:-1])}
		refs = list(range(len(points) - 1)) + [0]
		self.assertTrue(Polygon(points).is_valid)
		self.assertFalse(Polygon(LineString(points).simplify(0.5, preserve_topology=False).coords).is_valid)
		kept = build.simplify_refs(refs, coords, 0.5)
		self.assertEqual((kept[0], kept[-1]), (0, 0))
		self.assertTrue(Polygon([coords[r] for r in kept]).is_valid)

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

	def test_lines_that_never_reach_the_tile_are_left_out(self):
		west, south, east, north = tile_bounds(12, 2030, 1360)
		size = east - west
		mid_lat = (south + north) / 2
		near_lon, far_lon = west + size / 2, east + 3 * size  # in the tile; three tiles east
		data = osm(
			nodes=[pyo5m.Node(1, mid_lat, west - size), pyo5m.Node(2, mid_lat, east + size),  # crosses the tile
				pyo5m.Node(3, mid_lat, far_lon), pyo5m.Node(4, mid_lat + size, far_lon),  # nowhere near
				pyo5m.Node(5, mid_lat + size, far_lon + size), pyo5m.Node(6, mid_lat, far_lon + size),
				pyo5m.Node(7, mid_lat - size, near_lon)],
			ways=[pyo5m.Way(10, [1, 2], {"boundary": "administrative", "admin_level": "4"}),
				pyo5m.Way(11, [3, 4], {"boundary": "administrative", "admin_level": "4"}),
				pyo5m.Way(12, [3, 4, 5, 6, 3], {"natural": "water"}),  # an area: kept
				pyo5m.Way(13, [4, 5], {}), pyo5m.Way(14, [5, 6, 3], {})],
			relations=[
				# A boundary drawn from its ways: only the one reaching the tile
				pyo5m.Relation(20, [pyo5m.Member("way", 10, "outer"), pyo5m.Member("way", 11, "outer")],
					{"type": "boundary", "boundary": "administrative", "admin_level": "4"}),
				# A multipolygon's members are kept, as it may cover the tile
				pyo5m.Relation(21, [pyo5m.Member("way", 13, "outer"), pyo5m.Member("way", 14, "outer")],
					{"type": "multipolygon", "natural": "wood"})])
		out = build.summarize(data, 12, (2030, 1360))
		self.assertEqual([w.id for w in out.ways], [10, 12, 13, 14])
		self.assertEqual([m.ref for m in next(r for r in out.relations if r.id == 20).members], [10])
		# Without a tile, nothing is left out
		self.assertEqual([w.id for w in build.summarize(data, 12).ways], [10, 11, 12, 13, 14])

	def ring_data(self, size=0.01, split=True, extra_ways=(), extra_relations=(), lat=50.0, lon=-1.0):
		"""A wood multipolygon whose outer ring is a square of size degrees,
		joined from three member ways (or one closed way)."""
		nodes = [pyo5m.Node(1, lat, lon), pyo5m.Node(2, lat, lon + size), pyo5m.Node(3, lat + size, lon + size),
			pyo5m.Node(4, lat + size, lon)]
		if split:
			ways = [pyo5m.Way(10, [1, 2], {}), pyo5m.Way(11, [3, 2], {}), pyo5m.Way(12, [3, 4, 1], {})]
		else:
			ways = [pyo5m.Way(10, [1, 2, 3, 4, 1], {})]
		wood = pyo5m.Relation(20, [pyo5m.Member("way", w.id, "outer") for w in ways],
			{"type": "multipolygon", "natural": "wood"})
		return osm(nodes=nodes, ways=ways + list(extra_ways), relations=[wood] + list(extra_relations))

	def test_a_multipolygon_is_kept_as_its_rings(self):
		out = build.summarize(self.ring_data(), 12)
		ring = build._ring_id(20, 10)
		self.assertGreaterEqual(ring, build.RING_ID_BASE)
		self.assertEqual([w.id for w in out.ways], [ring])  # the three member ways joined
		self.assertEqual(out.ways[0].refs[0], out.ways[0].refs[-1])
		self.assertEqual([(m.ref, m.role) for m in out.relations[0].members], [(ring, "outer")])
		# The same ID in another summary of the same relation, so merging keeps one
		again = build.summarize(self.ring_data(), 12)
		self.assertEqual(len(build.merge([(out, 1), (again, 1)]).ways), 1)

	def test_a_closed_member_way_keeps_its_id(self):
		out = build.summarize(self.ring_data(split=False), 12)
		self.assertEqual([w.id for w in out.ways], [10])

	def test_rings_too_small_to_see_are_left_out(self):
		# Under MIN_RING_PIXELS at level 6: the relation goes with it
		small = build.MIN_RING_PIXELS * 360.0 / (1 << 6) / 4096 / 2
		out = build.summarize(self.ring_data(size=small), 6)
		self.assertEqual((out.ways, out.relations), ([], []))
		self.assertEqual(len(build.summarize(self.ring_data(size=small * 4), 6).relations), 1)

	def test_rings_not_reaching_the_tile_are_left_out(self):
		west, south, east, north = tile_bounds(12, 2030, 1360)
		inside = self.ring_data(size=(east - west) / 4, lat=south + (north - south) / 4, lon=west + (east - west) / 4)
		far = self.ring_data(size=(east - west) / 4, lat=south, lon=east + 3 * (east - west))
		self.assertEqual(len(build.summarize(inside, 12, (2030, 1360)).relations), 1)
		self.assertEqual(build.summarize(far, 12, (2030, 1360)).relations, [])

	def test_a_chain_that_cant_be_closed_keeps_its_ways(self):
		data = self.ring_data()
		data.ways = [w for w in data.ways if w.id != 12]  # a member missing
		data.ways.append(pyo5m.Way(12, [3, 4], {}))  # ends short of the start
		out = build.summarize(data, 12)
		self.assertEqual(sorted(w.id for w in out.ways), [10, 11, 12])

	def test_a_way_another_relation_needs_is_kept(self):
		border = pyo5m.Relation(30, [pyo5m.Member("way", 10, "outer")],
			{"type": "boundary", "boundary": "administrative", "admin_level": "2"})
		out = build.summarize(self.ring_data(extra_relations=[border]), 12)
		self.assertIn(10, [w.id for w in out.ways])
		self.assertEqual(len(out.relations), 2)

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


class MergeObjectsTests(TestCase):
	# Tile (1, 0, 0) covers lon [-180, 0], lat [0, ~85]
	def _data(self):
		return osm(
			nodes=[
				pyo5m.Node(1, 10, -90), pyo5m.Node(2, 20, -90), pyo5m.Node(3, 30, -90),
				pyo5m.Node(4, 40, -90), pyo5m.Node(5, 50, -90), pyo5m.Node(6, 60, -90),
				pyo5m.Node(7, -10, -90),   # lat < 0: outside the tile
				pyo5m.Node(8, 10, -80), pyo5m.Node(9, 20, -80), pyo5m.Node(10, 30, -80),
				pyo5m.Node(11, 10, -70), pyo5m.Node(12, 20, -70), pyo5m.Node(13, 30, -70),
			],
			ways=[
				pyo5m.Way(100, [1, 2, 3], {"highway": "primary"}),
				pyo5m.Way(101, [3, 4], {"highway": "primary"}),
				pyo5m.Way(102, [5, 6], {"highway": "primary"}),
				pyo5m.Way(103, [2, 7], {"highway": "primary"}),  # boundary: node 7 outside
				pyo5m.Way(104, [8, 9, 10, 8], {"natural": "water"}),  # closed
				pyo5m.Way(105, [11, 12], {"landuse": "forest"}),
				pyo5m.Way(106, [12, 13], {"landuse": "grass"}),
			],
			relations=[
				pyo5m.Relation(200, [pyo5m.Member("way", 105, "outer"), pyo5m.Member("way", 106, "outer")],
					{"type": "multipolygon", "landuse": "forest"}),
				pyo5m.Relation(201, [pyo5m.Member("way", 104, "outer")],
					{"type": "multipolygon", "natural": "water"}),
			])

	def test_connected_ways_with_same_tags_are_concatenated(self):
		out = build.merge_objects(self._data(), 1, 0, 0)
		by_id = {w.id: w for w in out.ways}
		self.assertEqual(by_id[100].refs, [1, 2, 3, 4])  # 100 + 101 merged
		self.assertNotIn(101, by_id)                        # absorbed
		self.assertEqual(by_id[102].refs, [5, 6])            # not connected, kept

	def test_ways_with_different_tags_are_not_merged(self):
		out = build.merge_objects(self._data(), 1, 0, 0)
		by_id = {w.id: w for w in out.ways}
		self.assertEqual(by_id[105].refs, [11, 12])
		self.assertEqual(by_id[106].refs, [12, 13])

	def test_boundary_ways_are_not_merged(self):
		out = build.merge_objects(self._data(), 1, 0, 0)
		by_id = {w.id: w for w in out.ways}
		self.assertEqual(by_id[103].refs, [2, 7])  # kept as-is

	def test_chains_grow_at_the_start_without_jumps(self):
		# The chain starts from way 10 (2-3), then grows before its start:
		# way 11 starts at 2 and must be reversed, way 12 ends at 1 and must
		# not be. Every step of the chain is a step of one of the ways.
		tags = {"highway": "primary"}
		data = osm(nodes=[pyo5m.Node(i, 0.1 + i * 0.01, 0.1) for i in range(5)],
			ways=[pyo5m.Way(10, [2, 3], tags), pyo5m.Way(11, [2, 1], tags), pyo5m.Way(12, [0, 1], tags),
				pyo5m.Way(13, [3, 4], tags)])
		out = build.merge_objects(data, 1, 1, 0)
		self.assertEqual([w.refs for w in out.ways], [[0, 1, 2, 3, 4]])

	def test_closed_ways_are_not_concatenated(self):
		out = build.merge_objects(self._data(), 1, 0, 0)
		by_id = {w.id: w for w in out.ways}
		self.assertEqual(by_id[104].refs, [8, 9, 10, 8])

	def test_multipolygons_with_same_tags_are_combined(self):
		out = build.merge_objects(self._data(), 1, 0, 0)
		self.assertEqual(len(out.relations), 2)  # 200+201 share no tags, 200 alone
		by_id = {r.id: r for r in out.relations}
		self.assertIn(200, by_id)
		self.assertIn(201, by_id)

	def test_multipolygons_with_identical_tags_are_combined(self):
		data = osm(
			nodes=[pyo5m.Node(1, 10, -90), pyo5m.Node(2, 20, -90), pyo5m.Node(3, 30, -90),
				pyo5m.Node(4, 40, -90)],
			ways=[pyo5m.Way(10, [1, 2], {"landuse": "forest"}), pyo5m.Way(11, [3, 4], {"landuse": "forest"})],
			relations=[
				pyo5m.Relation(20, [pyo5m.Member("way", 10, "outer")], {"type": "multipolygon", "landuse": "forest"}),
				pyo5m.Relation(21, [pyo5m.Member("way", 11, "outer")], {"type": "multipolygon", "landuse": "forest"}),
			])
		out = build.merge_objects(data, 1, 0, 0)
		self.assertEqual(len(out.relations), 1)
		self.assertEqual(out.relations[0].id, 20)
		self.assertEqual(len(out.relations[0].members), 2)

	def test_disabled_by_setting(self):
		data = self._data()
		with override_settings(MERGE_SUMMARY_OBJECTS=False):
			out = build.merge_objects(data, 1, 0, 0)
		self.assertEqual(len(out.ways), 7)  # unchanged

	def test_chains_merge_in_order(self):
		data = osm(
			nodes=[pyo5m.Node(i, 10 + i, -90) for i in range(1, 6)],
			ways=[
				pyo5m.Way(1, [1, 2, 3], {"highway": "primary"}),
				pyo5m.Way(2, [3, 4], {"highway": "primary"}),
				pyo5m.Way(3, [4, 5], {"highway": "primary"}),
			])
		out = build.merge_objects(data, 1, 0, 0)
		self.assertEqual(len(out.ways), 1)
		self.assertEqual(out.ways[0].refs, [1, 2, 3, 4, 5])


	def test_reverse_direction_is_merged(self):
		data = osm(
			nodes=[pyo5m.Node(1, 10, -90), pyo5m.Node(2, 20, -90), pyo5m.Node(3, 30, -90)],
			ways=[
				pyo5m.Way(1, [1, 2], {"highway": "primary"}),
				pyo5m.Way(2, [3, 2], {"highway": "primary"}),  # reversed: ends at 2
			])
		out = build.merge_objects(data, 1, 0, 0)
		self.assertEqual(len(out.ways), 1)
		self.assertEqual(sorted(out.ways[0].refs), [1, 2, 3])


class PyramidTests(TestCase):
	def test_tiles_beneath(self):
		self.assertEqual(build.tiles_beneath(11, 3, 4, 12), [(12, 6, 8), (12, 6, 9), (12, 7, 8), (12, 7, 9)])
		self.assertEqual(len(build.tiles_beneath(10, 0, 0, 12)), 16)
		self.assertEqual(build.finer_level(10), 12)
		self.assertIsNone(build.finer_level(12))

	def test_old_recipe_is_out_of_date_but_still_usable(self):
		build.store(12, 1, 2, osm(), 5)
		self.assertEqual(build.missing([(12, 1, 2), (12, 1, 3)]), [(12, 1, 3)])
		with override_settings(SUMMARY_SIMPLIFY_PIXELS=settings.SUMMARY_SIMPLIFY_PIXELS * 2):
			# Out of date, to be built again...
			self.assertFalse(build.current(TileSummary.objects.filter(zoom=12, x=1, y=2)).exists())
			# ...but what is made from it can still go ahead
			self.assertEqual(build.missing([(12, 1, 2)]), [])
			self.assertEqual(build.load(12, 1, 2)[1], 5)
			self.assertEqual(build.built_in(12, 1, 1, 2, 2), {(1, 2)})

	def test_changing_the_lua_changes_the_recipe(self):
		with tempfile.TemporaryDirectory() as work:
			lua = Path(work) / "summary.lua"
			lua.write_bytes(Path(settings.SUMMARY_LUA).read_bytes())
			with override_settings(SUMMARY_LUA=lua):
				before = build.recipe()
				lua.write_text(lua.read_text() + "\n-- changed\n")
				self.assertNotEqual(build.recipe(), before)

	def test_builds_from_the_finer_level(self):
		# A road across the tile (a line elsewhere would be left out)
		west, south, east, north = tile_bounds(10, 0, 0)
		lat = (south + north) / 2
		road = osm(nodes=[pyo5m.Node(1, lat, west), pyo5m.Node(2, lat, east)],
			ways=[pyo5m.Way(10, [1, 2], {"highway": "primary"})])
		beneath = build.tiles_beneath(10, 0, 0, 12)
		for level, x, y in beneath[:-1]:
			build.store(level, x, y, road, 3)
		self.assertEqual(build.build_from_finer(10, 0, 0), [beneath[-1]])
		build.store(*beneath[-1], road, 4)
		self.assertEqual(build.build_from_finer(10, 0, 0), [])
		summary = TileSummary.objects.get(zoom=10)
		self.assertEqual((summary.version, summary.ways), (4, 1))
