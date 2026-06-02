# Installation

BlackBull requires **Python 3.11+**.

## From PyPI

```bash
pip install blackbull
```

That installs the core framework — enough to serve HTTP/1.1, HTTP/2
(via ALPN), and WebSocket.

## Optional extras

| Extra | Adds | When to install |
|---|---|---|
| `compression` | `brotli`, `zstandard` | If you want the `Compression` middleware to negotiate br/zstd in addition to the built-in gzip. |
| `reload` | `watchfiles` | If you want `--reload` to restart workers on source-file changes (development only). |
| `speed` | `uvloop` | If you want to swap CPython's default asyncio policy for uvloop.  Linux/macOS only. |

```bash
pip install 'blackbull[compression]'
pip install 'blackbull[reload]'
pip install 'blackbull[speed]'
```

Combine in one command:

```bash
pip install 'blackbull[compression,reload,speed]'
```

## From source

```bash
git clone https://github.com/TOKUJI/BlackBull.git
cd BlackBull
pip install -e .
```

Editable installs (`pip install -e .`) are the right form when you
want to read, modify, or contribute to the framework itself.  The
test, documentation, and profiling toolchains are available as
extras for source checkouts:

```bash
pip install -e '.[testing]'    # pytest, pytest-asyncio, hypothesis, httpx[http2], websockets
pip install -e '.[docs]'       # mkdocs-material, mkdocstrings
pip install -e '.[profiling]'  # py-spy
```

## Verify

```bash
blackbull --version
```

```
blackbull 0.28.0
```

The version printed comes from `importlib.metadata.version('blackbull')`,
so it always agrees with the installed wheel.

## Next

- [Hello World](hello-world.md) — minimal app using the full ASGI
  `(scope, receive, send)` triplet.
- [Your First App](first-app.md) — using the simplified handler form
  so handlers omit the boilerplate they don't need.
