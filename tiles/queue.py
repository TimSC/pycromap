"""The render queue.

Requested work is queued as TileRequest rows: a request whose tile needs
rendering waits, and a renderer (manage.py render_tiles) claims the newest
waiting request, with every other request waiting for the same tile, adds
the tile to TileState as rendering, renders it, and answers the requests.
Work nobody has asked for yet (tiles.idle) is queued in TileState itself, as
before, and comes after every request. Claims are compare-and-set updates,
so any number of renderers can share the queue, even on SQLite.
"""

import asyncio
import os
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
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


def log_request(zoom, x, y):
	"""Records that a tile was asked for, in its TileState (when, and how
	often: the idle renderers work near the tiles asked for)."""
	with transaction.atomic():
		tile, _ = TileState.objects.get_or_create(kind=TileState.TILE, zoom=zoom, x=x, y=y,
			defaults={"status": TileState.DONE})
		TileState.objects.filter(pk=tile.pk).update(requested_at=timezone.now(),
			request_count=F("request_count") + 1)


# ---- Requested work: TileRequest rows

def wait_for_render(request_id):
	"""Queues a request for its tile to be rendered: it waits for a renderer."""
	TileRequest.objects.filter(pk=request_id).update(status=TileRequest.WAITING)


async def answered_by_cache(request_id):
	"""Flags a request as being answered with a redirect to its tile's cached
	file (TileRequest.cache_redirect): renderers don't claim it."""
	await TileRequest.objects.filter(pk=request_id).aupdate(cache_redirect=True)


async def await_render(request_id, timeout):
	"""Waits, holding no thread, until a request's tile has been rendered.
	Returns its status then, DONE or FAILED, or None if it wasn't in timeout
	seconds. Cancelled with the request if the client goes away."""
	deadline = time.monotonic() + timeout
	while True:
		status = await TileRequest.objects.filter(pk=request_id).values_list("status", flat=True).afirst()
		if status in (TileRequest.DONE, TileRequest.FAILED):
			return status
		if status is None or time.monotonic() >= deadline:
			return None
		await asyncio.sleep(0.05)


def _answer_requests(tile, status):
	"""Answers the requests for a tile still waiting or claimed (DONE or
	FAILED), those that came while it was rendered too."""
	if tile.kind != TileState.TILE:
		return 0
	return TileRequest.objects.filter(zoom=tile.zoom, x=tile.x, y=tile.y,
		status__in=(TileRequest.WAITING, TileRequest.CLAIMED)).update(status=status)


def _claim_requested(worker):
	"""Claims the newest request waiting, and every other request waiting for
	the same tile, for a renderer; adds the tile to TileState, as rendering
	by it; and returns its TileState. None if no request is waiting, but for
	a tile that is being rendered already (those requests are answered when
	that rendering is). A cancelled request isn't claimed, nor one being
	answered from the cache (TileRequest.cache_redirect)."""
	rendering = TileState.objects.filter(kind=TileState.TILE, status=TileState.RENDERING,
		zoom=OuterRef("zoom"), x=OuterRef("x"), y=OuterRef("y"))
	while True:
		candidate = (TileRequest.objects.filter(status=TileRequest.WAITING, cancelled=False, cache_redirect=False)
			.exclude(Exists(rendering)).order_by("-started_at").values("zoom", "x", "y").first())
		if candidate is None:
			return None
		now = timezone.now()
		with transaction.atomic():
			claimed = TileRequest.objects.filter(status=TileRequest.WAITING, cancelled=False, cache_redirect=False,
				**candidate).update(status=TileRequest.CLAIMED, claimed_by=worker, claimed_at=now)
			if not claimed:
				continue  # another renderer got them first
			tile, _ = TileState.objects.get_or_create(kind=TileState.TILE, defaults={"status": TileState.DONE},
				**candidate)
			# A tile queued again after failing keeps count of its attempts
			attempts = tile.attempts + 1 if tile.status == TileState.QUEUED else 1
			taken = TileState.objects.filter(pk=tile.pk).exclude(status=TileState.RENDERING).update(
				status=TileState.RENDERING, claimed_by=worker, claimed_at=now, attempts=attempts,
				priority=time.time(), queued_at=now, depth=0, not_before=None, waiting_since=None, error="")
			if not taken:
				# Being rendered from the idle queue meanwhile: these requests
				# wait for that rendering
				TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_by=worker, **candidate).update(
					status=TileRequest.WAITING, claimed_by="", claimed_at=None)
				continue
		return TileState.objects.get(pk=tile.pk)


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


# ---- Claiming and finishing jobs

def requeue_abandoned():
	"""Gives the work claimed by renderers that have gone quiet to others: the
	jobs they were rendering are queued again, and the requests they claimed
	wait again."""
	cutoff = timezone.now() - timedelta(seconds=settings.RENDER_LEASE)
	TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_at__lt=cutoff).update(
		status=TileRequest.WAITING, claimed_by="", claimed_at=None)
	return TileState.objects.filter(status=TileState.RENDERING, claimed_at__lt=cutoff).update(
		status=TileState.QUEUED, claimed_by="", claimed_at=None)


def claim_next(worker, idle=True):
	"""Claims the next job for a renderer, or returns None if there is none:
	requested work first (_claim_requested), then the jobs queued in
	TileState, highest priority first: those queued again after failing, and
	idle work (negative priority), which only a renderer working ahead (idle)
	takes."""
	job = _claim_requested(worker)
	if job is not None:
		return job
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
	"""Records a rendering of a tile (gzipped), and answers the requests for
	it. A cached zoom's tile is written to TILE_CACHE_DIR, replacing its last
	file; any other's is kept with its state, for the requests waiting for it.
	A cached tile with a common content, as open sea or land with nothing
	mapped (see tiles.common), is pointed at its common file, so the ocean is
	one file, and one download for a browser, not millions."""
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
	_answer_requests(tile, TileRequest.DONE)
	# The last file goes, unless common or shared: other tiles may be using it
	if previous and previous != fields["file"] and not previous.startswith((COMMON_DIR + os.sep, SHARED_DIR + os.sep)):
		try:
			os.remove(os.path.join(settings.TILE_CACHE_DIR, previous))
		except FileNotFoundError:
			pass


def fail(tile, error, final=False):
	"""Records a failed job: it is queued again, and the requests for it wait
	again, until it has failed RENDER_ATTEMPTS times, or straight away if
	final; then the requests for it are answered as failed."""
	tile.refresh_from_db()
	status = TileState.FAILED if final or tile.attempts >= settings.RENDER_ATTEMPTS else TileState.QUEUED
	TileState.objects.filter(pk=tile.pk).update(status=status, error=str(error)[:2000],
		claimed_by="", claimed_at=None, not_before=None, waiting_since=None)
	if status == TileState.FAILED:
		_answer_requests(tile, TileRequest.FAILED)
	elif tile.kind == TileState.TILE:
		TileRequest.objects.filter(zoom=tile.zoom, x=tile.x, y=tile.y, status=TileRequest.CLAIMED).update(
			status=TileRequest.WAITING, claimed_by="", claimed_at=None)


def finish_summary(job, version, seconds):
	"""Records a built summary (stored as a TileSummary)."""
	TileState.objects.filter(pk=job.pk).update(status=TileState.DONE, rendered_at=timezone.now(),
		data_version=version, render_seconds=seconds, error="", claimed_by="", claimed_at=None,
		not_before=None, waiting_since=None)
