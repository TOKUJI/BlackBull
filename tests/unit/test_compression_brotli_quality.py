"""Brotli quality is configurable; default targets dynamic content (q=4).

The brotli library's own default is q=11 (max compression, ~5-15 ms of CPU
per call on small payloads).  That's appropriate for build-time / static
pre-compression, but for dynamic response bodies it saturates the event
loop.  BlackBull's default is 4 — matches Google/Cloudflare's
dynamic-content recommendation.  This file pins:

  - the default value reaching the bound brotli callable,
  - the constructor kwarg actually changing the bound quality,
  - the env var → Settings → Compression() path round-tripping.
"""
from __future__ import annotations

import pytest

brotli = pytest.importorskip('brotli')

from blackbull.middleware.compression import Compression, _detect_codecs


# A payload big enough that brotli q=1 vs q=11 produce visibly different
# output sizes — keeps the assertion robust to small per-version changes.
_SAMPLE = (b'BlackBull ASGI server - brotli quality test payload. ' * 200)


def _quality_of(compressed: bytes) -> int:
    """Stand-in for "what quality was used": q=11 compresses _SAMPLE
    strictly smaller than q=1 by a wide margin.  We don't need the exact
    level — just that the output matches the level we asked for."""
    return len(compressed)


def test_default_brotli_quality_is_4():
    """The module-level default is 4 (Cloudflare / Google recommendation
    for dynamic content).  Anyone bumping this should also update
    docs/reference/env-vars.md."""
    from blackbull.middleware.compression import _BROTLI_QUALITY
    assert _BROTLI_QUALITY == 4


def test_constructor_propagates_brotli_quality_to_bound_callable():
    """The quality kwarg passed to Compression() must reach the bound
    brotli compress callable."""
    mw_q1 = Compression(brotli_quality=1)
    mw_q11 = Compression(brotli_quality=11)

    out_q1 = mw_q1._available['br'](_SAMPLE)
    out_q11 = mw_q11._available['br'](_SAMPLE)

    # q=11 squeezes harder than q=1.  If both produced the same bytes,
    # functools.partial(quality=...) wasn't bound and both calls fell
    # through to brotli's library default.
    assert _quality_of(out_q11) < _quality_of(out_q1)
    # Sanity: each codec round-trips correctly regardless of quality.
    assert brotli.decompress(out_q1) == _SAMPLE
    assert brotli.decompress(out_q11) == _SAMPLE


def test_detect_codecs_default_matches_module_default():
    """_detect_codecs() with no kwarg should produce a brotli callable
    bound at the module's default quality (4)."""
    from blackbull.middleware.compression import _BROTLI_QUALITY

    default_codecs = _detect_codecs()
    explicit_codecs = _detect_codecs(brotli_quality=_BROTLI_QUALITY)
    # Bound callables compare equal under functools.partial when bound
    # to the same func + same kwargs.
    assert default_codecs['br'](_SAMPLE) == explicit_codecs['br'](_SAMPLE)


def test_env_var_overrides_settings_brotli_quality(monkeypatch):
    """BB_BROTLI_QUALITY is read by get_settings() and surfaces on the
    Settings dataclass.  reset_settings_cache so this test sees the
    override rather than a cached pre-existing Settings."""
    from blackbull import env as _env

    monkeypatch.setenv('BB_BROTLI_QUALITY', '6')
    _env.reset_settings_cache()
    try:
        cfg = _env.get_settings()
        assert cfg.brotli_quality == 6
    finally:
        _env.reset_settings_cache()


def test_settings_default_brotli_quality_is_4(monkeypatch):
    """With no env override, Settings.brotli_quality is 4 — same as the
    module-level default."""
    from blackbull import env as _env

    monkeypatch.delenv('BB_BROTLI_QUALITY', raising=False)
    _env.reset_settings_cache()
    try:
        cfg = _env.get_settings()
        assert cfg.brotli_quality == 4
    finally:
        _env.reset_settings_cache()
