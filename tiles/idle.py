"""Work for idle renderers (RENDER_IDLE_WORK): tiles and summaries nobody has
asked for yet, near those they have, and, once those are done, the rest of the
planet.

Only work that can be done straight away is chosen, as for requests: a
summary at the base level (from the map data), a coarser summary whose
summaries beneath it all exist, or a tile of a cached zoom that can be
rendered now (rendering an uncached zoom's tile ahead would be wasted). The
highest zooms come first, then those nearest the newest requested tile: only
tiles someone asked for count, not those idle renderers did.

The rest of the planet is kept in TileSweep, one row per row of tiles (at each
kind and zoom). Idle renderers claim the rows and recheck which tiles are
rendered, so the first gap is what's left to do; the frontiers advance as the
rows work, and the whole world eventually renders, and a request for a tile
renders it, so the next recheck skips past it.
"""

import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, Q, When
from django.utils import timezone

from summaries import build as summaries
from summaries.models import TileSummary

from .models import TileState, TileSweep

# Idle jobs come after every request (whose priority is the time it came)
IDLE_PRIORITY = -1.0


def _seeds():
	"""The most recently requested tiles (zoom, x, y)."""
	return list(TileState.objects.filter(kind=TileState.TILE, request_count__gt=0)
		.order_by("-requested_at").values_list("zoom", "x", "y")[:settings.IDLE_SEEDS])


def _summaries_at(level):
	return set(summaries.current(TileSummary.objects.filter(zoom=level)).values_list("x", "y"))


def _busy(kind, zoom):
	"""Tiles with a job that is under way, or has failed."""
	return set(TileState.objects.filter(kind=kind, zoom=zoom,
		status__in=(TileState.QUEUED, TileState.RENDERING, TileState.FAILED)).values_list("x", "y"))


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


def _near(seeds, zoom, radius):
	"""Tiles at zoom around the seeds: (distance, x, y), the distance in tiles
	at zoom, counting diagonals as one. The newest seed's rings come before
	the next-newest seed's, so the place the user is now wins over where they
	were."""
	n = 1 << zoom
	seen = set()
	for cx, cy in _centres(seeds, zoom):
		for r in range(radius + 1):
			for x in range(cx - r, cx + r + 1):
				for y in range(cy - r, cy + r + 1):
					if max(abs(x - cx), abs(y - cy)) != r or not (0 <= x < n and 0 <= y < n) or (x, y) in seen:
						continue
					seen.add((x, y))
					yield r, x, y


def _candidate_levels(sweep):
	"""The (zoom, kind) pairs that could be done, highest zoom first. For the
	rings around the seeds, base-level summaries: the cheapest way to have the
	pyramid near where someone is looking. For the planet sweep, the base
	tiles instead: their rendering builds the summaries, so the sweep never
	builds a summary it would render over."""
	base = summaries.base_level()
	base_is_tile = base is not None and (settings.TILE_ZOOMS.get(base) or {}).get("source") == "data"
	levels = summaries.levels()
	if sweep and base_is_tile:
		levels = [level for level in levels if level != base]
	kinds = [(level, TileState.SUMMARY) for level in levels]
	# A tile of a cached zoom can be rendered ahead; the base tile, which is
	# rendered for the sweep (its rendering builds its summary)
	for zoom, config in settings.TILE_ZOOMS.items():
		if not (config.get("cache") or (sweep and zoom == base and base_is_tile)):
			continue
		kinds.append((zoom, TileState.TILE))
	# Highest zooms first; at the same zoom, tiles before summaries (rendering
	# a tile from the map data builds its summary too)
	return sorted(kinds, key=lambda k: (-k[0], k[1] != TileState.TILE))


def _candidates(sweep):
	"""As _candidate_levels, with a test of whether a tile at that zoom is
	worth doing and can be done now. For the sweep the test is None: the
	frontiers (tiles.idle._frontiers) do their own recheck of what is
	rendered, and the test would only cost the queries to build it."""
	if sweep:
		return [(zoom, kind, None) for zoom, kind in _candidate_levels(sweep)]
	kinds = []
	stored = {level: _summaries_at(level) for level in summaries.levels()}
	for level, kind in _candidate_levels(sweep):
		if kind == TileState.SUMMARY:
			finer = summaries.finer_level(level)

			def ready(x, y, level=level, finer=finer):
				if (x, y) in stored[level]:
					return False
				if finer is None:
					return True
				return all((fx, fy) in stored[finer] for _, fx, fy in summaries.tiles_beneath(level, x, y, finer))
			kinds.append((level, TileState.SUMMARY, ready))
		else:
			config = settings.TILE_ZOOMS[level]
			rendered = set(TileState.objects.filter(kind=TileState.TILE, zoom=level, status=TileState.DONE,
				rendered_at__isnull=False).values_list("x", "y"))

			def ready(x, y, level=level, config=config, rendered=rendered):
				if (x, y) in rendered:
					return False
				if config["source"] != "summary":
					return True
				summary_level = config["level"]
				return all((sx, sy) in stored.get(summary_level, ()) for _, sx, sy in summaries.tiles_beneath(level, x, y, summary_level))
			kinds.append((level, TileState.TILE, ready))
	return kinds


def _rendered_xy(kind, zoom, ys):
	"""The rendered x of each row y in ys at (kind, zoom), as {y: {x, ...}}:
	stored summaries, or rendered tiles."""
	if kind == TileState.SUMMARY:
		pairs = TileSummary.objects.filter(zoom=zoom, y__in=ys).values_list("y", "x")
	else:
		pairs = TileState.objects.filter(kind=TileState.TILE, zoom=zoom, y__in=ys,
			status=TileState.DONE, rendered_at__isnull=False).values_list("y", "x")
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


def _frontiers(limit, rank, kind, zoom, worker):
	"""Up to limit tiles from the front of (kind, zoom), with the rank: the
	first unrendered tiles, left to right, top to bottom. The worker's own
	rows come first (it is working them), then new rows. A row stays claimed
	while it has gaps, so other renderers don't waste a check on it; it is
	released, with a timestamp, once every tile in it is rendered."""
	_ensure_sweep_rows(kind, zoom)
	rows_qs = TileSweep.objects.filter(
		Q(kind=kind, zoom=zoom) & (Q(claimed_by=worker) | Q(claimed_by="", last_checked__isnull=True)))
	rows = list(rows_qs.order_by(
		Case(When(claimed_by=worker, then=0), default=1), "y"
	).values_list("pk", "y")[:limit])
	claimed = []
	n = 1 << zoom
	now = timezone.now()
	for pk, y in rows:
		# Claim new rows (a compare-and-set, as for jobs); own rows are
		# already claimed, and a race loses to the other renderer
		if not TileSweep.objects.filter(pk=pk, claimed_by="", last_checked__isnull=True).update(
				claimed_by=worker, claimed_at=now):
			if not TileSweep.objects.filter(pk=pk, claimed_by=worker).exists():
				continue
		claimed.append((pk, y))
	rendered = _rendered_xy(kind, zoom, [y for _, y in claimed])
	frontiers = []
	for pk, y in claimed:
		# The first gap in the row is what's left to do
		x = 0
		while x < n and x in rendered.get(y, ()):
			x += 1
		if x < n:
			frontiers.append((y, x))
		else:
			# Every tile is rendered: release the row, with a timestamp
			TileSweep.objects.filter(pk=pk).update(last_checked=now,
				claimed_by="", claimed_at=None)
	frontiers.sort()
	return [(rank, kind, zoom, x, y) for y, x in frontiers[:limit]]


def _sweep(limit, worker):
	"""Up to limit tiles from the fronts of the rows, the front of the highest
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
		for zoom, kind, ready in _candidates(sweep=False):
			if len(chosen) >= count:
				break
			busy = _busy(kind, zoom)
			if kind == TileState.SUMMARY and zoom == summaries.base_level():
				busy |= rendering
				if (settings.TILE_ZOOMS.get(zoom) or {}).get("source") == "data":
					busy |= _busy(TileState.TILE, zoom)
			for _, x, y in _near(seeds, zoom, settings.IDLE_RADIUS):
				if len(chosen) >= count:
					break
				if (x, y) in seen or (x, y) in busy or not ready(x, y):
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


def queue_idle_work(worker):
	"""Queues idle jobs until there are as many idle jobs queued as there are
	renderers (RENDER_WORKERS), so the whole fleet has work when the requests
	run out. Returns how many it queued."""
	reclaim_sweeps()
	queued = TileState.objects.filter(Q(not_before__isnull=True) | Q(not_before__lte=timezone.now()),
		status=TileState.QUEUED, priority__lt=0).count()
	wanted = settings.RENDER_WORKERS - queued
	if wanted <= 0:
		return 0
	now = timezone.now()
	made = 0
	for rank, (kind, zoom, x, y) in enumerate(plan(wanted, worker)):
		with transaction.atomic():
			job, _ = TileState.objects.get_or_create(kind=kind, zoom=zoom, x=x, y=y,
				defaults={"status": TileState.DONE})
			if job.status in (TileState.QUEUED, TileState.RENDERING, TileState.FAILED):
				continue
			# At the deepest depth, so that it never makes jobs of its own;
			# the negative priority marks it as idle work
			TileState.objects.filter(pk=job.pk).update(status=TileState.QUEUED,
				priority=IDLE_PRIORITY - rank, depth=settings.SUMMARY_MAX_DEPTH, queued_at=now,
				not_before=None, waiting_since=None, attempts=0, error="")
			made += 1
	return made
