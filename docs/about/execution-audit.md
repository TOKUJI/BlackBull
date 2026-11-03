# Execution audit

Before creating a release tag, run `just audit-code` on the source being
released. The same gate runs in CI on CPython 3.14. It uses the existing
coverage/testing dependencies; BlackBull's supported Python floor is unchanged.
Keep the generated report under `build/execution-audit/` until review is complete.

The gate runs unit, architecture, properties, integration and conformance suites
together, with integration enabled. Docker-dependent tests retain their normal
skip conditions. Quantitative tests marked `no_audit(reason=...)` must first pass
without instrumentation. Their exact JUnit identities are checked against the
tests skipped during collection; a missing control fails the gate.

The source inventory lists definitions; coverage records executed lines.
`sys.monitoring` records function
entry, including one-line bodies, and global/name reads. A module subtype records
attribute reads, including aliases, `from` imports and `getattr`. Values are not
proxied. Class references and method entry remain separate from class-definition
execution: importing a class does not exercise its methods.

The report lists **unobserved candidates**, not proven dead code. Static
reference-zero scans misclassify re-exports, aliases and registry callbacks.
For a documented API, add a test of its behavior. Remove a definition only after
confirming it is obsolete. Other retained candidates require an exact symbol
and a specific reason in `tools/execution_audit_allowlist.json`; there are no
wildcards or automatic baseline updates. Removed/renamed allowances fail too.

Each collection uses fresh output, source hashes and a run token. A failed test,
initialization error, missing observation or absent suite prevents success.
Normal subprocess, fork, spawn, forkserver, low-level exit and re-exec paths
save observations separately; pre-exec records survive a reused PID.

Limits: observation covers the executed configuration, not every possible
platform or input. Reads through `globals()`/`vars()` dictionaries, rebound code
namespaces and custom import loaders can evade the binding observer. Reads are
tracked by fixed symbol name, not assignment version. SIGKILL or a native crash
can lose a child's final observations. Instrumentation changes timing and
allocation, so this gate cannot measure performance or prove semantic equivalence.

For diagnosis, collect with `python -m tools.execution_audit collect --output DIR`
and inspect with `python -m tools.execution_audit check --output DIR --require-suites`.
A failed collection can still produce a diagnostic candidate list; it cannot
pass the gate. Reports and individual observations are generated evidence, not
files to commit. Record the method, classification and summary in the issue.
