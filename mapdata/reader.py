"""Reads an area of the pycrocosm map database (DATABASES["pycrocosm"]).

Like pgmap, it reads two layers of tables: the static layer (the imported
planet, PYCROCOSM_STATIC_PREFIX) and the modify layer of edits over it
(PYCROCOSM_MODIFY_PREFIX). An object is the modify layer's if that layer has
taken it over (its ID is in the layer's nodeids, wayids or relationids,
whether edited or deleted), and otherwise the static layer's. This is what
pgmap's <prefix>visible* views show, but either prefix can be chosen.

Everything is read in one repeatable-read transaction, with pgmap's tables
locked first, in pgmap's order, so the result is the map at one moment.

Ways and relations are found by their bounding boxes, which the map must
store. Ways are built from their nodes, which are fetched by ID where they lie
outside the area; multipolygons reaching outside the area have their other
ways fetched too, so that they are whole.
"""

import json

import pyo5m
from django.conf import settings
from django.db import connections, transaction

# The map schema versions whose tables this reader has been checked against.
# The views have named their columns since 15; 16 added atomic edit IDs.
SUPPORTED_SCHEMA_VERSIONS = {15, 16}

# pgmap's tables for a layer, in the order pgmap locks them (LockMap in
# pgmap.cpp). Everything using the map database must lock them in this order,
# in one statement, so that none can deadlock another.
LOCKED_TABLES = (
	"oldnodes", "oldways", "oldrelations", "livenodes", "liveways", "liverelations",
	"nodeids", "wayids", "relationids",
	"way_mems", "relation_mems_n", "relation_mems_w", "relation_mems_r",
	"nextids", "changesets", "meta", "usernames", "query_activity", "edit_activity",
)


class MapDataError(Exception):
	"""The map database can't be read as this reader expects."""


def _json(value):
	# Django has psycopg2 give jsonb columns back as text
	return json.loads(value) if isinstance(value, str) else value


def _tags(value):
	tags = _json(value) or {}
	return {str(k): str(v) for k, v in tags.items()}


class Layers:
	"""SQL for the objects the map shows, from the static and modify layers."""

	def __init__(self, connection, static_prefix, modify_prefix):
		self.quote = connection.ops.quote_name
		self.static_prefix = static_prefix
		self.modify_prefix = modify_prefix

	def prefixes(self):
		"""The layers' prefixes, static first, as pgmap locks them."""
		return [p for p in (self.static_prefix, self.modify_prefix) if p]

	def lock_sql(self, prefix):
		"""Locks a layer's tables as pgmap's readers do. ACCESS SHARE doesn't
		hold up pgmap's edits (which take EXCLUSIVE), only changes to the
		tables themselves."""
		tables = ", ".join(self.quote(prefix + t) for t in LOCKED_TABLES)
		return "LOCK TABLE {} IN ACCESS SHARE MODE".format(tables)

	def table(self, prefix, name):
		return self.quote(prefix + name)

	def objects(self, kind, columns, where):
		"""SELECT columns of the objects of a kind ("node", "way" or
		"relation") that match where, which may use named parameters. With
		no static prefix, the modify layer's views are read alone."""
		if not self.static_prefix:
			return "SELECT {} FROM {} WHERE {}".format(
				columns, self.table(self.modify_prefix, "visible" + kind + "s"), where)
		return ("SELECT {columns} FROM {parent} p WHERE ({where}) AND NOT EXISTS "
			"(SELECT 1 FROM {ids} x WHERE x.id = p.id) "
			"UNION ALL SELECT {columns} FROM {own} WHERE {where}").format(
			columns=columns, where=where,
			parent=self.table(self.static_prefix, "visible" + kind + "s"),
			ids=self.table(self.modify_prefix, kind + "ids"),
			own=self.table(self.modify_prefix, "live" + kind + "s"))


class AreaReader:
	def __init__(self, cursor, layers):
		self.cursor = cursor
		self.layers = layers
		self.data = pyo5m.OsmData()
		self.node_ids = set()
		self.way_ids = set()

	def check(self):
		"""Refuses tables this reader doesn't understand."""
		for prefix in self.layers.prefixes():
			meta = self.layers.table(prefix, "meta")
			self.cursor.execute("SELECT value FROM {} WHERE key = 'schema_version'".format(meta))
			row = self.cursor.fetchone()
			version = int(row[0]) if row else 0
			if version not in SUPPORTED_SCHEMA_VERSIONS:
				raise MapDataError("Map schema version {} ({}) is not supported".format(version, prefix))

			self.cursor.execute("SELECT count(*) FROM pg_attribute WHERE attrelid = to_regclass(%s) "
				"AND attname = 'bbox' AND NOT attisdropped", [self.layers.table(prefix, "liveways")])
			if self.cursor.fetchone()[0] == 0:
				raise MapDataError("The map does not store way and relation bounding boxes, which reading it needs")

	def data_version(self):
		"""The newest edit_activity ID of the modify layer: the version of the
		map data, which tiles built from it record."""
		self.cursor.execute("SELECT coalesce(max(id), 0) FROM {}".format(
			self.layers.table(self.layers.modify_prefix, "edit_activity")))
		return self.cursor.fetchone()[0]

	def read(self, west, south, east, north):
		box = {"west": west, "south": south, "east": east, "north": north}
		envelope = "ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326)"

		# Relations reaching into the area
		self.read_relations("bbox && " + envelope, box)

		# Ways reaching into the area, and the member ways of multipolygons,
		# which are needed to build them whole
		self.read_ways("bbox && " + envelope, box)
		missing_ways = set()
		for relation in self.data.relations:
			if relation.tags.get("type") != "multipolygon":
				continue
			for member in relation.members:
				if member.type == "way" and member.ref not in self.way_ids:
					missing_ways.add(member.ref)
		if missing_ways:
			self.read_ways("id = ANY(%(ids)s::bigint[])", {"ids": sorted(missing_ways)})

		# Nodes in the area, then any outside it that ways need, and
		# relations' node members (for labels)
		self.read_nodes("geom && " + envelope, box)
		missing_nodes = set()
		for way in self.data.ways:
			missing_nodes.update(ref for ref in way.refs if ref not in self.node_ids)
		for relation in self.data.relations:
			missing_nodes.update(m.ref for m in relation.members
				if m.type == "node" and m.ref not in self.node_ids)
		if missing_nodes:
			self.read_nodes("id = ANY(%(ids)s::bigint[])", {"ids": sorted(missing_nodes)})

		self.data.bounds.append(pyo5m.Bounds(west, south, east, north))
		# Sorted by ID, as PBF readers expect
		for objects in (self.data.nodes, self.data.ways, self.data.relations):
			objects.sort(key=lambda o: o.id)
		return self.data

	def read_nodes(self, where, params):
		self.cursor.execute(self.layers.objects("node", "id, ST_Y(geom), ST_X(geom), tags", where), params)
		for id, lat, lon, tags in self.cursor.fetchall():
			if id in self.node_ids:
				continue
			self.node_ids.add(id)
			self.data.nodes.append(pyo5m.Node(id, lat, lon, _tags(tags)))

	def read_ways(self, where, params):
		self.cursor.execute(self.layers.objects("way", "id, tags, members", where), params)
		for id, tags, members in self.cursor.fetchall():
			if id in self.way_ids:
				continue
			self.way_ids.add(id)
			refs = [int(ref) for ref in _json(members) or []]
			self.data.ways.append(pyo5m.Way(id, refs, _tags(tags)))

	def read_relations(self, where, params):
		self.cursor.execute(self.layers.objects("relation", "id, tags, members, memberroles", where), params)
		for id, tags, members, roles in self.cursor.fetchall():
			members = _json(members) or []
			roles = _json(roles) or []
			self.data.relations.append(pyo5m.Relation(id, [
				pyo5m.Member(str(member[0]), int(member[1]), str(roles[i]) if i < len(roles) else "")
				for i, member in enumerate(members)], _tags(tags)))


def read_area(west, south, east, north, using="pycrocosm"):
	"""The map data within the bounds (in degrees), as a pyo5m OsmData, and the
	version of the map data it is from."""
	connection = connections[using]
	layers = Layers(connection, settings.PYCROCOSM_STATIC_PREFIX, settings.PYCROCOSM_MODIFY_PREFIX)
	with transaction.atomic(using=using):
		with connection.cursor() as cursor:
			# Before any query: the snapshot is only taken at the first
			# query, so it is taken once the locks are held
			cursor.execute("SET TRANSACTION READ ONLY")
			for prefix in layers.prefixes():
				cursor.execute(layers.lock_sql(prefix))

			reader = AreaReader(cursor, layers)
			reader.check()
			version = reader.data_version()
			data = reader.read(west, south, east, north)
	return data, version
