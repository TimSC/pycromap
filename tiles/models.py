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
	# Idle work (tiles.idle) is queued here with a negative priority, higher
	# rendered sooner; a tile claimed for requests (TileRequest) gets the time
	# it was claimed, so a negative priority marks an idle job
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
	# highest atomic edit ID of the objects it holds), how long it took, and the
	# tile, gzipped: for a cached zoom, the file it is in, relative to
	# TILE_CACHE_DIR, and otherwise the tile itself (neither for a tile with
	# nothing in it)
	rendered_at = models.DateTimeField(null=True, blank=True)
	data_version = models.BigIntegerField(null=True, blank=True)
	# When the map in a cached tile's area was last found unedited since the
	# tile was made (tiles.views._needs_rendering): it isn't asked again for
	# EDIT_CHECK_INTERVAL seconds after that, or after the tile was rendered
	edits_checked_at = models.DateTimeField(null=True, blank=True)
	render_seconds = models.FloatField(null=True, blank=True)
	file = models.CharField(max_length=255, blank=True)
	data = models.BinaryField(null=True, blank=True)

	class Meta:
		constraints = [
			models.UniqueConstraint(fields=["kind", "zoom", "x", "y"], name="tilestate_unique_job"),
		]
		indexes = [
			models.Index(fields=["status", "-priority"], name="tilestate_queue"),
			# The most recently requested tiles (tiles.idle._seeds), among the
			# few that have been requested at all
			models.Index(fields=["-requested_at"], condition=models.Q(request_count__gt=0), name="tilestate_requested"),
		]

	def __str__(self):
		return "{} {}/{}/{} ({})".format(self.kind, self.zoom, self.x, self.y, self.status)


class TileSweep(models.Model):
	"""The idle fleet's progress over the planet, one row per row of tiles.

	At (kind, zoom) the tiles are a 2^zoom by 2^zoom grid; this is row y of it.
	A renderer claims a row and rechecks which of its tiles are rendered, so
	the first gap is what's left to do (see tiles.idle); the row stays claimed
	while it has gaps, and is released, with a timestamp, once every tile in
	it is rendered.
	"""

	kind = models.CharField(max_length=16, choices=TileState.KINDS, default=TileState.TILE)
	zoom = models.PositiveSmallIntegerField()
	y = models.PositiveIntegerField()
	# The renderer working the row, and since when (a claim, as for jobs)
	claimed_by = models.CharField(max_length=100, blank=True)
	claimed_at = models.DateTimeField(null=True, blank=True)
	# When every tile in the row was last verified rendered
	last_checked = models.DateTimeField(null=True, blank=True)

	class Meta:
		constraints = [
			models.UniqueConstraint(fields=["kind", "zoom", "y"], name="tilesweep_unique_row"),
		]
		indexes = [
			models.Index(fields=["claimed_by", "last_checked"], name="tilesweep_claim"),
		]

	def __str__(self):
		return "sweep {} {}/{}".format(self.kind, self.zoom, self.y)


class TileRequest(models.Model):
	"""A request for a tile, while it is answered; and the queue of requested
	work. A request whose tile must be rendered waits (WAITING) until a
	renderer claims it, with every other request waiting for the same tile
	(CLAIMED), renders the tile, and answers them (DONE, or FAILED). The row
	is deleted once the request is answered, whatever the answer. One whose
	client went away first (Django cancels the view) is flagged cancelled,
	isn't claimed, and is kept CANCELLED_REQUEST_KEEP seconds; one left longer
	than a request waits (TILE_REQUEST_WAIT), as by a server that stopped, is
	deleted too (see queue.forget_requests).

	zoom, x and y are the tile to render: for a zoom cut from another
	(TILE_ZOOMS' "crop"), the tile it is cut from."""
	ANSWERING = ""         # being answered, with no rendering to wait for (yet)
	WAITING = "waiting"    # waiting for a renderer to claim it
	CLAIMED = "claimed"    # a renderer is rendering its tile
	DONE = "done"          # its tile has been rendered
	FAILED = "failed"      # its tile couldn't be rendered
	STATUSES = [(s, s or "answering") for s in (ANSWERING, WAITING, CLAIMED, DONE, FAILED)]

	zoom = models.PositiveSmallIntegerField()
	x = models.PositiveIntegerField()
	y = models.PositiveIntegerField()
	started_at = models.DateTimeField(auto_now_add=True)
	status = models.CharField(max_length=16, choices=STATUSES, default=ANSWERING, blank=True)
	# The renderer that claimed it, and when
	claimed_by = models.CharField(max_length=100, blank=True)
	claimed_at = models.DateTimeField(null=True, blank=True)
	# How many times its tile's rendering has failed while it waited: it is
	# answered as failed once that reaches RENDER_ATTEMPTS
	attempts = models.PositiveSmallIntegerField(default=0)
	# Being answered with a redirect to the tile's cached file: there is
	# nothing to render for it, and renderers don't claim it
	cache_redirect = models.BooleanField(default=False)
	# What its tile is made from directly, built or not, as [kind, zoom, x, y]
	# (queue.dependencies): none for a tile from the map data; the summaries
	# beneath it at its zoom's level for one from summaries
	dependencies = models.JSONField(default=list, blank=True)
	cancelled = models.BooleanField(default=False)
	cancelled_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		indexes = [
			# The queue: the newest waiting requests first
			models.Index(fields=["status", "-started_at"], name="tilerequest_queue"),
			# Every request for a tile, to claim and answer them together
			models.Index(fields=["zoom", "x", "y"], name="tilerequest_tile"),
			models.Index(fields=["cancelled_at"], name="tilerequest_cancelled"),
			models.Index(fields=["started_at"], name="tilerequest_started"),
		]

	def __str__(self):
		return "request {}/{}/{}{}{}{}".format(self.zoom, self.x, self.y,
			" " + self.status if self.status else "", " (cache redirect)" if self.cache_redirect else "",
			" (cancelled)" if self.cancelled else "")
