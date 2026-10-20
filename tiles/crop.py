"""Tiles cut from a tile at a lower zoom: the part of it a higher zoom's tile
covers, scaled up. Quick, and needs no map data, but has only the lower
tile's content and precision."""

import gzip

import mapbox_vector_tile
from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
import shapely
from shapely import affinity
from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon, shape

# How far beyond the tile's edge features are kept, in tile units (of 4096),
# as tilemaker keeps a buffer
BUFFER = 64

_MULTI = {0: MultiPoint, 1: MultiLineString, 2: MultiPolygon}


def _same_kind(geometry, dimension):
	"""The parts of a clipped geometry of the original's dimension (points,
	lines or areas), as one geometry: clipping can leave a collection, with,
	say, a point where a line touched the edge, which a tile can't hold."""
	if geometry.is_empty or geometry.geom_type != "GeometryCollection":
		return geometry
	parts = []
	for part in shapely.get_parts(geometry):
		if shapely.get_dimensions(part) == dimension:
			parts.extend(shapely.get_parts(part))
	if not parts:
		return shapely.Point()
	return parts[0] if len(parts) == 1 else _MULTI[dimension](parts)


def crop(parent_tile, parent_zoom, parent_x, parent_y, zoom, x, y):
	"""The tile zoom/x/y, from parent_tile (a gzipped vector tile, at a lower
	zoom, covering it). Returns it gzipped, or b"" if nothing is in it."""
	if not parent_tile:
		return b""
	scale = 1 << (zoom - parent_zoom)
	if (x // scale, y // scale) != (parent_x, parent_y):
		raise ValueError("{}/{}/{} is not within {}/{}/{}".format(zoom, x, y, parent_zoom, parent_x, parent_y))
	data = parent_tile
	if data[:2] == b"\x1f\x8b":
		data = gzip.decompress(data)
	decoded = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})

	layers = []
	for name, layer in decoded.items():
		extent = layer["extent"]
		# The child's part of the parent, in the parent's units
		offset_x = (x - parent_x * scale) * extent / scale
		offset_y = (y - parent_y * scale) * extent / scale
		features = []
		for feature in layer["features"]:
			geometry = shape(feature["geometry"])
			geometry = affinity.affine_transform(geometry, [scale, 0, 0, scale, -offset_x * scale, -offset_y * scale])
			# Clipping to a rectangle copes with the invalid polygons tiles
			# can have
			geometry = _same_kind(shapely.clip_by_rect(geometry, -BUFFER, -BUFFER, extent + BUFFER, extent + BUFFER),
				shapely.get_dimensions(geometry))
			if geometry.is_empty:
				continue
			out = {"geometry": geometry, "properties": feature["properties"]}
			if feature.get("id") is not None:
				out["id"] = feature["id"]
			features.append(out)
		if features:
			layers.append({"name": name, "features": features, "extent": extent})

	if not layers:
		return b""
	encoded = mapbox_vector_tile.encode(layers, default_options={
		"y_coord_down": True, "extents": layers[0]["extent"], "quantize_bounds": None,
		"on_invalid_geometry": on_invalid_geometry_make_valid})
	return gzip.compress(encoded)
