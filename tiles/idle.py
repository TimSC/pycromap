"""Work for idle renderers (RENDER_IDLE_WORK): tiles and summaries nobody has
asked for yet, near those they have.

Only work that can be done straight away is chosen, as for requests: a
summary at the base level (from the map data), a coarser summary whose
summaries beneath it all exist, or a tile of a cached zoom that can be
rendered now (rendering an uncached zoom's tile ahead would be wasted). The
highest zooms come first, then those nearest the newest requested tile: only
tiles someone asked for count, not those idle renderers did.
"""

import math

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from summaries import build as summaries
from summaries.models import TileSummary

from .models import TileState

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


def _near(seeds, zoom, radius):
	"""Tiles at zoom around the seeds: (distance, x, y), the distance in tiles
	at zoom, counting diagonals as one. The newest seed's rings come before
	the next-newest seed's, so the place the user is now wins over where they
	were."""
	n = 1 << zoom
	centres = []
	seen_centres = set()
	for seed_zoom, sx, sy in seeds:  # newest first
		scale = 2.0 ** (zoom - seed_zoom)
		centre = (min(n - 1, int(math.floor((sx + 0.5) * scale))), min(n - 1, int(math.floor((sy + 0.5) * scale))))
		if centre in seen_centres:
			continue
		seen_centres.add(centre)
		centres.append(centre)
	seen = set()
	for cx, cy in centres:
		for r in range(radius + 1):
			for x in range(cx - r, cx + r + 1):
				for y in range(cy - r, cy + r + 1):
					if max(abs(x - cx), abs(y - cy)) != r or not (0 <= x < n and 0 <= y < n) or (x, y) in seen:
						continue
					seen.add((x, y))
					yield r, x, y


def _candidates():
	"""What could be done now, as (kind, zoom), and a test of whether a tile at
	that zoom is worth doing and can be done now, highest zooms first."""
	kinds = []
	levels = summaries.levels()
	stored = {level: _summaries_at(level) for level in levels}
	for level in levels:
		finer = summaries.finer_level(level)

		def ready(x, y, level=level, finer=finer):
			if (x, y) in stored[level]:
				return False
			if finer is None:
				return True
			return all((fx, fy) in stored[finer] for _, fx, fy in summaries.tiles_beneath(level, x, y, finer))
		kinds.append((level, TileState.SUMMARY, ready))

	for zoom, config in settings.TILE_ZOOMS.items():
		if not config.get("cache"):
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


def plan(count):
	"""Up to count jobs (kind, zoom, x, y) for idle renderers, best first."""
	seeds = _seeds()
	if not seeds or count <= 0:
		return []
	chosen = []
	# Base-level tiles from the map data whose rendering builds their summary
	rendering = set()
	for zoom, kind, ready in _candidates():
		busy = _busy(kind, zoom)
		if kind == TileState.SUMMARY and zoom == summaries.base_level():
			busy |= rendering
			if (settings.TILE_ZOOMS.get(zoom) or {}).get("source") == "data":
				busy |= _busy(TileState.TILE, zoom)
		for _, x, y in _near(seeds, zoom, settings.IDLE_RADIUS):
			if (x, y) in busy or not ready(x, y):
				continue
			chosen.append((kind, zoom, x, y))
			if kind == TileState.TILE and zoom == summaries.base_level() and settings.TILE_ZOOMS[zoom]["source"] == "data":
				rendering.add((x, y))
			if len(chosen) >= count:
				return chosen
	return chosen


def queue_idle_work():
	"""Queues idle jobs until there are as many idle jobs queued as there are
	renderers (RENDER_WORKERS), so the whole fleet has work when the requests
	run out. Returns how many it queued."""
	queued = TileState.objects.filter(Q(not_before__isnull=True) | Q(not_before__lte=timezone.now()),
		status=TileState.QUEUED, idle=True).count()
	wanted = settings.RENDER_WORKERS - queued
	if wanted <= 0:
		return 0
	now = timezone.now()
	made = 0
	for rank, (kind, zoom, x, y) in enumerate(plan(wanted)):
		with transaction.atomic():
			job, _ = TileState.objects.get_or_create(kind=kind, zoom=zoom, x=x, y=y,
				defaults={"status": TileState.DONE})
			if job.status in (TileState.QUEUED, TileState.RENDERING, TileState.FAILED):
				continue
			# At the deepest depth, so that it never makes jobs of its own
			TileState.objects.filter(pk=job.pk).update(status=TileState.QUEUED, idle=True,
				priority=IDLE_PRIORITY - rank, depth=settings.SUMMARY_MAX_DEPTH, queued_at=now,
				not_before=None, waiting_since=None, attempts=0, error="")
			made += 1
	return made
