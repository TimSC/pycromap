from django.test import SimpleTestCase

from mapdata.reader import LOCKED_TABLES, Layers


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
