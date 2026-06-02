# TLS

BlackBull terminates TLS itself when you pass `certfile` and
`keyfile`.  ALPN negotiates HTTP/2 (`h2`) or HTTP/1.1
automatically on the same listening port — there's no separate
HTTP/2 port and no upgrade dance.

This page covers self-signed certs for local development, the
ALPN behaviour you get for free, and mutual TLS (mTLS) when you
need it.

## Quick start

```python
app.run(port=8443, certfile='cert.pem', keyfile='key.pem')
```

Or via the CLI:

```bash
blackbull myapp:app --bind 0.0.0.0:8443 \
    --certfile cert.pem --keyfile key.pem
```

That single listener serves:

- **HTTP/1.1** to any client that negotiates `http/1.1` (or
  doesn't speak ALPN).
- **HTTP/2** to any client that negotiates `h2` (modern
  browsers, curl `--http2`, httpx with `http2=True`, h2load).
- **WebSocket** over either protocol — see
  [WebSockets](../guide/websockets.md).

## Self-signed certificate for local HTTPS

```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj '/CN=localhost' \
    -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
```

Then start the server:

```bash
python app.py --cert cert.pem --key key.pem --port 8443
# Open https://localhost:8443  (accept the browser security warning once)
```

!!! note "Browsers and HTTP/2"
    Browsers only use HTTP/2 over HTTPS.  With TLS the server
    negotiates HTTP/2 automatically via ALPN; plain HTTP
    connections use HTTP/1.1.

## h2c — cleartext HTTP/2

For deployments where TLS is terminated upstream (typical in
private networks behind a load balancer), BlackBull also
detects HTTP/2 over cleartext via the connection preface — no
upgrade dance:

```bash
python app.py --port 8000     # serves both HTTP/1.1 and h2c on the same socket
```

Modern HTTP clients that send the HTTP/2 preface directly get
HTTP/2; everyone else gets HTTP/1.1.

## Mutual TLS (mTLS)

mTLS requires clients to present a certificate signed by a
trusted CA.  Set it up before starting the server:

```python
import asyncio
from blackbull import BlackBull
from blackbull.server import ASGIServer

app = BlackBull()

# ... define routes ...

# Build the server manually so we can configure mTLS before accepting connections.
server = ASGIServer(app, certfile='cert.pem', keyfile='key.pem')
server.open_socket(8443)
server.configure_mtls(ca_cert='ca.pem')   # enables CERT_REQUIRED
asyncio.run(server.run())
```

`configure_mtls` raises `RuntimeError` if called before TLS is
configured (i.e. before `certfile` and `keyfile` are provided).

### Generating a test CA and client certificate

```bash
# CA
openssl req -x509 -newkey rsa:4096 -keyout ca.key -out ca.pem \
    -days 365 -nodes -subj '/CN=Test CA'

# Server cert signed by the CA
openssl req -newkey rsa:4096 -keyout key.pem -out server.csr \
    -nodes -subj '/CN=localhost'
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key \
    -CAcreateserial -out cert.pem -days 365

# Client cert signed by the CA
openssl req -newkey rsa:4096 -keyout client.key -out client.csr \
    -nodes -subj '/CN=client'
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key \
    -CAcreateserial -out client.pem -days 365
```

A client that doesn't present a valid certificate signed by the
configured CA is rejected at the TLS handshake — your
application code never sees the connection.

## When to terminate TLS upstream instead

For most production deployments, terminating TLS at a reverse
proxy (nginx, Caddy) is simpler:

- Certificate rotation lives next to the proxy's existing
  certificate management.
- The reverse proxy already buffers slow clients (mitigating
  slowloris before BlackBull sees the connection).
- You can use `AF_UNIX` between proxy and BlackBull, removing
  TCP overhead.

See [Behind nginx](behind-nginx.md) for the configuration on
both sides, and [Unix and fd inheritance](unix-and-fd.md) for
the UDS bind pattern.

Terminate TLS *in* BlackBull when:

- You want HTTP/2 directly to the client without a proxy in the
  data path.
- You need mTLS (clients presenting certificates).
- You're running a single-process deployment where adding nginx
  isn't worth the operational complexity.

## Next

- [Behind nginx](behind-nginx.md) — terminating TLS upstream.
- [Unix and fd inheritance](unix-and-fd.md) — `AF_UNIX` and
  systemd socket activation.
- [HTTP/2](../guide/http2.md) — what the framework does with
  HTTP/2 once ALPN picks it.
