"""The render queue.

Requested work is queued as TileRequest rows: a request whose tile needs
rendering waits, and a renderer (manage.py render_tiles) claims the newest
waiting request, with every other request waiting for the same tile, adds
a Job for the tile, as rendering, renders it into a Tile, and answers the
requests. Work nobody has asked for yet (tiles.idle) is queued as Jobs too,
and comes after every request. A job is deleted once done; one that failed
is kept, to be tried again or given up. Claims are compare-and-set updates,
so any number of renderers can share the queue, even on SQLite.
"""

import asyncio
import os
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, Exists, F, OuterRef, When
from django.utils import timezone

from .models import Job, Tile, TileRequest, TileSweep
from .tilegeo import tile_key
from .common import COMMON_DIR, common_file


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


# ---- Requested work: TileRequest rows

def wait_for_render(request_id):
	"""Queues a request for its tile to be rendered: it waits for a renderer."""
	TileRequest.objects.filter(pk=request_id).update(status=TileRequest.WAITING)


async def answered_by_cache(request_id):
	"""Flags a request as being answered with a redirect to its tile's cached
	file (TileRequest.cache_redirect): renderers don't claim it."""
	await TileRequest.objects.filter(pk=request_id).aupdate(cache_redirect=True)


# The longest a request waiting for its tile waits between looks
AWAIT_POLL_MAX = 0.5


async def await_render(request_id, timeout):
	"""Waits, holding no thread, until a request's tile has been rendered.
	Returns its status then, DONE or FAILED, or None if it wasn't in timeout
	seconds. Cancelled with the request if the client goes away. It looks
	soon, then less and less often, up to every AWAIT_POLL_MAX seconds: a
	quick rendering is answered at once, and many waiting for slow ones
	don't keep the database busy."""
	deadline = time.monotonic() + timeout
	poll = 0.05
	while True:
		status = await TileRequest.objects.filter(pk=request_id).values_list("status", flat=True).afirst()
		if status in (TileRequest.DONE, TileRequest.FAILED):
			return status
		if status is None or time.monotonic() >= deadline:
			return None
		await asyncio.sleep(poll)
		poll = min(poll * 1.5, AWAIT_POLL_MAX)


def _answer_requests(job, status):
	"""Answers the requests for a job's tile still waiting or claimed (DONE or
	FAILED), those that came while it was rendered too."""
	if job.kind != Job.TILE:
		return 0
	return TileRequest.objects.filter(zoom=job.zoom, x=job.x, y=job.y,
		status__in=(TileRequest.WAITING, TileRequest.CLAIMED)).update(status=status)


def _claim_requested(worker):
	"""Claims the newest request waiting, and every other request waiting for
	the same tile, for a renderer: the newest at or above CASCADE_MIN_ZOOM,
	then, when there are none, the newest below it (queued only when what
	they are made from can be built straight away: see
	tiles.views._can_render); adds a Job for the tile, as rendering by it;
	and returns the job. None if no request is waiting, but for
	a tile that is being rendered already (those requests are answered when
	that rendering is). A cancelled request isn't claimed, nor one being
	answered from the cache (TileRequest.cache_redirect)."""
	rendering = Job.objects.filter(kind=Job.TILE, status=Job.RENDERING,
		zoom=OuterRef("zoom"), x=OuterRef("x"), y=OuterRef("y"))
	while True:
		candidate = (TileRequest.objects.filter(status=TileRequest.WAITING, cancelled=False, cache_redirect=False)
			.exclude(Exists(rendering))
			.order_by(Case(When(zoom__gte=settings.CASCADE_MIN_ZOOM, then=0), default=1), "-started_at")
			.values("zoom", "x", "y").first())
		if candidate is None:
			return None
		now = timezone.now()
		with transaction.atomic():
			claimed = TileRequest.objects.filter(status=TileRequest.WAITING, cancelled=False, cache_redirect=False,
				**candidate).update(status=TileRequest.CLAIMED, claimed_by=worker, claimed_at=now)
			if not claimed:
				continue  # another renderer got them first
			job, _ = Job.objects.get_or_create(kind=Job.TILE, **candidate)
			# One more attempt than the requests have waited through
			attempts = 1 + max(TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_by=worker,
				**candidate).values_list("attempts", flat=True), default=0)
			taken = Job.objects.filter(pk=job.pk).exclude(status=Job.RENDERING).update(
				status=Job.RENDERING, claimed_by=worker, claimed_at=now, attempts=attempts,
				idle=False, priority=time.time(), queued_at=now, error="")
			if not taken:
				# Being rendered from the idle queue meanwhile: these requests
				# wait for that rendering
				TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_by=worker, **candidate).update(
					status=TileRequest.WAITING, claimed_by="", claimed_at=None)
				continue
		return Job.objects.get(pk=job.pk)


# ---- Dependencies a renderer builds for a tile it has claimed (see
# render_tiles.build_dependencies)

def underway(summary_tiles):
	"""Those of the summaries (level, x, y) whose jobs some renderer is
	working on."""
	wanted = set(summary_tiles)
	levels = {level for level, _, _ in wanted}
	return wanted & set(Job.objects.filter(kind=Job.SUMMARY, status=Job.RENDERING,
		zoom__in=levels).values_list("zoom", "x", "y"))


def claim_dependency(level, x, y, worker, priority):
	"""Adds a job for a summary, if there isn't one, and claims it for a
	renderer building it for a tile it has claimed (at that tile's priority).
	Returns the job, or None if another renderer has it."""
	with transaction.atomic():
		job, _ = Job.objects.get_or_create(kind=Job.SUMMARY, zoom=level, x=x, y=y)
		now = timezone.now()
		taken = Job.objects.filter(pk=job.pk).exclude(status=Job.RENDERING).update(
			status=Job.RENDERING, claimed_by=worker, claimed_at=now, idle=False, priority=priority,
			queued_at=now, attempts=1, error="")
	return Job.objects.get(pk=job.pk) if taken else None


def still_wanted(tile):
	"""Whether any request not cancelled still waits for a tile."""
	return TileRequest.objects.filter(zoom=tile.zoom, x=tile.x, y=tile.y, cancelled=False,
		status__in=(TileRequest.WAITING, TileRequest.CLAIMED)).exists()


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

def _release(requests, jobs, reason):
	"""Gives claimed work to other renderers: the requests (claimed) wait
	again, the jobs (rendering) that were idle work are queued again, and the
	others are marked failed, for the reason, so the requests for their tiles
	can be claimed again. Returns how many jobs."""
	requests.update(status=TileRequest.WAITING, claimed_by="", claimed_at=None)
	return (jobs.filter(idle=True).update(status=Job.QUEUED, claimed_by="", claimed_at=None) +
		jobs.update(status=Job.FAILED, claimed_by="", claimed_at=None, error=reason))


def requeue_abandoned():
	"""Gives the work claimed by renderers that have gone quiet (no heartbeat
	for RENDER_LEASE seconds) to others (see _release)."""
	cutoff = timezone.now() - timedelta(seconds=settings.RENDER_LEASE)
	return _release(TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_at__lt=cutoff),
		Job.objects.filter(status=Job.RENDERING, claimed_at__lt=cutoff), "abandoned by its renderer")


def heartbeat(worker):
	"""Marks what a renderer holds (its jobs, the requests it claimed, its
	sweep rows) as still held, so it isn't taken for abandoned
	(RENDER_LEASE). The jobs are found by status, through the queue's index
	(job_queue), as there are few being rendered."""
	now = timezone.now()
	Job.objects.filter(status=Job.RENDERING, claimed_by=worker).update(claimed_at=now)
	TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_by=worker).update(claimed_at=now)
	TileSweep.objects.filter(claimed_by=worker).update(claimed_at=now)


def release_claims(worker):
	"""Gives the work a renderer has claimed to others as it stops (see
	_release), rather than leaving it until RENDER_LEASE has passed."""
	return _release(TileRequest.objects.filter(status=TileRequest.CLAIMED, claimed_by=worker),
		Job.objects.filter(status=Job.RENDERING, claimed_by=worker), "its renderer stopped")


def claim_next(worker, idle=True):
	"""Claims the next job for a renderer, or returns None if there is none:
	requested work first (_claim_requested), then, for a renderer working
	ahead (idle), the idle jobs queued, highest priority first."""
	job = _claim_requested(worker)
	if job is not None or not idle:
		return job
	while True:
		jobs = Job.objects.filter(status=Job.QUEUED, idle=True)
		candidate = jobs.order_by("-priority", "queued_at").values_list("pk", flat=True).first()
		if candidate is None:
			return None
		claimed = Job.objects.filter(pk=candidate, status=Job.QUEUED).update(
			status=Job.RENDERING, claimed_by=worker, claimed_at=timezone.now(),
			attempts=F("attempts") + 1)
		if claimed:
			return Job.objects.get(pk=candidate)
		# Another renderer got it first; try the next


def finish(job, data, version, seconds, cached):
	"""Records a rendering of a job's tile (gzipped) as its Tile, deletes the
	job, and answers the requests for it. A cached zoom's tile is written to
	TILE_CACHE_DIR, replacing its last file; any other's is kept in the
	database, for the requests waiting for it. A cached tile with a common
	content, as open sea or land with nothing mapped (see tiles.common), is
	pointed at its common file, so the ocean is one file, and one download for
	a browser, not millions."""
	fields = {"rendered_at": timezone.now(), "data_version": version, "render_seconds": seconds,
		"file": "", "data": None}
	key = tile_key(job.zoom, job.x, job.y)
	previous = Tile.objects.filter(pk=key).values_list("file", flat=True).first()
	common = common_file(data) if cached else None
	if common:
		fields["file"] = common
	elif data and cached:
		fields["file"] = tile_file(job.zoom, job.x, job.y, version)
		_write(fields["file"], data)
	elif data:
		fields["data"] = data
	with transaction.atomic():
		Tile.objects.update_or_create(pk=key, defaults={"zoom": job.zoom, "x": job.x, "y": job.y, **fields})
		Job.objects.filter(pk=job.pk).delete()
		_answer_requests(job, TileRequest.DONE)
	# The last file goes, unless common: other tiles may be using it
	if previous and previous != fields["file"] and not previous.startswith(COMMON_DIR + os.sep):
		try:
			os.remove(os.path.join(settings.TILE_CACHE_DIR, previous))
		except FileNotFoundError:
			pass


def fail(job, error, final=False):
	"""Records a failed job. Until it has failed RENDER_ATTEMPTS times (or at
	once if final), it is tried again: a requested tile's job is marked
	failed, and its requests wait again, keeping count; idle work is queued
	again. After that, the requests for the tile are answered as failed, and
	the job is kept, failed, so the idle sweep passes it over. A job deleted
	meanwhile (manage.py clear_queue --all) is left deleted."""
	try:
		job.refresh_from_db()
	except Job.DoesNotExist:
		return
	given_up = final or job.attempts >= settings.RENDER_ATTEMPTS
	retry_idle = job.idle and not given_up
	Job.objects.filter(pk=job.pk).update(status=Job.QUEUED if retry_idle else Job.FAILED,
		error=str(error)[:2000], claimed_by="", claimed_at=None)
	if given_up:
		_answer_requests(job, TileRequest.FAILED)
	elif job.kind == Job.TILE and not retry_idle:
		TileRequest.objects.filter(zoom=job.zoom, x=job.x, y=job.y,
			status__in=(TileRequest.CLAIMED, TileRequest.WAITING)).update(
			status=TileRequest.WAITING, claimed_by="", claimed_at=None, attempts=job.attempts)


def finish_summary(job):
	"""Deletes a summary's job once the summary is built (stored as a
	summaries.TileSummary)."""
	Job.objects.filter(pk=job.pk).delete()
