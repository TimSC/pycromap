# pycromap: Django, the renderer and tilemaker-core in one image.
# Built by docker-compose.yml, which supplies pyo5m (see README.md).

# ---- tilemaker-core, built from the submodule

FROM python:3.12-slim-bookworm AS tilemaker
ARG BUILD_JOBS=4

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	build-essential cmake liblua5.1-0-dev libsqlite3-dev libshp-dev \
	libboost-program-options-dev libboost-filesystem-dev libboost-system-dev \
	rapidjson-dev libexpat1-dev zlib1g-dev libprotobuf-dev protobuf-compiler && \
	rm -rf /var/lib/apt/lists/*

WORKDIR /src/tilemaker
COPY tilemaker/CMakeLists.txt tilemaker/VERSION ./
COPY tilemaker/cmake ./cmake
COPY tilemaker/src ./src
COPY tilemaker/include ./include
COPY tilemaker/cppo5m ./cppo5m
COPY tilemaker/resources/config-schema.json ./resources/config-schema.json
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DTILEMAKER_BUILD_TESTS=OFF && \
	cmake --build build --parallel ${BUILD_JOBS} --target tilemaker && \
	strip build/tilemaker

# ---- pycromap

FROM python:3.12-slim-bookworm

# tilemaker-core's libraries
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	liblua5.1-0 shapelib libsqlite3-0 libboost-filesystem1.74.0 libboost-program-options1.74.0 \
	libexpat1 libprotobuf32 zlib1g && \
	rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
COPY --from=pyo5m . /src/pyo5m
RUN pip install --no-cache-dir -r requirements.txt /src/pyo5m

COPY --from=tilemaker /src/tilemaker/build/tilemaker tilemaker/build/tilemaker
COPY tilemaker/config.json tilemaker/process.lua tilemaker/
COPY manage.py ./
COPY pycromap ./pycromap
COPY mapdata ./mapdata
COPY tiles ./tiles

# The database and tile cache live in /data, a volume shared by the web
# server and the renderer
RUN useradd --create-home --uid 1000 pycromap && mkdir /data && chown pycromap /data
USER pycromap
ENV PYCROMAP_DB=/data/db.sqlite3 \
	TILE_CACHE_DIR=/data/tilecache \
	PYTHONUNBUFFERED=1
