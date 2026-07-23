"""
HTTP/2 Priority Example — BlackBull server
==========================================
Demonstrates how to read RFC 9218 priority hints from the native
``Connection`` — ``conn.extensions['http.response.priority']`` — and act
on them in handler code.

RFC 9218 Priority field
-----------------------
Clients send an urgency hint in one of two ways:

  1. PRIORITY_UPDATE frame (type 0x10) — sent by HTTP/2-aware clients before
     or after the HEADERS frame.  BlackBull stores this on the stream and
     exposes it via the Connection's ``extensions`` before calling the app.

  2. 'priority' HTTP header — e.g. ``priority: u=1, i``.  BlackBull parses
     this as a fallback when no PRIORITY_UPDATE frame was received.

The priority field has two components:

  urgency     integer 0 (most urgent) – 7 (least urgent), default 3
  incremental boolean 'i' present → True, absent → False

BlackBull does not reorder responses based on urgency.  Acting on the hint
is entirely up to application code, as shown below.

Connection location (BlackBull v0.31+):
  conn.extensions['http.response.priority'] → {'urgency': int, 'incremental': bool}

The legacy top-level ``scope['http2_priority']`` key does not survive the
native ``Connection`` model (Sprint 80) — only ``conn.extensions`` is
carried across the boundary. Read the extension key.

Endpoints
---------
  GET /              → JSON listing all endpoints
  GET /priority-echo → returns the priority hint as JSON
  GET /work          → simulates a variable-cost operation;
                       cost scales with urgency so high-urgency requests
                       return quickly and low-urgency ones take longer

Run (HTTPS required for HTTP/2):
  python server.py --port 8443 --cert ../../tests/cert.pem --key ../../tests/key.pem
"""
import argparse
import asyncio
import logging

from blackbull import BlackBull, JSONResponse

logging.basicConfig(level=logging.INFO,
                    format='%(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)

app = BlackBull()

_DEFAULT_PRIORITY = {'urgency': 3, 'incremental': False}


def _priority(conn) -> dict:
    """Return the request's priority hint, defaulting to RFC 9218 §4.1 values.

    Reads ``conn.extensions['http.response.priority']`` — the extension
    surface BlackBull populates for HTTP/2 requests (v0.31+).  *conn* is the
    native :class:`~blackbull.connection.Connection` (handlers receive it as
    ``scope``; the ``request_received`` event carries it as
    ``event.detail['conn']``).
    """
    return conn.extensions.get('http.response.priority', _DEFAULT_PRIORITY)


@app.on('request_received')
async def log_priority(event):
    """Observer: log the RFC 9218 priority hint for every incoming request.

    Uses @app.on (observer) so priority logging is fire-and-forget and can
    never block or short-circuit the handler.
    """
    hint = _priority(event.detail['conn'])
    logger.info(
        '%s %s | priority: u=%d incremental=%s',
        event.detail['method'],
        event.detail['path'],
        hint['urgency'],
        hint['incremental'],
    )


@app.route(path='/')
async def handle_index(scope, receive, send):
    body = {
        'endpoints': {
            '/': 'This listing',
            '/priority-echo': 'Returns the request priority hint as JSON',
            '/work': (
                'Simulates variable-cost work. '
                'High urgency (u=0-2) → fast; low urgency (u=5-7) → slow. '
                'Send a "priority" header to control it: priority: u=1'
            ),
        },
        'how_to_set_priority': (
            'Add a "priority" header to your request, e.g.  priority: u=1'
        ),
    }
    await send(JSONResponse(body))


@app.route(path='/priority-echo')
async def handle_priority_echo(scope, receive, send):
    """Return the priority hint BlackBull resolved for this request."""
    hint = _priority(scope)
    logger.info('priority-echo: urgency=%d incremental=%s',
                hint['urgency'], hint['incremental'])
    await send(JSONResponse({
        'priority': hint,
        'conn_location': "conn.extensions['http.response.priority']",
        'note': (
            'Set via PRIORITY_UPDATE frame or "priority" HTTP header. '
            'Defaults: urgency=3, incremental=false (RFC 9218 §4.1).'
        ),
    }))


@app.route(path='/work')
async def handle_work(scope, receive, send):
    """Variable-cost endpoint: work duration scales with urgency.

    urgency 0 → 0 ms, urgency 7 → 700 ms.
    High-urgency clients get a response immediately; background fetches wait.
    """
    hint = _priority(scope)
    urgency = hint['urgency']
    delay = urgency * 0.1           # 0 – 0.7 s

    logger.info('work: urgency=%d → sleeping %.0f ms', urgency, delay * 1000)
    if delay > 0:
        await asyncio.sleep(delay)

    await send(JSONResponse({
        'urgency': urgency,
        'delay_ms': round(delay * 1000),
        'message': f'Completed work for urgency={urgency}.',
    }))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Priority example server')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', default='../../tests/cert.pem')
    parser.add_argument('--key',  default='../../tests/key.pem')
    args = parser.parse_args()

    logger.info('Listening on https://localhost:%d', args.port)
    logger.info('Endpoints: / /priority-echo /work')
    try:
        app.run(port=args.port,
                            certfile=args.cert,
                            keyfile=args.key)
    except KeyboardInterrupt:
        logger.info('Stopped.')
