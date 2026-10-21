"""Whether an area of the pycrocosm map has been edited since a version.

A version is an atomic edit ID: every object an edit makes is stamped with it
(atomic_edit_id, from schema 16), and every version an edit replaces or
deletes with it too (superseded_atomic_edit_id). A tile records the newest
edit of what it was made from (its data_version, see reader.read_area), so it
is out of date if, since that edit:
  - an object in its area was made or changed (the object's new version is
    in the modify layer's live tables), or
  - an object that was in its area was changed or deleted: moved away, its
    old version is where it was (the modify layer's old tables, or, for an
    object of the imported map, the static tables, with the edit recorded in
    the modify layer's ID tables).

Only the modify layer is ever edited, and it holds only what edits made, so
this asks little of the database however large the map is.

These are the queries of pgmap's DbChangedSince (pycrocosm's
replication/changed), run here directly on the map database; pycrocosm's
replicate tests check them with real edits, so the two should be kept the
same.

Not seen: a way or relation changed only through an edit outside the area,
as a way crossing it whose node outside it moves. pgmap copies such a parent
into the modify layer without an edit ID (it is a copy of a version the map
had), so nothing in the area has a newer edit. Edits made before the
columns existed have no IDs either, and are not seen.
"""

from django.conf import settings
from django.db import connections

from .reader import Layers

AREA = "ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326)"


def _conditions(layers):
	"""The SQL conditions, each an EXISTS, for something in AREA having
	changed since the edit %(version)s."""
	m = lambda name: layers.table(layers.modify_prefix, name)
	conditions = [
		# Made or changed since: the live versions in the modify layer
		"EXISTS (SELECT 1 FROM {} o WHERE o.atomic_edit_id > %(version)s AND o.geom && {A})".format(m("livenodes"), A=AREA),
		"EXISTS (SELECT 1 FROM {} o WHERE o.atomic_edit_id > %(version)s AND o.bbox && {A})".format(m("liveways"), A=AREA),
		# A relation's box may be two either side of the antimeridian, which
		# && sees as one round the world
		"EXISTS (SELECT 1 FROM {} o WHERE o.atomic_edit_id > %(version)s AND o.bbox && {A} "
			"AND ST_Intersects(o.bbox, {A}))".format(m("liverelations"), A=AREA),
		# Replaced or deleted since: where the old versions were. An old node
		# has its position; an old way or relation has no box, so where its
		# members are
		"EXISTS (SELECT 1 FROM {} o WHERE o.superseded_atomic_edit_id > %(version)s AND o.geom && {A})".format(
			m("oldnodes"), A=AREA),
		"EXISTS (SELECT 1 FROM {old} o WHERE o.superseded_atomic_edit_id > %(version)s AND EXISTS ("
			"SELECT 1 FROM jsonb_array_elements_text(o.members) e(ref) JOIN {nodes} n ON n.id = e.ref::bigint "
			"WHERE n.geom && {A}))".format(old=m("oldways"), nodes=m("visiblenodes"), A=AREA),
		"EXISTS (SELECT 1 FROM {old} o, jsonb_array_elements(o.members) e(member) WHERE "
			"o.superseded_atomic_edit_id > %(version)s AND ("
			"(e.member->>0 = 'node' AND EXISTS (SELECT 1 FROM {nodes} n WHERE n.id = (e.member->>1)::bigint "
			"AND n.geom && {A})) OR "
			"(e.member->>0 = 'way' AND EXISTS (SELECT 1 FROM {ways} w WHERE w.id = (e.member->>1)::bigint "
			"AND w.bbox && {A}))))".format(old=m("oldrelations"), nodes=m("visiblenodes"), ways=m("visibleways"), A=AREA),
	]
	if layers.static_prefix:
		s = lambda name: layers.table(layers.static_prefix, name)
		# Objects of the imported map the modify layer has taken over since:
		# where the static map has them
		for kind, place in (("node", "geom"), ("way", "bbox"), ("relation", "bbox")):
			exact = " AND ST_Intersects(o.bbox, {A})".format(A=AREA) if kind == "relation" else ""
			conditions.append(
				"EXISTS (SELECT 1 FROM {ids} i JOIN {live} o ON o.id = i.id WHERE "
				"i.superseded_atomic_edit_id > %(version)s AND o.{place} && {A}{exact})".format(
					ids=m(kind + "ids"), live=s("live" + kind + "s"), place=place, A=AREA, exact=exact))
	return conditions


def changed_since_sql(layers):
	"""One statement, so that it sees the map at one moment."""
	return "SELECT " + " OR ".join(_conditions(layers))


def changed_since(version, west, south, east, north, using="pycrocosm"):
	"""Whether the map within the bounds (in degrees) has been edited since
	the edit numbered version (0: since the map was imported)."""
	connection = connections[using]
	layers = Layers(connection, settings.PYCROCOSM_STATIC_PREFIX, settings.PYCROCOSM_MODIFY_PREFIX)
	with connection.cursor() as cursor:
		cursor.execute(changed_since_sql(layers), {"version": version or 0,
			"west": west, "south": south, "east": east, "north": north})
		return bool(cursor.fetchone()[0])
