# BLA-526 probe/target image (G2-4): runs THIS worktree's blackbull — the
# image copies the package sources over the base's pinned installation, so
# the container tier probes the code under review, not a released copy.
# Build: docker build -t bb-vuln -f tools/security/container.Dockerfile .
FROM bb-multiport:latest
ENV PYTHONPATH=/srv/src PYTHONDONTWRITEBYTECODE=1
COPY blackbull /srv/src/blackbull
COPY tools/security/*.py /srv/tools/security/
COPY tools/security/static/hello.txt /srv/tools/security/static/hello.txt
COPY tools/security/fixture_secret.txt /srv/tools/security/fixture_secret.txt
RUN ln -s hello.txt /srv/tools/security/static/hello-link.txt && \
    ln -s ../fixture_secret.txt /srv/tools/security/static/escape-link.txt
RUN pip install --no-cache-dir -q cryptography
WORKDIR /srv
