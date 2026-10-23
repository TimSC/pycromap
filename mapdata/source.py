"""Where the map data comes from (MAP_SOURCE): the pycrocosm map database
directly ("database": mapdata.reader and mapdata.changes), or pycrocosm's web
API ("api": mapdata.api)."""

from django.conf import settings
from django.db import DatabaseError

from . import api, changes, reader

# What reading the map raises when it can't be read, whichever the source
UNAVAILABLE = (DatabaseError, api.ApiError)


def _from_api():
	return settings.MAP_SOURCE == "api"


def read_area(west, south, east, north):
	"""The map data within the bounds (in degrees), and its version (see
	mapdata.reader.read_area)."""
	return (api if _from_api() else reader).read_area(west, south, east, north)


def changed_since(version, west, south, east, north):
	"""Whether the map within the bounds has been edited since the version
	(see mapdata.changes.changed_since)."""
	return (api if _from_api() else changes).changed_since(version, west, south, east, north)
