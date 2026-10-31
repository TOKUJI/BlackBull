# Deprecations

Every feature BlackBull plans to remove is listed here, with what to use
instead and when it goes.

## Policy

- A deprecated feature keeps working for at least one year after it is first
  deprecated. It is then removed in a release that meets its **Removal**
  condition below; where that names both a version and a date, both must have
  passed.
- Each deprecation has an ID (`BBDnnn`). Where it can warn at runtime, the
  warning is a `blackbull.BlackBullDeprecationWarning`, a subclass of
  `DeprecationWarning`, and names the ID, the replacement and the removal.
- `CHANGELOG.md` records each deprecation under *Deprecated* and each removal
  under *Removed*.

## Seeing the warnings

Python hides a `DeprecationWarning` unless the code that triggered it runs as
`__main__`. pytest shows them, and so does `python -X dev`. To show only
BlackBull's, or to turn them into errors in a test run:

```bash
python -W default::blackbull.BlackBullDeprecationWarning app.py
pytest -W error::blackbull.BlackBullDeprecationWarning
```

## Current deprecations

| ID | Deprecated | Use instead | Since | Removal | Warns | Issue |
|---|---|---|---|---|---|---|
| BBD001 | blackbull.middleware.CacheMiddleware, CompressionMiddleware and TrustedProxyMiddleware | Cache, Compression and TrustedProxy | v0.28.0 | no earlier than 2027-05-20 | yes | BLA-581 |
| BBD002 | the pre-built blackbull.middleware.compress instance | an explicit Compression() | v0.28.0 | no earlier than 2027-05-20 | yes | BLA-581 |
| BBD003 | blackbull.client.scenario and blackbull.client.scenario_oracle | blackbull.fault_injection | v0.42.0 | in v0.82.0 | yes | BLA-578 |
| BBD004 | blackbull.Request | blackbull.Connection, which has the same members | v0.60.0 | no earlier than 2027-08-01 | yes | BLA-582 |
| BBD005 | SendBytes and H2CSendBytes | SendRawBytes (H1CSendRawBytes and H2CSendRawBytes in blackbull.fault_injection) | v0.78.0 | no earlier than 2027-08-19 | yes | BLA-583 |
| BBD006 | scenario JSON in the v1 format | the current format: to_json(from_json(text)) | v0.81.1 | no earlier than v0.84.0 and 2026-11-28 | yes | BLA-584 |
| BBD007 | the encode_* helpers of scenario_h1_server and scenario_h2_client | each step's wire() | v0.81.1 | no earlier than v0.84.0 and 2026-11-28 | yes | BLA-584 |
| BBD008 | the BLACKBULL_* environment variables | the same names with the BB_ prefix | v0.81.1 | no earlier than October 2027 | no | BLA-559 |
| BBD009 | Python 3.11 support | Python 3.12 or later | v0.81.1 | in the first release from October 2027 | no | BLA-586 |

The table is generated from `blackbull/deprecations.py`; a test fails when
the two differ.
