"""Raw TCP echo server riding BlackBull's Non-ASGI bridge (Sprint 50).

One BlackBull process serves HTTP on :8000 *and* a raw TCP echo protocol on
:9000 — the echo handler bypasses ASGI entirely and speaks the socket directly.

Run it::

    python examples/echo_tcp.py

Then, in another shell::

    curl http://127.0.0.1:8000/          # normal HTTP route
    printf 'hello\\n' | nc 127.0.0.1 9000  # raw TCP echo

A non-ASGI handler is an async ``(reader, writer, ctx)`` callable that owns the
connection for its whole lifetime.  ``ctx`` (a ``ProtocolContext``) carries the
peer address, a ``connection_id`` for log correlation, and the shared event
aggregator for protocol-agnostic Level B events.
"""
from blackbull import BlackBull

app = BlackBull()


@app.route(path='/')
async def index():
    return "BlackBull is serving HTTP here, and raw TCP echo on :9000."


@app.raw_handler('echo', port=9000)
async def echo(reader, writer, ctx):
    """Echo every chunk back until the peer closes the connection."""
    while True:
        data = await reader.read(1024)
        if not data:
            break
        await writer.write(data)
        if ctx.aggregator is not None:
            await ctx.aggregator.on_message_received('echo', 'data', len(data))


if __name__ == '__main__':
    app.run(port=8000)
