"""Work for idle renderers (RENDER_IDLE_WORK): the summaries for the whole
planet, which nobody has asked for yet, and, once summary work runs out, the
tiles of the zooms marked "idle_tile_gen" in TILE_ZOOMS. Other tiles are only
rendered when asked for.

The sweep is kept in TileSweep, one row per row of summaries at each summary
level. Idle renderers claim rows and recheck which summaries in them are built
and current, so the gaps are what's left to do; each row hands out its first
gaps that can be built now: at the base level, from the map data, any; at a
coarser level, those whose finer summaries are all current. The highest level
comes first, then the coarser ones as the rows beneath them fill. A summary
built for a request is skipped on the next recheck, and the whole pyramid is
eventually built.

Tiles are swept the same way, in TileSweep rows of kind "tile": the highest
marked zoom first, with the budget the summaries leave. A tile is done once
rendered (a Tile) with its file still in the cache, and is only handed out
once every summary it is made from is stored (idle jobs build nothing first).
What is under way, or was given up, is in the jobs (Job).
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

from .common import COMMON_DIR
from .models import Job, Tile, TileSweep
from .tilegeo import key_tile, row_keys

# The priority of the first idle job planned; the rest come after it, in the
# order planned (Job.idle marks them as idle work)
IDLE_PRIORITY = -1.0

# The jobs a sweep row hands out at a time: its first gaps that can be done
# now, enough for the fleet to share a row, and few enough that looking along
# it stays cheap
SWEEP_ROW_JOBS = 32


# The queries below ask for summaries and tiles by ranges of their keys
# (tilegeo.row_keys): a run of a row's tiles is one range.

def _file_present(file):
	"""Whether a rendered tile's file is in the cache: a tile without one (an
	empty tile) needs none, and the common files are managed by hand, so
	taken to be there."""
	return not file or file.startswith(COMMON_DIR + "/") or (Path(settings.TILE_CACHE_DIR) / file).exists()


def _built_in_row(kind, zoom, y):
	"""The x of what is done in row y at a zoom: current summaries (built as
	summaries are now; see summaries.build.current), or rendered tiles whose
	files are still there."""
	keys = row_keys(zoom, y, 0, (1 << zoom) - 1)
	if kind == Job.SUMMARY:
		# By key, from the index (tilesummary_current), not the rows
		return {key_tile(key)[1] for key in summaries.current(TileSummary.objects.filter(pk__range=keys))
			.values_list("pk", flat=True)}
	return {x for x, file in Tile.objects.filter(pk__range=keys).values_list("x", "file") if _file_present(file)}


def _covered(zoom, y, xs, level):
	"""Those of x in xs whose tile (zoom, x, y) has every summary beneath it
	at a level stored, current or not (see summaries.build.missing). One
	query, a range of keys for each row beneath, from the first to the last
	of xs."""
	scale = 1 << (level - zoom)
	stored = summaries.built_in(level, min(xs) * scale, (max(xs) + 1) * scale - 1,
		y * scale, (y + 1) * scale - 1) if xs else set()
	return [x for x in xs if all((sx, sy) in stored for sx in range(x * scale, (x + 1) * scale)
		for sy in range(y * scale, (y + 1) * scale))]


def _ready_in_row(kind, zoom, y, xs):
	"""Those of (zoom, x, y), x in xs, that can be done now, building
	nothing first: a base summary, from the map data, or a tile made from it,
	any; a coarser summary, or a tile made from summaries, those whose
	summaries beneath are all stored (current or not)."""
	if kind == Job.SUMMARY:
		beneath = summaries.finer_level(zoom)
	else:
		config = settings.TILE_ZOOMS[zoom]
		beneath = config["level"] if config["source"] == "summary" else None
	return list(xs) if beneath is None else _covered(zoom, y, xs, beneath)


def _passed_over(kind, zoom):
	"""What the sweep doesn't hand out at a zoom: jobs under way (queued or
	rendering), and those given up, having failed RENDER_ATTEMPTS times,
	which a row moves past so one that can't be done doesn't hold it up. A
	job that failed fewer times is handed out again. As ({(x, y)} under way,
	{(x, y)} given up). Every job is one of these (a job is deleted once
	done), so they are few."""
	underway, given_up = set(), set()
	for x, y, status, attempts in Job.objects.filter(kind=kind, zoom=zoom).values_list(
			"x", "y", "status", "attempts"):
		if status != Job.FAILED:
			underway.add((x, y))
		elif attempts >= settings.RENDER_ATTEMPTS:
			given_up.add((x, y))
	return underway, given_up


def _ensure_sweep_rows(kind, zoom):
	"""Gives a zoom a TileSweep row for every row of its grid, if it hasn't
	already: 2^zoom rows."""
	have = set(TileSweep.objects.filter(kind=kind, zoom=zoom).values_list("y", flat=True))
	missing = [TileSweep(kind=kind, zoom=zoom, y=y) for y in range(1 << zoom) if y not in have]
	if missing:
		TileSweep.objects.bulk_create(missing, ignore_conflicts=True)


def _frontiers(limit, kind, zoom, worker):
	"""Up to limit jobs (kind, zoom, x, y) from the front of a zoom: in each
	row, left to right, the first SWEEP_ROW_JOBS gaps that can be done now,
	rows top to bottom. The worker's own rows come first (it is working
	them), then new rows. A row stays claimed while it has gaps, so other
	renderers don't waste a look at it; it is released, with a timestamp,
	once everything in it is done (or given up)."""
	_ensure_sweep_rows(kind, zoom)
	rows_qs = TileSweep.objects.filter(
		Q(kind=kind, zoom=zoom) & (Q(claimed_by=worker) | Q(claimed_by="", last_checked__isnull=True)))
	rows = list(rows_qs.order_by(
		Case(When(claimed_by=worker, then=0), default=1), "y"
	).values_list("pk", "y")[:limit])
	underway, given_up = _passed_over(kind, zoom)
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
		done = _built_in_row(kind, zoom, y)
		gaps = [x for x in range(1 << zoom) if x not in done and (x, y) not in given_up]
		if not gaps:
			# Everything is done: release the row, with a timestamp
			TileSweep.objects.filter(pk=pk).update(last_checked=now, claimed_by="", claimed_at=None)
			continue
		take = min(SWEEP_ROW_JOBS, limit - len(jobs))
		# Looking a little further along the row than it takes, past those
		# not yet ready
		todo = _ready_in_row(kind, zoom, y, [x for x in gaps if (x, y) not in underway][:take * 4])
		jobs.extend((kind, zoom, x, y) for x in todo[:take])
	return jobs


def _tile_zooms():
	"""The zooms whose tiles idle renderers render ahead (marked
	"idle_tile_gen" in TILE_ZOOMS), highest first."""
	return sorted((zoom for zoom, config in settings.TILE_ZOOMS.items() if config.get("idle_tile_gen")),
		reverse=True)


def plan(count, worker):
	"""Up to count jobs (kind, zoom, x, y) for idle renderers, best first: the
	summaries at the fronts of the sweep's rows, the highest level first, and
	the rest of the budget shared by the coarser levels, in their order; then,
	with what is left, the tiles of the zooms marked "idle_tile_gen", the
	highest first, those whose summaries are all built."""
	jobs = []
	for level in summaries.levels():  # finest first
		if len(jobs) >= count:
			break
		jobs.extend(_frontiers(count - len(jobs), Job.SUMMARY, level, worker))
	for zoom in _tile_zooms():
		if len(jobs) >= count:
			break
		jobs.extend(_frontiers(count - len(jobs), Job.TILE, zoom, worker))
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
	return Job.objects.filter(status=Job.QUEUED, idle=True).count()


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
				job, created = Job.objects.get_or_create(kind=kind, zoom=zoom, x=x, y=y, defaults={
					"status": Job.QUEUED, "idle": True, "priority": IDLE_PRIORITY - rank, "queued_at": now})
				if created:
					made += 1
					continue
				# A failed job is tried again (the sweep hands it out once it
				# can be done), counting on from its attempts, until it has
				# failed RENDER_ATTEMPTS times
				if job.status != Job.FAILED or job.attempts >= settings.RENDER_ATTEMPTS:
					continue
				Job.objects.filter(pk=job.pk).update(status=Job.QUEUED, idle=True,
					priority=IDLE_PRIORITY - rank, queued_at=now, error="")
				made += 1
		return made
