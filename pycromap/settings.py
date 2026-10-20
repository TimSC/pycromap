"""
Django settings for pycromap, a map tile server for a pycrocosm map database.

Values that differ between installations, and secrets, come from the
environment, or from a .env file beside manage.py (see .env.example).
"""

import os
from pathlib import Path

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

WSGI_APPLICATION = "pycromap.wsgi.application"

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
			"init_command": "PRAGMA journal_mode=WAL;",
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

# ---- The pycrocosm map database (DATABASES["pycrocosm"])

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
}

# How each zoom is made. Each zoom served has an entry:
#   "source": "data" - rendered from the map data of the tile's area
#             "summary" - rendered from the summaries beneath the tile at
#             "level" (a summary level, no lower than the zoom), merged
#             "crop" - cut from the tile covering it at zoom "from" (a lower
#             zoom served), scaled up: quick, with that tile's content
#   "summary": the zooms marked so form the summary pyramid: the finest is
#              built from the map data (as its tiles render, or on its own);
#              each coarser from the summaries of the next finer level
#              beneath it, merged (see summaries/build.py)
#   "cache": whether rendered tiles are kept and served as static files, or
#            rendered afresh for every request (not for "crop", never kept)
#   "max_age": for a cached zoom, seconds after which a tile is rendered
#              again when asked for (none: kept until it is gone)
# Zooms without an entry are not served. The highest zoom rendered from the
# data should have everything the zooms cut from it show: the profile gives
# z12 the content of upstream's z14 (see profile/process.lua).
TILE_ZOOMS = {
	#14: {"source": "crop", "from": 12},
	#13: {"source": "crop", "from": 12},
	12: {"source": "data", "cache": True, "summary": True},
	11: {"source": "summary", "level": 12, "cache": True},
	10: {"source": "summary", "level": 12, "cache": True, "summary": True},
	9: {"source": "summary", "level": 10, "cache": True},
	8: {"source": "summary", "level": 10, "cache": True, "summary": True},
	7: {"source": "summary", "level": 8, "cache": True},
	6: {"source": "summary", "level": 8, "cache": True},
}

# ---- Summaries

# What summaries keep, at each level
SUMMARY_LUA = Path(env("SUMMARY_LUA", str(BASE_DIR / "summaries" / "summary.lua")))

# How far simplified geometry may stray, in pixels of a 4096-pixel tile at the
# summary's level: 16 is about a pixel on screen. Changing it makes stored
# summaries out of date.
SUMMARY_SIMPLIFY_PIXELS = float(env("SUMMARY_SIMPLIFY_PIXELS", "16"))

# How deep jobs for summaries may go: a tile's request makes jobs for the
# summaries it needs at depth 1; those may make jobs for the summaries beneath
# them at depth 2, and so on, up to this. A tile whose summaries can't be
# built within it is a placeholder (see README.md).
SUMMARY_MAX_DEPTH = int(env("SUMMARY_MAX_DEPTH", "1"))

# A job waiting for summaries is looked at again after SUMMARY_WAIT seconds at
# first, then after a quarter of the time it has waited so far, up to
# SUMMARY_WAIT_MAX; it fails once it has waited SUMMARY_WAIT_LIMIT
SUMMARY_WAIT = 1
SUMMARY_WAIT_MAX = 60
SUMMARY_WAIT_LIMIT = int(env("SUMMARY_WAIT_LIMIT", "1800"))

# Where cached tiles are written, and the URL they are served from (by Django
# when DEBUG is on; in production, by the web server)
TILE_CACHE_DIR = Path(env("TILE_CACHE_DIR", str(BASE_DIR / "tilecache")))
TILE_CACHE_URL = env("TILE_CACHE_URL", "/tilecache/")

# Seconds a tile request waits for the renderer before giving up. The web
# server must let a request run this long (see WEB_TIMEOUT in
# docker-compose.yml).
TILE_REQUEST_WAIT = float(env("TILE_REQUEST_WAIT", "600"))

# How many render_tiles processes to run: each renders one job at a time
# (rendering is CPU-bound, and threads in one process share the GIL)
RENDER_WORKERS = int(env("RENDER_WORKERS", "8"))
# Whether idle renderers work ahead: on summaries, and tiles of cached zooms,
# that nobody has asked for yet, highest zooms first, then nearest the
# IDLE_SEEDS most recently requested tiles, within IDLE_RADIUS tiles of them
# (see tiles/idle.py). With nothing to do, they look again after IDLE_POLL
# seconds.
RENDER_IDLE_WORK = env("RENDER_IDLE_WORK", "false").lower() in ("1", "true", "yes")
IDLE_RADIUS = int(env("IDLE_RADIUS", "4"))
IDLE_SEEDS = int(env("IDLE_SEEDS", "200"))
IDLE_POLL = 10

# Seconds after which a tile claimed by a renderer that has gone quiet is
# given to another: longer than reading the map data and running tilemaker
# can both take
RENDER_LEASE = 2 * RENDER_TIMEOUT + 60
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
