"""Building tile summaries.

A summary keeps the objects of a tile's area that summary.lua picks for its
level, whole, with their geometry simplified; but not lines that never come
near the tile (see LINE_MARGIN). The summary levels are the
zooms TILE_ZOOMS marks "summary"; the finest is built from the map data; each
coarser level from the summaries of the next finer level beneath it, merged.
"""

import gzip
import hashlib
import io
import json
import math
import os

import pyo5m
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from shapely.geometry import LineString, box

from tiles.tilegeo import key_tile, row_keys, tile_bounds, tile_key
from .lua import summary_filter
from .models import Recipe, TileSummary


# The encoding of summaries' data: o5m, gzipped
FORMAT = "o5m.gz"


_recipes = {}


def _shaping():
	"""The settings that shape summaries, as JSON."""
	return json.dumps({"levels": levels(), "simplify": settings.SUMMARY_SIMPLIFY_PIXELS,
		"format": FORMAT, "line_margin": LINE_MARGIN, "min_ring_pixels": MIN_RING_PIXELS,
		"min_area": {level: _min_area(level) for level in levels()}}, sort_keys=True)


def recipe():
	"""The id of the Recipe summaries are built with now: summary.lua and the
	settings that shape them, recorded the first time they are used. A
	summary with another recipe is out of date."""
	shaping = _shaping()
	made, _ = Recipe.objects.get_or_create(hash=recipe_hash(shaping),
		defaults={"settings": shaping, "created_at": timezone.now()})
	return made.pk


def recipe_hash(shaping=None):
	"""A hash of summary.lua and the settings that shape summaries (shaping,
	as JSON): what tells recipes apart."""
	shaping = _shaping() if shaping is None else shaping
	path = str(settings.SUMMARY_LUA)
	key = (path, os.stat(path).st_mtime_ns, shaping)
	if key not in _recipes:
		digest = hashlib.sha256()
		with open(path, "rb") as f:
			digest.update(f.read())
		digest.update(shaping.encode())
		_recipes[key] = digest.hexdigest()
	return _recipes[key]


def current(queryset):
	"""The summaries of a queryset built as summaries now are. Only whether a
	summary is to be built again depends on this: anything stored can be
	built on (see missing)."""
	return queryset.filter(format=FORMAT, recipe=recipe())


# ---- The pyramid

def levels():
	"""The summary levels, finest first: the zooms TILE_ZOOMS marks
	"summary". Each summary zoom is checked to render from one of them."""
	levels = sorted((zoom for zoom, config in settings.TILE_ZOOMS.items() if config.get("summary")),
		reverse=True)
	for zoom, config in settings.TILE_ZOOMS.items():
		if config["source"] == "summary" and (config["level"] not in levels or config["level"] < zoom):
			raise ValueError("zoom {} renders from level {}, which is not a summary level "
				"(mark it \"summary\" in TILE_ZOOMS) or is lower than the zoom".format(zoom, config["level"]))
	return levels


def base_level():
	"""The finest summary level (None if TILE_ZOOMS marks none)."""
	return levels()[0] if levels() else None


def finer_level(level):
	"""The level a level's summaries are built from (None for the base)."""
	finer = [l for l in levels() if l > level]
	return min(finer) if finer else None


def tiles_beneath(zoom, x, y, level):
	"""The tiles at a finer (or the same) zoom, level, covering a tile."""
	scale = 1 << (level - zoom)
	return [(level, x * scale + i, y * scale + j) for i in range(scale) for j in range(scale)]


# ---- Filtering and simplifying

def _latp(lat):
	"""Web Mercator y, in degrees, so that simplifying works in map space."""
	lat = max(-85.0511, min(85.0511, lat))
	return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


# How far beyond its tile a summary keeps lines, as a fraction of the tile's
# width (tiles are drawn with a buffer around them, so lines just outside
# matter at the edges). The map database finds ways by their bounding boxes,
# so a long border, coastline or river that curves or runs diagonally is read
# for every tile in its box, most of which it never reaches: kept, those were
# most of what summaries held. Part of the recipe.
LINE_MARGIN = 0.1


def _reach(level, x, y):
	"""A tile's area, with LINE_MARGIN around it, in the degrees of longitude
	and Web Mercator y that summarize works in (a tile is square in them)."""
	west, south, east, north = tile_bounds(level, x, y)
	margin = (east - west) * LINE_MARGIN
	return box(west - margin, _latp(south) - margin, east + margin, _latp(north) + margin)


def tolerance(level):
	"""How far, in degrees of Web Mercator, simplified geometry may stray: a
	fraction (SUMMARY_SIMPLIFY_PIXELS) of a pixel of a 4096-pixel tile at the
	level."""
	return 360.0 / (1 << level) / 4096 * settings.SUMMARY_SIMPLIFY_PIXELS


def simplify_refs(refs, coords, tol):
	"""A way's node IDs, fewer: those of the vertices that simplifying its line
	keeps. Nodes without a location are left out. A closed way (a ring of an
	area) is simplified without crossing itself: simplified as a plain line,
	a ring can fold over itself, and an area whose ring does is drawn wrongly,
	or not at all (most of a forest's outer ring, say, at a coarser level).
	Summaries built before this share their recipe with those built since:
	see PENDING.md."""
	refs = [ref for ref in refs if ref in coords]
	if len(refs) < 3 or tol <= 0:
		return refs
	points = [coords[ref] for ref in refs]
	closed = len(refs) > 3 and refs[0] == refs[-1]
	kept = LineString(points).simplify(tol, preserve_topology=closed).coords
	# Simplifying keeps vertices as they were, so they can be matched back to
	# their nodes, in order
	out, i = [], 0
	for point in kept:
		while i < len(points) and points[i] != tuple(point):
			i += 1
		if i == len(points):
			break
		out.append(refs[i])
		i += 1
	return out


# The profile draws an area at zoom z only if it is bigger than a pixel of a
# 256-pixel tile at z - 1, squared (SetMinZoomByArea, profile/process.lua).
# Summaries keep areas down to this share of that, at the highest zoom drawn
# from their level: tilemaker measures an area's true size, smaller than its
# size on the map away from the equator, so this keeps a little more than is
# drawn. Part of the recipe.
AREA_KEEP_FRACTION = 0.25

# The tags of the areas the profile draws by their size
_AREA_KEYS = ("landuse", "natural", "leisure", "water", "aeroway")
_LINE_KEYS = ("highway", "railway", "waterway", "boundary")


def _min_area(level):
	"""The smallest area kept in summaries at a level, in square pixels of a
	4096-pixel tile at the level (see AREA_KEEP_FRACTION), or None if no zoom
	is drawn from the level."""
	zooms = [zoom for zoom, config in settings.TILE_ZOOMS.items()
		if config.get("source") == "summary" and config.get("level") == level]
	if not zooms:
		return None
	side = 16 * 2 ** (level - max(zooms) + 1)  # a 256-pixel tile's pixel at that zoom - 1, in level pixels
	return side * side * AREA_KEEP_FRACTION


def _ring_area(refs, coords, pixel):
	"""The area of a ring, in square pixels (pixel: a pixel's size in degrees)."""
	points = [coords[ref] for ref in refs if ref in coords]
	twice = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))
	return abs(twice) / 2 / (pixel * pixel)


def _is_area(tags):
	"""Whether a closed way's kept tags make it an area the profile draws by
	its size, not a line (a boundary, a road)."""
	return any(key in tags for key in _AREA_KEYS) and not any(key in tags for key in _LINE_KEYS)


# Rings of a multipolygon smaller than this, in pixels of a 4096-pixel tile
# at the summary's level (no more than half that at the zooms drawn from it),
# are left out of its summary: too small to see. Part of the recipe.
MIN_RING_PIXELS = 2

# The first ID of the ways a summary makes for multipolygon rings joined from
# several member ways: far above any OSM way ID, with room for IDs that don't
# collide. They are above what tilemaker takes, so a tile's input numbers
# them afresh (tiles.render).
RING_ID_BASE = 1 << 56


def _ring_id(relation_id, first_way_id):
	"""The ID of a ring of a relation joined from member ways, the lowest of
	them first_way_id: the same in every summary holding it, so that merging
	summaries (which keeps one object for each ID) keeps one copy of it."""
	digest = hashlib.sha1("{}:{}".format(relation_id, first_way_id).encode()).digest()
	return RING_ID_BASE + int.from_bytes(digest[:6], "big")


def _join_rings(parts):
	"""Joins ways (id, refs) end to end into closed rings. Returns (rings,
	leftovers): the rings as (refs, the IDs of the ways joined), and the IDs
	of the ways of chains that couldn't be closed (a member missing, say)."""
	rings, open_ = [], []
	for way_id, refs in parts:
		# Closed, however few its nodes: a ring simplified down to a point or
		# two (by an older summary) is a ring too small to see, not a chain
		if len(refs) >= 2 and refs[0] == refs[-1]:
			rings.append((list(refs), [way_id]))
		elif len(refs) >= 2:
			open_.append((way_id, list(refs)))
	by_end = {}
	for i, (_, refs) in enumerate(open_):
		by_end.setdefault(refs[0], []).append(i)
		by_end.setdefault(refs[-1], []).append(i)
	used = [False] * len(open_)
	leftovers = []
	for start in range(len(open_)):
		if used[start]:
			continue
		used[start] = True
		ids, refs = [open_[start][0]], list(open_[start][1])
		# Along the chain from its end until it comes back to its start (each
		# node of a ring ends two of its ways)
		while refs[0] != refs[-1]:
			end = refs[-1]
			following = next((i for i in by_end.get(end, []) if not used[i]), None)
			if following is None:
				break
			used[following] = True
			other = open_[following][1]
			refs.extend(other[1:] if other[0] == end else list(reversed(other[:-1])))
			ids.append(open_[following][0])
		if refs[0] == refs[-1]:
			rings.append((refs, ids))
		else:
			leftovers.extend(ids)
	return rings, leftovers


def summarize(data, level, tile=None):
	"""The objects of data worth keeping at a level, with simplified geometry.

	A multipolygon drawn as an area is kept as its rings: its member ways are
	joined into rings, each simplified whole, and rings too small to see at
	the level (MIN_RING_PIXELS), or to be drawn at the zooms drawn from it
	(_min_area), or, for a tile (x, y), that don't reach it, are left out.
	An area that is a closed way of its own is left out when too small to be
	drawn too. Simplified way by way, a ring joined from many short ways
	(a woodland of thousands of woods, say) keeps both ends of every one of
	them, at any level. Ways a chain can't be closed from are kept as they are.

	For a tile, lines that never come within LINE_MARGIN of it are left out
	too: a line is only drawn where it is, and the tiles it does reach keep it
	whole. Closed ways are kept, as an area can cover the tile without its
	outline reaching it."""
	keep = summary_filter()
	tol = tolerance(level)
	coords = {node.id: (node.lon, _latp(node.lat)) for node in data.nodes}
	reach = _reach(level, *tile) if tile is not None else None
	pixel = 360.0 / (1 << level) / 4096
	min_area = _min_area(level)
	source = {way.id: way for way in data.ways}

	relations = []
	for relation in data.relations:
		tags = keep("relation", relation.tags, level)
		if tags is not None:
			relations.append((relation, tags))
	relation_ids = {relation.id for relation, _ in relations}
	areas = {relation.id for relation, _ in relations if relation.tags.get("type") == "multipolygon"}
	# The member ways of the other relations (boundaries, routes): kept as
	# they are
	member_ways = {m.ref for relation, _ in relations if relation.id not in areas
		for m in relation.members if m.type == "way"}

	ways = {}
	area_members = {}  # an area's members, its rings in place of the ways joined into them
	unjoined = set()  # the member ways of areas that couldn't be joined into rings
	for relation, _ in relations:
		if relation.id not in areas:
			continue
		members = []
		for inner in (False, True):
			parts = [(m.ref, [ref for ref in source[m.ref].refs if ref in coords]) for m in relation.members
				if m.type == "way" and m.ref in source and (m.role == "inner") == inner]
			role = "inner" if inner else "outer"
			rings, leftovers = _join_rings(parts)
			for refs, ids in rings:
				xs = [coords[ref][0] for ref in refs]
				ys = [coords[ref][1] for ref in refs]
				if max(max(xs) - min(xs), max(ys) - min(ys)) < MIN_RING_PIXELS * pixel:
					continue
				if min_area is not None and _ring_area(refs, coords, pixel) < min_area:
					continue
				if reach is not None and not reach.intersects(box(min(xs), min(ys), max(xs), max(ys))):
					continue
				refs = simplify_refs(refs, coords, tol)
				if len(refs) < 4 or refs[0] != refs[-1]:
					continue
				ring_id = ids[0] if len(ids) == 1 else _ring_id(relation.id, min(ids))
				ways[ring_id] = pyo5m.Way(ring_id, refs, {})
				members.append(pyo5m.Member("way", ring_id, role))
			roles = {m.ref: m.role for m in relation.members if m.type == "way"}
			members.extend(pyo5m.Member("way", way_id, roles[way_id]) for way_id in leftovers)
			unjoined.update(leftovers)
		members.extend(m for m in relation.members if m.type != "way")
		area_members[relation.id] = members

	for way in data.ways:
		closed = len(way.refs) > 3 and way.refs[0] == way.refs[-1]
		tags = keep("way", way.tags, level, closed)
		needed = way.id in member_ways or way.id in unjoined
		if tags is None and not needed:
			continue
		# An area too small to be drawn at the zooms drawn from the level
		if (closed and not needed and min_area is not None and _is_area(tags)
				and _ring_area(way.refs, coords, pixel) < min_area):
			continue
		refs = simplify_refs(way.refs, coords, tol)
		# An area simplified away is too small to show, unless a relation
		# needs it
		if len(refs) < 2 or (closed and len(refs) < 4 and not needed):
			continue
		if (reach is not None and not closed and way.id not in unjoined
				and not reach.intersects(LineString([coords[ref] for ref in refs]))):
			continue
		ways[way.id] = pyo5m.Way(way.id, refs, tags or {})

	out = pyo5m.OsmData()
	needed_nodes = {ref for way in ways.values() for ref in way.refs}
	for relation, tags in relations:
		members = [m for m in area_members.get(relation.id, relation.members)
			if (m.type == "way" and m.ref in ways)
			or (m.type == "node" and m.ref in coords)
			or (m.type == "relation" and m.ref in relation_ids)]
		if not members:
			continue
		needed_nodes.update(m.ref for m in members if m.type == "node")
		out.relations.append(pyo5m.Relation(relation.id, members, tags))

	for node in data.nodes:
		tags = keep("node", node.tags, level) if node.tags else None
		if tags is None and node.id not in needed_nodes:
			continue
		out.nodes.append(pyo5m.Node(node.id, node.lat, node.lon, tags or {}))
	out.ways = sorted(ways.values(), key=lambda w: w.id)
	out.nodes.sort(key=lambda n: n.id)
	return out


def merge(summaries):
	"""Several summaries' objects as one, without duplicates (by ID). Whole
	objects appear in the summaries of every tile they reach into: the copy
	from the newest version of the map data is kept. summaries are
	(OsmData, version) pairs."""
	newest_first = sorted(summaries, key=lambda s: s[1], reverse=True)
	out = pyo5m.OsmData()
	for kind in ("nodes", "ways", "relations"):
		seen = {}
		for data, _ in newest_first:
			for obj in getattr(data, kind):
				seen.setdefault(obj.id, obj)
		setattr(out, kind, sorted(seen.values(), key=lambda o: o.id))
	return out


# ---- Merging similar objects

def merge_objects(data, zoom, x, y):
	"""Merges similar objects entirely within the tile (see
	MERGE_SUMMARY_OBJECTS): ways with the same tags are concatenated into
	chains, and multipolygon relations with the same tags are combined. Objects
	reaching the tile's edge are left alone, as their copies in adjacent tiles
	must match."""
	if not settings.MERGE_SUMMARY_OBJECTS:
		return data
	west, south, east, north = tile_bounds(zoom, x, y)
	nodes_in = {n.id for n in data.nodes if west <= n.lon <= east and south <= n.lat <= north}
	ways_in = {w.id for w in data.ways if all(r in nodes_in for r in w.refs)}
	interior_w = [w for w in data.ways if w.id in ways_in]
	exterior_w = [w for w in data.ways if w.id not in ways_in]
	data.ways = sorted(exterior_w + _concatenate_ways(interior_w), key=lambda w: w.id)
	interior_r = [r for r in data.relations
		if all(m.type == "node" and m.ref in nodes_in
			or m.type == "way" and m.ref in ways_in
			for m in r.members)]
	interior_ids = {r.id for r in interior_r}
	exterior_r = [r for r in data.relations if r.id not in interior_ids]
	data.relations = sorted(exterior_r + _combine_multipolygons(interior_r), key=lambda r: r.id)
	return data


def _concatenate_ways(ways):
	"""Concatenates open ways with the same tags into chains. Closed ways
	(polygons) are kept as they are."""
	groups = {}
	for w in ways:
		groups.setdefault(json.dumps(w.tags, sort_keys=True), []).append(w)
	result = []
	for group in groups.values():
		closed = [w for w in group if w.refs[0] == w.refs[-1]]
		open_ = [w for w in group if w.refs[0] != w.refs[-1]]
		result.extend(closed)
		by_end = {}
		for i, w in enumerate(open_):
			by_end.setdefault(w.refs[0], []).append(i)
			by_end.setdefault(w.refs[-1], []).append(i)
		used = [False] * len(open_)
		for start in range(len(open_)):
			if used[start]:
				continue
			used[start] = True
			refs = list(open_[start].refs)
			changed = True
			while changed:
				changed = False
				for node in (refs[0], refs[-1]):
					for i in by_end.get(node, []):
						if used[i]:
							continue
						other = open_[i].refs
						if node == refs[-1]:
							if other[0] == node:
								refs.extend(other[1:])
							elif other[-1] == node:
								refs.extend(reversed(other[:-1]))
							else:
								continue
						elif node == refs[0]:
							# Added before the start, so as to end at it
							if other[-1] == node:
								refs = list(other[:-1]) + refs
							elif other[0] == node:
								refs = list(reversed(other[1:])) + refs
							else:
								continue
						used[i] = True
						changed = True
						break
			result.append(pyo5m.Way(open_[start].id, refs, open_[start].tags))
	return result


def _combine_multipolygons(relations):
	"""Combines multipolygon relations with the same tags into one, with the
	union of their members. Other relations are kept as they are."""
	groups = {}
	other = []
	for r in relations:
		if r.tags.get("type") == "multipolygon":
			groups.setdefault(json.dumps(r.tags, sort_keys=True), []).append(r)
		else:
			other.append(r)
	result = list(other)
	for group in groups.values():
		if len(group) == 1:
			result.append(group[0])
			continue
		first = group[0]
		members, seen = [], set()
		for r in group:
			for m in r.members:
				if (m.type, m.ref) not in seen:
					seen.add((m.type, m.ref))
					members.append(m)
		result.append(pyo5m.Relation(first.id, members, first.tags))
	return result


# ---- Storing

def encode(data):
	"""Map data as FORMAT."""
	buffer = io.BytesIO()
	data.stream_to(pyo5m.encoder(buffer, "o5m"))
	return gzip.compress(buffer.getvalue())


def decode(blob, format=FORMAT):
	"""Map data from a summary's data, in its format: o5m, or o5m.gz."""
	blob = bytes(blob)
	if format.endswith(".gz"):
		blob = gzip.decompress(blob)
	data = pyo5m.OsmData()
	pyo5m.decoder(io.BytesIO(blob), data, "o5m").run()
	return data


def store(level, x, y, data, version):
	return TileSummary.objects.update_or_create(pk=tile_key(level, x, y), defaults={
		"zoom": level, "x": x, "y": y, "data": encode(data), "format": FORMAT, "recipe_id": recipe(), "version": version,
		"built_at": timezone.now(),
		"nodes": len(data.nodes), "ways": len(data.ways), "relations": len(data.relations),
	})[0]


def load(level, x, y):
	"""A stored summary, current or not, as (OsmData, version), or None."""
	summary = TileSummary.objects.filter(pk=tile_key(level, x, y)).first()
	if summary is None:
		return None
	return decode(summary.data, summary.format), summary.version


def built_in(level, x0, x1, y0, y1):
	"""The stored summaries, current or not, at a level within a range of
	tiles (x0 to x1, y0 to y1, inclusive), as {(x, y)}: one query, a range of
	keys for each row."""
	rows = Q()
	for y in range(y0, y1 + 1):
		rows |= Q(pk__range=row_keys(level, y, x0, x1))
	# By key, which the index has, so the rows (and their data) aren't read
	return {key_tile(key)[1:] for key in TileSummary.objects.filter(rows).values_list("pk", flat=True)}


def missing(tiles):
	"""Those of the summaries (level, x, y) that aren't stored. One out of date
	(built with another recipe) can still be built on: whether it is out of
	date only decides whether it is built again (by the idle sweep), not
	whether what is made from it can go ahead, so a recipe change doesn't
	hold anything up until the rebuild reaches it."""
	tiles = list(tiles)
	stored = set(TileSummary.objects.filter(pk__in=[tile_key(*tile) for tile in tiles])
		.values_list("pk", flat=True))
	return [tile for tile in tiles if tile_key(*tile) not in stored]


def build_from_data(level, x, y, data, version):
	"""Builds and stores the summary of the base level's tile from its map data,
	unless one from this version of the map data is already stored. The base
	level is the finest zoom TILE_ZOOMS marks "summary"; no other zoom has
	a summary built from the map data."""
	if level != base_level():
		return
	if current(TileSummary.objects.filter(pk=tile_key(level, x, y), version=version)).exists():
		return
	data = summarize(data, level, (x, y))
	store(level, x, y, merge_objects(data, level, x, y), version)


def build_from_finer(level, x, y):
	"""Builds and stores a summary from the summaries beneath it at the next
	finer level. Returns the tiles of those that are missing, if any, without
	building."""
	beneath = tiles_beneath(level, x, y, finer_level(level))
	absent = missing(beneath)
	if absent:
		return absent
	loaded = [load(*tile) for tile in beneath]
	data = summarize(merge(loaded), level, (x, y))
	store(level, x, y, merge_objects(data, level, x, y), max(version for _, version in loaded))
	return []
