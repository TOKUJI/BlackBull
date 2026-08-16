"""``BB_MAX_CONNECTIONS`` ships a finite default, derived rather than picked.

The mechanism (503 + ``Retry-After: 1``) has existed for a while; the
default was ``0``, uncapped, on the reasoning that Kestrel's
``MaxConcurrentConnections`` also defaults to unlimited.  The argument
that made 30 MiB the right body default — *a limit nobody sets protects
nobody* — applies here identically, so the default is now finite.

The value is **derived from the process's own file-descriptor budget**,
not chosen.  A cap above ``RLIMIT_NOFILE`` is decorative: ``accept()``
fails with ``EMFILE`` before the cap is ever consulted, and the peer gets
a dropped connection instead of the 503 the mechanism exists to send.
Derived, the cap can only refuse connections the OS was going to refuse
anyway, which is what makes it safe to turn on by default — and it tracks
the operator's own statement of intent, since raising the fd limit is how
an operator says how big this process is allowed to get.
"""
import resource

import pytest

from blackbull.env import (FD_RESERVE, get_settings, reset_settings_cache,
                           resolve_max_connections)


@pytest.fixture(autouse=True)
def _fresh_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestDerivation:
    def test_auto_derives_from_the_fd_budget(self):
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert resolve_max_connections('auto') == soft - FD_RESERVE

    def test_the_default_is_auto(self, monkeypatch):
        monkeypatch.delenv('BB_MAX_CONNECTIONS', raising=False)
        reset_settings_cache()
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)

        assert get_settings().max_connections == soft - FD_RESERVE, (
            'the shipped default is no longer finite')

    def test_a_reserve_is_held_back(self):
        """Listeners, epoll, log files and the app's own descriptors.

        Handing every descriptor to connections would make the first
        accepted connection past the budget fail somewhere else — a log
        write or a database checkout — which is a worse failure than a
        refused connection because it happens to a request already
        accepted.
        """
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert resolve_max_connections('auto') < soft

    def test_a_tiny_fd_budget_still_yields_a_usable_cap(self, monkeypatch):
        """The reserve must never drive the cap to zero or negative.

        A cap of 0 means *uncapped* in this server's vocabulary, so an
        arithmetic slip here would turn the tightest possible environment
        into the least protected one.
        """
        monkeypatch.setattr(
            'blackbull.env.resource.getrlimit',
            lambda _res: (16, 16))
        assert resolve_max_connections('auto') >= 1


class TestExplicitValues:
    def test_zero_still_means_uncapped(self, monkeypatch):
        monkeypatch.setenv('BB_MAX_CONNECTIONS', '0')
        reset_settings_cache()
        assert get_settings().max_connections == 0

    def test_an_explicit_number_wins(self, monkeypatch):
        monkeypatch.setenv('BB_MAX_CONNECTIONS', '512')
        reset_settings_cache()
        assert get_settings().max_connections == 512

    def test_an_explicit_number_is_not_clamped_to_the_fd_budget(self, monkeypatch):
        """An operator who names a number means it.

        Silently lowering it would make the running configuration differ
        from the configured one with nothing to show for it; the
        derivation is what `auto` is for.
        """
        monkeypatch.setattr(
            'blackbull.env.resource.getrlimit',
            lambda _res: (128, 128))
        monkeypatch.setenv('BB_MAX_CONNECTIONS', '4096')
        reset_settings_cache()
        assert get_settings().max_connections == 4096

    def test_nonsense_falls_back_to_auto(self, monkeypatch):
        """Unparseable input must not silently disable the cap."""
        monkeypatch.setenv('BB_MAX_CONNECTIONS', 'banana')
        reset_settings_cache()
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert get_settings().max_connections == soft - FD_RESERVE

    def test_a_negative_value_falls_back_to_auto(self, monkeypatch):
        monkeypatch.setenv('BB_MAX_CONNECTIONS', '-5')
        reset_settings_cache()
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert get_settings().max_connections == soft - FD_RESERVE


class TestObservability:
    @pytest.mark.asyncio
    async def test_the_derived_value_is_logged_at_startup(self, caplog, monkeypatch):
        """A default nobody can see is a default nobody can size.

        Unlike a value in a config file, the derived one depends on the
        host, so the only way an operator learns it is by being told.
        """
        monkeypatch.delenv('BB_MAX_CONNECTIONS', raising=False)
        reset_settings_cache()
        from blackbull import BlackBull
        from blackbull.server import ASGIServer

        with caplog.at_level('INFO', logger='blackbull'):
            server = ASGIServer(BlackBull(),
                                max_connections=get_settings().max_connections)
            server.open_socket(0)
            try:
                messages = [r.getMessage() for r in caplog.records
                            if 'max_connections' in r.getMessage()]
                assert messages, (
                    'the derived connection cap was never reported; an '
                    'operator has no way to learn it')
                assert 'derived' in messages[-1], (
                    f'a derived value was reported as if someone configured '
                    f'it: {messages[-1]!r}.  That sends an operator hunting '
                    f'for a setting nobody wrote.')
            finally:
                server.close()

    @pytest.mark.asyncio
    async def test_an_explicit_value_is_reported_as_explicit(self, caplog,
                                                             monkeypatch):
        monkeypatch.setenv('BB_MAX_CONNECTIONS', '512')
        reset_settings_cache()
        from blackbull import BlackBull
        from blackbull.server import ASGIServer

        with caplog.at_level('INFO', logger='blackbull'):
            server = ASGIServer(BlackBull(), max_connections=512)
            server.open_socket(0)
            try:
                messages = [r.getMessage() for r in caplog.records
                            if 'max_connections' in r.getMessage()]
                assert messages and 'explicitly' in messages[-1], messages
            finally:
                server.close()
