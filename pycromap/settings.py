"""
Django settings for pycromap, a map tile server for a pycrocosm map database.

Values that differ between installations, and secrets, come from the
environment, or from a .env file beside manage.py (see .env.example).
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
	"""Sets environment variables from KEY=value lines, without replacing any
	already set."""
	if not path.exists():
		return
	for line in path.read_text().splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		os.environ.setdefault(key.strip(), value.strip())


_load_env_file(BASE_DIR / ".env")


def env(key, default=None):
	return os.environ.get(key, default)


DEBUG = env("DJANGO_DEBUG", "true").lower() in ("1", "true", "yes")

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
	if not DEBUG:
		raise RuntimeError("Set DJANGO_SECRET_KEY when DEBUG is off")
	SECRET_KEY = "pycromap-development-only"

ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
	"django.contrib.admin",
	"django.contrib.auth",
	"django.contrib.contenttypes",
	"django.contrib.sessions",
	"django.contrib.messages",
	"django.contrib.staticfiles",
	"mapdata",
	"summaries",
	"tiles",
]

MIDDLEWARE = [
	"django.middleware.security.SecurityMiddleware",
	"django.contrib.sessions.middleware.SessionMiddleware",
	"django.middleware.common.CommonMiddleware",
	"django.middleware.csrf.CsrfViewMiddleware",
	"django.contrib.auth.middleware.AuthenticationMiddleware",
	"django.contrib.messages.middleware.MessageMiddleware",
	"django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "pycromap.urls"

TEMPLATES = [
	{
		"BACKEND": "django.template.backends.django.DjangoTemplates",
		"DIRS": [],
		"APP_DIRS": True,
		"OPTIONS": {
			"context_processors": [
				"django.template.context_processors.request",
				"django.contrib.auth.context_processors.auth",
				"django.contrib.messages.context_processors.messages",
			],
		},
	},
]

# Seconds each part of rendering a tile may take: each query of the map
# database, and tilemaker
RENDER_TIMEOUT = int(env("RENDER_TIMEOUT", "600"))

# pycromap's own database: the state of tile rendering, and summaries. SQLite
# for now. WAL lets the web server read while the renderer writes, and
# immediate transactions take the write lock up front, so two writers wait
# for each other rather than fail.
#
# "pycrocosm" is the pycrocosm map database, which the map data is read from
# (see mapdata/). It is only read: the router keeps migrations off it. Its
# transactions are repeatable read, so that everything one read sees is from
# one moment; the password is best kept in the environment or .env.
PYCROMAP_DB = env("PYCROMAP_DB", str(BASE_DIR / "db.sqlite3"))
DATABASES = {
	"default": {
		"ENGINE": "django.db.backends.sqlite3",
		"NAME": PYCROMAP_DB,
		"OPTIONS": {
			"timeout": 30,
			"transaction_mode": "IMMEDIATE",
			# A commit in WAL mode needn't wait for the disk (synchronous=NORMAL):
			# the database stays sound, and a power cut can lose only the last
			# commits, work done again. Each commit holds the one write lock,
			# so waiting for the disk there stalls every renderer.
			"init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
		},
		# A file beside the database, as tests render in threads: an in-memory
		# database's shared cache fails at once on a locked table, rather than
		# waiting
		"TEST": {"NAME": str(Path(PYCROMAP_DB).parent / "test_db.sqlite3")},
	},
	"pycrocosm": {
		"ENGINE": "django.db.backends.postgresql",
		"HOST": env("PYCROCOSM_DB_HOST", "localhost"),
		"PORT": env("PYCROCOSM_DB_PORT", "5432"),
		"NAME": env("PYCROCOSM_DB_NAME", "db_map"),
		"USER": env("PYCROCOSM_DB_USER", "pycrocosm"),
		"PASSWORD": env("PYCROCOSM_DB_PASSWORD", ""),
		# Kept open between jobs (and requests), rather than a new connection,
		# and a new PostgreSQL process, for each: a renderer reads the map
		# for many jobs a second. Checked before reuse, so one the server
		# dropped is replaced.
		"CONN_MAX_AGE": 600,
		"CONN_HEALTH_CHECKS": True,
		"OPTIONS": {
			# psycopg2's ISOLATION_LEVEL_REPEATABLE_READ
			"isolation_level": 2,
			# Milliseconds a statement may take
			"options": "-c statement_timeout={}".format(RENDER_TIMEOUT * 1000),
		},
	},
}

DATABASE_ROUTERS = ["mapdata.routers.MapDataRouter"]

AUTH_PASSWORD_VALIDATORS = [
	{"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
	{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
	{"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
	{"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- The pycrocosm map

# Where the map data is read from: "database", the pycrocosm map database
# itself (DATABASES["pycrocosm"]), or "api", pycrocosm's web API at
# PYCROCOSM_API_URL (Overpass and replication: see mapdata/api.py), for a
# pycromap that can't reach the database. The database is quicker, and
# dates each tile by the newest edit of what is in it, not of the whole map
MAP_SOURCE = env("MAP_SOURCE", "database")
if MAP_SOURCE not in ("database", "api"):
	raise ImproperlyConfigured('MAP_SOURCE must be "database" or "api", not {!r}'.format(MAP_SOURCE))
PYCROCOSM_API_URL = env("PYCROCOSM_API_URL", "http://localhost:8000/")

# The ranges of the map's object IDs, which decide where the multipolygon
# rings summaries make are numbered in tilemaker's input, clear of every real
# way (see tiles/render.py): "fosm", whose objects are numbered below 1e12 or,
# made since pycrocosm, from 2e12; or "osm", OpenStreetMap's, far below 1e12
MAP_ID_RANGES = env("MAP_ID_RANGES", "fosm")
if MAP_ID_RANGES not in ("fosm", "osm"):
	raise ImproperlyConfigured('MAP_ID_RANGES must be "fosm" or "osm", not {!r}'.format(MAP_ID_RANGES))

# The pycrocosm map database (DATABASES["pycrocosm"]), for MAP_SOURCE "database":

# The static layer of tables (the imported planet, pgmap's dbtableprefix) and
# the modify layer of edits over it (dbtablemodifyprefix)
PYCROCOSM_STATIC_PREFIX = env("PYCROCOSM_STATIC_PREFIX", "planet_static_")
PYCROCOSM_MODIFY_PREFIX = env("PYCROCOSM_MODIFY_PREFIX", "pycrocosm_mod_")

# ---- Rendering

# tilemaker-core, built in the submodule (see README.md)
TILEMAKER = {
	"program": env("TILEMAKER_PROGRAM", str(BASE_DIR / "tilemaker" / "build" / "tilemaker")),
	# The profile: pycromap's working copy of tilemaker's OpenMapTiles one
	"config": env("TILEMAKER_CONFIG", str(BASE_DIR / "profile" / "config.json")),
	"process": env("TILEMAKER_PROCESS", str(BASE_DIR / "profile" / "process.lua")),
	# tilemaker runs here, which is where the config's shapefile paths
	# (coastline/, landcover/) are relative to
	"directory": env("TILEMAKER_DIRECTORY", str(BASE_DIR / "tilemaker")),
	# The format the map data is given to tilemaker in: "o5m" or "pbf"
	"input_format": env("TILEMAKER_INPUT_FORMAT", "o5m"),
	# Seconds a tile may take
	"timeout": RENDER_TIMEOUT,
	# Whether the multipolygon rings summaries make, whose IDs are above what
	# tilemaker takes (2^42), are numbered afresh in each tile's input (see
	# tiles/render.py). Turned off, they are given as they are, which stock
	# tilemaker refuses: only for a tilemaker built without that limit.
	"renumber_rings": env("TILEMAKER_RENUMBER_RINGS", "true").lower() in ("1", "true", "yes"),
}

# How each zoom is made. Each zoom served has an entry:
#   "source": "data" - rendered from the map data of the tile's area
#             "summary" - rendered from the summaries beneath the tile at
#             "level" (a summary level, no lower than the zoom), merged
#   "summary": the zooms marked so form the summary pyramid: the finest is
#              built from the map data (as its tiles render, or on its own);
#              each coarser from the summaries of the next finer level
#              beneath it, merged (see summaries/build.py)
#   "cache": whether rendered tiles are kept and served as static files, or
#            rendered afresh for every request
#   "max_age": for a cached zoom, seconds after which a tile is rendered
#              again when asked for (none: kept until it is gone)
#   "check_edits": for a cached zoom made from the map data, whether a tile
#              is rendered again when asked for once the map in its area has
#              been edited since (see mapdata/changes.py); True unless set.
#              Zooms made from summaries aren't checked: rendering them again
#              would only use the same summaries.
#   "idle_tile_gen": for a cached zoom, whether idle renderers render its
#              tiles ahead, once summary work runs out (see tiles/idle.py):
#              the highest such zoom first, each tile once every summary it
#              is made from is built. Otherwise its tiles are only rendered
#              when asked for.
# Zooms without an entry are not served. Above the highest, the map viewer
# scales its tiles up (VIEWER_MAX_ZOOM): the profile gives z12 the content of
# upstream's z14 (see profile/process.lua).
TILE_ZOOMS = {
	12: {"source": "data", "cache": True, "summary": True},
	11: {"source": "summary", "level": 12, "cache": True},
	10: {"source": "summary", "level": 12, "cache": True, "summary": True},
	9: {"source": "summary", "level": 10, "cache": True},
	8: {"source": "summary", "level": 10, "cache": True, "summary": True},
	7: {"source": "summary", "level": 8, "cache": True},
	6: {"source": "summary", "level": 8, "cache": True, "summary": True, "idle_tile_gen": True},
	5: {"source": "summary", "level": 6, "cache": True, "idle_tile_gen": True},
	4: {"source": "summary", "level": 6, "cache": True, "summary": True, "idle_tile_gen": True},
	3: {"source": "summary", "level": 4, "cache": True, "idle_tile_gen": True},
	2: {"source": "summary", "level": 4, "cache": True, "summary": True, "idle_tile_gen": True},
	1: {"source": "summary", "level": 2, "cache": True, "idle_tile_gen": True},
	0: {"source": "summary", "level": 2, "cache": True, "idle_tile_gen": True},
}
for _zoom, _config in TILE_ZOOMS.items():
	if _config.get("idle_tile_gen") and not _config.get("cache"):
		raise ImproperlyConfigured("TILE_ZOOMS[{}]: idle_tile_gen needs cache: an uncached tile "
			"isn't kept".format(_zoom))

# ---- Summaries

# What summaries keep, at each level
SUMMARY_LUA = Path(env("SUMMARY_LUA", str(BASE_DIR / "summaries" / "summary.lua")))

# How far simplified geometry may stray, in pixels of a 4096-pixel tile at the
# summary's level: 16 is about a pixel on screen. Changing it makes stored
# summaries out of date.
SUMMARY_SIMPLIFY_PIXELS = float(env("SUMMARY_SIMPLIFY_PIXELS", "16"))

# Whether summaries merge similar objects entirely within a tile: ways with
# the same tags are concatenated, and multipolygon relations with the same
# tags are combined. Objects reaching the tile's edge are left alone, as
# their copies in adjacent tiles must match.
MERGE_SUMMARY_OBJECTS = env("MERGE_SUMMARY_OBJECTS", "1") == "1"



# Where cached tiles are written, and the URL they are served from (by Django
# when DEBUG is on; in production, by the web server)
TILE_CACHE_DIR = Path(env("TILE_CACHE_DIR", str(BASE_DIR / "tilecache")))
TILE_CACHE_URL = env("TILE_CACHE_URL", "/tilecache/")

# Seconds a tile request waits for the renderer before giving up. The web
# server, uvicorn, sets requests no time limit of its own.
TILE_REQUEST_WAIT = float(env("TILE_REQUEST_WAIT", "600"))

# The lowest zoom whose tiles are rendered on demand whatever they need. A
# request for a tile at or above it is queued, and the renderer that claims it
# builds every summary it needs that is missing, down to the map data, before
# rendering it (see render_tiles.build_dependencies): the lower the zoom, the
# more that can be, 16 times more for each zoom between summary levels. Below
# it, a request is queued only if each summary its tile is made from is
# built, or can be built straight away from what is built, and, once there
# are no requests at or above it, the renderer builds those missing (one
# level, not what they are made from) and renders it; otherwise it gets a
# placeholder at once.
CASCADE_MIN_ZOOM = int(env("CASCADE_MIN_ZOOM", "8"))

# Seconds a request whose client went away is kept, flagged cancelled (see
# tiles.models.TileRequest), before the renderers' housekeeping deletes it
CANCELLED_REQUEST_KEEP = float(env("CANCELLED_REQUEST_KEEP", "600"))

# Seconds after a cached tile was rendered, or last found unedited, before a
# request for it asks the map database again whether its area has been edited
# (see "check_edits" in TILE_ZOOMS): an edit shows within this long, and a
# busy tile costs a query at most this often
EDIT_CHECK_INTERVAL = float(env("EDIT_CHECK_INTERVAL", "600"))

# How many render_tiles processes to run: each renders one job at a time
# (rendering is CPU-bound, and threads in one process share the GIL)
RENDER_WORKERS = int(env("RENDER_WORKERS", "8"))
# Whether idle renderers work ahead, building the summaries for the whole
# planet that nobody has asked for yet (see tiles/idle.py). With nothing to
# do, they look again after IDLE_POLL seconds.
RENDER_IDLE_WORK = env("RENDER_IDLE_WORK", "false").lower() in ("1", "true", "yes")
IDLE_POLL = 10
# How many idle jobs are kept queued for the fleet. One renderer at a time
# plans them, and the queue is topped up once it is down to half, so the
# others render on while it plans: half must outlast a planning round (a few
# seconds), at the fleet's rate (summaries take a few hundredths of a second
# each).
IDLE_QUEUE = int(env("IDLE_QUEUE", "4096"))

# Seconds between a renderer's heartbeats: from a thread of its own, however
# long its job takes, it marks what it holds (its job, the requests it
# claimed, its sweep rows) as still held (claimed_at)
RENDER_HEARTBEAT = 15
# Seconds without a heartbeat after which what a renderer holds is given to
# others (queue.requeue_abandoned, once a minute): it stopped without letting
# go (killed, crashed, or a renderer of an older version)
RENDER_LEASE = 6 * RENDER_HEARTBEAT
# Renders tried before a tile is marked as failed
RENDER_ATTEMPTS = 3

# Where the map page opens: minlon,minlat,maxlon,maxlat
MAP_INITIAL_BBOX = env("MAP_INITIAL_BBOX", "-1.095,50.795,-1.085,50.8")

# How far the map page zooms in. Beyond the highest zoom in TILE_ZOOMS it
# shows that zoom's tiles, scaled up
VIEWER_MAX_ZOOM = int(env("VIEWER_MAX_ZOOM", "18"))

LOGGING = {
	"version": 1,
	"disable_existing_loggers": False,
	"handlers": {"console": {"class": "logging.StreamHandler"}},
	"loggers": {"tiles": {"handlers": ["console"], "level": "INFO"}},
}
