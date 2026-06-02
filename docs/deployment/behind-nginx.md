# Behind nginx

For most production deployments, running BlackBull behind a
reverse proxy (nginx, Caddy) is the simplest topology — the
proxy handles TLS termination, static files, and load balancing
across multiple processes.

This page covers the nginx side of that setup and the
`TrustedProxy` middleware on the BlackBull side that recovers
client IP and scheme from the proxy headers.

## Setup

Start BlackBull **without** TLS — nginx handles certificates:

```bash
python app.py --port 8000   # plain HTTP/1.1; no --cert / --key
```

nginx terminates TLS and HTTP/2 toward clients, then proxies to
BlackBull over HTTP/1.1.  Regular HTTP requests, WebSocket
upgrades, and Server-Sent Events all work through this topology.

## Complete nginx configuration

```nginx
upstream blackbull {
    server 127.0.0.1:8000;
    keepalive 64;
}

server {
    listen 443 ssl;
    http2 on;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    # ── Regular HTTP requests ─────────────────────────────────────────
    location / {
        proxy_pass         http://blackbull;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";   # enable keep-alive to upstream
    }

    # ── WebSocket ─────────────────────────────────────────────────────
    # WebSocket requires HTTP/1.1 Upgrade; match paths that need it explicitly.
    location /ws {
        proxy_pass         http://blackbull;
        proxy_http_version 1.1;
        proxy_set_header   Host       $host;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 3600s;     # keep WS connection open
    }

    # ── Server-Sent Events ────────────────────────────────────────────
    location /sse {
        proxy_pass                http://blackbull;
        proxy_http_version        1.1;
        proxy_set_header          Host $host;
        proxy_set_header          Connection "";
        proxy_buffering           off;      # flush SSE events immediately
        proxy_cache               off;
        proxy_read_timeout        3600s;
        chunked_transfer_encoding on;
    }
}

# Redirect plain HTTP to HTTPS
server {
    listen 80;
    server_name example.com;
    return 301 https://$host$request_uri;
}
```

## Trusted-proxy headers

By default, `scope['client']` is the raw TCP peer (nginx's
loopback address) and `scope['scheme']` is `'http'` even when
the client connected via HTTPS.  Enable `TrustedProxy` to
rewrite both from the proxy's forwarded headers:

```python
# Shortcut on BlackBull — registers TrustedProxy automatically
app = BlackBull(trusted_proxies=['127.0.0.1', '::1'])

# Or register explicitly for more control (e.g. private subnet):
from blackbull import TrustedProxy
app.use(TrustedProxy(['127.0.0.1', '::1', '10.0.0.0/8']))
```

With trusted-proxy support active:

| `scope` key | Without middleware | With middleware |
|---|---|---|
| `scope['client']` | nginx's loopback IP | real client IP (from `X-Forwarded-For`) |
| `scope['scheme']` | `'http'` | `'https'` (from `X-Forwarded-Proto`) |

Supported headers, in precedence order:

1. RFC 7239 `Forwarded` — `for=<ip>; proto=<scheme>`
2. `X-Forwarded-For` — comma-separated chain; leftmost
   non-trusted IP wins
3. `X-Forwarded-Proto`

Headers are **only applied when the direct TCP peer is in the
trusted set**, preventing clients from spoofing their IP by
forging `X-Forwarded-For`.

## Docker

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install .
EXPOSE 8000
CMD ["python", "app.py", "--port", "8000"]
```

Environment variables for secrets (never hardcode):

```python
import os
DB_URL = os.environ['DATABASE_URL']
SECRET = os.environ['SECRET_KEY']
PORT   = int(os.environ.get('PORT', 8000))
```

## Going one step further — `AF_UNIX`

When the reverse proxy and BlackBull are on the same host, an
`AF_UNIX` socket between them removes TCP overhead and avoids
exposing a port on `0.0.0.0`.  See
[Unix and fd inheritance](unix-and-fd.md).

The nginx upstream then looks like:

```nginx
upstream blackbull { server unix:/run/blackbull.sock; }
```

## Next

- [Workers](workers.md) — multi-worker is a natural fit behind
  nginx; each worker can saturate one core while the proxy
  load-balances.
- [Unix and fd inheritance](unix-and-fd.md) — `AF_UNIX` bind
  pattern.
- [TLS](tls.md) — if you decide to terminate TLS in BlackBull
  instead of upstream.
