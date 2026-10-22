# Pending

## The idle sweep doesn't rebuild out-of-date summaries

When the summary recipe changes (`summaries/summary.lua`, or the settings that
shape summaries), every stored summary is out of date. Requests rebuild the
ones they need, but the planet-wide idle sweep never rebuilds the rest:

- `_rendered_xy` in `tiles/idle.py` counts every stored `TileSummary` as
  done, current or not (it doesn't filter with `summaries.current`), so an
  out-of-date summary never becomes a sweep row's frontier.
- `_frontiers` only claims rows that have never been checked (`last_checked`
  is null). Once a row is full it is released with a timestamp and never
  looked at again.

So out-of-date summaries away from what people look at stay that way until
someone requests a tile over them.

To do:

1. Have `_rendered_xy` count only current summaries
   (`summaries.current(TileSummary.objects.filter(...))`). The query must still
   find each row's few summaries through the index (see the comment above
   `_stored_in`).
2. When the recipe changes, reset the summary rows, so the sweep starts over:
   clear `last_checked` on `TileSweep` rows with `kind="summary"`. A
   management command, or a check at startup comparing the recipe with the one
   the rows were swept with (stored next to them), would do it.

Pending since glaciers were dropped from `summary.lua`, which made all
1.39M summaries out of date.
