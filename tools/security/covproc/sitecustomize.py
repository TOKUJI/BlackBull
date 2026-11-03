"""Start coverage in every forked process (BLA-526 M5, G3-3).

On PYTHONPATH with COVERAGE_PROCESS_START set, each process the fixture
spawns measures itself into its own ``.coverage.*`` data file, which
``coverage combine`` merges.  The fixture preforks workers, so a single
``coverage run`` would only see the manager.
"""
try:  # pragma: no cover - environment glue
    import coverage
    coverage.process_startup()
except Exception:  # noqa: BLE001 - measurement must never break the server
    import os
    if os.environ.get('BB_EXECUTION_AUDIT_DIR'):
        from pathlib import Path
        import traceback
        (Path(os.environ['BB_EXECUTION_AUDIT_DIR']) / f'initialization-error-{os.getpid()}.txt').write_text(traceback.format_exc())
        raise SystemExit('Execution audit initialization failed')
