from django.db import models


class TileState(models.Model):
	"""A tile: how far rendering it has got, and its rendered result. A row is
	made the first time the tile is asked for."""

	QUEUED = "queued"        # waiting for a renderer
	RENDERING = "rendering"  # a renderer has claimed it
	DONE = "done"            # rendered
	FAILED = "failed"        # rendering failed RENDER_ATTEMPTS times
	STATUSES = [(s, s) for s in (QUEUED, RENDERING, DONE, FAILED)]

	zoom = models.PositiveSmallIntegerField()
	x = models.PositiveIntegerField()
	y = models.PositiveIntegerField()

	status = models.CharField(max_length=16, choices=STATUSES, default=QUEUED)
	# Higher is rendered sooner. For now, the time it was last asked for, so
	# that the most recently requested tiles come first.
	priority = models.FloatField(default=0)
	queued_at = models.DateTimeField(null=True, blank=True)

	requested_at = models.DateTimeField(null=True, blank=True)
	request_count = models.PositiveIntegerField(default=0)

	# The renderer working on it, and since when
	claimed_by = models.CharField(max_length=100, blank=True)
	claimed_at = models.DateTimeField(null=True, blank=True)
	attempts = models.PositiveSmallIntegerField(default=0)
	error = models.TextField(blank=True)

	# The last rendering: when, from which version of the map data (the
	# newest edit_activity ID of the modify layer), how long it took, and the
	# file it is in, relative to TILE_CACHE_DIR ("" for a tile with nothing
	# in it)
	rendered_at = models.DateTimeField(null=True, blank=True)
	data_version = models.BigIntegerField(null=True, blank=True)
	render_seconds = models.FloatField(null=True, blank=True)
	file = models.CharField(max_length=255, blank=True)

	class Meta:
		constraints = [
			models.UniqueConstraint(fields=["zoom", "x", "y"], name="tilestate_unique_tile"),
		]
		indexes = [
			models.Index(fields=["status", "-priority"], name="tilestate_queue"),
		]

	def __str__(self):
		return "{}/{}/{} ({})".format(self.zoom, self.x, self.y, self.status)
