from django.db import models


class TileKeyField(models.BigIntegerField):
	"""A table's primary key that is a tile's key (tilegeo.tile_key), not a
	counter. On SQLite its column is "integer", so that it is the table's
	rowid: the table is stored in key order, and needs no index of its own.
	Elsewhere a bigint."""

	def __init__(self, *args, **kwargs):
		kwargs["primary_key"] = True
		super().__init__(*args, **kwargs)

	def deconstruct(self):
		name, path, args, kwargs = super().deconstruct()
		del kwargs["primary_key"]
		return name, path, args, kwargs

	def db_type(self, connection):
		if connection.vendor == "sqlite":
			return "integer"
		return super().db_type(connection)
