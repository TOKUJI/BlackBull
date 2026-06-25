"""Frame-assembly-fast-path Tier 1 — hot-path logging is lazy / guarded.

These guard the change that removed eager per-frame logging on the HTTP/2
frame hot path (frame.py, frame_types.py, stream.py):

* the per-`create()` INFO trace and the bare-int PRIORITY INFO were deleted;
* the per-frame DEBUG traces now use lazy ``%``-args or an ``isEnabledFor``
  guard, so nothing is formatted/concatenated when the level is off;
* DEBUG output is otherwise unchanged when DEBUG *is* enabled.
"""
import logging
import re
from pathlib import Path

import pytest

from blackbull.protocol.frame import FrameFactory


_HOT_FILES = [
    'blackbull/protocol/frame.py',
    'blackbull/protocol/frame_types.py',
    'blackbull/protocol/stream.py',
]

_EAGER_FSTRING = re.compile(r"logger\.(info|debug)\(\s*f['\"]")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('rel', _HOT_FILES)
def test_no_eager_fstring_logging_on_frame_hot_path(rel):
    """No ``logger.info(f'…')`` / ``logger.debug(f'…')`` on the frame hot path.

    Eager f-strings format unconditionally, even when the level is disabled.
    Hot-path logging must use lazy ``%``-args (deferred formatting) or sit
    behind an ``isEnabledFor`` guard.  This is the 'did we miss a site' guard
    the proposal asks for.
    """
    text = (_repo_root() / rel).read_text()
    offenders = [
        line for line in text.splitlines()
        if _EAGER_FSTRING.search(line)
    ]
    assert not offenders, (
        f'{rel} has eager f-string logging on the frame hot path:\n'
        + '\n'.join(offenders)
    )


def test_frame_create_emits_no_per_create_info(caplog):
    """The deleted per-``create()`` INFO trace must not reappear — it fired on
    every outbound frame and every inbound load()."""
    caplog.set_level(logging.INFO, logger='blackbull.protocol.frame')
    FrameFactory().window_update(stream_id=1, increment=10)
    frame_records = [r for r in caplog.records
                     if r.name == 'blackbull.protocol.frame']
    assert frame_records == [], (
        'frame.FrameFactory.create() must not log per-create at INFO; '
        f'got {[r.getMessage() for r in frame_records]}')


def test_frame_debug_still_emitted_when_enabled(caplog):
    """The guards must not suppress DEBUG output when DEBUG *is* enabled —
    FrameBase.save() still logs the saved frame at DEBUG."""
    caplog.set_level(logging.DEBUG, logger='blackbull.protocol.frame_types')
    wire = FrameFactory().window_update(stream_id=3, increment=35).save()
    assert wire  # sanity: save produced bytes
    saved = [r for r in caplog.records
             if r.name == 'blackbull.protocol.frame_types'
             and 'saving a frame' in r.getMessage()]
    assert saved, 'FrameBase.save() must still emit its DEBUG trace when DEBUG is on'


def test_frame_init_debug_uses_lazy_args(caplog):
    """FrameBase.__init__ logs its fields at DEBUG via lazy %-args (so the
    record renders correctly) when DEBUG is enabled."""
    caplog.set_level(logging.DEBUG, logger='blackbull.protocol.frame_types')
    FrameFactory().window_update(stream_id=7, increment=1)
    init_records = [r for r in caplog.records
                    if r.name == 'blackbull.protocol.frame_types'
                    and 'stream_id=7' in r.getMessage()]
    assert init_records, 'FrameBase.__init__ DEBUG trace should render stream_id=7'
