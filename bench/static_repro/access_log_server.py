"""Server wrapper that enables the access log and writes it to a file.

Sprint 30 Tier 2 probe — measures **per-request server-side
duration** directly from the access log, so we can compare
latencies (not throughput) with and without watermarks.

Usage:
    python3 bench/static_repro/access_log_server.py --port 8080

Env vars consumed:
    STATIC_DIR, DATASET_PATH — same as ``server_probe.py``
    ACCESS_LOG_PATH          — file to write access log to (default
                               /tmp/blackbull_access.log)
    BB_ACCEPT_PAUSE_HIGH_WATERMARK / LOW_WATERMARK / MAX_CONNECTIONS —
                               passed through to BlackBull.

Access log format (per ``blackbull.server.access_log``):
    {ip} "{method} {path} HTTP/{ver}" {status} {bytes} {duration}ms

The probe driver parses the ``{duration}ms`` field and the line's
log timestamp to compute latency percentiles for the measurement
window (post-warmup).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import runpy
import sys


# prctl constants (matches server_probe.py — let py-spy attach without sudo)
PR_SET_PTRACER = 0x59616d61
PR_SET_PTRACER_ANY = ctypes.c_ulong(-1).value


def _allow_any_ptracer() -> None:
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    libc.prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0)


def _setup_access_log(path: str) -> None:
    """Route the ``blackbull.access`` logger to ``path``.

    The format MUST keep timestamp + message together so the probe
    driver can filter entries by time.  We prepend a sortable ISO
    timestamp.
    """
    handler = logging.FileHandler(path, mode='w', encoding='utf-8')
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    ))
    access_logger = logging.getLogger('blackbull.access')
    access_logger.setLevel(logging.INFO)
    access_logger.addHandler(handler)
    # Do NOT propagate — keeps access lines out of the root logger.
    access_logger.propagate = False


def _patch_access_log_format() -> None:
    """Monkeypatch ``AccessLogRecord.format`` to print sub-ms precision.

    Sprint 30 Tier 2 probe — the production format
    ``f'{self.duration_ms():.0f}ms'`` integer-rounds, which wipes the
    signal on static-file workloads where each request takes <1 ms.
    Local-probe-only override; production format stays integer-ms.
    """
    from blackbull.server import access_log as _al

    def _format(self):
        if self.close_code is not None:
            return (f'{self.client_ip} '
                    f'"{self.method} {self.path} WS/{self.http_version}" '
                    f'101 close={self.close_code} '
                    f'{self.duration_ms():.3f}ms')
        return (f'{self.client_ip} '
                f'"{self.method} {self.path} HTTP/{self.http_version}" '
                f'{self.status} {self.response_bytes} '
                f'{self.duration_ms():.3f}ms')

    _al.AccessLogRecord.format = _format


if __name__ == '__main__':
    _allow_any_ptracer()

    # Make sure access logging is ON; the harness uses BB_ACCESS_LOG=0
    # by default for peer-comparable benchmarks, but the Tier 2 latency
    # probe NEEDS it.
    os.environ['BB_ACCESS_LOG'] = '1'

    log_path = os.environ.get('ACCESS_LOG_PATH', '/tmp/blackbull_access.log')
    _setup_access_log(log_path)
    _patch_access_log_format()
    sys.stderr.write(f'access log → {log_path} (sub-ms precision)\n')

    here = os.path.dirname(os.path.abspath(__file__))
    app_py = os.path.normpath(os.path.join(here, '..', 'httparena', 'app.py'))
    sys.argv = [app_py] + sys.argv[1:]
    runpy.run_path(app_py, run_name='__main__')
