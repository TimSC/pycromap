"""Work for idle renderers (RENDER_IDLE_WORK): tiles and summaries nobody has
asked for yet, near those they have, and, once those are done, the rest of the
planet.

Only work that can be done straight away is chosen, as for requests: a
summary at the base level (from the map data), a coarser summary whose
summaries beneath it all exist, or a tile of a cached zoom that can be
rendered now (rendering an uncached zoom's tile ahead would be wasted). The
highest zooms come first, then those nearest the newest requested tile: only
tiles someone asked for count, not those idle renderers did.

The rest of the planet is summaries only (its tiles are rendered when asked
for), kept in TileSweep, one row per row of tiles (at each summary level);
each row hands out its first gaps that can be built now. Idle renderers claim the rows and recheck which tiles are
rendered, so the first gap is what's left to do; the frontiers advance as the
rows work, and the whole world eventually renders, and a request for a tile
renders it, so the next recheck skips past it.
"""

import fcntl
import math
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


def _seeds():
	"""The most recently requested tiles (zoom, x, y). Only tiles are ever
	requested (summaries are queued for them), so the kind needn't be
	asked for, which would steer SQLite to the index of every tile. The
	condition is written out, not passed as a parameter, as SQLite only uses
	a partial index (tilestate_requested) for a query that states its
	condition."""
	return list(TileState.objects.extra(where=['"request_count" > 0'])
		.order_by("-requested_at").values_list("zoom", "x", "y")[:settings.IDLE_SEEDS])


# The ranges below ask for each column of tiles (x IN ...) and a range of rows
# (y BETWEEN ...), so that the index of each tile, by (kind, zoom, x, y) or
# (zoom, x, y), finds each column's few: a range of x would have it read
# every row of every column in it.

def _stored_in(level, x0, x1, y0, y1):
	"""The current summaries at a level within a range of tiles, as {(x, y)}."""
	return set(summaries.current(TileSummary.objects.filter(zoom=level, x__in=range(x0, x1 + 1),
		y__range=(y0, y1))).values_list("x", "y"))


def _file_present(file):
	"""Whether a rendered tile's file is in the cache: a tile without one (an
	empty tile, or one kept in the database) needs none. The database says
	what was rendered; a file deleted from the cache since is rendered
	again."""
	return not file or (Path(settings.TILE_CACHE_DIR) / file).exists()


def _rendered_in(zoom, x0, x1, y0, y1):
	"""The rendered tiles at a zoom within a range of tiles, as {(x, y)}:
	those whose files are still there."""
	return {(x, y) for x, y, file in TileState.objects.filter(kind=TileState.TILE, zoom=zoom,
		x__in=range(x0, x1 + 1), y__range=(y0, y1), status=TileState.DONE, rendered_at__isnull=False)
		.values_list("x", "y", "file") if _file_present(file)}


def _busy(kind, zoom):
	"""Tiles with a job that is under way, or has failed. Found by status
	alone, which the queue's index (tilestate_queue) finds the few of: asked
	for by kind and zoom as well, SQLite would read every tile of the zoom."""
	return {(x, y) for k, z, x, y in TileState.objects.filter(
		status__in=(TileState.QUEUED, TileState.RENDERING, TileState.FAILED)).values_list("kind", "zoom", "x", "y")
		if k == kind and z == zoom}


def _centres(seeds, zoom):
	"""The seeds, at zoom: (x, y), the newest first, deduplicated."""
	n = 1 << zoom
	centres = []
	seen = set()
	for seed_zoom, sx, sy in seeds:  # newest first
		scale = 2.0 ** (zoom - seed_zoom)
		centre = (min(n - 1, int(math.floor((sx + 0.5) * scale))), min(n - 1, int(math.floor((sy + 0.5) * scale))))
		if centre in seen:
			continue
		seen.add(centre)
		centres.append(centre)
	return centres


def _rings(cx, cy, radius, n):
	"""The tiles around (cx, cy), out to radius, nearest first: (x, y), the
	distance counting diagonals as one."""
	for r in range(radius + 1):
		for x in range(cx - r, cx + r + 1):
			for y in range(cy - r, cy + r + 1):
				if max(abs(x - cx), abs(y - cy)) == r and 0 <= x < n and 0 <= y < n:
					yield x, y


def _ready_around(kind, zoom, cx, cy, radius):
	"""A test of whether a tile around (cx, cy) at zoom is worth doing and can
	be done now: not done yet, and what it is made from there. It looks up
	only the tiles around the centre, with a range on the index of each, so
	it costs the same however much of the map is done."""
	n = 1 << zoom
	x0, x1, y0, y1 = max(0, cx - radius), min(n - 1, cx + radius), max(0, cy - radius), min(n - 1, cy + radius)

	def beneath(level):
		scale = 1 << (level - zoom)
		return _stored_in(level, x0 * scale, (x1 + 1) * scale - 1, y0 * scale, (y1 + 1) * scale - 1)

	if kind == TileState.SUMMARY:
		done = _stored_in(zoom, x0, x1, y0, y1)
		finer = summaries.finer_level(zoom)
		under = beneath(finer) if finer is not None else None

		def ready(x, y):
			if (x, y) in done:
				return False
			return under is None or all((fx, fy) in under for _, fx, fy in summaries.tiles_beneath(zoom, x, y, finer))
		return ready

	done = _rendered_in(zoom, x0, x1, y0, y1)
	config = settings.TILE_ZOOMS[zoom]
	level = config.get("level") if config["source"] == "summary" else None
	under = beneath(level) if level is not None else None

	def ready(x, y):
		if (x, y) in done:
			return False
		return under is None or all((sx, sy) in under for _, sx, sy in summaries.tiles_beneath(zoom, x, y, level))
	return ready


def _candidate_levels(sweep):
	"""The (zoom, kind) pairs that could be done, highest zoom first. The
	summaries, base level first: the cheapest way to have the pyramid. For
	the rings around the seeds, the tiles of cached zooms as well, to have
	them ready where someone is looking; the planet sweep builds only the
	summaries, leaving the tiles to be rendered when asked for."""
	kinds = [(level, TileState.SUMMARY) for level in summaries.levels()]
	if not sweep:
		kinds.extend((zoom, TileState.TILE) for zoom, config in settings.TILE_ZOOMS.items()
			if config.get("cache"))
	# Highest zooms first; at the same zoom, tiles before summaries (rendering
	# a tile from the map data builds its summary too)
	return sorted(kinds, key=lambda k: (-k[0], k[1] != TileState.TILE))


def _candidates(sweep):
	"""As _candidate_levels, as (zoom, kind, None): the sweep's frontiers
	(tiles.idle._frontiers) do their own recheck of what is rendered."""
	return [(zoom, kind, None) for zoom, kind in _candidate_levels(sweep)]


def _rendered_xy(kind, zoom, ys):
	"""The rendered x of each row y in ys at (kind, zoom), as {y: {x, ...}}:
	current summaries (one built as summaries are now; see
	summaries.build.current), or rendered tiles whose files are still
	there."""
	# Every column asked for, so that the index finds the rows' few (see
	# above _stored_in)
	columns = range(1 << zoom)
	if kind == TileState.SUMMARY:
		pairs = summaries.current(TileSummary.objects.filter(zoom=zoom, x__in=columns, y__in=ys)).values_list("y", "x")
	else:
		pairs = [(y, x) for y, x, file in TileState.objects.filter(kind=TileState.TILE, zoom=zoom, x__in=columns, y__in=ys,
			status=TileState.DONE, rendered_at__isnull=False).values_list("y", "x", "file") if _file_present(file)]
	rendered = {}
	for y, x in pairs:
		rendered.setdefault(y, set()).add(x)
	return rendered


def _ensure_sweep_rows(kind, zoom):
	"""Gives (kind, zoom) a TileSweep row for every row of its grid, if it
	hasn't already: 2^zoom rows."""
	n = 1 << zoom
	have = set(TileSweep.objects.filter(kind=kind, zoom=zoom).values_list("y", flat=True))
	missing = [TileSweep(kind=kind, zoom=zoom, y=y) for y in range(n) if y not in have]
	if missing:
		TileSweep.objects.bulk_create(missing, ignore_conflicts=True)


# The jobs a sweep row hands out at a time: its first gaps that can be done
# now, enough for the fleet to share a row, and few enough that looking along
# it stays cheap
SWEEP_ROW_JOBS = 32


def _passed_over(kind, zoom):
	"""The tiles of (kind, zoom) the sweep doesn't hand out: those whose jobs
	are under way (queued or rendering), and those given up, having failed
	RENDER_ATTEMPTS times, which a row moves past so one tile that can't be
	done doesn't hold it up. A job that failed fewer times is handed out
	again. As ({(x, y)} under way, {(x, y)} given up). Found by status
	alone, as in _busy."""
	underway, given_up = set(), set()
	for k, z, x, y, status, attempts in TileState.objects.filter(
			status__in=(TileState.QUEUED, TileState.RENDERING, TileState.FAILED)).values_list(
			"kind", "zoom", "x", "y", "status", "attempts"):
		if k != kind or z != zoom:
			continue
		if status != TileState.FAILED:
			underway.add((x, y))
		elif attempts >= settings.RENDER_ATTEMPTS:
			given_up.add((x, y))
	return underway, given_up


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


def _frontiers(limit, rank, kind, zoom, worker):
	"""Up to limit tiles from the front of (kind, zoom), with the rank: in
	each row, left to right, the first SWEEP_ROW_JOBS gaps that can be done
	now (a coarser summary once the finer ones beneath it are current), rows
	top to bottom. The worker's own rows come first (it is working them), then
	new rows. A row stays claimed while it has gaps, so other renderers don't
	waste a look at it; it is released, with a timestamp, once every tile in
	it is rendered (or given up)."""
	_ensure_sweep_rows(kind, zoom)
	rows_qs = TileSweep.objects.filter(
		Q(kind=kind, zoom=zoom) & (Q(claimed_by=worker) | Q(claimed_by="", last_checked__isnull=True)))
	rows = list(rows_qs.order_by(
		Case(When(claimed_by=worker, then=0), default=1), "y"
	).values_list("pk", "y")[:limit])
	underway, given_up = _passed_over(kind, zoom)
	n = 1 << zoom
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
		rendered = _rendered_xy(kind, zoom, [y]).get(y, set())
		gaps = [x for x in range(n) if x not in rendered and (x, y) not in given_up]
		if not gaps:
			# Every tile is rendered: release the row, with a timestamp
			TileSweep.objects.filter(pk=pk).update(last_checked=now, claimed_by="", claimed_at=None)
			continue
		take = min(SWEEP_ROW_JOBS, limit - len(jobs))
		todo = [x for x in gaps if (x, y) not in underway]
		if kind == TileState.SUMMARY:
			# Looking a little further along the row than it takes, past
			# those not yet ready
			todo = _ready_in_row(zoom, y, todo[:take * 4])
		jobs.extend((rank, kind, zoom, x, y) for x in todo[:take])
	return jobs


def _sweep(limit, worker):
	"""Up to limit summaries from the fronts of the rows, the front of the highest
	zoom first, and the rest of the budget shared by the lower zooms, in
	their order."""
	frontiers = []
	for rank, (zoom, kind, ready) in enumerate(_candidates(sweep=True)):
		frontiers.extend(_frontiers(limit - len(frontiers), rank, kind, zoom, worker))
		if len(frontiers) >= limit:
			break
	frontiers.sort(key=lambda frontier: frontier[0])
	return [frontier[1:] for frontier in frontiers[:limit]]


def plan(count, worker):
	"""Up to count jobs (kind, zoom, x, y) for idle renderers, best first.

	First the IDLE_RADIUS rings around the seeds, nearest first; then the rest
	of the planet, row by row, from the frontiers."""
	if count <= 0:
		return []
	chosen = []
	seen = set()
	# Base-level tiles from the map data whose rendering builds their summary
	rendering = set()
	seeds = _seeds()
	if seeds:
		radius = settings.IDLE_RADIUS
		for zoom, kind in _candidate_levels(sweep=False):
			if len(chosen) >= count:
				break
			busy = _busy(kind, zoom)
			if kind == TileState.SUMMARY and zoom == summaries.base_level():
				busy |= rendering
				if (settings.TILE_ZOOMS.get(zoom) or {}).get("source") == "data":
					busy |= _busy(TileState.TILE, zoom)
			near = set()
			# The newest seed's rings before the next-newest seed's, so the
			# place the user is now wins over where they were
			for cx, cy in _centres(seeds, zoom):
				if len(chosen) >= count:
					break
				ready = None
				for x, y in _rings(cx, cy, radius, 1 << zoom):
					if len(chosen) >= count:
						break
					if (x, y) in near or (x, y) in seen or (x, y) in busy:
						continue
					near.add((x, y))
					# Looked up once a centre has a tile worth asking about
					ready = ready or _ready_around(kind, zoom, cx, cy, radius)
					if not ready(x, y):
						continue
					seen.add((x, y))
					chosen.append((kind, zoom, x, y))
					if kind == TileState.TILE and zoom == summaries.base_level() and settings.TILE_ZOOMS[zoom]["source"] == "data":
						rendering.add((x, y))
	for job in _sweep(count - len(chosen), worker):
		if job in seen:
			continue
		seen.add(job)
		chosen.append(job)
		if len(chosen) >= count:
			return chosen
	return chosen


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
