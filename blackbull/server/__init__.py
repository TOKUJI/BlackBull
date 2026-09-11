"""BlackBull's own server: the socket layer beneath ``BlackBull.run()``.

[`Server`][blackbull.server.server.Server] — exported as ``ASGIServer`` as
well — binds the listeners, accepts connections, and hands each one to a
[`ConnectionActor`][blackbull.server.connection_actor.ConnectionActor], which
detects the protocol off the connection's own buffer and spawns the actor that
speaks it.  The rest of the package is that machinery: readers and recipients
on the way in, senders on the way out, and the limits, deadlines and access
logging around both.

Construct a [`Server`][blackbull.server.server.Server] directly to embed the
server under an event loop you already own, or to bind a socket before forking
a worker.
"""
from .server import ASGIServer, Server

__all__ = ['Server', 'ASGIServer']
