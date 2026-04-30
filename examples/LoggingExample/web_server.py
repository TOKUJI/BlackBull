"""Web server — BlackBull helloworld that forwards access logs to log_server.py.

Usage (start log_server.py first):
    python log_server.py &
    python web_server.py            # listens on localhost:8000

Every request to http://localhost:8000/ is answered with "Hello, world!" and
one JSON access log record is POSTed to http://localhost:9000/logs.

Why @app.on (observer) instead of @app.intercept?
    Access logging must never block or short-circuit the request.  Observers
    (@app.on) run fire-and-forget in an independent asyncio.Task — even if the
    observer raises, the request is unaffected.  Interceptors (@app.intercept)
    block the handler and can abort the request, which is wrong for logging.

Why QueueHandler + QueueListener?
    BlackBull runs on asyncio.  A plain logging.Handler.emit() call is
    synchronous; if it blocks (e.g. waiting for an HTTP response), it
    blocks the event-loop thread and stalls all other requests.
    QueueHandler.emit() is non-blocking — it just enqueues the record.
    QueueListener runs in a background thread and calls JsonHTTPHandler
    (the blocking part) there, keeping the event loop free.

Reference: https://docs.python.org/3/library/logging.handlers.html#logging.handlers.HTTPHandler
"""

import asyncio
import http.client
import json
import logging
import queue
import time
from logging.handlers import QueueHandler, QueueListener

from blackbull import BlackBull, JSONResponse, Response

LOG_SERVER_HOST = 'localhost:9000'
LOG_SERVER_URL  = '/logs'


# ---------------------------------------------------------------------------
# Custom handler: sends one LogRecord to the log server as JSON
# ---------------------------------------------------------------------------

class JsonHTTPHandler(logging.Handler):
    """Forward a LogRecord to a remote HTTP endpoint as a JSON POST body.

    Designed to be driven by a QueueListener in a background thread so that
    the blocking HTTP call never runs on the asyncio event-loop thread.
    """

    def __init__(self, host: str, url: str = '/logs') -> None:
        super().__init__()
        self.host = host
        self.url  = url

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.dumps({
                # Standard LogRecord fields
                'created': record.created,
                'level':   record.levelname,
                'logger':  record.name,
                'message': self.format(record),
                # Access-log fields injected via extra= in AccessLogRecord.as_extra()
                'client_ip':      getattr(record, 'client_ip',      '-'),
                'method':         getattr(record, 'method',         '-'),
                'path':           getattr(record, 'path',           '-'),
                'http_version':   getattr(record, 'http_version',   '-'),
                'status':         getattr(record, 'status',         '-'),
                'response_bytes': getattr(record, 'response_bytes',  0),
                'duration_ms':    getattr(record, 'duration_ms',    0.0),
            }).encode()

            conn = http.client.HTTPConnection(self.host)
            conn.request(
                'POST', self.url, payload,
                {
                    'Content-Type':   'application/json',
                    'Content-Length': str(len(payload)),
                },
            )
            conn.getresponse()
            conn.close()
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Wire up non-blocking access logging
# ---------------------------------------------------------------------------

_log_queue    = queue.Queue(-1)                         # unbounded, thread-safe
_json_handler = JsonHTTPHandler(LOG_SERVER_HOST, LOG_SERVER_URL)
_listener     = QueueListener(_log_queue, _json_handler, respect_handler_level=True)
_listener.start()

# Logger that feeds into the non-blocking queue pipeline.
# We use our own namespace ('example.access') so application events are
# explicitly driven by @app.on observers rather than framework internals.
_access_logger = logging.getLogger('example.access')
_access_logger.addHandler(QueueHandler(_log_queue))
_access_logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = BlackBull()


@app.on('before_handler')
async def log_request(event):
    """Observer: log every incoming request — fire-and-forget, never blocks."""
    _access_logger.info(
        '%s %s',
        event.detail['method'],
        event.detail['path'],
        extra={
            'client_ip':      event.detail['client_ip'] or '-',
            'method':         event.detail['method'],
            'path':           event.detail['path'],
            'http_version':   '-',
            'status':         '-',
            'response_bytes': 0,
            'duration_ms':    0.0,
        },
    )


@app.on('after_handler')
async def log_response(event):
    """Observer: log the outcome; captures exceptions without affecting the request."""
    exc = event.detail.get('exception')
    status = 'ERROR' if exc else 'OK'
    _access_logger.info(
        '%s %s → %s',
        event.detail['method'],
        event.detail['path'],
        status,
        extra={
            'client_ip':      event.detail['client_ip'] or '-',
            'method':         event.detail['method'],
            'path':           event.detail['path'],
            'http_version':   '-',
            'status':         status,
            'response_bytes': 0,
            'duration_ms':    0.0,
        },
    )


@app.route(path='/')
async def hello(scope, receive, send):
    await send(Response(b'Hello, world!'))


@app.route(path='/tasks')
async def tasks(scope, receive, send):
    await send(JSONResponse([{'id': 1, 'title': 'Buy milk'},
                             {'id': 2, 'title': 'Walk the dog'}]))


if __name__ == '__main__':
    print('Web server on http://localhost:8000')
    print(f'Access logs → http://{LOG_SERVER_HOST}{LOG_SERVER_URL}')
    print('Press Ctrl-C to stop.\n')
    try:
        asyncio.run(app.run(port=8000))
    finally:
        _listener.stop()   # flush queue and join background thread cleanly
