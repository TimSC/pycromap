# Pending

## Finished sweep rows aren't swept again when the summary recipe changes

When the summary recipe changes (`summaries/summary.lua`, or the settings that
shape summaries), every stored summary is out of date. The idle sweep counts
only current summaries as built (`_rendered_xy` in `tiles/idle.py`), so a row
it is still working on rebuilds them. But `_frontiers` only claims rows that
have never been finished (`last_checked` is null): once a row is full it is
released with a timestamp and never looked at again, so the out-of-date
summaries in finished rows stay that way until someone requests a tile over
them.

No row has been finished yet (the sweep hasn't completed one since the
glacier change made all 1.39M summaries out of date), so nothing is stuck
today; it will matter at the next recipe change.

To do: when the recipe changes, reset the summary rows so the sweep starts
over, by clearing `last_checked` on `TileSweep` rows with `kind="summary"`. A
management command, or a check at startup comparing the recipe with the one
the rows were swept with (stored next to them), would do it.
