from django.db import models

from tiles.models import TileKeyed


class Recipe(models.Model):
	"""A way summaries have been built: a hash of summary.lua and the settings
	that shape summaries (see summaries.build.recipe), with those settings. A
	summary refers to its recipe by this row's small id, not the hash."""

	hash = models.CharField(max_length=64, unique=True)
	settings = models.TextField()  # the shaping settings, as JSON
	created_at = models.DateTimeField()

	def __str__(self):
		return "recipe {} ({})".format(self.pk, self.hash[:12])


class TileSummary(TileKeyed):
	"""A tile's summary at a level of the summary pyramid (zoom is the level):
	the objects of its area worth drawing at lower zooms, filtered by
	summary.lua, with their geometry simplified, as gzipped o5m. Whole objects
	are kept, so neighbouring summaries share objects; merging removes the
	duplicates. Keyed by its tile (tiles.models.TileKeyed)."""

	# How data is encoded: o5m, gzipped ("o5m.gz")
	format = models.CharField(max_length=16, default="o5m.gz")
	# What built it: summary.lua and the settings that shape summaries (see
	# summaries.build.recipe). One built another way is out of date, and is
	# built again when needed. Not indexed on its own: tilesummary_current
	# has it, after the key.
	recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, db_index=False, related_name="+")
	# The version of the map data it was built from (the highest atomic edit
	# ID of the objects it holds), separate from any rendered tile's version
	version = models.BigIntegerField()
	built_at = models.DateTimeField()

	nodes = models.PositiveIntegerField(default=0)
	ways = models.PositiveIntegerField(default=0)
	relations = models.PositiveIntegerField(default=0)

	# Last, so the columns before it are read without reading through it
	data = models.BinaryField()

	class Meta:
		indexes = [
			# Which summaries there are, and whether they are current, by
			# ranges of keys, from the index alone (build.built_in, build.missing,
			# tiles.idle), without reading the rows, which hold the data
			models.Index(fields=["id", "format", "recipe"], name="tilesummary_current"),
		]

	def __str__(self):
		return "summary {}/{}/{} v{}".format(self.zoom, self.x, self.y, self.version)
