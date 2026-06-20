"""Tests for ``blackbull serve`` — the zero-code static file server (B4).

Covers the CLI surface (argument parsing, dispatch, error exit codes) and
the behaviour of the app built by :func:`blackbull.cli._build_static_app`
(file serving, directory index, ETag / 304 conditional requests).
"""
from __future__ import annotations

import pytest

import blackbull.cli as cli
from blackbull.cli import _build_static_app
from blackbull.testing import TestClient


@pytest.fixture()
def site(tmp_path):
    (tmp_path / 'index.html').write_text('<h1>home</h1>')
    (tmp_path / 'a.txt').write_text('alpha')
    return tmp_path


# ---------------------------------------------------------------------------
# _build_static_app — directory validation
# ---------------------------------------------------------------------------

def test_build_static_app_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _build_static_app(str(tmp_path / 'nope'), etag=True, cache=False,
                          index='index.html')


def test_build_static_app_file_not_dir_raises(tmp_path):
    f = tmp_path / 'a_file'
    f.write_text('x')
    with pytest.raises(NotADirectoryError):
        _build_static_app(str(f), etag=True, cache=False, index='index.html')


# ---------------------------------------------------------------------------
# Serving behaviour
# ---------------------------------------------------------------------------

def test_serves_a_file(site):
    app = _build_static_app(str(site), etag=True, cache=False, index='index.html')
    with TestClient(app) as c:
        r = c.get('/a.txt')
    assert r.status_code == 200
    assert r.text == 'alpha'


def test_directory_index_served_on_root(site):
    app = _build_static_app(str(site), etag=True, cache=False, index='index.html')
    with TestClient(app) as c:
        r = c.get('/')
    assert r.status_code == 200
    assert '<h1>home</h1>' in r.text


def test_index_disabled_means_root_404(site):
    app = _build_static_app(str(site), etag=True, cache=False, index=None)
    with TestClient(app) as c:
        r = c.get('/')
    assert r.status_code == 404


def test_missing_file_is_404(site):
    app = _build_static_app(str(site), etag=True, cache=False, index='index.html')
    with TestClient(app) as c:
        r = c.get('/does-not-exist.txt')
    assert r.status_code == 404


def test_etag_and_conditional_304(site):
    app = _build_static_app(str(site), etag=True, cache=False, index='index.html')
    with TestClient(app) as c:
        r = c.get('/a.txt')
        etag = r.headers.get('etag')
        assert etag is not None
        r2 = c.get('/a.txt', headers={'if-none-match': etag})
    assert r2.status_code == 304


def test_no_etag_omits_etag_header(site):
    app = _build_static_app(str(site), etag=False, cache=False, index='index.html')
    with TestClient(app) as c:
        r = c.get('/a.txt')
    assert r.status_code == 200
    assert 'etag' not in {k.lower() for k in r.headers}


# ---------------------------------------------------------------------------
# main() dispatch + exit codes
# ---------------------------------------------------------------------------

def test_main_dispatches_serve_to_static(site, monkeypatch):
    captured: dict = {}

    def _fake_serve(app, **kwargs):
        captured['app'] = app
        captured.update(kwargs)

    monkeypatch.setattr(cli, '_serve', _fake_serve)
    rc = cli.main(['serve', str(site), '--bind', '127.0.0.1:8123'])
    assert rc == 0
    assert captured['port'] == 8123
    # The dispatched app is a BlackBull instance serving the directory.
    from blackbull import BlackBull
    assert isinstance(captured['app'], BlackBull)


def test_main_serve_missing_dir_returns_1(tmp_path, capsys):
    rc = cli.main(['serve', str(tmp_path / 'absent')])
    assert rc == 1
    assert 'not found' in capsys.readouterr().err


def test_main_serve_bad_bind_returns_1(site, capsys):
    rc = cli.main(['serve', str(site), '--bind', '0.0.0.0'])  # no port
    assert rc == 1
    assert 'blackbull:' in capsys.readouterr().err


def test_main_serve_rejects_fd_bind(site, capsys):
    rc = cli.main(['serve', str(site), '--bind', 'fd://3'])
    assert rc == 1
    assert 'fd://' in capsys.readouterr().err
