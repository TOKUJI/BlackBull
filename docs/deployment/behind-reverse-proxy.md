# Behind a reverse proxy

For most production deployments, running BlackBull behind a reverse proxy is
the simplest topology — the proxy handles TLS termination, static files, and
load balancing across multiple processes.

This page covers three proxies (nginx, HAProxy, Envoy), the choice between
them, and the `TrustedProxy` middleware on the BlackBull side that recovers
client IP and scheme from the proxy's forwarded headers.

## Choosing a proxy

The question that separates them is whether the proxy can speak **HTTP/2 to
the backend**. BlackBull speaks HTTP/2 natively, so a proxy that downgrades to
HTTP/1.1 on the back leg throws that away — you get HTTP/2 between client and
proxy only.

| Proxy | HTTP/2 to backend | Reach for it when |
|---|---|---|
| **nginx** | ⚠️ since 1.29.4 (2025-12) — new, [three CVEs so far][nginx-sec] | You already run nginx, and an HTTP/1.1 back leg is fine |
| **HAProxy** | ✅ stable since 1.9 (2019) | You want the HTTP/2 back leg, high throughput, and a config you can read |
| **Envoy** | ✅ stable since 2016 | Kubernetes or a service mesh; heavy gRPC; dynamic configuration |

[nginx-sec]: https://nginx.org/en/security_advisories.html

An HTTP/1.1 back leg is a perfectly good default. It is what the nginx section
below configures, it is what most deployments run, and it costs nothing for
ordinary request/response traffic. The HTTP/2 back leg earns its keep when the
proxy would otherwise open many upstream connections — high-concurrency APIs,
gRPC, or long-lived streams — because multiplexing collapses them onto one.

!!! note "nginx's `proxy_http_version 2` is young"
    nginx only gained HTTP/2 proxying to the backend in 1.29.4, and the
    feature has already carried security advisories. HAProxy and Envoy have
    shipped it for years. If you want the HTTP/2 back leg today, prefer one of
    those; revisit nginx once the feature has more road behind it.

Not covered: **Caddy** (capable, but little enterprise deployment — ask if you
need it), **Traefik** (no HTTP/2 backend support), and **Apache httpd** (its
HTTP/2 proxying has been experimental for a decade).

## Common setup — the BlackBull side

Start BlackBull **without** TLS. The proxy handles certificates:

```bash
python app.py --port 8000   # plain HTTP; no --cert / --key
```

### Trusted-proxy headers

By default `conn.client` is the raw TCP peer — the proxy's address — and
`conn.scheme` is `'http'` even when the client connected over HTTPS. Enable
`TrustedProxy` to recover both:

```python
# Shortcut on BlackBull — registers TrustedProxy automatically
app = BlackBull(trusted_proxies=['127.0.0.1', '::1'])

# Or register explicitly for more control (e.g. a private subnet):
from blackbull import TrustedProxy
app.use(TrustedProxy(['127.0.0.1', '::1', '10.0.0.0/8']))
```

| | Without middleware | With middleware |
|---|---|---|
| `conn.client` | the proxy's IP | real client IP (from `X-Forwarded-For`) |
| `conn.scheme` | `'http'` | `'https'` (from `X-Forwarded-Proto`) |

Supported headers, in precedence order:

1. RFC 7239 `Forwarded` — `for=<ip>; proto=<scheme>`
2. `X-Forwarded-For` — comma-separated chain; leftmost non-trusted IP wins
3. `X-Forwarded-Proto`

Headers are **only applied when the direct TCP peer is in the trusted set**,
which is what stops a client forging `X-Forwarded-For` to spoof its own IP.
Every configuration below sets these headers on the proxy side; the trusted
set on the BlackBull side must match where the proxy actually connects from.

## nginx

Terminates TLS and HTTP/2 toward clients, proxies to BlackBull over HTTP/1.1.
Regular requests, WebSocket upgrades, and Server-Sent Events all work.

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
        proxy_set_header   Connection        "";   # enable keep-alive upstream
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

## HAProxy

### HTTP/1.1 backend

```haproxy
global
    log stdout format raw local0

defaults
    mode    http
    timeout connect 5s
    timeout client  60s
    timeout server  60s

frontend https_in
    bind *:443 ssl crt /etc/haproxy/certs/example.com.pem alpn h2,http/1.1
    bind *:80
    http-request redirect scheme https unless { ssl_fc }

    # Forwarded headers — TrustedProxy reads these.
    http-request set-header X-Forwarded-Proto https if { ssl_fc }
    http-request set-header X-Forwarded-Proto http  unless { ssl_fc }
    option forwardfor                       # appends X-Forwarded-For

    default_backend blackbull

backend blackbull
    # WebSocket needs no special handling: HAProxy tunnels the Upgrade
    # automatically in HTTP mode.  Raise the server timeout for long-lived
    # connections (WebSocket, SSE).
    timeout tunnel 3600s
    server bb1 127.0.0.1:8000 check
```

### HTTP/2 backend

Change one thing — the `server` line. `proto h2` forces cleartext HTTP/2
(h2c) to the backend:

```haproxy
backend blackbull
    timeout tunnel 3600s
    server bb1 127.0.0.1:8000 proto h2 check
```

Over TLS to the backend, negotiate it with ALPN instead:

```haproxy
    server bb1 10.0.0.5:8443 ssl alpn h2 verify required \
        ca-file /etc/haproxy/certs/ca.pem check
```

!!! warning "WebSocket over an HTTP/2 backend"
    WebSocket over HTTP/2 uses Extended CONNECT (RFC 8441), which is a
    different mechanism from the HTTP/1.1 `Upgrade` handshake. BlackBull
    supports it, but if you hit trouble, route WebSocket paths to an
    HTTP/1.1 backend and keep HTTP/2 for the rest:

    ```haproxy
    frontend https_in
        acl is_ws path_beg /ws
        use_backend blackbull_h1 if is_ws
        default_backend blackbull_h2
    ```

## Envoy

### HTTP/1.1 backend

```yaml
static_resources:
  listeners:
  - name: https_listener
    address:
      socket_address: {address: 0.0.0.0, port_value: 443}
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: ingress_http
          use_remote_address: true      # populates X-Forwarded-For / -Proto
          upgrade_configs:
          - upgrade_type: websocket     # required for WebSocket
          route_config:
            virtual_hosts:
            - name: backend
              domains: ["*"]
              routes:
              - match: {prefix: "/"}
                route:
                  cluster: blackbull
                  timeout: 0s           # no timeout: SSE / streaming
          http_filters:
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
      transport_socket:
        name: envoy.transport_sockets.tls
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext
          common_tls_context:
            alpn_protocols: ["h2", "http/1.1"]
            tls_certificates:
            - certificate_chain: {filename: "/etc/envoy/certs/fullchain.pem"}
              private_key:       {filename: "/etc/envoy/certs/privkey.pem"}

  clusters:
  - name: blackbull
    connect_timeout: 5s
    type: STATIC
    load_assignment:
      cluster_name: blackbull
      endpoints:
      - lb_endpoints:
        - endpoint:
            address:
              socket_address: {address: 127.0.0.1, port_value: 8000}
```

### HTTP/2 backend

Add `typed_extension_protocol_options` to the **cluster**. Everything else
stays as above:

```yaml
  clusters:
  - name: blackbull
    connect_timeout: 5s
    type: STATIC
    typed_extension_protocol_options:
      envoy.extensions.upstreams.http.v3.HttpProtocolOptions:
        "@type": type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions
        explicit_http_config:
          http2_protocol_options: {}      # h2c to the backend
    load_assignment:
      cluster_name: blackbull
      endpoints:
      - lb_endpoints:
        - endpoint:
            address:
              socket_address: {address: 127.0.0.1, port_value: 8000}
```

For gRPC, this is the configuration you want — Envoy will multiplex all RPCs
onto one upstream connection. See [gRPC](../guide/grpc.md).

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

In Compose, the proxy reaches BlackBull by service name, so the trusted set
must cover the Docker network rather than loopback:

```python
app = BlackBull(trusted_proxies=['172.16.0.0/12'])
```

## Going one step further — `AF_UNIX`

When the proxy and BlackBull share a host, an `AF_UNIX` socket between them
removes TCP overhead and avoids exposing a port on `0.0.0.0`. See
[Unix and fd inheritance](unix-and-fd.md).

```nginx
# nginx
upstream blackbull { server unix:/run/blackbull.sock; }
```

```haproxy
# HAProxy
backend blackbull
    server bb1 /run/blackbull.sock
```

```yaml
# Envoy — in the cluster's endpoint address
address:
  pipe: {path: /run/blackbull.sock}
```

With a Unix socket there is no meaningful IP to trust, so `TrustedProxy` needs
the peer that the socket presents rather than a network range — verify what
`conn.client` reports before assuming a value.

## When fronting HTTP/2 is the right call

HTTP/2 costs more per request than HTTP/1.1 in *every* implementation — each
stream carries state, framing, and flow control that HTTP/1.1 does not. That
is the protocol, not a BlackBull property, but it has a deployment
consequence worth naming: if a workload needs maximum throughput on a single
HTTP/2 connection at high multiplex, the usual production shape is a fronting
HTTP/2 terminator with BlackBull on HTTP/1.1 behind it.

BlackBull's own HTTP/2 is conformant (`h2spec`, RFC 9113). How its per-stream
cost compares to other servers is **not measured**: the
`bench/CHARACTERIZATION.md` A-lane (h2load at n=1/10/50) is specified but has
no recorded results, so no comparative claim is made in either direction.

## Next

- [Workers](workers.md) — multi-worker is a natural fit behind a proxy; each
  worker can saturate one core while the proxy load-balances.
- [Unix and fd inheritance](unix-and-fd.md) — the `AF_UNIX` bind pattern.
- [TLS](tls.md) — if you decide to terminate TLS in BlackBull instead.
- [HTTP/2](../guide/http2.md) — what the native HTTP/2 support gives you.
