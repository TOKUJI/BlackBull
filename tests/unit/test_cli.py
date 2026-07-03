"""Unit tests for ``blackbull/cli.py``.

End-to-end (``blackbull module:app`` actually serves traffic) lives in
``tests/integration/test_cli.py``; this file covers the pure-function
bits — bind parsing, import resolution, and the argparse surface.
"""
from __future__ import annotations

import sys

import pytest

from blackbull.cli import _build_parser, _import_app, _split_bind, _load_config


# ---------------------------------------------------------------------------
# --bind parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('spec,expect', [
    ('0.0.0.0:8443',    ('tcp', '0.0.0.0',   8443)),
    ('127.0.0.1:8080',  ('tcp', '127.0.0.1', 8080)),
    (':8443',           ('tcp', '',          8443)),
    ('localhost:80',    ('tcp', 'localhost', 80)),
    ('[::]:8443',       ('tcp', '::',        8443)),
    ('[::1]:9000',      ('tcp', '::1',       9000)),
    ('unix:/run/blackbull.sock', ('unix', '/run/blackbull.sock')),
    ('unix:relative.sock',       ('unix', 'relative.sock')),
    ('fd://3',   ('fd', 3)),
    ('fd://0',   ('fd', 0)),
    ('fd://100', ('fd', 100)),
])
def test_split_bind_valid(spec, expect):
    assert _split_bind(spec) == expect


def test_split_bind_unix_empty_path_raises():
    with pytest.raises(ValueError, match='unix:'):
        _split_bind('unix:')


@pytest.mark.parametrize('spec', [
    'fd://',          # missing fd number
    'fd://abc',       # non-integer fd
    'fd://-1',        # negative fd
    '0.0.0.0',        # no port
    '8443',           # no colon
    '0.0.0.0:abc',    # non-numeric port
    '0.0.0.0:70000',  # port out of range
    '0.0.0.0:-1',     # negative port
    '[::1',           # unterminated bracket
    '[::1]8443',      # missing ':' after ']'
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


def test_parser_version_flag(capsys):
    """`blackbull --version` prints the package version and exits 0."""
    from blackbull import __version__
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(['--version'])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert out.startswith('blackbull ')


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


# ---------------------------------------------------------------------------
# _load_config — Sprint 12c
# ---------------------------------------------------------------------------

def test_load_config_maps_server_workers(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[server]\nworkers = 4\n')
    result = _load_config(str(cfg_file))
    assert result['BB_WORKERS'] == '4'


def test_load_config_maps_limits_max_connections(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[limits]\nmax_connections = 100\n')
    result = _load_config(str(cfg_file))
    assert result['BB_MAX_CONNECTIONS'] == '100'


def test_load_config_maps_logging_booleans(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[logging]\naccess_log = false\nasync_logging = true\n')
    result = _load_config(str(cfg_file))
    assert result['BB_ACCESS_LOG'] == '0'
    assert result['BB_ASYNC_LOGGING'] == '1'


def test_load_config_maps_logging_sink_knobs(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text(
        '[logging]\nlog_format = "json"\nsyslog_addr = "127.0.0.1:514"\n'
        'batch_size = 128\nbatch_timeout_ms = 5\n')
    result = _load_config(str(cfg_file))
    assert result['BB_LOG_FORMAT'] == 'json'
    assert result['BB_SYSLOG_ADDR'] == '127.0.0.1:514'
    assert result['BB_LOG_BATCH_SIZE'] == '128'
    assert result['BB_LOG_BATCH_TIMEOUT_MS'] == '5'


def test_load_config_tls_cert_and_key(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[tls]\ncert = "cert.pem"\nkey = "key.pem"\n')
    result = _load_config(str(cfg_file))
    assert result['_certfile'] == 'cert.pem'
    assert result['_keyfile'] == 'key.pem'


def test_load_config_ignores_unknown_sections(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[unknown_section]\nfoo = 42\n')
    result = _load_config(str(cfg_file))
    assert result == {}


def test_load_config_ignores_unknown_keys(tmp_path):
    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[server]\nworkers = 2\nunknown_key = 99\n')
    result = _load_config(str(cfg_file))
    assert 'BB_WORKERS' in result
    assert len([k for k in result if not k.startswith('_')]) == 1


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_config(str(tmp_path / 'nonexistent.toml'))


def test_load_config_invalid_toml_raises(tmp_path):
    cfg_file = tmp_path / 'bad.toml'
    cfg_file.write_text('this is not valid toml ===\n')
    with pytest.raises(ValueError, match='TOML parse error'):
        _load_config(str(cfg_file))


def test_main_config_file_sets_env_var(tmp_path, monkeypatch, capsys):
    """A config file with [server] workers=3 must be visible to _serve."""
    from blackbull import cli
    import types, sys as _sys

    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[server]\nworkers = 3\n')

    called = {}
    def _fake_serve(app, **kwargs):
        import os
        called['BB_WORKERS'] = os.environ.get('BB_WORKERS')
    monkeypatch.setattr(cli, '_serve', _fake_serve)
    # Clear any pre-existing BB_WORKERS so the config can set it.
    monkeypatch.delenv('BB_WORKERS', raising=False)

    mod = types.ModuleType('dummy_cfg_app')
    mod.app = object()
    _sys.modules['dummy_cfg_app'] = mod
    try:
        rc = cli.main(['dummy_cfg_app:app', '--config', str(cfg_file)])
    finally:
        del _sys.modules['dummy_cfg_app']

    assert rc == 0
    assert called['BB_WORKERS'] == '3'


def test_main_env_var_takes_precedence_over_config(tmp_path, monkeypatch):
    """An env var already set must not be overwritten by the config file."""
    from blackbull import cli
    import types, sys as _sys

    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[server]\nworkers = 99\n')

    called = {}
    def _fake_serve(app, **kwargs):
        import os
        called['BB_WORKERS'] = os.environ.get('BB_WORKERS')
    monkeypatch.setattr(cli, '_serve', _fake_serve)
    monkeypatch.setenv('BB_WORKERS', '1')  # env var wins

    mod = types.ModuleType('dummy_cfg_precedence_app')
    mod.app = object()
    _sys.modules['dummy_cfg_precedence_app'] = mod
    try:
        rc = cli.main(['dummy_cfg_precedence_app:app', '--config', str(cfg_file)])
    finally:
        del _sys.modules['dummy_cfg_precedence_app']

    assert rc == 0
    assert called['BB_WORKERS'] == '1'  # env var was not overwritten


def test_main_config_tls_fallback(tmp_path, monkeypatch):
    """[tls] cert/key in config are used when --certfile/--keyfile are absent."""
    from blackbull import cli
    import types, sys as _sys

    cfg_file = tmp_path / 'bb.toml'
    cfg_file.write_text('[tls]\ncert = "my.crt"\nkey = "my.key"\n')

    called = {}
    def _fake_serve(app, **kwargs):
        called.update(kwargs)
    monkeypatch.setattr(cli, '_serve', _fake_serve)

    mod = types.ModuleType('dummy_tls_fallback_app')
    mod.app = object()
    _sys.modules['dummy_tls_fallback_app'] = mod
    try:
        rc = cli.main(['dummy_tls_fallback_app:app', '--config', str(cfg_file)])
    finally:
        del _sys.modules['dummy_tls_fallback_app']

    assert rc == 0
    assert called['certfile'] == 'my.crt'
    assert called['keyfile'] == 'my.key'


def test_main_bad_config_path_returns_1(capsys):
    from blackbull.cli import main
    rc = main(['myapp:app', '--config', '/no/such/file.toml'])
    assert rc == 1
    err = capsys.readouterr().err
    assert 'blackbull:' in err


def test_main_invalid_toml_returns_1(tmp_path, capsys):
    from blackbull.cli import main
    bad = tmp_path / 'bad.toml'
    bad.write_text('not toml ===\n')
    rc = main(['myapp:app', '--config', str(bad)])
    assert rc == 1
    err = capsys.readouterr().err
    assert 'blackbull:' in err
