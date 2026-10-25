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
from django.utils import timezone
from shapely.geometry import LineString, box

from tiles.tilegeo import tile_bounds
from .lua import summary_filter
from .models import TileSummary


# The encoding of summaries' data: o5m, gzipped
FORMAT = "o5m.gz"


_recipes = {}


def recipe():
	"""What summaries are built with: a hash of summary.lua and the settings
	that shape them. A summary with another recipe is out of date."""
	shaping = json.dumps({"levels": levels(), "simplify": settings.SUMMARY_SIMPLIFY_PIXELS,
		"format": FORMAT, "line_margin": LINE_MARGIN}, sort_keys=True)
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


def summarize(data, level, tile=None):
	"""The objects of data worth keeping at a level, with simplified geometry.
	For a tile (x, y), lines that never come within LINE_MARGIN of it are
	left out: a line is only drawn where it is, and the tiles it does reach
	keep it whole. Areas are kept, as one can cover the tile without its
	outline reaching it: closed ways, and the members of multipolygons."""
	keep = summary_filter()
	tol = tolerance(level)
	coords = {node.id: (node.lon, _latp(node.lat)) for node in data.nodes}
	reach = _reach(level, *tile) if tile is not None else None

	relations = []
	for relation in data.relations:
		tags = keep("relation", relation.tags, level)
		if tags is not None:
			relations.append((relation, tags))
	relation_ids = {relation.id for relation, _ in relations}
	member_ways = {m.ref for relation, _ in relations for m in relation.members if m.type == "way"}
	area_members = {m.ref for relation, _ in relations if relation.tags.get("type") == "multipolygon"
		for m in relation.members if m.type == "way"}

	ways = {}
	for way in data.ways:
		closed = len(way.refs) > 3 and way.refs[0] == way.refs[-1]
		tags = keep("way", way.tags, level, closed)
		if tags is None and way.id not in member_ways:
			continue
		refs = simplify_refs(way.refs, coords, tol)
		# An area simplified away is too small to show, unless a relation
		# needs it
		if len(refs) < 2 or (closed and len(refs) < 4 and way.id not in member_ways):
			continue
		if (reach is not None and not closed and way.id not in area_members
				and not reach.intersects(LineString([coords[ref] for ref in refs]))):
			continue
		ways[way.id] = pyo5m.Way(way.id, refs, tags or {})

	out = pyo5m.OsmData()
	needed_nodes = {ref for way in ways.values() for ref in way.refs}
	for relation, tags in relations:
		members = [m for m in relation.members
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
	return TileSummary.objects.update_or_create(zoom=level, x=x, y=y, defaults={
		"data": encode(data), "format": FORMAT, "recipe": recipe(), "version": version,
		"built_at": timezone.now(),
		"nodes": len(data.nodes), "ways": len(data.ways), "relations": len(data.relations),
	})[0]


def load(level, x, y):
	"""A stored summary, current or not, as (OsmData, version), or None."""
	summary = TileSummary.objects.filter(zoom=level, x=x, y=y).first()
	if summary is None:
		return None
	return decode(summary.data, summary.format), summary.version


def built_in(level, x0, x1, y0, y1):
	"""The stored summaries, current or not, at a level within a range of
	tiles (x0 to x1, y0 to y1, inclusive), as {(x, y)}: one query, asking for
	each column, so the index (zoom, x, y) finds each column's few rather than
	every summary of the level."""
	return set(TileSummary.objects.filter(zoom=level, x__in=range(x0, x1 + 1),
		y__range=(y0, y1)).values_list("x", "y"))


def missing(tiles):
	"""Those of the summaries (level, x, y) that aren't stored. One out of date
	(built with another recipe) can still be built on: whether it is out of
	date only decides whether it is built again (by the idle sweep), not
	whether what is made from it can go ahead, so a recipe change doesn't
	hold anything up until the rebuild reaches it."""
	return [tile for tile in tiles
		if not TileSummary.objects.filter(zoom=tile[0], x=tile[1], y=tile[2]).exists()]


def build_from_data(level, x, y, data, version):
	"""Builds and stores the summary of the base level's tile from its map data,
	unless one from this version of the map data is already stored. The base
	level is the finest zoom TILE_ZOOMS marks "summary"; no other zoom has
	a summary built from the map data."""
	if level != base_level():
		return
	if current(TileSummary.objects.filter(zoom=level, x=x, y=y, version=version)).exists():
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
