"""Reads the pycrocosm map through pycrocosm's web API (MAP_SOURCE "api"), for
a pycromap that can't reach the map database: what mapdata.reader and
mapdata.changes do directly on the database.

An area is read with Overpass (api/interpreter), as pycrocosm's Overpass
finds ways and relations by their stored bounding boxes, as the reader does,
so a multipolygon covering an area without a member in it is found too. The
API's map call (api/0.6/map) finds them only through members in the area.
A second query fetches what the reader would have too: the other member
ways of multipolygons drawn whole (reader.needs_whole), and relations' node
members.

The version of what is read is the map's latest atomic edit as the read
begins (replication/edit_ids), not the newest edit of the objects read, as
the API doesn't give objects' edits: an edit made after it, in the area, is
seen by changed_since (replication/changed).
"""

import gzip
import io
import xml.etree.ElementTree as ET
from urllib.error import URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import pyo5m
from django.conf import settings

from .reader import needs_whole


class ApiError(Exception):
	"""The pycrocosm API couldn't be asked, or its answer couldn't be read."""


def _ask(path, params=None, form=None):
	"""The body of the answer to a GET (with params) or, with form, a POST to
	the API's path."""
	url = urljoin(settings.PYCROCOSM_API_URL, path)
	if params:
		url += "?" + urlencode(params)
	request = Request(url, data=urlencode(form).encode() if form else None,
		headers={"Accept-Encoding": "gzip", "User-Agent": "pycromap"})
	try:
		with urlopen(request, timeout=settings.RENDER_TIMEOUT) as response:
			body = response.read()
			if response.headers.get("Content-Encoding") == "gzip":
				body = gzip.decompress(body)
	except (URLError, OSError, ValueError) as e:
		raise ApiError("{}: {}".format(url, e)) from e
	return body


def _attribute(path, name, params=None):
	"""An attribute of the root element of the API's XML answer."""
	try:
		value = ET.fromstring(_ask(path, params)).get(name)
	except ET.ParseError as e:
		raise ApiError("{}: {}".format(path, e)) from e
	if value is None:
		raise ApiError("{}: no {} in the answer".format(path, name))
	return value


def _overpass(statements):
	"""The map data an Overpass script's statements find, as OsmData."""
	script = "[out:xml][timeout:{}];{}out;".format(settings.RENDER_TIMEOUT, statements)
	data = pyo5m.OsmData()
	try:
		pyo5m.decoder(io.BytesIO(_ask("api/interpreter", form={"data": script})), data, "osm").run()
	except (pyo5m.DecodeError, ET.ParseError) as e:
		raise ApiError("api/interpreter: {}".format(e)) from e
	return data


def latest_edit():
	"""The map's latest atomic edit ID."""
	return int(_attribute("replication/edit_ids", "atomic_edit_id"))


def read_area(west, south, east, north):
	"""The map data within the bounds (in degrees), as a pyo5m OsmData, and
	the version of the map data it is from: as mapdata.reader.read_area,
	through the API."""
	version = latest_edit()
	box = "{!r},{!r},{!r},{!r}".format(south, west, north, east)
	# Nodes in the area, ways and relations reaching into it (by their boxes),
	# and the nodes of the ways
	data = _overpass("(node({b});way({b});)->.a;(.a;>;)->.b;rel({b})->.r;(.b;.r;);".format(b=box))

	way_ids = {way.id for way in data.ways}
	node_ids = {node.id for node in data.nodes}
	missing_ways = sorted({member.ref for relation in data.relations
		if relation.tags.get("type") == "multipolygon" and needs_whole(relation.tags)
		for member in relation.members if member.type == "way" and member.ref not in way_ids})
	missing_nodes = sorted({member.ref for relation in data.relations
		for member in relation.members if member.type == "node" and member.ref not in node_ids})
	if missing_ways or missing_nodes:
		statements = "".join("{}(id:{});".format(kind, ",".join(map(str, ids)))
			for kind, ids in (("way", missing_ways), ("node", missing_nodes)) if ids)
		more = _overpass("({});(._;>;);".format(statements))
		data.ways += [way for way in more.ways if way.id not in way_ids]
		data.nodes += [node for node in more.nodes if node.id not in node_ids]

	data.bounds.append(pyo5m.Bounds(west, south, east, north))
	# Sorted by ID, as PBF readers expect
	for objects in (data.nodes, data.ways, data.relations):
		objects.sort(key=lambda o: o.id)
	return data, version


def changed_since(version, west, south, east, north):
	"""Whether the map within the bounds (in degrees) has been edited since
	the edit numbered version: as mapdata.changes.changed_since, through the
	API."""
	bbox = ",".join(repr(value) for value in (west, south, east, north))
	return _attribute("replication/changed", "changed", {"bbox": bbox, "since": version or 0}) == "true"
