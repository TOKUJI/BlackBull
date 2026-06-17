# Brewing Coffee Over HTTP: A Python Implementation of RFC 2324 (HTCPCP)

**Five lines of Python. That's all it takes to turn your ASGI server into a fully RFC-compliant, HTTP-controlled coffee pot — custom methods, status 418, and all.**

---

```python
from blackbull import BlackBull
from blackbull_htcpcp import HtcpcpExtension

app = BlackBull()
HtcpcpExtension(app=app, pot_type='coffee')
```

This isn't a mock — and no, there's no USB coffee pot. It's a pure software implementation of the protocol: `BREW /pot` starts brewing (a virtual brew cycle, not a physical one). `PROPFIND /pot` inspects the pot. `WHEN /pot` asks when it'll be ready. Ask a teapot to brew coffee? You get `418 I'm a teapot` — with `Content-Type: message/coffeepot`. Every method, status code, and header behaves exactly as the RFC specifies. The only thing missing is the actual caffeine. All from a single constructor call, all through the framework's public extension API. No core modifications.

[`blackbull-htcpcp`](https://pypi.org/project/blackbull-htcpcp/) is the first Python package on PyPI to implement the full Hyper Text Coffee Pot Control Protocol — both [RFC 2324](https://datatracker.ietf.org/doc/html/rfc2324) (coffee pots, 1998) and [RFC 7168](https://datatracker.ietf.org/doc/html/rfc7168) (teapots, 2014). Both were published on April 1st. Both were jokes. But implementing a joke protocol turns out to be a remarkably effective way to answer a real question: **can you ship a non-trivial application protocol — custom HTTP methods, custom status codes, custom content types, custom error semantics — purely on top of an ASGI framework's public extension surface, without modifying the framework?**

Spoiler: yes. Here's how, and why it matters.

---

### Why a joke RFC is worth implementing

In 2017, the IETF HTTP Working Group proposed removing status code 418 from Node.js, Go, and Python. A 15-year-old developer started the [\#save418](https://save418.com/) movement — and won. Python 3.9 shipped with `HTTPStatus.IM_A_TEAPOT`. Google serves [google.com/teapot](https://www.google.com/teapot). The Russian military briefly used 418 as DDoS protection. The teapot endures.

**But nobody had published a Python package that actually implements the full protocol.** Until now.

---

## What `blackbull-htcpcp` Implements

[`blackbull-htcpcp`](https://pypi.org/project/blackbull-htcpcp/) is a Python package that implements the complete HTCPCP specification — both RFC 2324 (coffee pots) and RFC 7168 (teapots) — as an extension for the [BlackBull](https://github.com/TOKUJI/BlackBull) ASGI framework.

| Feature | RFC | Implementation |
|---|---|---|
| `BREW` method | 2324 §2.2 | Start brewing. POST fallback for environments where the stdlib rejects non-IANA HTTP methods. |
| `PROPFIND` method | 2324 §2.2 | Inspect the pot (type, state, capacity, supported additions). |
| `WHEN` method | 2324 §2.2 | Ask when the coffee will be ready. |
| `GET /pot` | — | Browser-friendly pot state. |
| Status `418 I'm a teapot` | 2324 §2.2.2 | Returned when a teapot is asked to brew coffee, or vice versa. |
| `Accept-Additions` header | 2324 §2.2.3 | Coffee: cream, sugar, vanilla, whisky, aquavit… Tea: milk, lemon, honey, ginger, bergamot… |
| `message/coffeepot` content type | 2324 §2.2.1 | All `/pot` responses carry the official HTCPCP media type. |
| Teapot discrimination | 7168 §2.1 | Teapots brew tea (200); coffee additions on a teapot → 418. |

---

## Five Lines of Code

```python
from blackbull import BlackBull
from blackbull_htcpcp import HtcpcpExtension

app = BlackBull()
HtcpcpExtension(app=app, pot_type='coffee')
```

That's it. Four routes on `/pot`, an `app.on_error(418)` handler, and the extension registers itself in `app.extensions['htcpcp']` — all from a single constructor call.

```bash
$ pip install blackbull-htcpcp
$ python -c "
from blackbull import BlackBull
from blackbull_htcpcp import HtcpcpExtension
BlackBull().run(HtcpcpExtension(app=BlackBull(), pot_type='coffee'))
"
# Then:
$ curl -X BREW -H 'Accept-Additions: cream; sugar' http://localhost:8000/pot
{"pot-type": "coffee", "state": "ready", "message": "Brewing complete", "additions": ["cream", "sugar"]}

$ curl -X BREW http://localhost:8000/pot   # teapot mode
{"error": "I'm a teapot", "pot-type": "teapot"}
```

### Deferred Wiring

Prefer explicit initialization? The extension supports deferred wiring:

```python
ext = HtcpcpExtension(pot_type='teapot')
# ... configure app ...
ext.init_app(app)
```

Both eager and deferred styles follow BlackBull's documented [`init_app(app)` convention](https://github.com/TOKUJI/BlackBull/blob/master/docs/guide/extensions.md) — the same pattern used by `blackbull-session`, the framework's session-management extension.

---

## Why This Is Interesting Beyond the Joke

HTCPCP is funny. But `blackbull-htcpcp` exists for a serious reason: **it validates that a non-trivial application protocol — custom HTTP methods, custom status codes, custom content types, custom error semantics — can be layered onto an ASGI framework purely through its public extension surface, without modifying the framework itself.**

The extension registers:

- **Routes with non-standard HTTP methods** (`BREW`, `PROPFIND`, `WHEN`). Because Python's `http.HTTPMethod` enum rejects non-IANA verbs, the extension keeps a `POST` fallback for clients that can't send custom methods — and registers the real verbs directly now that BlackBull 0.42.1 accepts any RFC 9110 §5.6.2 token as an HTTP method.
- **A custom error handler** via `app.on_error(418)` that returns `message/coffeepot` JSON — any code path in the application that emits 418 gets the same teapot response body.
- **Custom header parsing** for `Accept-Additions` with full security hardening.
- **Self-registration** into `app.extensions['htcpcp']` so other extensions can discover it.

No changes to BlackBull's core. No plugin registry. No dependency injection framework. Just `app.route`, `app.on_error`, and `app.extensions` — all public, stable APIs.

This is the second external `blackbull-*` package (after `blackbull-session`), and it compounds the story: **BlackBull's extension surface can carry real protocols.**

---

## Security: The Threat Model Behind a Joke Protocol

Because HTCPCP extends HTTP with custom headers, it inherits all of HTTP's attack surface. The `Accept-Additions` parser was designed with a threat model — documented in the test suite as test cases S001 through S015:

| Category | Threat | Mitigation |
|---|---|---|
| **Header injection** | CRLF / bare LF in addition tokens | Rejected at parse time (S001–S002) |
| **Header injection** | NULL bytes in addition tokens | Rejected at parse time (S003) |
| **DoS — oversized tokens** | Addition tokens > 256 characters | Rejected (S004) |
| **DoS — too many additions** | More than 64 addition tokens | Rejected (S005) |
| **DoS — concurrent BREW** | Brewing while already brewing | 409 Conflict (S006) |
| **DoS — oversized body** | BREW body > 1 MiB | 413 Request Entity Too Large (S007) |
| **Control characters** | Non-printable chars (except HTAB) | Rejected (S008) |
| **418 abuse — cache poisoning** | 418 responses carrying cache headers | No permissive cache headers on 418 (S010) |

Every rejection returns `message/coffeepot` content type — consistent error formatting even on security responses.

This is the point where the joke RFC meets real engineering. The protocol is absurd; the implementation is not.

---

## The Cultural Precedent: IPoAC

In 1990, [RFC 1149](https://datatracker.ietf.org/doc/html/rfc1149) specified IP datagram transmission via avian carrier (homing pigeons). In 2001, the Bergen Linux User Group [actually implemented it](https://en.wikipedia.org/wiki/IP_over_Avian_Carriers#Real-world_implementation), sending 9 packets over 5 km with a 55% packet loss rate — mostly due to avian interference.

HTCPCP follows the same arc: a joke RFC, a real implementation, a crowd that *gets it*. The cultural through-line from IPoAC (2001) through \#save418 (2017) to `blackbull-htcpcp` (2026) is that **implementing joke protocols is a form of protocol literacy**. If you can implement HTCPCP correctly, you understand HTTP's extension points well enough to implement a real one.

---

## Comparison With Existing Implementations

There are about 105 HTCPCP repositories on GitHub across all languages. The landscape is sparse:

| Implementation | Language | Stars | Last Updated | RFC 7168 |
|---|---|---|---|---|
| [HyperTextCoffeePot](https://github.com/HyperTextCoffeePot/HyperTextCoffeePot) | Python (Flask) | 78 | 2015 (archived) | ❌ |
| [madmaze/HTCPCP](https://github.com/madmaze/HTCPCP) | C | 49 | 2011 | ❌ |
| [node-htcpcp](https://github.com/stephen/node-htcpcp) | Node.js | 36 | 2013 | ❌ |
| [htcpcp-delonghi](https://github.com/dkundel/htcpcp-delonghi) | JS (Tessel 2) | 29 | 2023 (archived) | ❌ |
| [Go teapot](https://godoc.org/github.com/davsk/teapot) | Go | — | — | ✅ |

`blackbull-htcpcp` is:

- **The first Python package on PyPI** implementing HTCPCP (zero prior PyPI packages).
- **One of two known implementations** covering both RFC 2324 + RFC 7168 (alongside Go's `teapot`).
- **The only ASGI-native implementation** — all prior implementations are standalone servers or framework-specific hacks.
- **Actively maintained** — the top 5 GitHub implementations by stars are all dead or archived.

---

## Getting Started

```bash
pip install blackbull-htcpcp
```

Minimal application:

```python
from blackbull import BlackBull
from blackbull_htcpcp import HtcpcpExtension

app = BlackBull()
HtcpcpExtension(app=app, pot_type='coffee', capacity_ml=1500)
app.run()
```

Then:

```bash
# Brew coffee with additions
curl -X BREW -H 'Accept-Additions: cream; sugar; vanilla' http://localhost:8000/pot

# Inspect the pot
curl http://localhost:8000/pot

# Ask when it's ready
curl http://localhost:8000/pot/when

# Teapot mode — ask a teapot to brew coffee
curl -X BREW -H 'Accept-Additions: cream' http://localhost:8000/pot
# → 418 I'm a teapot
```

---

---

*`blackbull-htcpcp` is on [PyPI](https://pypi.org/project/blackbull-htcpcp/) and [GitHub](https://github.com/TOKUJI/blackbull-htcpcp), licensed under Apache 2.0. The [BlackBull framework](https://github.com/TOKUJI/BlackBull) is a pure-Python ASGI server with HTTP/1.1, HTTP/2, and WebSocket at the protocol level — a personal learning project where correctness over the wire matters more than API stability.*
