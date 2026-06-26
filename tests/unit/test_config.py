"""Unit tests for :class:`blackbull.AppConfig` and its resolution in
:meth:`blackbull.BlackBull.run`.

These cover the precedence chain only — explicit ``run()`` arg beats the
bound ``AppConfig`` beats ``serve``'s built-in default — without actually
binding a socket: ``blackbull.app.serve`` is monkeypatched to capture the
keyword arguments it would have been called with.
"""
from __future__ import annotations

import dataclasses

import pytest

import blackbull.app as app_module
from blackbull import AppConfig, BlackBull
from blackbull.config import resolve_run_config


@pytest.fixture()
def captured_serve(monkeypatch):
    """Replace the module-level ``serve`` so ``run()`` records its kwargs
    instead of binding a real socket."""
    captured: dict = {}

    def _fake_serve(app, **kwargs):
        captured.clear()
        captured.update(kwargs)

    monkeypatch.setattr(app_module, 'serve', _fake_serve)
    return captured


# ---------------------------------------------------------------------------
# AppConfig dataclass surface
# ---------------------------------------------------------------------------

def test_appconfig_defaults_are_sentinels():
    cfg = AppConfig()
    assert cfg.certfile is None
    assert cfg.keyfile is None
    assert cfg.port == 0
    assert cfg.unix_path is None
    assert cfg.inherited_fd is None
    assert cfg.workers is None
    assert cfg.max_connections is None
    assert cfg.stream_queue_depth is None
    assert cfg.ws_queue_depth is None
    assert cfg.reload is False
    assert cfg.reload_paths is None


def test_appconfig_is_frozen():
    cfg = AppConfig(port=8000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.port = 9000  # type: ignore[misc]


def test_appconfig_has_no_host_field():
    # host binding is dual-stack-only at the socket layer; a host field
    # would silently do nothing, so it is deliberately absent.
    assert 'host' not in {f.name for f in dataclasses.fields(AppConfig)}


# ---------------------------------------------------------------------------
# run() resolution
# ---------------------------------------------------------------------------

def test_run_without_config_uses_serve_defaults(captured_serve):
    BlackBull().run()
    assert captured_serve['port'] == 0
    assert captured_serve['certfile'] is None
    assert captured_serve['keyfile'] is None
    assert captured_serve['workers'] is None
    assert captured_serve['reload'] is False


def test_run_resolves_from_bound_config(captured_serve):
    cfg = AppConfig(port=8443, certfile='c.pem', keyfile='k.pem', workers=4)
    BlackBull(config=cfg).run()
    assert captured_serve['port'] == 8443
    assert captured_serve['certfile'] == 'c.pem'
    assert captured_serve['keyfile'] == 'k.pem'
    assert captured_serve['workers'] == 4


def test_explicit_arg_overrides_config(captured_serve):
    cfg = AppConfig(port=8443, certfile='c.pem', keyfile='k.pem')
    app = BlackBull(config=cfg)
    app.run(port=9000)
    assert captured_serve['port'] == 9000          # explicit wins
    assert captured_serve['certfile'] == 'c.pem'   # config still supplies TLS


def test_explicit_reload_true_overrides_config_false(captured_serve):
    cfg = AppConfig(reload=False)
    BlackBull(config=cfg).run(reload=True)
    assert captured_serve['reload'] is True


def test_config_unix_path_and_queue_depths_flow_through(captured_serve):
    cfg = AppConfig(unix_path='/run/bb.sock', stream_queue_depth=128,
                    ws_queue_depth=512, max_connections=1024)
    BlackBull(config=cfg).run()
    assert captured_serve['unix_path'] == '/run/bb.sock'
    assert captured_serve['stream_queue_depth'] == 128
    assert captured_serve['ws_queue_depth'] == 512
    assert captured_serve['max_connections'] == 1024


# ---------------------------------------------------------------------------
# BLACKBULL_* environment variable resolution
# ---------------------------------------------------------------------------

def test_env_port_resolved_and_coerced_to_int(captured_serve, monkeypatch):
    monkeypatch.setenv('BLACKBULL_PORT', '9001')
    BlackBull().run()
    assert captured_serve['port'] == 9001          # str → int


def test_env_overrides_appconfig(captured_serve, monkeypatch):
    monkeypatch.setenv('BLACKBULL_PORT', '9001')
    BlackBull(config=AppConfig(port=8443)).run()
    assert captured_serve['port'] == 9001          # env beats config


def test_explicit_arg_overrides_env(captured_serve, monkeypatch):
    monkeypatch.setenv('BLACKBULL_PORT', '9001')
    BlackBull().run(port=7000)
    assert captured_serve['port'] == 7000          # explicit beats env


def test_env_cert_and_key(captured_serve, monkeypatch):
    monkeypatch.setenv('BLACKBULL_CERT', '/etc/ssl/cert.pem')
    monkeypatch.setenv('BLACKBULL_KEY', '/etc/ssl/key.pem')
    BlackBull().run()
    assert captured_serve['certfile'] == '/etc/ssl/cert.pem'
    assert captured_serve['keyfile'] == '/etc/ssl/key.pem'


@pytest.mark.parametrize('raw,expected', [
    ('1', True), ('true', True), ('TRUE', True), ('yes', True), ('on', True),
    ('0', False), ('false', False), ('no', False), ('', False),
])
def test_env_reload_bool_parsing(captured_serve, monkeypatch, raw, expected):
    monkeypatch.setenv('BLACKBULL_RELOAD', raw)
    BlackBull().run()
    assert captured_serve['reload'] is expected


def test_tuning_knob_not_resolved_from_blackbull_namespace(captured_serve, monkeypatch):
    # workers is a BB_* tuning knob, not a BLACKBULL_* deploy setting: a
    # BLACKBULL_WORKERS var must NOT be picked up (no double-sourcing).
    monkeypatch.setenv('BLACKBULL_WORKERS', '8')
    BlackBull().run()
    assert captured_serve['workers'] is None        # serve() applies BB_WORKERS


# ---------------------------------------------------------------------------
# .env file resolution (lowest of the env tiers)
# ---------------------------------------------------------------------------

def test_dotenv_file_resolved(captured_serve, monkeypatch, tmp_path):
    monkeypatch.delenv('BLACKBULL_PORT', raising=False)
    (tmp_path / '.env').write_text('BLACKBULL_PORT=9100\n')
    monkeypatch.chdir(tmp_path)
    BlackBull().run()
    assert captured_serve['port'] == 9100


def test_real_env_beats_dotenv(captured_serve, monkeypatch, tmp_path):
    (tmp_path / '.env').write_text('BLACKBULL_PORT=9100\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BLACKBULL_PORT', '9200')
    BlackBull().run()
    assert captured_serve['port'] == 9200           # process env wins over .env


# ---------------------------------------------------------------------------
# resolve_run_config — source attribution
# ---------------------------------------------------------------------------

def test_sources_label_argument_env_config_default(monkeypatch):
    monkeypatch.setenv('BLACKBULL_CERT', '/c.pem')
    monkeypatch.delenv('BLACKBULL_PORT', raising=False)
    resolved, sources = resolve_run_config(
        {'keyfile': '/explicit.key'},
        AppConfig(port=8443),
    )
    assert sources['keyfile'] == 'argument'
    assert sources['certfile'] == '$BLACKBULL_CERT'
    assert sources['port'] == 'AppConfig'
    assert sources['reload'] == 'default'
    assert resolved['port'] == 8443
    assert resolved['certfile'] == '/c.pem'


def test_startup_logging_reports_non_default_sources(monkeypatch, caplog):
    import logging
    monkeypatch.setenv('BLACKBULL_PORT', '9001')

    def _fake_serve(app, **kwargs):
        pass

    monkeypatch.setattr(app_module, 'serve', _fake_serve)
    with caplog.at_level(logging.INFO, logger='blackbull.config'):
        BlackBull().run(certfile='/explicit.pem')

    msgs = [r.getMessage() for r in caplog.records if r.name == 'blackbull.config']
    # env-sourced port is reported; explicit certfile and default keyfile are not.
    assert any('port=9001' in m and 'BLACKBULL_PORT' in m for m in msgs)
    assert not any('certfile' in m for m in msgs)
    assert not any('keyfile' in m for m in msgs)
