# pycromap

A map tile server for a [pycrocosm](https://github.com/TimSC/pycrocosm) map database. Django handles the requests and decides what to render; [tilemaker-core](tilemaker/), a git submodule, renders each vector tile from the map data of its area.

## How it works

1. The map asks for a tile, `/tiles/{z}/{x}/{y}.pbf`. The request is kept as a `TileRequest` row while it is answered.
2. A cached tile with a current file is answered with a redirect to that file, and its request is flagged `cache_redirect`: there is nothing to render for it, and no renderer claims it. Otherwise the request is the queue's entry for the tile: it waits (up to `TILE_REQUEST_WAIT` seconds) for a renderer.
3. A renderer, `manage.py render_tiles`, claims the newest waiting request, with every other waiting for the same tile, and adds a `Job` for the tile, as rendering. It reads the map data of the tile's area from the pycrocosm database, or through pycrocosm's web API (`mapdata/`, see below), writes it as o5m (or PBF), and runs tilemaker-core with `--tile`. A request for a tile already being rendered waits for that rendering. If the client goes away first, its request is flagged cancelled, and no renderer claims it.
4. The tile is written to `TILE_CACHE_DIR`, named with the version of the map data it came from, recorded as a `Tile` row, its job is deleted, and every request waiting for it is answered: a redirect to the file for a cached zoom, the tile itself otherwise, or 204 if there was nothing to draw. The request's row is then deleted (a cancelled one is kept `CANCELLED_REQUEST_KEEP` seconds).

`TILE_ZOOMS` in the settings says how each zoom is made:

| Zoom | Made from | Kept |
|------|-----------|------|
| 12 | the map data | as a file, rendered again when asked for after an hour (`max_age`) |
| 11, 10 | the level-12 summaries beneath it | as a file |
| 9, 8 | the level-10 summaries beneath it | as a file |
| 7, 6 | the level-8 summaries beneath it | as a file |

z12 is the highest zoom rendered: the profile gives it everything upstream's profile draws up to z14, and the map viewer scales its tiles up beyond it, to `VIEWER_MAX_ZOOM`.

### The profile

[profile/](profile/) is pycromap's copy of tilemaker's OpenMapTiles profile (`config.json` and `process.lua`), to change as pycromap needs; `TILEMAKER_CONFIG` and `TILEMAKER_PROCESS` point elsewhere. It differs from upstream in drawing at z12 what upstream draws only at z13 and z14 (`TOP_ZOOM` in `process.lua`, and the layers' `minzoom` and `simplify_below` in `config.json`), and in its `maxzoom` of 12.

### Working ahead

With `RENDER_IDLE_WORK` on, renderers with nothing to do build the summaries for the whole planet, so the pyramid is there before anyone asks; tiles are only rendered when asked for, except those of zooms marked `idle_tile_gen` (below). Only what can be done straight away is chosen: a base-level summary from the map data, or a coarser one whose finer summaries are all current. `TileSweep` has one row per row of summaries at each level; a renderer claiming a row rechecks which of its summaries are built and current, and hands out the first gaps that can be built (up to 32 at a time), the finest level first. A summary that fails is tried again once it can be built, and passed over after `RENDER_ATTEMPTS` failures. The rows advance in parallel, and a summary built for a request is skipped on the next recheck. Once summary work runs out, they render the tiles of the zooms marked `idle_tile_gen` in `TILE_ZOOMS` the same way (`TileSweep` rows of kind `tile`), the highest such zoom first: a tile is done once rendered with its file still in the cache, and handed out once every summary it is made from is built. One renderer plans at a time, keeping `IDLE_QUEUE` idle jobs queued as `Job` rows (flagged `idle`); renderers take requested work first, and a request for a tile queued as idle work takes the tile. With it off, renderers leave jobs queued ahead alone.

### Summaries

A tile's summary keeps the objects of its area worth drawing at lower zooms, picked by [summaries/summary.lua](summaries/summary.lua), whole, with their geometry simplified. Summaries form a pyramid of the levels `TILE_ZOOMS` marks `summary` (12, 10 and 8). A level-12 summary is built from the map data whenever its z12 tile is rendered, or on its own if one is needed; each coarser level is built from the summaries of the next finer level beneath it, merged (keeping the newest copy of objects found in several), filtered again and simplified more. A tile rendered from summaries merges those beneath it at its zoom's level: z11 and z10 the four level-12 summaries beneath them, z9 the four level-10 summaries beneath it.

Summaries are stored in pycromap's database (`TileSummary`), with the version of the map data they were built from, their format, and a recipe: the id of a `Recipe` row, which records a hash of `summary.lua` and the settings that shape summaries, and those settings, the first time they are used. Change either, and existing summaries are out of date and built again when needed.

The database keeps work and results apart. `Job` holds only work to do: a tile or summary queued, being rendered, or failed (kept, to be tried again, and passed over after `RENDER_ATTEMPTS` failures); a job is deleted once done. What is done is a `Tile` (a rendered tile: its file, or for an uncached zoom the tile itself) or a `TileSummary`. Both are keyed by their tile, `(zoom << 48) | (y << 24) | x` (`tiles.tilegeo.tile_key`), as SQLite's rowid: the tables are stored in that order, and the tiles of a run of a row are a range of keys, so looking along a row is one range read, with no index of its own. Their `data` column comes last, so the columns before it are read without reading through it.

A tile is rendered on demand at or above `CASCADE_MIN_ZOOM` (8). The renderer that claims a request for one made from summaries is responsible for it and for every summary it needs that is missing, all the way down: the summaries it is made from, those they are built from, and so on, to the base level, built from the map data. Again and again it takes, of those missing and not underway, one at the highest zoom whose own summaries are all there, adds a `Job` for it, claims it and builds it; then it renders the tile. Summaries other renderers are building, it waits for. It keeps its claims fresh as it goes, so a long cascade isn't taken for abandoned, and stops if no request wants the tile any more (what it has built is kept), or fails the tile if a summary it needs can't be built. A cascade can be long: each zoom between summary levels multiplies it by 16, so a z8 tile nobody has looked near can need 256 base summaries, each a read of a z12 tile's map data. Below `CASCADE_MIN_ZOOM`, a request is queued only if each summary its tile is made from is built, or can be built straight away from what is built (one level, not what that is made from): once no request at or above `CASCADE_MIN_ZOOM` is waiting, the renderer builds those missing and renders it. So a z6 tile can be drawn once the level-10 summaries beneath its level-8 ones are built, by the idle renderers (which build the pyramid from the bottom) or by requests nearby. Until then, the web server answers it at once with a placeholder (an empty tile, 204, with `X-Tile-Status: placeholder`, not to be cached), so a request that can't be rendered yet never waits in the queue.

The map data is read from the pycrocosm map database directly, or, with `MAP_SOURCE=api`, through pycrocosm's web API at `PYCROCOSM_API_URL`, for a pycromap that can't reach the database. Through the API, an area is read with pycrocosm's Overpass (`api/interpreter`), which finds ways and relations by their stored bounding boxes as the database reader does, then the other member ways of multipolygons drawn whole and relations' node members; edits are checked with `replication/changed`. It reads the same objects and renders the same tiles, several times slower, and dates each tile by the map's latest edit as it was read rather than by the newest edit of what is in it.

The map database is read the way pgmap reads it: in one repeatable-read, read-only transaction, with pgmap's tables locked first (static layer, then modify layer, each in pgmap's order) so that everything read is from one moment. The static and modify layers are merged as pgmap's views do.

## Layout

- `pycromap/` - the Django project: settings and URLs
- `mapdata/` - reading the pycrocosm map: its database (`DATABASES["pycrocosm"]`, only ever read), or its web API (`api.py`), as `MAP_SOURCE` says (`source.py`)
- `tiles/` - tile state and the render queue, rendering, the tile and map page views, and the `render_tiles` command
- `summaries/` - tile summaries: building, merging, and `summary.lua`
- `profile/` - the tilemaker profile tiles are rendered with
- `tilemaker/` - tilemaker-core, the renderer (a submodule)

## Setting up

Fetch the submodules, and build tilemaker-core (see [its INSTALL.md](tilemaker/docs/INSTALL.md) for the packages it needs):

    git submodule update --init --recursive
    cmake -S tilemaker -B tilemaker/build -DCMAKE_BUILD_TYPE=Release
    cmake --build tilemaker/build --parallel 4

Make a Python environment and install the requirements, and pyo5m from pycrocosm's extractupdate:

    python3 -m venv venv
    venv/bin/pip install -r requirements.txt
    venv/bin/pip install -e ../extractupdate/pyo5m

Copy `.env.example` to `.env` and give it the pycrocosm database's details, then make pycromap's own database (SQLite, `db.sqlite3`):

    venv/bin/python manage.py migrate

## Running

Run the web server and, alongside it, the renderer fleet: `RENDER_WORKERS` (8) renderers, each in its own process (rendering is CPU-bound, so more threads in one process would not help), supervised and replaced by `render_fleet` if they die:

    venv/bin/uvicorn pycromap.asgi:application --port 8000 --reload
    venv/bin/python manage.py render_fleet

The map is at http://localhost:8000/. The tile view is asynchronous: under an ASGI server, as uvicorn is, waiting for the renderer holds no thread, and when the client goes away, as a map does with the tiles it no longer shows, Django cancels the request, and a tile still waiting for a renderer is left until the requests still waiting have been seen to (and is put back in front if it is asked for again). `--reload` restarts it when the code changes, as for development. Tile states can be seen in the admin, at `/admin/` (`manage.py createsuperuser` first).

Each renderer renders one job at a time; `RENDER_WORKERS` (8) of them, in separate processes, render that many at once. A renderer that stops gives what it holds to the others as it goes; one that dies without doing so stops sending its heartbeat (every `RENDER_HEARTBEAT` seconds), and what it held is given to the others once `RENDER_LEASE` seconds (90) have passed without one. The web server clears out the requests table as it starts (`manage.py clear_requests`), as requests in flight when it stopped have gone with it. A tile that fails `RENDER_ATTEMPTS` times is marked as failed until it is asked for again.

Tiles are gzipped. A cached zoom's tiles are files in `TILE_CACHE_DIR`, one per tile, named with the data version they came from (`{z}/{x}/{y}.{version}.pbf.gz`); a newer version replaces the older. Every tile is rendered by tilemaker-core, and what comes out is then looked up among the common tiles: contents a great many tiles have, as open sea or land with nothing mapped, each kept once in `common/` (see `tiles/common.py`). A tile with a common content points at that file rather than having its own, so the ocean is one file, which a browser fetches once. Which contents are common is chosen by hand: a file in `common/` makes its content common. Files are named for what they show where a short name fits (`land`, `sea`, `sea-2poly`, `sea-part`, `park`), and otherwise by their content's hash. `tile_hash_counts.py` counts how many cached tiles have each content, and `common_from_counts.py` makes those above a threshold common (both throw-away scripts). Other zooms keep only each tile's latest rendering, in the database. In production, have the web server serve `TILE_CACHE_DIR` at `TILE_CACHE_URL`, saying the files are gzipped vector tiles; with `DEBUG` on, Django does. With nginx:

    location /tilecache/ {
        alias /path/to/tilecache/;
        types { }
        default_type application/vnd.mapbox-vector-tile;
        add_header Content-Encoding gzip;
        # The version is in the name, so a file never changes
        add_header Cache-Control "public, max-age=31536000, immutable";
    }

## With Docker

`docker-compose.yml` runs the web server and renderers, with pycromap's database and tile cache in a `data/` directory on the host they share:

    git submodule update --init --recursive
    docker compose up --build

The map is then at http://localhost:8082/. Its view is kept in the address, as `#zoom/lat/lon`: http://localhost:8082/#14/50.8/-1.09 opens on Portsmouth at z14, and a link copied from the address bar opens where you were. The image builds tilemaker-core from the submodule, and pyo5m from `../extractupdate/pyo5m` (`PYO5M_SOURCE` to change it).

The pycrocosm database's details come from `.env`, as without Docker, but the containers reach it at `host.docker.internal`, the machine running Docker, unless `PYCROMAP_DB_HOST` says otherwise. PostgreSQL there must accept connections from the Docker network. Set `COASTLINE_DIR` and `LANDCOVER_DIR` to where the shapefiles the profile reads are, if you have them.

The web server is uvicorn, with `WEB_WORKERS` processes (16), each of which can have many tile requests waiting for the renderer at once; a request is cancelled when its client goes away. A request waits up to `TILE_REQUEST_WAIT` seconds (600); uvicorn sets no time limit of its own.

## Tests

    venv/bin/python manage.py test

They don't use the pycrocosm database. Those that run tilemaker-core are skipped if it hasn't been built.
