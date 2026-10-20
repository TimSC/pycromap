"""Building tile summaries.

A summary keeps the objects of a tile's area that summary.lua picks for its
level, whole, with their geometry simplified. The base level (the finest in
SUMMARY_LEVELS, 13) is built from the map data; each coarser level from the
summaries of the next finer level beneath it, merged.
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
from shapely.geometry import LineString

from .lua import summary_filter
from .models import TileSummary


# The encoding of summaries' data: o5m, gzipped
FORMAT = "o5m.gz"


_recipes = {}


def recipe():
	"""What summaries are built with: a hash of summary.lua and the settings
	that shape them. A summary with another recipe is out of date."""
	shaping = json.dumps({"levels": levels(), "simplify": settings.SUMMARY_SIMPLIFY_PIXELS,
		"format": FORMAT}, sort_keys=True)
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
	"""The summaries of a queryset built as summaries now are."""
	return queryset.filter(format=FORMAT, recipe=recipe())


# ---- The pyramid

def levels():
	"""The summary levels, finest first."""
	return sorted(settings.SUMMARY_LEVELS, reverse=True)


def base_level():
	return levels()[0]


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


def tolerance(level):
	"""How far, in degrees of Web Mercator, simplified geometry may stray: a
	fraction (SUMMARY_SIMPLIFY_PIXELS) of a pixel of a 4096-pixel tile at the
	level."""
	return 360.0 / (1 << level) / 4096 * settings.SUMMARY_SIMPLIFY_PIXELS


def simplify_refs(refs, coords, tol):
	"""A way's node IDs, fewer: those of the vertices that simplifying its line
	keeps. Nodes without a location are left out."""
	refs = [ref for ref in refs if ref in coords]
	if len(refs) < 3 or tol <= 0:
		return refs
	points = [coords[ref] for ref in refs]
	kept = LineString(points).simplify(tol, preserve_topology=False).coords
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


def summarize(data, level):
	"""The objects of data worth keeping at a level, with simplified geometry."""
	keep = summary_filter()
	tol = tolerance(level)
	coords = {node.id: (node.lon, _latp(node.lat)) for node in data.nodes}

	relations = []
	for relation in data.relations:
		tags = keep("relation", relation.tags, level)
		if tags is not None:
			relations.append((relation, tags))
	relation_ids = {relation.id for relation, _ in relations}
	member_ways = {m.ref for relation, _ in relations for m in relation.members if m.type == "way"}

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
	"""A current stored summary, as (OsmData, version), or None."""
	summary = current(TileSummary.objects.filter(zoom=level, x=x, y=y)).first()
	if summary is None:
		return None
	return decode(summary.data, summary.format), summary.version


def missing(tiles):
	"""Those of the summaries (level, x, y) that aren't stored, or are out of
	date."""
	return [tile for tile in tiles
		if not current(TileSummary.objects.filter(zoom=tile[0], x=tile[1], y=tile[2])).exists()]


def obtainable(level, x, y, depth):
	"""Whether a missing summary could be built by a job at depth (see
	TileState.depth): one at the base level from the map data, any other from
	the summaries beneath it, those missing built by jobs one deeper. Jobs
	aren't made deeper than SUMMARY_MAX_DEPTH, so a request for a coarse tile
	never sets off building the pyramid beneath it."""
	if depth > settings.SUMMARY_MAX_DEPTH:
		return False
	if level == base_level():
		return True
	return all(obtainable(*tile, depth + 1)
		for tile in missing(tiles_beneath(level, x, y, finer_level(level))))


def build_from_data(level, x, y, data, version):
	"""Builds and stores the summary of the base level's tile from its map data,
	unless one from this version of the map data is already stored."""
	if current(TileSummary.objects.filter(zoom=level, x=x, y=y, version=version)).exists():
		return
	store(level, x, y, summarize(data, level), version)


def build_from_finer(level, x, y):
	"""Builds and stores a summary from the summaries beneath it at the next
	finer level. Returns the tiles of those that are missing, if any, without
	building."""
	beneath = tiles_beneath(level, x, y, finer_level(level))
	absent = missing(beneath)
	if absent:
		return absent
	loaded = [load(*tile) for tile in beneath]
	store(level, x, y, summarize(merge(loaded), level), max(version for _, version in loaded))
	return []
