# pycromap

A map tile server for a [pycrocosm](https://github.com/TimSC/pycrocosm) map database. Django handles the requests and decides what to render; [tilemaker-core](tilemaker/), a git submodule, renders each vector tile from the map data of its area.

## How it works

1. The map asks for a tile, `/tiles/{z}/{x}/{y}.pbf`. The request is logged in the tile's `TileState` row.
2. A cached tile with a current file is answered with a redirect to that file. Anything else is queued, ahead of everything asked for earlier, and the request waits for it (up to `TILE_REQUEST_WAIT` seconds).
3. A renderer, `manage.py render_tiles`, claims queued tiles, highest priority first. For each, it reads the map data of the tile's area from the pycrocosm database (`mapdata/`), writes it as o5m (or PBF), and runs tilemaker-core with `--tile`.
4. The tile is written to `TILE_CACHE_DIR`, named with the version of the map data it came from (the newest `edit_activity` ID), and the request is answered: a redirect to the file for a cached zoom, the tile itself otherwise, or 204 if there was nothing to draw.

`TILE_ZOOMS` in the settings says how each zoom is made. For now only z13 is served, rendered afresh for every request.

The map database is read the way pgmap reads it: in one repeatable-read, read-only transaction, with pgmap's tables locked first (static layer, then modify layer, each in pgmap's order) so that everything read is from one moment. The static and modify layers are merged as pgmap's views do.

## Layout

- `pycromap/` - the Django project: settings and URLs
- `mapdata/` - reading the pycrocosm map database (`DATABASES["pycrocosm"]`, only ever read)
- `tiles/` - tile state and the render queue, rendering, the tile and map page views, and the `render_tiles` command
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

Run the web server and, alongside it, at least one renderer:

    venv/bin/python manage.py runserver
    venv/bin/python manage.py render_tiles --workers 2

The map is at http://localhost:8000/. Tile states can be seen in the admin, at `/admin/` (`manage.py createsuperuser` first).

Each renderer process renders `--workers` tiles at once; run more processes to render more. A tile claimed by a renderer that stops is given to another after `RENDER_LEASE` seconds, and a tile that fails `RENDER_ATTEMPTS` times is marked as failed until it is asked for again.

In production, have the web server serve `TILE_CACHE_DIR` at `TILE_CACHE_URL`; with `DEBUG` on, Django does.

## With Docker

`docker-compose.yml` runs the web server and a renderer, with pycromap's database and tile cache in a volume they share:

    git submodule update --init --recursive
    docker compose up --build

The map is then at http://localhost:8082/. The image builds tilemaker-core from the submodule, and pyo5m from `../extractupdate/pyo5m` (`PYO5M_SOURCE` to change it).

The pycrocosm database's details come from `.env`, as without Docker, but the containers reach it at `host.docker.internal`, the machine running Docker, unless `PYCROMAP_DB_HOST` says otherwise. PostgreSQL there must accept connections from the Docker network. Set `COASTLINE_DIR` and `LANDCOVER_DIR` to where the shapefiles the tilemaker config reads are, if you have them. The web server is Django's development server, for now.

## Tests

    venv/bin/python manage.py test

They don't use the pycrocosm database. Those that run tilemaker-core are skipped if it hasn't been built.
