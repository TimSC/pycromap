from django.db import models


class TileState(models.Model):
	"""A job in the render queue, and its result: rendering a tile (made the
	first time the tile is asked for), or building a tile's summary (made
	when something needs it; the summary itself is a TileSummary)."""

	TILE = "tile"
	SUMMARY = "summary"
	KINDS = [(k, k) for k in (TILE, SUMMARY)]

	QUEUED = "queued"        # waiting for a renderer
	RENDERING = "rendering"  # a renderer has claimed it
	DONE = "done"            # rendered
	FAILED = "failed"        # rendering failed RENDER_ATTEMPTS times
	STATUSES = [(s, s) for s in (QUEUED, RENDERING, DONE, FAILED)]

	kind = models.CharField(max_length=16, choices=KINDS, default=TILE)
	zoom = models.PositiveSmallIntegerField()  # for a summary, its level
	x = models.PositiveIntegerField()
	y = models.PositiveIntegerField()

	status = models.CharField(max_length=16, choices=STATUSES, default=QUEUED)
	# Higher is rendered sooner. For now, the time it was last asked for, so
	# that the most recently requested tiles come first.
	priority = models.FloatField(default=0)
	# How many summary jobs lie between this job and the request it is for: 0
	# for a tile, 1 for a summary a tile needs, and so on. Jobs aren't made
	# deeper than SUMMARY_MAX_DEPTH.
	depth = models.PositiveSmallIntegerField(default=0)
	# Queued by an idle renderer, rather than for a request (see tiles.idle)
	idle = models.BooleanField(default=False)
	queued_at = models.DateTimeField(null=True, blank=True)
	# A job waiting for the summaries it needs: since when, and not to be
	# claimed again before not_before
	waiting_since = models.DateTimeField(null=True, blank=True)
	not_before = models.DateTimeField(null=True, blank=True)

	requested_at = models.DateTimeField(null=True, blank=True)
	request_count = models.PositiveIntegerField(default=0)

	# The renderer working on it, and since when
	claimed_by = models.CharField(max_length=100, blank=True)
	claimed_at = models.DateTimeField(null=True, blank=True)
	attempts = models.PositiveSmallIntegerField(default=0)
	error = models.TextField(blank=True)

	# The last rendering: when, from which version of the map data (the
	# newest edit_activity ID of the modify layer), how long it took, and the
	# tile, gzipped: for a cached zoom, the file it is in, relative to
	# TILE_CACHE_DIR, and otherwise the tile itself (neither for a tile with
	# nothing in it)
	rendered_at = models.DateTimeField(null=True, blank=True)
	data_version = models.BigIntegerField(null=True, blank=True)
	render_seconds = models.FloatField(null=True, blank=True)
	file = models.CharField(max_length=255, blank=True)
	data = models.BinaryField(null=True, blank=True)

	class Meta:
		constraints = [
			models.UniqueConstraint(fields=["kind", "zoom", "x", "y"], name="tilestate_unique_job"),
		]
		indexes = [
			models.Index(fields=["status", "-priority"], name="tilestate_queue"),
		]

	def __str__(self):
		return "{} {}/{}/{} ({})".format(self.kind, self.zoom, self.x, self.y, self.status)
