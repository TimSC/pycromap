from django.db import models


class TileSummary(models.Model):
	"""A tile's summary at a level of the summary pyramid: the objects of its
	area worth drawing at lower zooms, filtered by summary.lua, with their
	geometry simplified, as gzipped o5m. Whole objects are kept, so neighbouring
	summaries share objects; merging removes the duplicates."""

	zoom = models.PositiveSmallIntegerField()  # the summary level
	x = models.PositiveIntegerField()
	y = models.PositiveIntegerField()

	data = models.BinaryField()
	# How data is encoded: o5m, gzipped ("o5m.gz")
	format = models.CharField(max_length=16, default="o5m.gz")
	# What built it: a hash of summary.lua and the settings that shape
	# summaries (see summaries.build.recipe). One built another way is out of
	# date, and is built again when needed.
	recipe = models.CharField(max_length=64, default="")
	# The version of the map data it was built from (the highest atomic edit
	# ID of the objects it holds), separate from any rendered tile's version
	version = models.BigIntegerField()
	built_at = models.DateTimeField()

	nodes = models.PositiveIntegerField(default=0)
	ways = models.PositiveIntegerField(default=0)
	relations = models.PositiveIntegerField(default=0)

	class Meta:
		constraints = [
			models.UniqueConstraint(fields=["zoom", "x", "y"], name="tilesummary_unique_tile"),
		]
		indexes = [
			# Which summaries there are, and whether they are current, from
			# the index alone (build.missing, tiles.idle): in the table,
			# format and recipe come after the data, and reading them means
			# reading through it
			models.Index(fields=["zoom", "x", "y", "format", "recipe"], name="tilesummary_current"),
		]

	def __str__(self):
		return "summary {}/{}/{} v{}".format(self.zoom, self.x, self.y, self.version)
