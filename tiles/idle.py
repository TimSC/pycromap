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
from django.db.models import Q
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


def _candidates(sweep):
	"""What could be done now, as (kind, zoom), and a test of whether a tile at
	that zoom is worth doing and can be done now, highest zooms first. For the
	rings around the seeds, base-level summaries: the cheapest way to have the
	pyramid near where someone is looking. For the planet sweep, the base
	tiles instead: their rendering builds the summaries, so the sweep never
	builds a summary it would render over.
	"""
	kinds = []
	base = summaries.base_level()
	base_is_tile = base is not None and (settings.TILE_ZOOMS.get(base) or {}).get("source") == "data"
	levels = summaries.levels()
	if sweep and base_is_tile:
		levels = [level for level in levels if level != base]
	stored = {level: _summaries_at(level) for level in summaries.levels()}
	for level in levels:
		finer = summaries.finer_level(level)

		def ready(x, y, level=level, finer=finer):
			if (x, y) in stored[level]:
				return False
			if finer is None:
				return True
			return all((fx, fy) in stored[finer] for _, fx, fy in summaries.tiles_beneath(level, x, y, finer))
		kinds.append((level, TileState.SUMMARY, ready))

	# A tile of a cached zoom can be rendered ahead; the base tile, which is
	# rendered for the sweep (its rendering builds its summary)
	for zoom, config in settings.TILE_ZOOMS.items():
		if not (config.get("cache") or (sweep and zoom == base and base_is_tile)):
			continue
		rendered = set(TileState.objects.filter(kind=TileState.TILE, zoom=zoom, status=TileState.DONE,
			rendered_at__isnull=False).values_list("x", "y"))

		def ready(x, y, zoom=zoom, config=config, rendered=rendered):
			if (x, y) in rendered:
				return False
			if config["source"] != "summary":
				return True
			level = config["level"]
			return all((sx, sy) in stored.get(level, ()) for _, sx, sy in summaries.tiles_beneath(zoom, x, y, level))
		kinds.append((zoom, TileState.TILE, ready))

	# Highest zooms first; at the same zoom, tiles before summaries (rendering
	# a tile from the map data builds its summary too)
	return sorted(kinds, key=lambda k: (-k[0], k[1] != TileState.TILE))


def _rendered_x(kind, zoom, y):
	"""The x of row y at (kind, zoom) that are done: stored summaries, or
	rendered tiles."""
	if kind == TileState.SUMMARY:
		return TileSummary.objects.filter(zoom=zoom, y=y).values_list("x", flat=True)
	return TileState.objects.filter(kind=TileState.TILE, zoom=zoom, y=y,
		status=TileState.DONE, rendered_at__isnull=False).values_list("x", flat=True)


def _ensure_sweep_rows(kind, zoom):
	"""Gives (kind, zoom) a TileSweep row for every row of its grid, if it
	hasn't already: 2^zoom rows."""
	n = 1 << zoom
	have = set(TileSweep.objects.filter(kind=kind, zoom=zoom).values_list("y", flat=True))
	missing = [TileSweep(kind=kind, zoom=zoom, y=y) for y in range(n) if y not in have]
	if missing:
		TileSweep.objects.bulk_create(missing, ignore_conflicts=True)


def _frontiers(limit, rank, kind, zoom, worker):
	"""Up to limit tiles from the front of (kind, zoom): the first unrendered
	tiles, left to right, top to bottom, with the rank. The rows they are in
	are claimed for the check, as for jobs, so any number of renderers can
	work the rows of a zoom at once. Which tiles are rendered is rechecked on
	the claim, so a tile that has rendered (by a request or another renderer)
	is never planned again, whatever held it up (a missing summary, a failed
	attempt, a renderer that died). A row is done only when every tile in it
	is rendered, so a zoom nobody has touched keeps its rows open, ready for
	the sweep."""
	_ensure_sweep_rows(kind, zoom)
	rows = list(TileSweep.objects.filter(kind=kind, zoom=zoom, done=False, claimed_by="")
		.order_by("y").values_list("pk", "y")[:limit])
	claimed = []
	for pk, y in rows:
		if TileSweep.objects.filter(pk=pk, done=False, claimed_by="").update(
				claimed_by=worker, claimed_at=timezone.now()):
			claimed.append(y)
	frontiers = []
	n = 1 << zoom
	for y in claimed:
		rendered = set(_rendered_x(kind, zoom, y))
		x = 0
		while x in rendered:
			x += 1
		if x < n:
			frontiers.append((y, x))
	frontiers.sort()
	for y in claimed:
		TileSweep.objects.filter(y=y, claimed_by=worker).update(claimed_by="", claimed_at=None)
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
