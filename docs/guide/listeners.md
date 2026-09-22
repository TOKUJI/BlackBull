# Listeners — the sockets you ask for

`app.run(port=8000)` is one listener stated the short way. When a deployment
needs more than one socket — cleartext and TLS at once, two certificates, an
admin port bound to loopback — you state them directly instead:

```python
import ssl
from blackbull import BlackBull, Listener, Tcp

tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain('cert.pem', 'key.pem')

app = BlackBull()

@app.route(path='/')
async def index():
    return 'served on all four'

app.run(listeners=[
    Listener(Tcp(8080)),                  # cleartext: HTTP/1.1, h2c, WebSocket
    Listener(Tcp(8082)),                  # another one, same stack
    Listener(Tcp(8443), tls=tls),         # TLS: ALPN picks h2 or http/1.1
    Listener(Tcp(9443), tls=tls),         # and another
], workers=4)
```

All four run in **one process**, and every worker serves every one of them.
Before listeners existed this deployment cost four processes, each with the
full worker count.

## What a listener says

```python
Listener(where, speaks='http', tls=None, workers=None)
```

| | |
|---|---|
| `where` | `Tcp(port, host=None)`, `Unix(path)`, or `InheritedFd(fd)` |
| `speaks` | `'http'`, or the name of a protocol registered with [`raw_handler`](raw-protocols.md) |
| `tls` | an `ssl.SSLContext`, or `None` for cleartext |
| `workers` | `'all'` or `'one'` — left unset it follows `speaks` |

`speaks='http'` selects the stack that detects HTTP/1.1, h2c and WebSocket
upgrades; under TLS, ALPN picks between h2 and http/1.1. So the four ports
above need no protocol configuration — they differ only in address and in
whether TLS terminates there.

`Tcp(port)` binds every interface, IPv4 and IPv6. Naming a host binds that
interface and nothing else, which is the point of naming it:

```python
Listener(Tcp(9000, host='127.0.0.1'))     # reachable from this machine only
```

## TLS belongs to the listener

Each listener terminates the certificate it names, so two ports can present
different ones — or one can be cleartext while its neighbour is not:

```python
app.run(listeners=[
    Listener(Tcp(8080)),                     # no certificate here
    Listener(Tcp(8443), tls=public_cert),
    Listener(Tcp(9443), tls=internal_cert),  # a different one
])
```

`app.run(certfile=..., keyfile=...)` still means HTTPS: it builds the context
that the one listener it constructs terminates.

## Who serves a listener

`workers` says how many worker processes own a listener, and it is the only
place that question is answered:

- `'all'` — every worker accepts on it. The default for `speaks='http'`, and
  what lets HTTP scale.
- `'one'` — exactly one worker accepts on it. The default for anything else,
  because a protocol that keeps state between exchanges must answer them from
  the same process.

Workers that do not own a listener close its descriptor at startup, so a
protocol with one owner cannot be answered elsewhere even by accident.

## Saying it the short way

`port=`, `unix_path=` and `inherited_fd=` still work and are unchanged — they
build one listener for you:

```python
app.run(port=8000)                       # Listener(Tcp(8000))
app.run(unix_path='/run/bb.sock')        # Listener(Unix('/run/bb.sock'))
app.run(inherited_fd=3)                  # Listener(InheritedFd(3))
```

They are a shorthand for the same thing, not a separate path, so `listeners=`
replaces them rather than joining them — passing both is refused.

## Reading back what got bound

`Tcp(0)` asks the OS for a free port, which is what tests want. The request is
not rewritten; the answer is read back:

```python
server = Server(app, listeners=[Listener(Tcp(0))])
server.open_socket()
for listener, socks in server.bound_listeners:
    print(listener.speaks, socks[0].getsockname())
```

`server.port` and `server.unix_path` describe the first listener, as they
always did.

## Startup is all-or-nothing

BlackBull does not publish or serve a partial listener set. It first acquires
every requested TCP, Unix, inherited-fd, and protocol-specific socket; only
then does it expose the set through `bound_listeners`. If a later bind or
socket inspection fails, all sockets acquired by that attempt are closed and
the `Server` remains unbound, so the same object can retry `open_socket()`.

Once event-loop servers are created, accepting still waits for lifespan
startup to complete. A failure while grouping listeners by TLS context, during
lifespan startup, or while starting acceptance closes every earlier group and
reclaims the lifespan task before the original startup exception is reported.

## A failing shutdown is a failing process

`run()` reports what the application answered:

| the app … | `run()` … |
|---|---|
| sends `lifespan.startup.failed` | raises, carrying the app's message |
| sends `lifespan.shutdown.failed` | raises on the way out, carrying the app's message |
| never answers | waits at startup; at shutdown, ends at the cleanup budget |

A raising `@app.on_shutdown` hook therefore makes a single-worker process log
the failure and exit `1`, so a supervisor can act on it.  With `workers > 1`
or `--reload` the master exits `0`; check the workers' logs.

**Signals.**  `SIGINT` and `SIGTERM` both run the shutdown.  `SIGTERM` lets
in-flight requests finish for up to
[`BB_WORKER_DRAIN_TIMEOUT`](../reference/env-vars.md#connection-limits-and-timeouts);
keep it below your supervisor's grace period (`docker stop`: 10 s).

Put shutdown work in `@app.on_shutdown`, not in a `SIGTERM` handler of your
own.  BlackBull takes `SIGTERM` while serving and restores your handler
afterwards; it then runs once per signal received, after the drain, and its
exit status wins.  Off the main thread BlackBull leaves `SIGTERM` alone.

`Server.stop()` ends `run()` cleanly at any point, including during lifespan
startup.
