from django.test import SimpleTestCase

from mapdata.reader import LOCKED_TABLES, Layers, needs_whole


class FakeOps:
	def quote_name(self, name):
		return '"{}"'.format(name)


class FakeConnection:
	ops = FakeOps()


class LayerTests(SimpleTestCase):
	def test_locks_in_pgmap_order_static_first(self):
		layers = Layers(FakeConnection(), "static_", "mod_")
		self.assertEqual(layers.prefixes(), ["static_", "mod_"])
		sql = layers.lock_sql("static_")
		self.assertTrue(sql.startswith('LOCK TABLE "static_oldnodes", "static_oldways", "static_oldrelations", "static_livenodes"'))
		self.assertTrue(sql.endswith('"static_query_activity", "static_edit_activity" IN ACCESS SHARE MODE'))
		self.assertEqual(len(LOCKED_TABLES), 19)

	def test_modify_layer_takes_over_static(self):
		sql = Layers(FakeConnection(), "static_", "mod_").objects("way", "id, tags", "bbox && x")
		self.assertIn('FROM "static_visibleways" p WHERE (bbox && x) AND NOT EXISTS (SELECT 1 FROM "mod_wayids" x WHERE x.id = p.id)', sql)
		self.assertIn('UNION ALL SELECT id, tags FROM "mod_liveways" WHERE bbox && x', sql)

	def test_without_static_prefix_reads_the_modify_views(self):
		layers = Layers(FakeConnection(), "", "mod_")
		self.assertEqual(layers.prefixes(), ["mod_"])
		self.assertEqual(layers.objects("node", "id", "true"), 'SELECT id FROM "mod_visiblenodes" WHERE true')


class NeedsWholeTests(SimpleTestCase):
	def test_areas_are_read_whole(self):
		for tags in ({"natural": "water"}, {"landuse": "forest", "name": "Big Wood"}, {"place": "island"},
				{"boundary": "national_park"}, {"boundary": "protected_area"}, {"type": "multipolygon"},
				{"boundary": "administrative", "admin_level": "8", "place": "island"}):
			self.assertTrue(needs_whole(tags), tags)

	def test_boundaries_alone_are_not(self):
		for tags in ({"boundary": "administrative", "admin_level": "2", "name": "United States of America",
				"maritime": "yes"}, {"boundary": "postal_code", "postal_code": "PO1"},
				# Labelled from its node, not its area
				{"boundary": "administrative", "admin_level": "2", "place": "country", "name": "Canada"}):
			self.assertFalse(needs_whole(tags), tags)


class ChangedSinceSqlTests(SimpleTestCase):
	def test_both_layers(self):
		from mapdata.changes import changed_since_sql
		sql = changed_since_sql(Layers(FakeConnection(), "s_", "m_"))
		# New or changed versions, old ones where they were, and the imported
		# map's objects taken over, in one statement
		for table in ('"m_livenodes"', '"m_liveways"', '"m_liverelations"', '"m_oldnodes"', '"m_oldways"',
				'"m_oldrelations"', '"m_nodeids"', '"s_livenodes"', '"m_wayids"', '"s_liveways"',
				'"m_relationids"', '"s_liverelations"'):
			self.assertIn(table, sql)
		self.assertTrue(sql.startswith("SELECT EXISTS"))
		# 9 conditions; old ways look up their nodes, old relations their nodes and ways
		self.assertEqual(sql.count("EXISTS (SELECT 1 FROM"), 9 + 3)

	def test_modify_layer_alone(self):
		from mapdata.changes import changed_since_sql
		sql = changed_since_sql(Layers(FakeConnection(), "", "m_"))
		self.assertNotIn("ids", sql)
		self.assertIn('"m_livenodes"', sql)
