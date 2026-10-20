import gzip
from unittest import mock

import mapbox_vector_tile
from django.test import SimpleTestCase

from tiles.crop import crop


def vector_tile(features, extent=4096):
	return gzip.compress(mapbox_vector_tile.encode([{"name": "place", "features": features, "extent": extent}],
		default_options={"y_coord_down": True, "extents": extent}))


def decoded(tile):
	return mapbox_vector_tile.decode(gzip.decompress(tile), default_options={"y_coord_down": True})


class CropTests(SimpleTestCase):
	parent = vector_tile([
		{"geometry": "POINT (1000 1000)", "properties": {"name": "near"}},
		{"geometry": "POINT (3000 3000)", "properties": {"name": "far"}},
		{"geometry": "LINESTRING (0 1024, 4096 1024)", "properties": {"name": "road"}},
	])

	def test_scales_up_the_part_covered(self):
		# The top-left quarter of 12/10/20
		layer = decoded(crop(self.parent, 12, 10, 20, 13, 20, 40))["place"]
		features = {f["properties"]["name"]: f["geometry"] for f in layer["features"]}
		self.assertEqual(set(features), {"near", "road"})
		self.assertEqual(features["near"]["coordinates"], [2000, 2000])
		xs = [x for x, _ in features["road"]["coordinates"]]
		ys = {y for _, y in features["road"]["coordinates"]}
		self.assertEqual(ys, {2048})
		# Cut off just beyond the edge
		self.assertEqual((min(xs), max(xs)), (0, 4096 + 64))

	def test_other_quarters(self):
		layer = decoded(crop(self.parent, 12, 10, 20, 13, 21, 41))["place"]
		self.assertEqual([f["properties"]["name"] for f in layer["features"]], ["far"])
		self.assertEqual(layer["features"][0]["geometry"]["coordinates"], [(3000 - 2048) * 2, (3000 - 2048) * 2])

	def test_keeps_only_the_kind_of_geometry_it_was(self):
		# The square touches the child's edge at a corner, as well as reaching in
		parent = vector_tile([{"geometry": "POLYGON ((2048 2048, 2048 1000, 3000 1000, 3000 2048, 2048 2048))",
			"properties": {"name": "area"}},
			{"geometry": "LINESTRING (100 100, 2400 100)", "properties": {"name": "line"}}])
		layer = decoded(crop(parent, 12, 0, 0, 14, 1, 0))["place"]
		kinds = {f["properties"]["name"]: f["geometry"]["type"] for f in layer["features"]}
		self.assertEqual(kinds, {"area": "Polygon", "line": "LineString"})

	def test_invalid_polygon(self):
		# As decoded from a tile tilemaker made: the encoder won't write one
		figure_of_eight = {"type": "Polygon", "coordinates": [[[0, 0], [4000, 3000], [4000, 0], [0, 1000], [0, 0]]]}
		layers = {"place": {"extent": 4096, "features": [{"geometry": figure_of_eight, "properties": {}}]}}
		with mock.patch("tiles.crop.mapbox_vector_tile.decode", return_value=layers):
			layer = decoded(crop(b"tile", 12, 0, 0, 13, 0, 0))["place"]
		self.assertEqual(len(layer["features"]), 1)

	def test_nothing_there(self):
		self.assertEqual(crop(vector_tile([{"geometry": "POINT (100 100)", "properties": {}}]), 12, 0, 0, 13, 1, 1), b"")
		self.assertEqual(crop(b"", 12, 0, 0, 13, 1, 1), b"")

	def test_must_be_within(self):
		with self.assertRaises(ValueError):
			crop(self.parent, 12, 10, 20, 13, 22, 40)
