"""Unit tests for ``blackbull/cli.py``.

End-to-end (``blackbull module:app`` actually serves traffic) lives in
``tests/integration/test_cli.py``; this file covers the pure-function
bits — bind parsing, import resolution, and the argparse surface.
"""
from __future__ import annotations

import sys

import pytest

from blackbull.cli import _build_parser, _import_app, _split_bind


# ---------------------------------------------------------------------------
# --bind parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('spec,expect', [
    ('0.0.0.0:8443',    ('0.0.0.0',   8443)),
    ('127.0.0.1:8080',  ('127.0.0.1', 8080)),
    (':8443',           ('',          8443)),
    ('localhost:80',    ('localhost', 80)),
    ('[::]:8443',       ('::',        8443)),
    ('[::1]:9000',      ('::1',       9000)),
])
def test_split_bind_valid(spec, expect):
    assert _split_bind(spec) == expect


@pytest.mark.parametrize('spec', [
    '0.0.0.0',            # no port
    '8443',               # no colon
    '0.0.0.0:abc',        # non-numeric port
    '0.0.0.0:70000',      # port out of range
    '0.0.0.0:-1',         # negative port
    '[::1',               # unterminated bracket
    '[::1]8443',          # missing ':' after ']'
])
def test_split_bind_invalid(spec):
    with pytest.raises(ValueError):
        _split_bind(spec)


# ---------------------------------------------------------------------------
# module:attr resolver
# ---------------------------------------------------------------------------

def test_import_app_resolves_attribute(tmp_path, monkeypatch):
    """A fresh on-disk module is importable by name and the attribute returned."""
    pkg = tmp_path / 'sample_cli_app.py'
    pkg.write_text('app = "i-am-the-app"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    assert _import_app('sample_cli_app:app') == 'i-am-the-app'


def test_import_app_rejects_missing_colon():
    with pytest.raises(ValueError, match="module:attribute"):
        _import_app('not_a_path')


@pytest.mark.parametrize('spec', [':app', 'mod:', ':'])
def test_import_app_rejects_empty_sides(spec):
    with pytest.raises(ValueError, match='non-empty'):
        _import_app(spec)


def test_import_app_unknown_module():
    with pytest.raises(ImportError, match='could not import'):
        _import_app('definitely_not_a_real_module_xyz:app')


def test_import_app_missing_attribute(tmp_path, monkeypatch):
    pkg = tmp_path / 'sample_cli_app2.py'
    pkg.write_text('not_app = 1\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AttributeError, match="has no attribute"):
        _import_app('sample_cli_app2:app')


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------

def test_parser_requires_app():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])  # missing positional


def test_parser_defaults():
    parser = _build_parser()
    args = parser.parse_args(['myapp:app'])
    assert args.app == 'myapp:app'
    assert args.bind == '127.0.0.1:8000'
    assert args.workers is None
    assert args.max_connections is None
    assert args.reload is False
    assert args.reload_paths is None


def test_parser_full_flags():
    parser = _build_parser()
    args = parser.parse_args([
        'pkg.mod:app',
        '--bind', '0.0.0.0:8443',
        '--certfile', 'c.pem', '--keyfile', 'k.pem',
        '--workers', '4',
        '--max-connections', '500',
        '--stream-queue-depth', '128',
        '--ws-queue-depth', '512',
        '--reload',
        '--reload-path', '/etc/app',
        '--reload-path', '/srv/templates',
    ])
    assert args.app == 'pkg.mod:app'
    assert args.bind == '0.0.0.0:8443'
    assert args.certfile == 'c.pem'
    assert args.keyfile == 'k.pem'
    assert args.workers == 4
    assert args.max_connections == 500
    assert args.stream_queue_depth == 128
    assert args.ws_queue_depth == 512
    assert args.reload is True
    assert args.reload_paths == ['/etc/app', '/srv/templates']


# ---------------------------------------------------------------------------
# main() — exit-code paths (without actually starting a server)
# ---------------------------------------------------------------------------

def test_main_bad_bind_returns_1(capsys):
    from blackbull.cli import main
    rc = main(['myapp:app', '--bind', '0.0.0.0'])  # missing port
    assert rc == 1
    err = capsys.readouterr().err
    assert 'blackbull:' in err and 'expected host:port' in err


def test_main_bad_app_returns_1(capsys):
    from blackbull.cli import main
    rc = main(['not_a_real_module_zzz:app', '--bind', '127.0.0.1:1'])
    assert rc == 1
    err = capsys.readouterr().err
    assert 'could not import' in err


def test_main_warns_on_specific_host(capsys, monkeypatch):
    """A non-wildcard host triggers the 'advisory in v1' warning before serving."""
    from blackbull import cli

    # Replace _serve so main returns without actually binding.
    called = {}
    def _fake_serve(app, **kwargs):
        called['app'] = app
        called['kwargs'] = kwargs
    monkeypatch.setattr(cli, '_serve', _fake_serve)
    # Provide a dummy app on disk.
    import sys as _sys, types
    mod = types.ModuleType('dummy_cli_warn_app')
    mod.app = object()
    _sys.modules['dummy_cli_warn_app'] = mod
    try:
        rc = cli.main(['dummy_cli_warn_app:app', '--bind', '10.0.0.5:8443'])
    finally:
        del _sys.modules['dummy_cli_warn_app']

    assert rc == 0
    err = capsys.readouterr().err
    assert 'advisory in v1' in err
    assert called['kwargs']['port'] == 8443
