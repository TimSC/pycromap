from django.test import SimpleTestCase

from tiles.tilegeo import key_tile, row_keys, tile_key


class TileKeyTests(SimpleTestCase):
	def test_a_key_gives_back_its_tile(self):
		for tile in ((0, 0, 0), (12, 4095, 17), (22, (1 << 22) - 1, (1 << 22) - 1)):
			self.assertEqual(key_tile(tile_key(*tile)), tile)

	def test_keys_are_ordered_by_zoom_then_row_then_column(self):
		tiles = [(4, 0, 1), (3, 7, 7), (4, 15, 0), (4, 1, 1), (4, 0, 0)]
		self.assertEqual(sorted(tiles, key=lambda t: tile_key(*t)), [(3, 7, 7), (4, 0, 0), (4, 15, 0), (4, 0, 1), (4, 1, 1)])

	def test_a_run_of_a_row_is_a_range_of_keys(self):
		first, last = row_keys(4, 2, 3, 5)
		inside = {key_tile(k) for k in range(first, last + 1)}
		self.assertEqual(inside, {(4, 3, 2), (4, 4, 2), (4, 5, 2)})
