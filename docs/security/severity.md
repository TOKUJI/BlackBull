# Finding severity criteria (BLA-526)

How severity is assigned to findings from the local robustness research
(`just vuln-check`) and any follow-up investigation. The scheme is a five-rank
scale whose names and boundaries mirror the CVSS severity bands one-to-one, so
a finding's rank can be carried into a CVSS vector without translation loss.

## Ranks

| Rank | CVSS v4.0 base score | Meaning |
|---|---|---|
| **Critical** | 9.0 – 10.0 | Code execution or full control |
| **High** | 7.0 – 8.9 | Integrity/confidentiality compromise, or remote hard unavailability |
| **Medium** | 4.0 – 6.9 | Bounded availability impact, non-secret disclosure, latent intermediary desync |
| **Low** | 0.1 – 3.9 | Deterministic deviation from the specification, no security consequence |
| **Info (None)** | 0.0 | Spec deviation whose safe-side outcome is inconsistent (see below) |

### Critical

Arbitrary code execution and equivalents: arbitrary file read/write outside the
served root, sandbox escape, authentication/authorization bypass that yields full
control of the process. Typical CWE classes: CWE-78, CWE-94, CWE-787 abused to
code execution.

### High

Compromise short of full control, or remotely triggered hard unavailability:

- HTTP request smuggling / desync (CWE-444) — request or response interpretation
  mismatch that can poison caches or hijack adjacent requests
- HTTP response splitting / header injection (CWE-113 — CRLF in names or values
  that reaches the wire)
- Path traversal that reads or writes outside the served root (CWE-22)
- Memory corruption in shipped code
- Unauthenticated hard DoS: worker or process crash, or unbounded resource
  consumption per request/connection (CWE-400)
- TLS verification bypass on the client side

### Medium

- Soft DoS: bounded slowdown or bounded resource amplification — the service
  degrades but stays within configured caps (connection limits, timeouts)
- Disclosure of non-secret internal state (CWE-200): stack traces, internal
  paths, configuration detail
- Framing or interpretation ambiguity that the server itself refuses safely but
  that could desynchronize a downstream intermediary if deployed behind one

### Low

- A deterministic deviation from the specification with no security consequence
  (for example: wrong-but-safe status class, an RFC MUST not implemented where
  the behavior is still safely rejecting)
- Minor information exposure, e.g. server version or build detail in response
  headers

### Info (None) — the lowest rank

A behavior that deviates from the specification where **every** observed outcome
is on the safe side, but the safe-side outcome is **inconsistent**: the same
malformed input yields `400` on one path and a bare connection close on another,
or the chosen rejection status varies across workers or runs. Nothing is
compromised; the defect is protocol determinism and predictability. These are
tracked and normalized over time because inconsistency is where future bugs hide.

## Decision procedure

Apply in order; first match wins:

1. Code execution, arbitrary file access, or full control? → **Critical**
2. Integrity/confidentiality impact, or remote crash / unbounded resource use? → **High**
3. Bounded availability impact, non-secret disclosure, latent desync? → **Medium**
4. Deterministic safe deviation or minor leak? → **Low**
5. Safe-side but inconsistent outcome? → **Info (None)**

## Adjustment factors

Mirroring the CVSS base metrics (AV/AC/PR/UI/S/CIA):

- **Self-harm only**: if the defect can only affect the connection that sends the
  abusive input (the client hurts itself), cap the rank at **Low**, usually
  **Info**. Most raw-socket robustness probes live here.
- **Pre-auth, no user interaction, network reachable** (`AV:N/PR:N/UI:N`):
  do not discount the rank.
- **Non-default configuration required** (a specific middleware or feature
  enabled): state the configuration in the finding and adjust one rank down only
  if the configuration is uncommon in production.
- **Deterministic reproduction**: required for High and above; a flaky Critical
  or High finding is reported as such and re-verified before ranking.

## External alignment

- **CVSS v4.0** (FIRST) and **CVSS v3.1**: severity bands are
  `None 0.0`, `Low 0.1–3.9`, `Medium 4.0–6.9`, `High 7.0–8.9`,
  `Critical 9.0–10.0`. NVD and JPCERT/CC publish against these bands. Our rank
  names and boundaries match them exactly; every finding of rank Medium or above
  should carry a CVSS v4.0 base vector in its report.
- **CWE**: findings cite the weakness class (e.g. CWE-444 smuggling, CWE-113
  CRLF injection, CWE-22 traversal, CWE-400 resource consumption, CWE-200
  exposure). CWE Top 25 covers the High/Critical classes we probe for.
- **OWASP** (ASVS / risk rating): used for web-impact vocabulary when writing up
  exploit scenarios.

## Defaults for the probe checks

The severity recorded in probe output is the rank a **failure** of that check
would carry. The convention throughout: a hang or crash is **High**
(remotely triggered hard unavailability), whatever the base rank — the probe's
"or High if crash/hang" escalation.

| Check | Rank if it fails | CWE |
|---|---|---|
| GRPC-BASE-001 | High (availability: the gRPC service does not answer calls) | CWE-400 |
| GRPC-BASE-002 | High (availability: the gRPC service does not answer calls) | CWE-400 |
| ROUTES-001 | Medium (information exposure: an auto API surface appeared) | CWE-200 |
| ROUTES-002 | Medium (response corruption: streamed length drifted) | CWE-400 |
| ROUTES-003 | Medium (input validation: the parser answered 5xx) | CWE-20 |
| ROUTES-004 | Medium (sensitive data exposure: a traceback in the body) | CWE-209 |
| ROUTES-005 | Medium (misconfiguration: a middleware contract broke) | CWE-400 |
| LANE-001 | High (availability: the lane did not negotiate its protocol) | CWE-444 |
| LANE-002 | High (availability: the lane did not negotiate its protocol) | CWE-444 |
| BASELINE-001 | High (availability: server down or crashed) | CWE-400 |
| BASELINE-002 | High (availability: server down or crashed) | CWE-400 |
| BASELINE-003 | High (availability: server down or crashed) | CWE-400 |
| H2-BASE-001 | High (availability: server down or crashed) | CWE-400 |
| H2-BASE-002 | High (availability: server down or crashed) | CWE-400 |
| H1-ROBUST-001 (unknown method) | Info, or High if crash/hang | CWE-755 |
| H1-ROBUST-002 (oversized header) | Medium, or High if crash/hang | CWE-400 |
| H1-ROBUST-003 (garbage request line) | Info, or High if crash/hang | CWE-755 |
| H1-ROBUST-004 (CRLF in header value) | High | CWE-113 |
| H1-ROBUST-005 (truncated body) | Info, or High if crash/hang | CWE-755 |
| H1-ROBUST-006 (whitespace in field name) | Medium (latent intermediary desync), or High if crash/hang | CWE-444 |
| H1-ROBUST-007 (obs-fold) | Medium (latent intermediary desync), or High if crash/hang | CWE-444 |
| H1-ROBUST-008 (missing Host) | Low, or High if crash/hang | CWE-755 |
| H1-ROBUST-009 (absolute-form) | Info, or High if crash/hang | CWE-755 |
| H1-ROBUST-010 (NUL/overlong path) | High (path confusion can reach another resource) | CWE-158 |
| H1-ROBUST-011 (slow-send lite, long tier) | Medium (bounded availability), or High if crash/hang | CWE-400 |
| SMUGGLE-001 (CL and TE together) | High | CWE-444 |
| SMUGGLE-002 (obfuscated/duplicated TE) | High | CWE-444 |
| SMUGGLE-003 (duplicate Content-Length) | High | CWE-444 |
| CHUNK-001 (chunk extensions) | Medium (framing ambiguity / latent intermediary desync), or High if crash/hang | CWE-444 |
| CHUNK-002 (malformed chunk sizes) | Medium (framing ambiguity / latent intermediary desync), or High if crash/hang | CWE-444 |
| STATE-001 (state contamination) | High (desync primitive) | CWE-444 |
| STATIC-001 (path traversal) | High | CWE-22 |
| HDR-001 (internal detail leak) | Low | CWE-200 |
| H2-ROBUST-001 (bad pseudo-headers) | Medium (malformed request dispatched to the app), or High if crash/hang | CWE-444 |
| H2-ROBUST-002 (DATA on idle/0 stream) | Medium (framing violation), or High if crash/hang | CWE-444 |
| H2-ROBUST-003 (unknown frame not ignored) | Low, or High if crash/hang | CWE-755 |
| H2-ROBUST-004 (oversized header list) | Medium (bounded availability), or High if crash/hang | CWE-400 |
| H2-ROBUST-005 (PRIORITY self-dependency) | Low, or High if crash/hang | CWE-755 |
| TRAILER-001 (chunked trailers) | High (desync/smuggling primitive) | CWE-444 |
| RANGE-001 (Range abuse) | Medium (bounded amplification class), or High if crash/hang | CWE-400 |
| EXPECT-001 (100-continue) | Medium (bounded availability / latent desync), or High if crash/hang | CWE-444 |
| HOST-001 (duplicate/empty Host) | Medium (host confusion / latent intermediary desync), or High if crash/hang | CWE-444 |
| SYMLINK-001 (symlink escape) | Critical (arbitrary file read outside the served root — the base rank is kept on timeout, since a hang here outranks plain availability) | CWE-59 |
| WS-001 (WebSocket handshake/masking) | Medium (protocol-confusion and cache-poisoning primitive via unmasked frames or a weak handshake), or High if crash/hang | CWE-444 |
| H2-ROBUST-006 (Rapid Reset lite) | Medium (bounded availability semantics), or High if crash/hang | CWE-400 |
| H2-ROBUST-007 (CONTINUATION flood lite) | Medium (bounded availability), or High if crash/hang | CWE-400 |
| H2-ROBUST-008 (HPACK bomb lite) | Medium (bounded amplification), or High if crash/hang | CWE-409 |
| TLS-001 (TLS floor / ALPN) | Medium (weak crypto accepted or h2 not negotiable) | CWE-326 |

## Reporting policy

- Run results and findings are recorded as comments on BLA-526.
- A confirmed defect gets its own issue with repro steps and the rank above;
  ranks **Critical/High block a release**, Medium is fixed in the next patch,
  Low/Info are tracked and normalized opportunistically.
- Fixes land as PRs with a fast deterministic regression test added to the
  normal pytest suite.
