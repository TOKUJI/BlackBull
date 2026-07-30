"""`app.static()` behaviour that must survive the move to a route middleware.

These are end-to-end through `TestClient`, deliberately: the point of the
change is *where* static serving is dispatched from, so a test that pokes
the middleware object directly would not notice if dispatch broke.

Written against the global-middleware implementation and expected to pass
unchanged afterwards — the bare-prefix cases in particular, since the `path`
converter is `r'.+'` and cannot match an empty segment.
"""
import pathlib

import pytest

from blackbull import BlackBull
from blackbull.testing import TestClient


@pytest.fixture
def site(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / 'hello.txt').write_bytes(b'Hello, static world!')
    (tmp_path / 'index.html').write_bytes(b'<h1>root index</h1>')
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'deep.txt').write_bytes(b'deep')
    (sub / 'index.html').write_bytes(b'<h1>sub index</h1>')
    return tmp_path


def _app(site: pathlib.Path, *, prefix: str = '/assets', **kw) -> BlackBull:
    app = BlackBull()
    app.static(prefix, str(site), **kw)

    @app.route(path='/')
    async def home():
        return 'home'

    return app


class TestFileServing:
    def test_serves_a_file_under_the_prefix(self, site):
        with TestClient(_app(site)) as client:
            r = client.get('/assets/hello.txt')
        assert r.status_code == 200
        assert r.content == b'Hello, static world!'

    def test_serves_a_nested_file(self, site):
        with TestClient(_app(site)) as client:
            r = client.get('/assets/sub/deep.txt')
        assert r.status_code == 200
        assert r.content == b'deep'

    def test_missing_file_under_the_prefix_is_404(self, site):
        with TestClient(_app(site)) as client:
            r = client.get('/assets/nope.txt')
        assert r.status_code == 404

    def test_non_static_route_is_untouched(self, site):
        with TestClient(_app(site)) as client:
            r = client.get('/')
        assert r.status_code == 200
        assert r.text == 'home'

    def test_traversal_out_of_root_is_rejected(self, site):
        with TestClient(_app(site)) as client:
            r = client.get('/assets/../../etc/passwd')
        assert r.status_code in (400, 404)

    def test_head_is_served(self, site):
        with TestClient(_app(site)) as client:
            r = client.head('/assets/hello.txt')
        assert r.status_code == 200


class TestBarePrefixWithIndex:
    """The compatibility risk called out in the proposal §7.4.

    With `index=` set, a request for the prefix itself resolves to the root
    directory and serves the index file.  `/assets/{filepath:path}` cannot
    match either spelling, so the route form has to register the bare prefix
    too or these regress.
    """

    def test_prefix_with_trailing_slash_serves_index(self, site):
        with TestClient(_app(site, index='index.html')) as client:
            r = client.get('/assets/')
        assert r.status_code == 200
        assert r.content == b'<h1>root index</h1>'

    def test_prefix_without_trailing_slash_serves_index(self, site):
        with TestClient(_app(site, index='index.html')) as client:
            r = client.get('/assets')
        assert r.status_code == 200
        assert r.content == b'<h1>root index</h1>'

    def test_subdirectory_serves_its_index(self, site):
        with TestClient(_app(site, index='index.html')) as client:
            r = client.get('/assets/sub/')
        assert r.status_code == 200
        assert r.content == b'<h1>sub index</h1>'

    def test_directory_without_index_option_is_not_listed(self, site):
        with TestClient(_app(site)) as client:
            r = client.get('/assets/sub/')
        assert r.status_code == 404


class TestMultipleStaticMounts:
    def test_each_prefix_serves_its_own_root(self, tmp_path):
        a, b = tmp_path / 'a', tmp_path / 'b'
        a.mkdir(), b.mkdir()
        (a / 'f.txt').write_bytes(b'from-a')
        (b / 'f.txt').write_bytes(b'from-b')
        app = BlackBull()
        app.static('/a', str(a))
        app.static('/b', str(b))
        with TestClient(app) as client:
            assert client.get('/a/f.txt').content == b'from-a'
            assert client.get('/b/f.txt').content == b'from-b'

    def test_static_roots_records_every_mount(self, tmp_path):
        a, b = tmp_path / 'a', tmp_path / 'b'
        a.mkdir(), b.mkdir()
        app = BlackBull()
        app.static('/a', str(a))
        app.static('/b', str(b))
        assert len(app._static_roots) == 2


class TestProductionGate:
    def test_production_does_not_serve_static(self, site, monkeypatch):
        monkeypatch.setenv('BLACKBULL_ENV', 'production')
        with TestClient(_app(site)) as client:
            r = client.get('/assets/hello.txt')
        assert r.status_code == 404

    def test_development_serves_static(self, site, monkeypatch):
        monkeypatch.setenv('BLACKBULL_ENV', 'development')
        with TestClient(_app(site)) as client:
            r = client.get('/assets/hello.txt')
        assert r.status_code == 200


class TestExplicitRoutesWin:
    """Registering a path twice replaces the first entry silently, so
    `static()` must not claim a bare prefix an explicit route already owns."""

    def test_root_mount_does_not_eat_the_apps_own_root_route(self, site):
        app = BlackBull()

        @app.route(path='/')
        async def home():
            return 'home'

        app.static('/', str(site), index='index.html')
        with TestClient(app) as client:
            r = client.get('/')
            asset = client.get('/hello.txt')
        assert r.text == 'home'
        assert asset.content == b'Hello, static world!'

    def test_root_mount_serves_index_when_no_root_route_exists(self, site):
        app = BlackBull()
        app.static('/', str(site), index='index.html')
        with TestClient(app) as client:
            r = client.get('/')
        assert r.status_code == 200
        assert r.content == b'<h1>root index</h1>'

    def test_explicit_route_on_the_prefix_wins(self, site):
        app = BlackBull()

        @app.route(path='/assets')
        async def listing():
            return 'listing'

        app.static('/assets', str(site), index='index.html')
        with TestClient(app) as client:
            assert client.get('/assets').text == 'listing'
            assert client.get('/assets/hello.txt').content == b'Hello, static world!'
