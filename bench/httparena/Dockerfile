# BlackBull-on-HttpArena container.
#
# Build context = bench/httparena/ (this directory).  BlackBull is
# installed from PyPI, so the host source tree is not needed at build
# time.  Build with:
#
#   docker build -t blackbull-httparena bench/httparena/
#
# Run with HttpArena-shaped mounts (cleartext on :8080, TLS on :8081
# and :8443):
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

WORKDIR /app

# Install BlackBull from PyPI — same install path real users follow.
# Pin the version so the container is reproducible; bump in lockstep
# with pyproject.toml on each release.  The [compression] extra pulls
# brotli + zstandard for the json-comp profile.
RUN pip install 'blackbull[compression]==0.28.0'

# Copy framework files last so app/launcher edits don't bust the
# slow pip layer.
COPY app.py launcher.py ./

EXPOSE 8080 8081 8443

CMD ["python", "launcher.py"]
