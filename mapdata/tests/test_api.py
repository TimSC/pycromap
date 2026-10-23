import io
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase, override_settings

from mapdata import api, source

EDIT_IDS = b"<?xml version='1.0' encoding='UTF-8'?><editids edit_activity_id='14' atomic_edit_id='11' />"

# The area: a node in it, a way across it, a lake (whose other member way is
# outside) and a boundary (whose other member is not wanted)
AREA = b"""<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">
<node id="3" lat="50.1" lon="-1.1"/><node id="1" lat="50.0" lon="-1.0"/><node id="2" lat="50.0" lon="-0.9"/>
<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/></way>
<relation id="20"><member type="way" ref="10" role="outer"/><member type="way" ref="11" role="outer"/>
<tag k="type" v="multipolygon"/><tag k="natural" v="water"/></relation>
<relation id="21"><member type="way" ref="12" role="outer"/><member type="node" ref="5" role="label"/>
<tag k="type" v="boundary"/><tag k="boundary" v="administrative"/></relation>
</osm>"""

MORE = b"""<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">
<node id="4" lat="50.2" lon="-1.2"/><node id="5" lat="50.3" lon="-1.3"><tag k="place" v="town"/></node>
<way id="11"><nd ref="4"/><nd ref="1"/></way>
</osm>"""


class Answer(io.BytesIO):
	headers = {}

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


@override_settings(PYCROCOSM_API_URL="http://pycrocosm.test/", RENDER_TIMEOUT=60)
class ApiTests(SimpleTestCase):
	def answering(self, *bodies):
		"""Patches the API's answers, in order, keeping the requests."""
		self.requests = []
		answers = iter(bodies)

		def urlopen(request, timeout):
			self.requests.append(request)
			return Answer(next(answers))
		return mock.patch("mapdata.api.urlopen", side_effect=urlopen)

	def script(self, request):
		return parse_qs(request.data.decode())["data"][0]

	def test_reads_an_area_with_what_the_reader_would_add(self):
		with self.answering(EDIT_IDS, AREA, MORE):
			data, version = api.read_area(-1.05, 49.95, -0.85, 50.05)
		self.assertEqual(version, 11)  # the map's latest edit, as the read began
		self.assertEqual(urlparse(self.requests[0].full_url).path, "/replication/edit_ids")
		area = self.script(self.requests[1])
		self.assertIn("way(49.95,-1.05,50.05,-0.85)", area)
		self.assertIn("rel(49.95,-1.05,50.05,-0.85)", area)
		# The lake's other member way, and the boundary's label node; not
		# the boundary's other way
		more = self.script(self.requests[2])
		self.assertIn("way(id:11);", more)
		self.assertIn("node(id:5);", more)
		self.assertNotIn("12", more)
		self.assertEqual([n.id for n in data.nodes], [1, 2, 3, 4, 5])  # sorted, as the reader's
		self.assertEqual([w.id for w in data.ways], [10, 11])
		self.assertEqual([r.id for r in data.relations], [20, 21])
		self.assertEqual(len(data.bounds), 1)

	def test_one_query_when_nothing_more_is_needed(self):
		simple = b'<osm version="0.6"><node id="1" lat="50.0" lon="-1.0"/></osm>'
		with self.answering(EDIT_IDS, simple):
			data, _ = api.read_area(-1.05, 49.95, -0.85, 50.05)
		self.assertEqual(len(self.requests), 2)
		self.assertEqual([n.id for n in data.nodes], [1])

	def test_changed_since(self):
		answer = b"<changed bbox='-1.0,50.0,-0.9,50.1' since='7' changed='true' edit_activity_id='14' atomic_edit_id='11'/>"
		with self.answering(answer):
			self.assertTrue(api.changed_since(7, -1.0, 50.0, -0.9, 50.1))
		query = parse_qs(urlparse(self.requests[0].full_url).query)
		self.assertEqual((query["bbox"], query["since"]), (["-1.0,50.0,-0.9,50.1"], ["7"]))
		with self.answering(answer.replace(b"'true'", b"'false'")):
			self.assertFalse(api.changed_since(None, -1.0, 50.0, -0.9, 50.1))
		self.assertEqual(parse_qs(urlparse(self.requests[0].full_url).query)["since"], ["0"])

	def test_failures_are_api_errors(self):
		with mock.patch("mapdata.api.urlopen", side_effect=URLError("refused")):
			with self.assertRaises(api.ApiError):
				api.read_area(-1.05, 49.95, -0.85, 50.05)
		with self.answering(b"not xml"):
			with self.assertRaises(api.ApiError):
				api.latest_edit()
		self.assertTrue(issubclass(api.ApiError, source.UNAVAILABLE))


class SourceTests(SimpleTestCase):
	def test_the_setting_picks_the_source(self):
		with mock.patch("mapdata.api.read_area", return_value="api") as from_api, \
				mock.patch("mapdata.reader.read_area", return_value="database") as from_database:
			with override_settings(MAP_SOURCE="api"):
				self.assertEqual(source.read_area(0, 0, 1, 1), "api")
			with override_settings(MAP_SOURCE="database"):
				self.assertEqual(source.read_area(0, 0, 1, 1), "database")
		from_api.assert_called_once(); from_database.assert_called_once()
		with mock.patch("mapdata.api.changed_since", return_value=True), override_settings(MAP_SOURCE="api"):
			self.assertTrue(source.changed_since(3, 0, 0, 1, 1))
