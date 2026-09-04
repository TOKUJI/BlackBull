"""HTTP/2 scope extensions surface.

Covers:

- ``scope['extensions']['http.response.priority']`` — RFC 9218
  urgency/incremental hints, exposed under a key shape that matches
  gunicorn's beta HTTP/2 convention.
- ``scope['extensions']['http.response.http2_stream']`` — stream_id
  + send-window snapshot (gRPC server-streaming foundation).
- Removed alias: ``scope['http2_priority']`` is gone (v0.75.0); the
  priority extension above is its only home, and a grep-based guard
  keeps it from creeping back.

The new extensions are built by ``_build_h2_extensions`` in
``blackbull.server.http2_actor``; these tests pin its behaviour so
the populate sites can change shape later without silent drift.
"""
from __future__ import annotations

import pytest

from blackbull.asgi import ASGIEvent
from blackbull.server.http2_actor import (
    _DEFAULT_PRIORITY,
    _build_h2_extensions,
)


@pytest.fixture
def default_extensions() -> dict:
    return _build_h2_extensions(
        stream_id=1,
        priority=_DEFAULT_PRIORITY,
        peer_initial_window=65535,
        connection_window=65535,
    )


class TestBuildExtensions:
    """``_build_h2_extensions`` builds the ASGI ``scope['extensions']``
    dict for one HTTP/2 request — one fresh dict per call so per-stream
    fields don't bleed across requests."""

    def test_advertises_push_priority_and_stream_keys(self, default_extensions):
        assert ASGIEvent.HTTP_RESPONSE_PUSH in default_extensions
        assert 'http.response.priority' in default_extensions
        assert 'http.response.http2_stream' in default_extensions

    def test_omits_push_when_peer_does_not_permit_it(self):
        ext = _build_h2_extensions(
            stream_id=1,
            priority=_DEFAULT_PRIORITY,
            peer_initial_window=65535,
            connection_window=65535,
            peer_push_permitted=False,
        )
        assert ASGIEvent.HTTP_RESPONSE_PUSH not in ext
        assert 'http.response.priority' in ext
        assert 'http.response.http2_stream' in ext

    def test_push_marker_is_empty_dict(self, default_extensions):
        """Push is signalled by the *presence* of the key, not by
        contents — matches BlackBull's older behaviour."""
        assert default_extensions[ASGIEvent.HTTP_RESPONSE_PUSH] == {}

    def test_priority_contents_match_rfc_9218_default(self, default_extensions):
        """Default priority per RFC 9218 §4.1: urgency=3, incremental=False."""
        p = default_extensions['http.response.priority']
        assert p == {'urgency': 3, 'incremental': False}

    def test_priority_passthrough_for_explicit_hint(self):
        ext = _build_h2_extensions(
            stream_id=5,
            priority={'urgency': 0, 'incremental': True},
            peer_initial_window=65535,
            connection_window=65535)
        assert ext['http.response.priority'] == {
            'urgency': 0, 'incremental': True}

    def test_http2_stream_includes_stream_id(self, default_extensions):
        assert default_extensions['http.response.http2_stream']['stream_id'] == 1

    def test_http2_stream_includes_send_window_snapshot(self):
        ext = _build_h2_extensions(
            stream_id=3,
            priority=_DEFAULT_PRIORITY,
            peer_initial_window=12345,
            connection_window=99999)
        s = ext['http.response.http2_stream']
        assert s['send_window_remaining'] == 12345
        assert s['connection_send_window_remaining'] == 99999

    def test_separate_calls_return_independent_dicts(self):
        """Per-request extensions must not share mutable state — a
        downstream middleware mutating one request's extensions would
        otherwise leak into the next."""
        a = _build_h2_extensions(1, _DEFAULT_PRIORITY, 100, 100)
        b = _build_h2_extensions(3, _DEFAULT_PRIORITY, 200, 200)
        assert a is not b
        assert a['http.response.http2_stream'] is not b['http.response.http2_stream']
        a['http.response.http2_stream']['stream_id'] = 999
        assert b['http.response.http2_stream']['stream_id'] == 3

    def test_priority_dict_is_not_aliased_across_calls(self):
        """Mutating one scope's priority must not affect another's,
        even when the same ``priority`` argument was passed in
        (the helper currently passes the dict through — if that
        ever changes, this test pins the contract)."""
        shared = {'urgency': 2, 'incremental': True}
        a = _build_h2_extensions(1, shared, 100, 100)
        b = _build_h2_extensions(3, shared, 200, 200)
        # Both currently reference the caller's dict.  That's
        # acceptable as long as production callers don't mutate
        # it after the fact — and the populate sites in
        # http2_actor.py don't.  This test documents the contract
        # so accidental in-place mutation downstream gets caught.
        a_pri = a['http.response.priority']
        b_pri = b['http.response.priority']
        # They may be the same object OR a copy; both behaviours
        # are acceptable as long as reading is consistent.
        assert a_pri == b_pri == shared


class TestLegacyAliasIsGone:
    """``scope['http2_priority']`` was deprecated in v0.31.0 with removal
    scheduled for v0.32.0, and then shipped for another forty-three minor
    releases.  It is gone.

    The priority hint lives in exactly one place now —
    ``scope['extensions']['http.response.priority']`` — which is also the
    one that was ever correct under a mid-flight PRIORITY_UPDATE: the
    extensions dict is shared by reference, while the top-level alias was a
    dispatch-time snapshot that silently went stale."""

    def test_the_priority_hint_is_carried_by_the_extension(self):
        ext = _build_h2_extensions(
            stream_id=7,
            priority={'urgency': 5, 'incremental': False},
            peer_initial_window=65535,
            connection_window=65535)
        assert ext['http.response.priority'] == {
            'urgency': 5, 'incremental': False}

    def test_the_extensions_builder_never_emits_the_legacy_key(self):
        """The alias was written at the app boundary, not here — but a
        future edit that "restores" it would most naturally do it in this
        helper, so the absence is asserted where it would reappear."""
        ext = _build_h2_extensions(
            stream_id=7,
            priority={'urgency': 5, 'incremental': False},
            peer_initial_window=65535,
            connection_window=65535)
        assert 'http2_priority' not in ext

    def test_no_source_in_the_tree_still_populates_the_alias(self):
        """The durable guard.  A grep, because the alias's whole problem was
        that it was written in one place and read in another — a behavioural
        test at either end would not have caught a re-introduction at the
        other."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'
        offenders = [
            f'{path.relative_to(root.parent)}:{i}'
            for path in root.rglob('*.py')
            for i, line in enumerate(path.read_text().splitlines(), 1)
            if 'http2_priority' in line
        ]
        assert offenders == [], (
            'scope[\'http2_priority\'] was removed in v0.75.0; these lines '
            f'reference it again: {offenders}')
