# pycromap

A map tile server for a [pycrocosm](https://github.com/TimSC/pycrocosm) map database. Django handles the requests and decides what to render; [tilemaker-core](tilemaker/), a git submodule, renders each vector tile from the map data of its area.

## How it works

1. The map asks for a tile, `/tiles/{z}/{x}/{y}.pbf`. The request is kept as a `TileRequest` row while it is answered, and logged in the tile's `TileState` row (how often, and when: the idle renderers work near the tiles asked for).
2. A cached tile with a current file is answered with a redirect to that file, and its request is flagged `cache_redirect`: there is nothing to render for it, and no renderer claims it. Otherwise the request is the queue's entry for the tile: it waits (up to `TILE_REQUEST_WAIT` seconds) for a renderer.
3. A renderer, `manage.py render_tiles`, claims the newest waiting request, with every other waiting for the same tile, and adds the tile to `TileState` as rendering. It reads the map data of the tile's area from the pycrocosm database (`mapdata/`), writes it as o5m (or PBF), and runs tilemaker-core with `--tile`. A request for a tile already being rendered waits for that rendering. If the client goes away first, its request is flagged cancelled, and no renderer claims it.
4. The tile is written to `TILE_CACHE_DIR`, named with the version of the map data it came from, and every request waiting for it is answered: a redirect to the file for a cached zoom, the tile itself otherwise, or 204 if there was nothing to draw. The request's row is then deleted (a cancelled one is kept `CANCELLED_REQUEST_KEEP` seconds).

`TILE_ZOOMS` in the settings says how each zoom is made:

| Zoom | Made from | Kept |
|------|-----------|------|
| 14, 13 | cut from the z12 tile covering it, scaled up | no |
| 12 | the map data | as a file, rendered again when asked for after an hour (`max_age`) |
| 11, 10 | the level-12 summaries beneath it | as a file |
| 9, 8 | the level-10 summaries beneath it | as a file |
| 7, 6 | the level-8 summaries beneath it | as a file |

z12 is the highest zoom rendered: the profile gives it everything upstream's profile draws up to z14, so z13 and z14 need no rendering of their own. Cutting a tile from a lower zoom's (`"source": "crop"`) decodes it, scales the part covered up, clips it a little beyond the edge, and encodes it again: quick, without the map data, but no more precise than the tile it came from. The tile it comes from is obtained as a request for it would be.

### The profile

[profile/](profile/) is pycromap's copy of tilemaker's OpenMapTiles profile (`config.json` and `process.lua`), to change as pycromap needs; `TILEMAKER_CONFIG` and `TILEMAKER_PROCESS` point elsewhere. It differs from upstream in drawing at z12 what upstream draws only at z13 and z14 (`TOP_ZOOM` in `process.lua`, and the layers' `minzoom` and `simplify_below` in `config.json`), and in its `maxzoom` of 12.

### Working ahead

With `RENDER_IDLE_WORK` on, renderers with nothing to do work on what nobody has asked for yet: summaries, and tiles of cached zooms (an uncached zoom's tiles would only be rendered again when asked for). Only what can be done straight away is chosen, as for requests. The highest zooms come first, then those nearest the newest requested tile, within `IDLE_RADIUS` tiles (4) of the `IDLE_SEEDS` (200) most recently requested; work done ahead never counts as requested, so it doesn't lead further. Then the idle fleet keeps going and renders the rest of the planet, so the whole world eventually has tiles: `TileSweep` has one row per row of tiles (at each zoom), and a renderer claiming a row rechecks which of its tiles are rendered, so the first gap is what's left to do. The rows advance in parallel, and a tile rendered by a request is skipped on the next recheck. One renderer plans at a time, keeping `IDLE_QUEUE` idle jobs queued in `TileState` (a negative priority marks them); renderers take requested work first, and a request for a tile queued as idle work takes the tile. With it off, renderers leave jobs queued ahead alone.

### Summaries

A tile's summary keeps the objects of its area worth drawing at lower zooms, picked by [summaries/summary.lua](summaries/summary.lua), whole, with their geometry simplified. Summaries form a pyramid of the levels `TILE_ZOOMS` marks `summary` (12, 10 and 8). A level-12 summary is built from the map data whenever its z12 tile is rendered, or on its own if one is needed; each coarser level is built from the summaries of the next finer level beneath it, merged (keeping the newest copy of objects found in several), filtered again and simplified more. A tile rendered from summaries merges those beneath it at its zoom's level: z11 and z10 the four level-12 summaries beneath them, z9 the four level-10 summaries beneath it.

Summaries are stored in pycromap's database (`TileSummary`), with the version of the map data they were built from, their format, and a recipe: a hash of `summary.lua` and the settings that shape summaries. Change either, and existing summaries are out of date and built again when needed.

A request doesn't queue the summaries a tile needs, for now. A tile made from summaries is rendered only once every summary it is made from is built; until then it is answered with a placeholder: an empty tile (204) with `X-Tile-Status: placeholder`, not to be cached, and nothing is queued for it (the request is logged). The pyramid fills from the bottom: a level-12 summary is built whenever its z12 tile is rendered, and the idle renderers build the rest. A summary job whose finer summaries aren't all built fails, rather than queueing them.

The map database is read the way pgmap reads it: in one repeatable-read, read-only transaction, with pgmap's tables locked first (static layer, then modify layer, each in pgmap's order) so that everything read is from one moment. The static and modify layers are merged as pgmap's views do.

## Layout

- `pycromap/` - the Django project: settings and URLs
- `mapdata/` - reading the pycrocosm map database (`DATABASES["pycrocosm"]`, only ever read)
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

Each renderer renders one job at a time; `RENDER_WORKERS` (8) of them, in separate processes, render that many at once. A tile claimed by a renderer that stops is given to another after `RENDER_LEASE` seconds, and a tile that fails `RENDER_ATTEMPTS` times is marked as failed until it is asked for again.

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
