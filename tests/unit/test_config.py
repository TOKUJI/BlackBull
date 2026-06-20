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
