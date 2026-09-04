# Security policy

BlackBull is an early-alpha framework.  We take security reports
seriously and welcome responsible disclosure from researchers and
adopters.

## Supported versions

Security fixes land on the latest released minor version and the
preceding one.  Older minor versions receive fixes only for
critical vulnerabilities.

| Version | Supported          |
| ------- | ------------------ |
| 0.80.x  | :white_check_mark: |
| 0.79.x  | :white_check_mark: |
| < 0.79  | :x:                |

This table updates with each minor release.

## Reporting a vulnerability

**Please do not file public GitHub issues for security
vulnerabilities.**

Use GitHub's [private vulnerability reporting](https://github.com/TOKUJI/BlackBull/security/advisories/new)
to submit a confidential report.  This gives us a private channel
to triage, develop a fix, and coordinate disclosure with you.

If private reporting is not an option for you, email the maintainer
listed on the GitHub repository profile.

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, including the BlackBull version, Python
  version, OS, and any relevant configuration (TLS posture,
  `BB_*` env vars, middleware in use).
- Any proof-of-concept code or traces you are willing to share.

We do not have a bug bounty program.  We do credit reporters in
release notes by request.

## Disclosure timeline

We aim to follow this timeline for valid reports:

- **Within 7 days**: acknowledge receipt and confirm the report is
  being investigated.
- **Within 30 days**: release a fix in a patch release, or — if
  the fix is more involved — share a target date for one.
- **After 90 days**: public disclosure, typically tied to the
  patch release notes.  We coordinate the exact date with the
  reporter where possible.

If a vulnerability is being actively exploited in the wild, the
timeline compresses; we will share details with you as we move.

## Scope

The following are in scope for security reports:

- BlackBull's HTTP/1.1, HTTP/2, and WebSocket protocol
  implementations under `blackbull/server/` and `blackbull/protocol/`.
- The connection read buffer (`blackbull/server/read_buffer.py`,
  `connection_protocol.py`) and the bounds it enforces: the head byte
  budget (`BB_HEADER_MAX_TOTAL`, applied to *every* request on a
  connection), the backpressure high-water mark, and the lingering
  close that lets a rejection reach the peer.  A way to make the
  server buffer without bound, or to read past a configured budget,
  is a security report.
- The write-side bound (`BB_WRITE_TIMEOUT`) on every response path,
  including the `sendfile` transfer for large static files.  A
  response path that a slow-reading peer can stall indefinitely — so
  holding a connection, its file descriptor, and its transport — is a
  security report even though no bytes are read from the attacker.
- **The resource bounds on every protocol**, and any way to grow past
  one: the request body by total size and by delivery rate
  (`BB_MAX_BODY_SIZE`, `BB_MIN_BODY_RATE`), the WebSocket message a
  handler receives — reassembled *and* inflated
  (`BB_WS_MAX_MESSAGE_SIZE`), the MQTT packet, outbound queue,
  retained store and session state (`BB_MQTT_MAX_SUBSCRIPTIONS`,
  `BB_MQTT_MAX_SESSIONS`, and the Session Expiry Interval the client
  declares), the HTTP/2 time bounds (`BB_H2_IDLE_TIMEOUT`,
  `BB_HEADER_TIMEOUT`), the per-type control-frame rate
  (`BB_FRAME_RATE_LIMIT`), and the connection cap
  (`BB_MAX_CONNECTIONS`).  `docs/about/security-model.md` states what
  each one guarantees and the qualifications on those guarantees.
  A path that grows without a bound, or past a configured one, is a
  security report.
- Middleware shipped in `blackbull/middleware/` (Compression,
  StaticFiles, CORS, etc.).
- Routing, request parsing, and response handling.
- The WebSocket handshake seam in `blackbull/websocket.py` and
  `blackbull/middleware/websocket.py`.  Rejection is expressed by
  closing *before* accepting, and the two layers coordinate through
  handshake state recorded on the connection — so a defect that lets
  a connection reach a handler as "accepted" when a middleware meant
  to reject it is a security report, not a bug report.
- The `blackbull` CLI and module-level boot path (`blackbull.app.serve`).
- **The async HTTP client under `blackbull/client/`** — experimental, in
  scope.  What holds at the shipped defaults, on **both** protocols: a peer
  that **stops** part-way through a response is abandoned with a named
  failure rather than waited on forever, and every bound that refuses names
  itself on the `blackbull.caps` logger — that record is what a client bound
  is for.  A response refused for breaching a limit is never read past as the
  next response: on HTTP/1.1 the connection is abandoned, because a refusal
  leaves the reader's position inside a message; on HTTP/2, where frames are
  self-delimiting, the refusal is a **stream** error and the connection
  deliberately survives — except where a field block refused before the
  decoder walked it leaves the connection's HPACK state unusable.  What holds
  only when configured: a ceiling on the size of a buffered response body
  (`BB_CLIENT_BODY_MAX_TOTAL`) and a floor under the rate one arrives at
  (`BB_CLIENT_MIN_BODY_RATE`) both exist and both ship **off**, so out of the
  box a peer that **trickles** satisfies every individual deadline and is not
  refused.  Why they ship off, and what enabling the floor asserts about your
  peer, is in `docs/reference/env-vars.md`; a report that one of them fails to
  hold *when set* is welcome rather than already known.  Not guaranteed at all:
  defence against a hostile third party.  A read path that grows without a
  bound, or waits on a connection already gone, is a security report.

  The standard is lower than the server's because the roles are not
  symmetric: a server cannot decline to be addressed, so its bounds are a
  defence; a client chooses its peer, so its bounds are a diagnostic.
  Pointing one at a misbehaving server is the advertised use —
  `blackbull.fault_injection` ships to do it — so a bound that made such a
  peer unobservable would be a defect, not a hardening.  A deployment
  calling a party you do not control is outside this paragraph; tell us,
  because that is the evidence that would change it.

  Absent by design rather than unbounded: the client follows no redirects and
  pools no connections.  Every bound it does have is in
  `docs/reference/env-vars.md`, and `docs/about/security-model.md` states the
  posture they add up to.  That posture is an audit result and not a proof:
  the paths were enumerated by hand, that method has missed paths here before,
  and no known gaps is not the same as no gaps.  A read path that is not on
  those pages is the report we most want.
- The gRPC layer under `blackbull/grpc/` (message framing,
  compression negotiation and decompression limits, deadline
  enforcement) and the MQTT 5 broker under `blackbull/mqtt/` —
  both parse untrusted network input.  The optional
  `blackbull-protobuf` package is maintained in
  [its own repository](https://github.com/TOKUJI/blackbull-protobuf);
  reports for it are accepted through either channel.
- The safety locks on `blackbull/fault_injection/` — the
  `BB_PRODUCTION` refuse-check and the localhost-only bind guard on
  `H2FaultServer`.  The deliberate-misbehaviour code paths *behind*
  those locks are by design; a bypass that lets the module run in a
  production process or bind to a non-loopback interface without
  `allow_remote=True` is a security report.
- The loopback-only bind on `NativeTestServer`
  (`blackbull/testing/native.py`).  It exists so a test never publishes
  the application under test to the network; a defect that lets it
  listen on a non-loopback interface is a security report, on the same
  reasoning as the `H2FaultServer` guard above.

The following are typically **out of scope**:

- Vulnerabilities in third-party dependencies (`hpack`, `beartype`,
  and optional extras such as `brotli`, `zstandard`, `uvloop`,
  `watchfiles`).  Report those to the upstream project.  We monitor
  `pip-audit` and Dependabot for CVE-class issues in our dependency
  tree.
- **Volumetric** denial-of-service — unauthenticated traffic at scales
  typical of a CDN's job.  BlackBull is designed to sit behind a CDN or
  reverse proxy for internet-facing deployments — see
  `docs/deployment/behind-reverse-proxy.md`.

  This exclusion is about *volume*, not about denial-of-service as a
  class.  **Asymmetric** resource attacks — where a small amount of
  attacker effort costs the server a large and growable amount of
  memory, time, or connection state — are firmly **in** scope: Rapid
  Reset and the other control-frame floods, slow-drip bodies,
  decompression amplification, unbounded reassembly, and anything that
  holds a connection open without paying for it.  If one peer on one
  connection can make the server spend without bound, report it.
- Bugs in code generated by tools using BlackBull (user
  applications); please report those to the application maintainer.

If you are unsure whether something is in scope, file a report
anyway and we will triage.

## Cryptography

BlackBull does not implement cryptographic primitives.  TLS is
provided by Python's stdlib `ssl` module (linked against OpenSSL).
For deployments requiring particular cipher posture, terminate TLS
at a reverse proxy.

## Hardening defaults

BlackBull ships with defensive defaults documented in
`docs/reference/env-vars.md` — request size caps, header size caps,
connection caps, slowloris idle timer, and so on.  Production
deployments should verify these match their threat model rather
than rely on the framework's defaults.
