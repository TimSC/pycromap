"""The render queue, kept in TileState rows.

Requests queue tiles; renderers (manage.py render_tiles) claim them, highest
priority first, and record the results. A claim is a compare-and-set update,
so any number of renderers can share the queue, even on SQLite.
"""

import os
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import TileState


def zoom_config(zoom):
	"""How a zoom is made (see TILE_ZOOMS), or None if it isn't served."""
	return settings.TILE_ZOOMS.get(zoom)


def tile_file(zoom, x, y, version):
	"""Where a rendering of a tile goes, relative to TILE_CACHE_DIR. The data
	version is in the name, so a newer rendering never changes a file that
	is being served."""
	return os.path.join(str(zoom), str(x), "{}.{}.pbf".format(y, version))


def request_tile(zoom, x, y, rerender):
	"""Records a request for a tile and, if it needs rendering, queues it
	ahead of everything requested earlier. rerender asks for a new rendering
	even if the tile has one. Returns the tile's state, and when it was
	queued (None if it wasn't)."""
	now = timezone.now()
	with transaction.atomic():
		# A new tile has no rendering, so it is queued below
		tile, _ = TileState.objects.get_or_create(zoom=zoom, x=x, y=y,
			defaults={"status": TileState.DONE})
		tile.requested_at = now
		tile.request_count = F("request_count") + 1
		queued_at = None
		if tile.status in (TileState.QUEUED, TileState.RENDERING) and tile.queued_at:
			# Already on its way: wait for that rendering
			queued_at = tile.queued_at
			tile.priority = time.time()
		elif rerender or tile.status == TileState.FAILED or tile.rendered_at is None:
			tile.status = TileState.QUEUED
			tile.priority = time.time()
			tile.queued_at = queued_at = now
			tile.attempts = 0
			tile.error = ""
		tile.save()
		tile.refresh_from_db()
	return tile, queued_at


def wait_for_tile(tile_id, queued_at, timeout):
	"""Waits until the tile has been rendered since queued_at. Returns its
	state, or None if it wasn't ready in time."""
	deadline = time.monotonic() + timeout
	while True:
		tile = TileState.objects.get(pk=tile_id)
		if tile.status == TileState.DONE and tile.rendered_at and tile.rendered_at >= queued_at:
			return tile
		if tile.status == TileState.FAILED or time.monotonic() >= deadline:
			return None
		time.sleep(0.05)


def requeue_abandoned():
	"""Gives tiles claimed by renderers that have gone quiet to others."""
	cutoff = timezone.now() - timedelta(seconds=settings.RENDER_LEASE)
	return TileState.objects.filter(status=TileState.RENDERING, claimed_at__lt=cutoff).update(
		status=TileState.QUEUED, claimed_by="", claimed_at=None)


def claim_next(worker):
	"""Claims the highest priority queued tile for a renderer, or returns None
	if there is none."""
	while True:
		candidate = (TileState.objects.filter(status=TileState.QUEUED)
			.order_by("-priority", "queued_at").values_list("pk", flat=True).first())
		if candidate is None:
			return None
		claimed = TileState.objects.filter(pk=candidate, status=TileState.QUEUED).update(
			status=TileState.RENDERING, claimed_by=worker, claimed_at=timezone.now(),
			attempts=F("attempts") + 1)
		if claimed:
			return TileState.objects.get(pk=candidate)
		# Another renderer got it first; try the next


def finish(tile, data, version, seconds):
	"""Records a rendering, writing the tile to TILE_CACHE_DIR."""
	path = ""
	if data:
		path = tile_file(tile.zoom, tile.x, tile.y, version)
		full = os.path.join(settings.TILE_CACHE_DIR, path)
		os.makedirs(os.path.dirname(full), exist_ok=True)
		temporary = full + ".tmp{}".format(os.getpid())
		with open(temporary, "wb") as f:
			f.write(data)
		os.replace(temporary, full)
	TileState.objects.filter(pk=tile.pk).update(status=TileState.DONE, rendered_at=timezone.now(),
		data_version=version, render_seconds=seconds, file=path, error="", claimed_by="", claimed_at=None)


def fail(tile, error):
	"""Records a failed rendering: the tile is queued again, until it has
	failed RENDER_ATTEMPTS times."""
	tile.refresh_from_db()
	status = TileState.FAILED if tile.attempts >= settings.RENDER_ATTEMPTS else TileState.QUEUED
	TileState.objects.filter(pk=tile.pk).update(status=status, error=str(error)[:2000],
		claimed_by="", claimed_at=None)
