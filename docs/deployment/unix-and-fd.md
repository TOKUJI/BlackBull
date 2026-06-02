# Unix sockets and fd inheritance

Two ways to bind that aren't a plain TCP port — `AF_UNIX` for
local reverse proxies, and fd inheritance for systemd socket
activation.

## `AF_UNIX` — local domain socket

When BlackBull runs behind a local reverse proxy (nginx, Caddy),
using an `AF_UNIX` socket eliminates TCP overhead and avoids
exposing a port on `0.0.0.0`:

```bash
# CLI
blackbull myapp:app --bind unix:/run/blackbull.sock

# nginx upstream
upstream blackbull { server unix:/run/blackbull.sock; }
```

```python
# In Python
app.run(unix_path='/run/blackbull.sock')
```

The socket file is created with mode `0660` so a reverse proxy
running in the same group can connect without a `chmod`.  A
leftover socket file at the path is removed automatically
before bind; BlackBull refuses to unlink a regular file or
directory at that path (safety check).

TCP-only socket options (`SO_REUSEPORT`, `TCP_USER_TIMEOUT`,
`IPV6_V6ONLY`) are skipped for `AF_UNIX` sockets — they carry
no meaning on a domain socket.

## fd inheritance — systemd socket activation

Systemd can pre-bind the port as root and then start BlackBull
unprivileged, handing the bound socket as an open file
descriptor:

```ini title="/etc/systemd/system/blackbull.socket"
[Socket]
ListenStream = 443

[Install]
WantedBy = sockets.target
```

```ini title="/etc/systemd/system/blackbull.service"
[Service]
User      = www-data
ExecStart = blackbull myapp:app --bind fd://3
```

```python
# In Python — equivalent shape
app.run(inherited_fd=3)
```

BlackBull validates the `$LISTEN_PID` / `$LISTEN_FDS`
environment variables per the `sd_listen_fds(3)` protocol:

- `LISTEN_PID` must equal the current process PID — if it
  points elsewhere BlackBull refuses to adopt the fd (prevents
  accidentally stealing another process's socket).
- `LISTEN_FDS` defines the valid fd window `[3, 3 + LISTEN_FDS)`.
  Fds outside that window are rejected.

When neither variable is set (non-systemd handoff, tests)
BlackBull accepts the fd unconditionally.

### What systemd activation buys you

- **Bind privileged ports without running as root.**  systemd
  binds `:443` while running as root; BlackBull starts as
  `www-data` and inherits the already-bound socket.
- **Zero-downtime restarts.**  systemd keeps the socket open
  across stop/start cycles — connections that arrive while the
  new BlackBull is launching queue in the kernel accept buffer
  instead of being refused.
- **Lazy activation.**  The socket is ready before BlackBull
  starts; the first connection wakes the service.

## Inspecting the bind

`server.port` returns the kernel-assigned port number after
`open_socket(port=0)` — useful in tests where you want an
ephemeral port:

```python
from blackbull.server import ASGIServer

server = ASGIServer(app)
server.open_socket(port=0)
print(server.port)        # e.g. 39423
```

For `AF_UNIX` sockets `server.port` is the path string instead
of a port number.

## Next

- [Behind nginx](behind-nginx.md) — pairing `AF_UNIX` with an
  upstream reverse proxy.
- [Running BlackBull](running.md) — the broader entry-point
  overview.
- [Configuration](../guide/configuration.md) — `--bind` syntax
  for the CLI (`host:port`, `unix:/path`, `fd://N`).
