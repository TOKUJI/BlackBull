"""Per-user runtime paths shared by the fixture app, the probe and the just recipes.

Everything the BLA-526 target publishes — PID file, log, generated TLS
certificate — lives in one per-user private directory.  A fixed /tmp path
would let another local user pre-create it and substitute the certificate
the probe trusts, so the directory is created with mode 0700 and its owner
and permissions are re-checked on every use.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

APP = 'bb-vuln-target'


class UnsafeRuntimeDirError(RuntimeError):
    """The runtime directory exists but is not private to the current user."""


def runtime_dir() -> Path:
    """Return (creating if needed) the private per-user runtime directory."""
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if xdg:
        path = Path(xdg) / APP
    else:
        path = Path(f'/tmp/{APP}-{os.getuid()}')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.stat()
    if stat.st_uid != os.getuid():
        raise UnsafeRuntimeDirError(
            f'{path} is owned by uid {stat.st_uid}, not uid {os.getuid()}')
    if stat.st_mode & 0o077:
        raise UnsafeRuntimeDirError(
            f'{path} is group/world accessible (mode {stat.st_mode & 0o777:o}); '
            f'refusing to publish credentials there')
    return path


def pid_file() -> Path:
    return runtime_dir() / 'server.pid'


def log_file() -> Path:
    return runtime_dir() / 'server.log'


def tls_dir() -> Path:
    path = runtime_dir() / 'tls'
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def cert_file() -> Path:
    return tls_dir() / 'cert.pem'


if __name__ == '__main__':
    # `python tools/security/paths.py pid|log|tls|cert` for the just recipes.
    choices = {'pid': pid_file, 'log': log_file, 'tls': tls_dir, 'cert': cert_file}
    if len(sys.argv) != 2 or sys.argv[1] not in choices:
        print(f'usage: {sys.argv[0]} pid|log|tls|cert', file=sys.stderr)
        raise SystemExit(2)
    print(choices[sys.argv[1]]())
