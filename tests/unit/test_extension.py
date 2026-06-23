"""Unit tests for the generic Extension mechanism (Sprint 53).

Covers the core `Extension` ABC + `BlackBull.add_extension`: registration,
return-for-chaining, duplicate-key guard, duck-typing of legacy extensions,
and the optional async startup/shutdown lifecycle wired into app_startup /
app_shutdown.
"""
import pytest

from blackbull import BlackBull
from blackbull.extension import Extension


class _RecordingExtension(Extension):
    extension_key = 'recording'

    def __init__(self):
        self.inited = False
        self.started = False
        self.stopped = False

    def init_app(self, app):
        self.inited = True
        self._register(app)

    async def startup(self, app):
        self.started = True

    async def shutdown(self, app):
        self.stopped = True


class _LegacyDuckExtension:
    """No Extension base class — only the init_app convention."""

    def __init__(self):
        self.inited = False

    def init_app(self, app):
        self.inited = True
        app.extensions['legacy'] = self


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_add_extension_calls_init_app_and_returns_ext():
    app = BlackBull()
    ext = _RecordingExtension()
    returned = app.add_extension(ext)
    assert returned is ext                      # returned for chaining
    assert ext.inited is True
    assert app.extensions['recording'] is ext


def test_add_extension_accepts_legacy_duck_typed():
    app = BlackBull()
    ext = app.add_extension(_LegacyDuckExtension())
    assert ext.inited is True
    assert app.extensions['legacy'] is ext


def test_add_extension_rejects_non_extension():
    app = BlackBull()
    with pytest.raises(TypeError, match='init_app'):
        app.add_extension(object())


def test_duplicate_key_raises():
    app = BlackBull()
    app.add_extension(_RecordingExtension())
    with pytest.raises(RuntimeError, match='already registered'):
        app.add_extension(_RecordingExtension())


def test_reregistering_same_instance_is_idempotent():
    app = BlackBull()
    ext = _RecordingExtension()
    app.add_extension(ext)
    # _register guards on identity, so re-initialising the same instance is fine.
    ext.init_app(app)
    assert app.extensions['recording'] is ext


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_extension_defaults_startup_shutdown_are_noops():
    class _Minimal(Extension):
        extension_key = 'minimal'

        def init_app(self, app):
            self._register(app)

    app = BlackBull()
    ext = app.add_extension(_Minimal())
    assert app.extensions['minimal'] is ext  # no startup/shutdown override needed


@pytest.mark.asyncio
async def test_startup_and_shutdown_fire_on_lifespan():
    app = BlackBull()
    ext = app.add_extension(_RecordingExtension())

    # Drive the ASGI lifespan protocol the way the server does.
    sent = []
    incoming = [
        {'type': 'lifespan.startup'},
        {'type': 'lifespan.shutdown'},
    ]

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message['type'])

    await app({'type': 'lifespan', 'asgi': {'version': '3.0'}}, receive, send)

    assert 'lifespan.startup.complete' in sent
    assert 'lifespan.shutdown.complete' in sent
    assert ext.started is True
    assert ext.stopped is True
