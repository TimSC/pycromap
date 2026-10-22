"""Work for idle renderers (RENDER_IDLE_WORK): the summaries for the whole
planet, which nobody has asked for yet. Tiles are only rendered when asked
for.

The sweep is kept in TileSweep, one row per row of summaries at each summary
level. Idle renderers claim rows and recheck which summaries in them are built
and current, so the gaps are what's left to do; each row hands out its first
gaps that can be built now: at the base level, from the map data, any; at a
coarser level, those whose finer summaries are all current. The highest level
comes first, then the coarser ones as the rows beneath them fill. A summary
built for a request is skipped on the next recheck, and the whole pyramid is
eventually built.
"""

import fcntl
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Case, Q, When
from django.utils import timezone

from summaries import build as summaries
from summaries.models import TileSummary

from .models import TileState, TileSweep

# The priority of the first idle job planned; the rest come after it, in the
# order planned (TileState.idle marks them as idle work)
IDLE_PRIORITY = -1.0

# The jobs a sweep row hands out at a time: its first gaps that can be done
# now, enough for the fleet to share a row, and few enough that looking along
# it stays cheap
SWEEP_ROW_JOBS = 32


# The queries below ask for each column of summaries (x IN ...), so that the
# index of each summary, by (zoom, x, y), finds a row's few: asked for by row
# alone (or a range of x), it would read every summary of the level.

def _built_in_row(level, y):
	"""The x of the current summaries (built as summaries are now; see
	summaries.build.current) in row y at a level."""
	return set(summaries.current(TileSummary.objects.filter(zoom=level, x__in=range(1 << level), y=y))
		.values_list("x", flat=True))


def _ready_in_row(level, y, xs):
	"""Those of the summaries (level, x, y), x in xs, that can be built now:
	at the base level, from the map data, any; at a coarser level, those
	whose finer summaries are all current. One query, for those columns."""
	finer = summaries.finer_level(level)
	if finer is None:
		return list(xs)
	scale = 1 << (finer - level)
	stored = set(summaries.current(TileSummary.objects.filter(zoom=finer,
		x__in=[fx for x in xs for fx in range(x * scale, (x + 1) * scale)],
		y__range=(y * scale, (y + 1) * scale - 1))).values_list("x", "y"))
	return [x for x in xs if all((fx, fy) in stored for fx in range(x * scale, (x + 1) * scale)
		for fy in range(y * scale, (y + 1) * scale))]


def _passed_over(level):
	"""The summaries at a level the sweep doesn't hand out: those whose jobs
	are under way (queued or rendering), and those given up, having failed
	RENDER_ATTEMPTS times, which a row moves past so one that can't be built
	doesn't hold it up. A job that failed fewer times is handed out again.
	As ({(x, y)} under way, {(x, y)} given up). Found by status alone, which
	the queue's index (tilestate_queue) finds the few of: asked for by kind
	and zoom as well, SQLite would read every job of the level."""
	underway, given_up = set(), set()
	for kind, zoom, x, y, status, attempts in TileState.objects.filter(
			status__in=(TileState.QUEUED, TileState.RENDERING, TileState.FAILED)).values_list(
			"kind", "zoom", "x", "y", "status", "attempts"):
		if kind != TileState.SUMMARY or zoom != level:
			continue
		if status != TileState.FAILED:
			underway.add((x, y))
		elif attempts >= settings.RENDER_ATTEMPTS:
			given_up.add((x, y))
	return underway, given_up


def _ensure_sweep_rows(level):
	"""Gives a level a TileSweep row for every row of its grid, if it hasn't
	already: 2^level rows."""
	have = set(TileSweep.objects.filter(kind=TileState.SUMMARY, zoom=level).values_list("y", flat=True))
	missing = [TileSweep(kind=TileState.SUMMARY, zoom=level, y=y) for y in range(1 << level) if y not in have]
	if missing:
		TileSweep.objects.bulk_create(missing, ignore_conflicts=True)


def _frontiers(limit, level, worker):
	"""Up to limit summaries (level, x, y) from the front of a level: in each
	row, left to right, the first SWEEP_ROW_JOBS gaps that can be built now,
	rows top to bottom. The worker's own rows come first (it is working
	them), then new rows. A row stays claimed while it has gaps, so other
	renderers don't waste a look at it; it is released, with a timestamp,
	once every summary in it is built (or given up)."""
	_ensure_sweep_rows(level)
	rows_qs = TileSweep.objects.filter(
		Q(kind=TileState.SUMMARY, zoom=level) & (Q(claimed_by=worker) | Q(claimed_by="", last_checked__isnull=True)))
	rows = list(rows_qs.order_by(
		Case(When(claimed_by=worker, then=0), default=1), "y"
	).values_list("pk", "y")[:limit])
	underway, given_up = _passed_over(level)
	now = timezone.now()
	jobs = []
	for pk, y in rows:
		if len(jobs) >= limit:
			break
		# Claim new rows (a compare-and-set, as for jobs); own rows are
		# already claimed, and a race loses to the other renderer
		if not TileSweep.objects.filter(pk=pk, claimed_by="", last_checked__isnull=True).update(
				claimed_by=worker, claimed_at=now):
			if not TileSweep.objects.filter(pk=pk, claimed_by=worker).exists():
				continue
		built = _built_in_row(level, y)
		gaps = [x for x in range(1 << level) if x not in built and (x, y) not in given_up]
		if not gaps:
			# Every summary is built: release the row, with a timestamp
			TileSweep.objects.filter(pk=pk).update(last_checked=now, claimed_by="", claimed_at=None)
			continue
		take = min(SWEEP_ROW_JOBS, limit - len(jobs))
		# Looking a little further along the row than it takes, past those
		# not yet ready
		todo = _ready_in_row(level, y, [x for x in gaps if (x, y) not in underway][:take * 4])
		jobs.extend((level, x, y) for x in todo[:take])
	return jobs


def plan(count, worker):
	"""Up to count jobs (kind, zoom, x, y) for idle renderers, best first: the
	summaries at the fronts of the sweep's rows, the highest level first, and
	the rest of the budget shared by the coarser levels, in their order."""
	jobs = []
	for level in summaries.levels():  # finest first
		if len(jobs) >= count:
			break
		jobs.extend((TileState.SUMMARY, zoom, x, y) for zoom, x, y in _frontiers(count - len(jobs), level, worker))
	return jobs


def reclaim_sweeps():
	"""Gives rows claimed by renderers that have gone quiet to others."""
	cutoff = timezone.now() - timedelta(seconds=settings.RENDER_LEASE)
	return TileSweep.objects.filter(claimed_by__gt="", claimed_at__lt=cutoff).update(
		claimed_by="", claimed_at=None)


def release_sweeps(worker):
	"""Gives a renderer's rows to others as it stops."""
	return TileSweep.objects.filter(claimed_by=worker).update(claimed_by="", claimed_at=None)


def idle_queued():
	"""How many idle jobs are queued and ready to be claimed."""
	return TileState.objects.filter(status=TileState.QUEUED, idle=True).count()


@contextmanager
def _planning():
	"""Whether this renderer may plan: only one at a time does, holding a
	lock on a file beside the database, so that the fleet doesn't all plan
	the same work at once when the queue runs low. Renderers sharing the
	database on other machines plan for themselves."""
	path = Path(settings.PYCROMAP_DB).parent / "idle-planner.lock"
	with open(path, "a") as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			yield False
			return
		try:
			yield True
		finally:
			fcntl.flock(lock, fcntl.LOCK_UN)


def planning_now():
	"""Whether another renderer is planning idle work at the moment."""
	with _planning() as planning:
		return not planning


def queue_idle_work(worker):
	"""Tops up the idle jobs to IDLE_QUEUE, once they are down to half, so
	the fleet has work when the requests run out. Only one renderer plans at
	a time (see _planning): the others go on rendering. Returns how many it
	queued."""
	queued = idle_queued()
	if queued * 2 > settings.IDLE_QUEUE:
		return 0
	with _planning() as planning:
		if not planning:
			return 0
		reclaim_sweeps()
		# Again, now it's this renderer's turn: another may just have
		queued = idle_queued()
		wanted = settings.IDLE_QUEUE - queued
		if queued * 2 > settings.IDLE_QUEUE:
			return 0
		now = timezone.now()
		made = 0
		jobs = plan(wanted, worker)
		# Queued in one transaction: one commit, not one a job, as each
		# commit holds the database's one write lock
		with transaction.atomic():
			for rank, (kind, zoom, x, y) in enumerate(jobs):
				job, _ = TileState.objects.get_or_create(kind=kind, zoom=zoom, x=x, y=y,
					defaults={"status": TileState.DONE})
				if job.status in (TileState.QUEUED, TileState.RENDERING):
					continue
				# A failed job is tried again (the sweep hands it out once it
				# can be done), counting on from its attempts, until it has
				# failed RENDER_ATTEMPTS times
				failed = job.status == TileState.FAILED
				if failed and job.attempts >= settings.RENDER_ATTEMPTS:
					continue
				TileState.objects.filter(pk=job.pk).update(status=TileState.QUEUED, idle=True,
					priority=IDLE_PRIORITY - rank, queued_at=now, attempts=job.attempts if failed else 0, error="")
				made += 1
		return made
