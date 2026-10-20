"""Tile coordinates, as tilemaker uses them (Web Mercator, y from the north)."""

import math


def tile_bounds(zoom, x, y):
	"""The tile's west, south, east and north edges, in degrees."""
	n = 1 << zoom
	west = x / n * 360.0 - 180.0
	east = (x + 1) / n * 360.0 - 180.0
	north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
	south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
	return west, south, east, north


def tile_area(zoom, x, y, margin=1 / 8):
	"""The area whose map data a tile needs: the tile and a margin of a
	fraction of it on each side, for what crosses its edges. tilemaker's
	--tile clips to the same margin (an eighth)."""
	west, south, east, north = tile_bounds(zoom, x, y)
	margin_lon = (east - west) * margin
	margin_lat = (north - south) * margin
	return west - margin_lon, south - margin_lat, east + margin_lon, north + margin_lat


def valid_tile(zoom, x, y):
	return 0 <= zoom <= 22 and 0 <= x < (1 << zoom) and 0 <= y < (1 << zoom)
