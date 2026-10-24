# Pending

## Summaries built before the ring fix still count as current

`simplify_refs` (`summaries/build.py`) used to simplify every way as a plain
line, so the rings of areas could fold over themselves, most at the coarser
levels: an area with such a ring is drawn wrongly, or partly not at all. Near
58.32,8.12 (southern Norway), a forest's outer ring broke at level 10, and the
z8 tile drew 48% of that area as wood against z10's 87.5%. Rings are now
simplified without crossing themselves (`preserve_topology` for closed ways).

The recipe was left alone, so that the current run could finish: summaries
built before the fix are still taken as current. To do: once the run is
done, change the recipe (add the ring simplification to `recipe()`'s shaping
settings), so they are all built again. Do the item below first, or the
finished sweep rows won't be.

## Finished sweep rows aren't swept again when the summary recipe changes

When the summary recipe changes (`summaries/summary.lua`, or the settings that
shape summaries), every stored summary is out of date. The idle sweep counts
only current summaries as built (`_built_in_row` in `tiles/idle.py`), so a row
it is still working on rebuilds them. But `_frontiers` only claims rows that
have never been finished (`last_checked` is null): once a row is full it is
released with a timestamp and never looked at again, so the out-of-date
summaries in finished rows stay that way until someone requests a tile over
them.

Since the database was recreated, rows have been finishing (1,320 of the
4,096 at z12 by 2026-10-24), so this matters at the next recipe change: the
ring fix above, for one.

To do: when the recipe changes, reset the summary rows so the sweep starts
over, by clearing `last_checked` on `TileSweep` rows with `kind="summary"`. A
management command, or a check at startup comparing the recipe with the one
the rows were swept with (stored next to them), would do it.
