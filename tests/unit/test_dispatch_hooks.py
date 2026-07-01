"""End-to-end tests for ``@app.register_converter`` — custom handler return
types, driven through the real router + ``_dispatch`` via TestClient.

Cleanup-on-completion is tested separately via the ``scope_completed`` event
(see ``test_scope_completed.py``); it is no longer a dedicated hook.
"""

from blackbull import BlackBull
from blackbull.testing import TestClient


# ---------------------------------------------------------------------------
# register_converter (end-to-end through _dispatch)
# ---------------------------------------------------------------------------

class Widget:
    def __init__(self, name: str):
        self.name = name


def test_register_converter_direct_form() -> None:
    app = BlackBull()
    app.register_converter(Widget, lambda w: {'widget': w.name})

    @app.route(path='/w')
    async def get_widget():
        return Widget('gizmo')

    with TestClient(app) as client:
        r = client.get('/w')
    assert r.status_code == 200
    assert r.json() == {'widget': 'gizmo'}


def test_register_converter_decorator_form() -> None:
    app = BlackBull()

    @app.register_converter(Widget)
    def _to_dict(w):
        return {'widget': w.name}

    @app.route(path='/w')
    async def get_widget():
        return Widget('sprocket')

    with TestClient(app) as client:
        r = client.get('/w')
    assert r.json() == {'widget': 'sprocket'}


def test_register_converter_after_route_registration() -> None:
    """The registry is shared by reference, so late registration still works."""
    app = BlackBull()

    @app.route(path='/w')
    async def get_widget():
        return Widget('late')

    # Registered *after* the route was adapted.
    app.register_converter(Widget, lambda w: {'widget': w.name})

    with TestClient(app) as client:
        r = client.get('/w')
    assert r.json() == {'widget': 'late'}


def test_converter_may_return_a_response() -> None:
    from http import HTTPStatus
    from blackbull import Response
    app = BlackBull()
    app.register_converter(
        Widget, lambda w: Response(w.name.encode(), status=HTTPStatus.CREATED))

    @app.route(path='/w')
    async def get_widget():
        return Widget('body')

    with TestClient(app) as client:
        r = client.get('/w')
    assert r.status_code == 201
    assert r.text == 'body'


def test_unregistered_type_is_a_server_error() -> None:
    app = BlackBull()

    @app.route(path='/w')
    async def get_widget():
        return Widget('no-converter')

    with TestClient(app) as client:
        r = client.get('/w')
    assert r.status_code == 500

