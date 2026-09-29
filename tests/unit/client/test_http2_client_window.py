"""Client-side H2 window seeding.

RFC 9113 §6.9.2: SETTINGS_INITIAL_WINDOW_SIZE governs the *initial* send
window of streams the peer has not yet opened; existing streams are
adjusted by the delta.  ``HTTP2Client`` propagated the delta to existing
senders but seeded new senders at the RFC default, so a sender created
*after* the SETTINGS exchange under- or over-estimated its send credit
(stall, or overrun with a lenient peer).  Both sides now seed at
construction through the shared ``HTTP2Sender(initial_window=...)``
parameter (2.11's shared seeding helper).
"""
from __future__ import annotations

from blackbull.client.http2 import HTTP2Client
from blackbull.server.sender import AbstractWriter
from blackbull.protocol.frame_types import DEFAULT_INITIAL_WINDOW_SIZE
import pytest


class _NullWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


def _client() -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._writer = _NullWriter()
    return c


class TestClientWindowSeeding:
    @pytest.mark.parametrize('window,stream_id', [
        pytest.param(123456, 1, id='sender-after-settings-seeded'),
        pytest.param(1024, 3, id='shrunk-window-seeds-low'),
    ])
    def test_sender_created_after_settings_is_seeded(self, window, stream_id):
        """A sender made after the SETTINGS exchange must start at the
        peer's announced initial window, not the RFC default — including a
        window below the default (e.g. 1024), or late senders overrun."""
        c = _client()
        c._on_initial_window_size(window)
        sender = c._make_sender(stream_id)
        assert sender.stream_window_size == window

    def test_sender_created_before_settings_gets_delta(self):
        """Existing behaviour pin: pre-SETTINGS senders are delta-adjusted."""
        c = _client()
        sender = c._make_sender(1)
        assert sender.stream_window_size == DEFAULT_INITIAL_WINDOW_SIZE
        c._on_initial_window_size(70000)
        assert sender.stream_window_size == (
            DEFAULT_INITIAL_WINDOW_SIZE + (70000 - DEFAULT_INITIAL_WINDOW_SIZE))

    def test_default_seed_matches_rfc_default(self):
        c = _client()
        sender = c._make_sender(1)
        assert sender.stream_window_size == DEFAULT_INITIAL_WINDOW_SIZE
