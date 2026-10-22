"""The render queue, kept in TileState rows.

Requests queue tiles; renderers (manage.py render_tiles) claim them, highest
priority first, and record the results. Jobs that need summaries which don't
exist yet queue jobs to build them, ahead of themselves, and step back until
they are built. A claim is a compare-and-set update,
so any number of renderers can share the queue, even on SQLite.
"""

import asyncio
import os
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import TileRequest, TileState
from .common import COMMON_DIR, common_file

# Where tiles that many others were the same as were kept, under
# TILE_CACHE_DIR, by the hash of their content, before common tiles (see
# tiles.common) were chosen by hand. Tiles may still be using them.
SHARED_DIR = "shared"


def zoom_config(zoom):
	"""How a zoom is made (see TILE_ZOOMS), or None if it isn't served."""
	return settings.TILE_ZOOMS.get(zoom)


def span(moment):
	"""A moment as a span from now: '42s ago', '4m ago', 'in 30s'."""
	if moment is None:
		return ""
	seconds = (moment - timezone.now()).total_seconds()
	sign = "in" if seconds >= 0 else "ago"
	seconds = abs(seconds)
	if seconds < 90:
		return "{}s {}".format(int(seconds), sign)
	if seconds < 90 * 60:
		return "{}m {}".format(int(seconds // 60), sign)
	return "{}h {}".format(int(seconds // (60 * 60)), sign)


def tile_file(zoom, x, y, version):
	"""Where a cached zoom's rendering of a tile goes, relative to
	TILE_CACHE_DIR: a gzipped vector tile. The data version is in the name, so
	a newer rendering never changes a file that is being served."""
	return os.path.join(str(zoom), str(x), "{}.{}.pbf.gz".format(y, version))


def _write(path, data):
	full = os.path.join(settings.TILE_CACHE_DIR, path)
	os.makedirs(os.path.dirname(full), exist_ok=True)
	temporary = full + ".tmp{}".format(os.getpid())
	with open(temporary, "wb") as f:
		f.write(data)
	os.replace(temporary, full)


def request_tile(zoom, x, y, rerender):
	"""Records a request for a tile and, if it needs rendering, queues it
	ahead of everything requested earlier. rerender asks for a new rendering
	even if the tile has one. Returns the tile's state, and when it was
	queued (None if it wasn't)."""
	now = timezone.now()
	with transaction.atomic():
		# A new tile has no rendering, so it is queued below
		tile, _ = TileState.objects.get_or_create(kind=TileState.TILE, zoom=zoom, x=x, y=y,
			defaults={"status": TileState.DONE})
		updates = {"requested_at": now, "request_count": F("request_count") + 1}
		queued_at = None
		if tile.status in (TileState.QUEUED, TileState.RENDERING) and tile.queued_at:
			# Already on its way: wait for that rendering
			queued_at = tile.queued_at
			updates["priority"] = time.time()
		elif rerender or tile.status == TileState.FAILED or tile.rendered_at is None:
			updates.update(status=TileState.QUEUED, priority=time.time(), queued_at=now,
				not_before=None, waiting_since=None, attempts=0, error="")
			queued_at = now
		TileState.objects.filter(pk=tile.pk).update(**updates)
		tile.refresh_from_db()
	return tile, queued_at


def log_request(zoom, x, y):
	"""Records a request for a tile without queueing it."""
	with transaction.atomic():
		tile, _ = TileState.objects.get_or_create(kind=TileState.TILE, zoom=zoom, x=x, y=y,
			defaults={"status": TileState.DONE})
		TileState.objects.filter(pk=tile.pk).update(requested_at=timezone.now(),
			request_count=F("request_count") + 1)


def wait_for_tile(tile_id, queued_at, timeout):
	"""Waits until the tile has been rendered since queued_at. Returns its
	state, or None if it wasn't ready in time."""
	deadline = time.monotonic() + timeout
	while True:
		tile = TileState.objects.defer("data").get(pk=tile_id)
		if tile.status == TileState.DONE and tile.rendered_at and tile.rendered_at >= queued_at:
			return TileState.objects.get(pk=tile_id)
		if tile.status == TileState.FAILED or time.monotonic() >= deadline:
			return None
		time.sleep(0.05)


async def await_tile(tile_id, queued_at, timeout):
	"""As wait_for_tile, for an async view: it waits without holding a thread,
	and is cancelled with the request if the client goes away."""
	deadline = time.monotonic() + timeout
	while True:
		tile = await TileState.objects.defer("data").aget(pk=tile_id)
		if tile.status == TileState.DONE and tile.rendered_at and tile.rendered_at >= queued_at:
			return await TileState.objects.aget(pk=tile_id)
		if tile.status == TileState.FAILED or time.monotonic() >= deadline:
			return None
		await asyncio.sleep(0.05)


def forget_requests():
	"""Deletes the requests flagged cancelled more than CANCELLED_REQUEST_KEEP
	seconds ago, and those that started more than TILE_REQUEST_WAIT seconds
	ago and are still there, which no request being answered can be: left by
	a server that stopped while answering (see TileRequest). Returns how
	many."""
	now = timezone.now()
	cancelled = TileRequest.objects.filter(cancelled_at__lt=now - timedelta(seconds=settings.CANCELLED_REQUEST_KEEP))
	unanswered = TileRequest.objects.filter(cancelled=False,
		started_at__lt=now - timedelta(seconds=settings.TILE_REQUEST_WAIT))
	return cancelled.delete()[0] + unanswered.delete()[0]


def requeue_abandoned():
	"""Gives tiles claimed by renderers that have gone quiet to others."""
	cutoff = timezone.now() - timedelta(seconds=settings.RENDER_LEASE)
	return TileState.objects.filter(status=TileState.RENDERING, claimed_at__lt=cutoff).update(
		status=TileState.QUEUED, claimed_by="", claimed_at=None)


def claim_next(worker, idle=True):
	"""Claims the highest priority queued job for a renderer, or returns None
	if there is none. Without idle, jobs queued by idle renderers are left
	waiting: theirs have a negative priority, a request's is positive, and a
	request taking one over makes it positive. A renderer not working ahead
	leaves the negative ones alone."""
	while True:
		ready = Q(not_before__isnull=True) | Q(not_before__lte=timezone.now())
		jobs = TileState.objects.filter(ready, status=TileState.QUEUED)
		if not idle:
			jobs = jobs.filter(priority__gte=0)
		candidate = jobs.order_by("-priority", "queued_at").values_list("pk", flat=True).first()
		if candidate is None:
			return None
		claimed = TileState.objects.filter(pk=candidate, status=TileState.QUEUED).update(
			status=TileState.RENDERING, claimed_by=worker, claimed_at=timezone.now(),
			attempts=F("attempts") + 1)
		if claimed:
			return TileState.objects.get(pk=candidate)
		# Another renderer got it first; try the next


def finish(tile, data, version, seconds, cached):
	"""Records a rendering of a tile (gzipped). A cached zoom's tile is written
	to TILE_CACHE_DIR, replacing its last file; any other's is kept with its
	state, for the requests waiting for it. A cached tile with a common
	content, as open sea or land with nothing mapped (see tiles.common), is
	pointed at its common file, so the ocean is one file, and one download for
	a browser, not millions."""
	fields = {"status": TileState.DONE, "rendered_at": timezone.now(), "data_version": version,
		"render_seconds": seconds, "error": "", "claimed_by": "", "claimed_at": None,
		"not_before": None, "waiting_since": None, "file": "", "data": None}
	previous = TileState.objects.filter(pk=tile.pk).values_list("file", flat=True).first()
	common = common_file(data) if cached else None
	if common:
		fields["file"] = common
	elif data and cached:
		fields["file"] = tile_file(tile.zoom, tile.x, tile.y, version)
		_write(fields["file"], data)
	elif data:
		fields["data"] = data
	TileState.objects.filter(pk=tile.pk).update(**fields)
	# The last file goes, unless common or shared: other tiles may be using it
	if previous and previous != fields["file"] and not previous.startswith((COMMON_DIR + os.sep, SHARED_DIR + os.sep)):
		try:
			os.remove(os.path.join(settings.TILE_CACHE_DIR, previous))
		except FileNotFoundError:
			pass


def fail(tile, error, final=False):
	"""Records a failed job: it is queued again, until it has failed
	RENDER_ATTEMPTS times, or straight away if final."""
	tile.refresh_from_db()
	status = TileState.FAILED if final or tile.attempts >= settings.RENDER_ATTEMPTS else TileState.QUEUED
	TileState.objects.filter(pk=tile.pk).update(status=status, error=str(error)[:2000],
		claimed_by="", claimed_at=None, not_before=None, waiting_since=None)


def finish_summary(job, version, seconds):
	"""Records a built summary (stored as a TileSummary)."""
	TileState.objects.filter(pk=job.pk).update(status=TileState.DONE, rendered_at=timezone.now(),
		data_version=version, render_seconds=seconds, error="", claimed_by="", claimed_at=None,
		not_before=None, waiting_since=None)


def queue_summaries(tiles, priority, depth):
	"""Queues jobs to build summaries, (level, x, y), ahead of priority, at a
	depth. A request at a lower depth than a job already queued supersedes it:
	the job takes the lower depth (and the higher priority). Returns the
	summaries whose jobs have failed, except those that failed only for being
	too deep, which the lower depth retries."""
	now = timezone.now()
	failed = []
	for level, x, y in tiles:
		with transaction.atomic():
			job, created = TileState.objects.get_or_create(kind=TileState.SUMMARY, zoom=level, x=x, y=y,
				defaults={"status": TileState.DONE, "depth": depth})
			deeper = not created and depth < job.depth
			if job.status == TileState.FAILED and not deeper:
				failed.append((level, x, y))
				continue
			if job.status in (TileState.QUEUED, TileState.RENDERING):
				TileState.objects.filter(pk=job.pk).update(priority=max(priority, job.priority),
					depth=min(depth, job.depth))
				continue
			TileState.objects.filter(pk=job.pk).update(status=TileState.QUEUED, priority=priority,
				depth=depth, queued_at=now, not_before=None, waiting_since=None, attempts=0, error="")
	return failed


class WaitedTooLong(Exception):
	pass


def defer(job):
	"""Puts a claimed job back, not to be claimed again for a while: it is
	waiting for summaries. Waiting isn't an attempt. The longer it has waited,
	the longer until it looks again (from SUMMARY_WAIT, a quarter of the time
	waited so far, up to SUMMARY_WAIT_MAX). Raises WaitedTooLong once it has
	waited SUMMARY_WAIT_LIMIT."""
	now = timezone.now()
	since = job.waiting_since or now
	waited = (now - since).total_seconds()
	if waited > settings.SUMMARY_WAIT_LIMIT:
		raise WaitedTooLong("waited {:.0f} seconds for summaries".format(waited))
	delay = min(settings.SUMMARY_WAIT_MAX, max(settings.SUMMARY_WAIT, waited / 4))
	TileState.objects.filter(pk=job.pk).update(status=TileState.QUEUED, claimed_by="", claimed_at=None,
		waiting_since=since, not_before=now + timedelta(seconds=delay), attempts=F("attempts") - 1)
