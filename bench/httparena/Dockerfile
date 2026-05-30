# BlackBull-on-HttpArena container.
#
# Build context = BlackBull repo root (so the local source tree is
# COPYable).  Build with:
#
#   docker build -f bench/httparena/Dockerfile -t blackbull-httparena .
#
# Run with HttpArena-shaped mounts (cleartext on :8080, TLS on :8081):
#
#   docker run --rm --network host \
#     -v $PWD/bench/httparena/_local/data:/data:ro \
#     -v $PWD/bench/httparena/_local/certs:/certs:ro \
#     blackbull-httparena

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src/BlackBull

# Copy source.  .dockerignore (see bench/httparena/.dockerignore) excludes
# tests, bench/results, etc., so the image stays small.
COPY pyproject.toml ./
COPY blackbull ./blackbull

# Install editable so __version__ resolves via importlib.metadata; pull
# the compression + validation extras so /json-comp can opt into brotli
# when wired up and so the router's `{count:int}` path-param validation
# (beartype-based) works.
RUN pip install -e '.[compression,validation]'

# Copy framework files last so app/launcher edits don't bust the slow layer.
COPY bench/httparena/app.py bench/httparena/launcher.py /src/httparena/

WORKDIR /src/httparena

EXPOSE 8080 8081

CMD ["python", "launcher.py"]
