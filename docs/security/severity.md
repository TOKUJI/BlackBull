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

## Defaults for the M1 probe checks

The severity recorded in probe output is the rank a **failure** of that check
would carry:

| Check | Rank if it fails | CWE |
|---|---|---|
| BASELINE-001/002/003 | High (availability: server down or crashed) | CWE-400 |
| H1-ROBUST-001 (unknown method) | Info, or High if crash/hang | CWE-755 |
| H1-ROBUST-002 (oversized header) | Medium, or High if crash/hang | CWE-400 |
| H1-ROBUST-003 (garbage request line) | Info, or High if crash/hang | CWE-755 |
| H1-ROBUST-004 (CRLF in header value) | High | CWE-113 |
| H1-ROBUST-005 (truncated body) | Info, or High if crash/hang | CWE-755 |
| SMUGGLE-001 (CL and TE together) | High | CWE-444 |
| STATIC-001 (path traversal) | High | CWE-22 |
| HDR-001 (internal detail leak) | Low | CWE-200 |

## Reporting policy

- Run results and findings are recorded as comments on BLA-526.
- A confirmed defect gets its own issue with repro steps and the rank above;
  ranks **Critical/High block a release**, Medium is fixed in the next patch,
  Low/Info are tracked and normalized opportunistically.
- Fixes land as PRs with a fast deterministic regression test added to the
  normal pytest suite.
