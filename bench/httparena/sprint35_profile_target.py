"""Sprint 35 Task 2 profiling target — single-worker static-file server."""
import os
from blackbull import BlackBull
from blackbull.middleware.compression import Compression

app = BlackBull()
app.use(Compression())
app.static('/static', os.environ.get('STATIC_DIR', '/tmp/HttpArena/data/static'))

if __name__ == '__main__':
    app.run(
        port=int(os.environ.get('PORT', '8080')),
        workers=int(os.environ.get('WEB_WORKERS', '1')),
    )
