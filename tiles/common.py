"""Common tiles: contents that a great many tiles have, as open sea or land
with nothing mapped, kept once each, in COMMON_DIR under TILE_CACHE_DIR.

Which contents are common is chosen by hand: a file put in COMMON_DIR makes
its content common, and a tile rendered with that content (queue.finish) is
pointed at the file rather than given one of its own. A file is named
describing its content where a short name can be found (describe), as
land.pbf.gz or sea.pbf.gz, and otherwise by its content's hash.
"""

import gzip
import hashlib
import os

import mapbox_vector_tile
from django.conf import settings
from shapely.geometry import box, shape
from shapely.ops import unary_union

COMMON_DIR = "common"

# The longest name describe gives
NAME_LENGTH = 10


def content_hash(raw):
	"""The hash a tile's content (not gzipped) is known by."""
	return hashlib.sha256(raw).hexdigest()[:32]


def ungzipped(data):
	return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def describe(raw):
	"""A short name (at most NAME_LENGTH characters) for what a tile's content
	(not gzipped) shows, or None if none can be found: "land" for nothing
	drawn; "sea" for the ocean all over, or "sea-2poly" when it is in two
	pieces, as a tile across a line the coastline data is cut along is;
	"sea-part" for ocean not all over; "park" for a park all over; "boundary"
	for a boundary line alone."""
	try:
		layers = mapbox_vector_tile.decode(raw, default_options={"y_coord_down": True})
	except Exception:
		return None
	features = [(name, layer["extent"], f) for name, layer in layers.items() for f in layer["features"]]
	if not features:
		return "land"
	extent = features[0][1]
	try:
		geometries = [shape(f["geometry"]) for _, _, f in features]
		covers = unary_union(geometries).buffer(0).covers(box(0, 0, extent, extent))
	except Exception:
		return None
	areas = all(g.geom_type in ("Polygon", "MultiPolygon") for g in geometries)
	names = {name for name, _, _ in features}
	if names == {"water"} and areas and all(f["properties"].get("class") == "ocean" for _, _, f in features):
		if not covers:
			return "sea-part"
		return "sea" if len(features) == 1 else "sea-{}poly".format(len(features))[:NAME_LENGTH]
	if names <= {"park", "landcover"} and areas and covers and "park" in names:
		return "park"
	if names == {"boundary"} and all(g.geom_type in ("LineString", "MultiLineString") for g in geometries):
		return "boundary"
	return None


_registry = {"mtime": None, "files": {}}


def common_files():
	"""The common files, by the hash of their content: {hash: path relative
	to TILE_CACHE_DIR}. Read again whenever COMMON_DIR changes."""
	folder = os.path.join(settings.TILE_CACHE_DIR, COMMON_DIR)
	try:
		mtime = os.stat(folder).st_mtime_ns
	except FileNotFoundError:
		return {}
	if _registry["mtime"] != (folder, mtime):
		files = {}
		for name in sorted(os.listdir(folder)):
			if not name.endswith(".pbf.gz"):
				continue
			with open(os.path.join(folder, name), "rb") as f:
				files[content_hash(ungzipped(f.read()))] = os.path.join(COMMON_DIR, name)
		_registry.update(mtime=(folder, mtime), files=files)
	return _registry["files"]


def common_file(data):
	"""The common file of a rendered tile's content (gzipped or not), or None
	if it isn't common."""
	return common_files().get(content_hash(ungzipped(data)))


def file_name(raw, taken=()):
	"""The name a common file of this content (not gzipped) would be given:
	what describe calls it, unless that is taken (names, without the
	extension), and otherwise its hash."""
	name = describe(raw)
	if name is None or name in taken:
		name = content_hash(raw)
	return name + ".pbf.gz"
