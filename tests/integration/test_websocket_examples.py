"""The shipped WebSocket examples must actually complete a handshake.

Covers who may drive the WebSocket handshake and how that is
recorded, which is precisely the part of an example that an import check
cannot see: `examples/…` modules import fine while their handlers hang or
mis-sequence the connect event.

Each example is driven through :class:`~blackbull.testing.TestClient`, so a
break shows up here rather than in someone's first `python examples/…` run.
The three cover the three distinct handshake shapes that exist in the tree:

- ``websocket_object.py`` — the object form drives the handshake itself, and
  its ``/raw-echo`` route drives it the old way in the same app.
- ``translation_hub.py`` — a raw handler with unrelated middleware.
- ``ChatServer`` — custom auth middleware consumes ``websocket.connect``
  *without* accepting, keeping the option of rejecting with a close code.
"""
import importlib.util
import pathlib
import sys

import pytest

from blackbull.testing import TestClient, WebSocketDisconnect

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / 'examples'


def _load(relative: str, name: str):
    """Import an example module by path, without installing it."""
    path = EXAMPLES / relative
    if not path.exists():
        pytest.skip(f'example not present: {relative}')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:                    # optional example deps
        pytest.skip(f'{relative} needs an unavailable dependency: {exc}')
    return module


@pytest.fixture(scope='module')
def websocket_object_app():
    return _load('websocket_object.py', '_ex_websocket_object').app


# ---------------------------------------------------------------------------
# examples/websocket_object.py — the object form
# ---------------------------------------------------------------------------

def test_example_object_echo_round_trips(websocket_object_app):
    with TestClient(websocket_object_app) as client:
        with client.websocket_connect('/echo') as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'
            ws.send_bytes(b'\x01\x02')
            assert ws.receive_bytes() == b'\x01\x02'


def test_example_object_json_round_trips(websocket_object_app):
    with TestClient(websocket_object_app) as client:
        with client.websocket_connect('/json') as ws:
            ws.send_text('{"a": 1}')
            assert ws.receive_json() == {'seen': 1, 'echo': {'a': 1}}


def test_example_object_injects_path_and_query_params(websocket_object_app):
    with TestClient(websocket_object_app) as client:
        with client.websocket_connect('/rooms/lobby') as ws:
            assert ws.receive_text() == 'welcome to lobby (from 0)'
            ws.send_text('hi')
            assert ws.receive_text() == '[lobby] hi'


def test_example_object_query_param_overrides_its_default(websocket_object_app):
    with TestClient(websocket_object_app) as client:
        with client.websocket_connect('/rooms/lobby?since=7') as ws:
            assert ws.receive_text() == 'welcome to lobby (from 7)'


def test_example_object_rejects_without_credentials(websocket_object_app):
    with TestClient(websocket_object_app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/private'):
                pass
    assert excinfo.value.code == 4401


def test_example_object_accepts_with_credentials(websocket_object_app):
    with TestClient(websocket_object_app) as client:
        with client.websocket_connect(
                '/private', headers=[('authorization', 'Bearer letmein')]) as ws:
            assert ws.receive_text() == 'welcome'


def test_example_raw_form_still_works_in_the_same_app(websocket_object_app):
    """Both forms shipped side by side in one example — both must run."""
    with TestClient(websocket_object_app) as client:
        with client.websocket_connect('/raw-echo') as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'


# ---------------------------------------------------------------------------
# examples/translation_hub.py — raw handler
# ---------------------------------------------------------------------------

def test_example_translation_hub_handshake_and_clean_close():
    app = _load('translation_hub.py', '_ex_translation_hub').app
    with TestClient(app) as client:
        with client.websocket_connect('/ws'):
            pass          # accept, then unwind cleanly when the client closes


# ---------------------------------------------------------------------------
# examples/ChatServer — middleware consumes connect without accepting
# ---------------------------------------------------------------------------

def test_example_chatserver_rejects_an_unauthenticated_socket():
    """`auth_mw` pops `websocket.connect` itself and closes with 4401.

    This is the handshake shape that distinguishes "connect consumed" from
    "handshake accepted"; if those two ever collapse, the authorized path of
    this example stops sending its accept.
    """
    sys.path.insert(0, str(EXAMPLES / 'ChatServer'))
    try:
        app = _load('ChatServer/chatserver.py', '_ex_chatserver').app
    finally:
        sys.path.remove(str(EXAMPLES / 'ChatServer'))

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/ws'):
                pass
    assert excinfo.value.code == 4401
