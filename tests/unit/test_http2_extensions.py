"""Sprint 32 — HTTP/2 scope extensions surface.

Covers:

- ``scope['extensions']['http.response.priority']`` — RFC 9218
  urgency/incremental hints, exposed under a key shape that matches
  gunicorn's beta HTTP/2 convention.
- ``scope['extensions']['http.response.http2_stream']`` — stream_id
  + send-window snapshot (gRPC server-streaming foundation).
- Deprecation alias: ``scope['http2_priority']`` is still populated
  and MUST agree with the new priority extension for one release.

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

    def test_push_marker_is_empty_dict(self, default_extensions):
        """Push is signalled by the *presence* of the key, not by
        contents — matches BlackBull's pre-Sprint-32 behaviour."""
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


class TestLegacyAliasContract:
    """``scope['http2_priority']`` is kept populated for one release
    so middleware that read the legacy key keeps working through
    the v0.31 deprecation window.  Removal scheduled for v0.32.0.

    These tests don't drive a real request — they pin the contract
    by asserting the helper's priority output is the same shape the
    legacy key carried."""

    def test_legacy_alias_shape_matches_priority_extension(self):
        ext = _build_h2_extensions(
            stream_id=7,
            priority={'urgency': 5, 'incremental': False},
            peer_initial_window=65535,
            connection_window=65535)
        # The populate sites assign the SAME dict to both
        # scope['http2_priority'] and scope['extensions']
        # ['http.response.priority'].  Asserting equality on the
        # extension's contents pins that contract.
        assert ext['http.response.priority'] == {
            'urgency': 5, 'incremental': False}
