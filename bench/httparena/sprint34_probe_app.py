"""Minimal BlackBull static-file probe for Sprint 34 native vs Docker A/B.

Mirrors Sprint 33's `probe_app.py` (the one that reached 36,961 r/s
natively) — single listener on :8080, Compression + StaticFiles, no
extra routes.  Worker count from WEB_WORKERS env (default 16),
static dir from STATIC_DIR env (default /tmp/static).
"""
import os

from blackbull import BlackBull
from blackbull.middleware.compression import Compression

app = BlackBull()
app.use(Compression())
app.static('/static', os.environ.get('STATIC_DIR', '/tmp/static'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    workers = int(os.environ.get('WEB_WORKERS', '16'))
    app.run(port=port, workers=workers)
