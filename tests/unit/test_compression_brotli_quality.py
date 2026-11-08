"""Brotli uses the declared default or the selected quality."""
from __future__ import annotations

import pytest

brotli = pytest.importorskip('brotli')

from blackbull.middleware.compression import Compression, _detect_codecs


_SAMPLE = (b'BlackBull ASGI server - brotli quality test payload. ' * 200)


def test_default_brotli_quality_is_4():
    from blackbull.env import Settings
    assert Settings.brotli_quality == 4


def test_constructor_propagates_brotli_quality_to_bound_callable():
    mw_q1 = Compression(brotli_quality=1)
    mw_q11 = Compression(brotli_quality=11)

    out_q1 = mw_q1._available['br'](_SAMPLE)
    out_q11 = mw_q11._available['br'](_SAMPLE)

    assert len(out_q11) < len(out_q1)
    assert brotli.decompress(out_q1) == _SAMPLE
    assert brotli.decompress(out_q11) == _SAMPLE


def test_detect_codecs_default_matches_module_default():
    from blackbull.env import Settings

    default_codecs = _detect_codecs()
    explicit_codecs = _detect_codecs(brotli_quality=Settings.brotli_quality)
    assert default_codecs['br'](_SAMPLE) == explicit_codecs['br'](_SAMPLE)


def test_env_var_overrides_settings_brotli_quality(monkeypatch):
    from blackbull import env as _env

    monkeypatch.setenv('BB_BROTLI_QUALITY', '6')
    _env.reset_settings_cache()
    try:
        cfg = _env.get_settings()
        assert cfg.brotli_quality == 6
    finally:
        _env.reset_settings_cache()


def test_settings_default_brotli_quality_is_4(monkeypatch):
    from blackbull import env as _env

    monkeypatch.delenv('BB_BROTLI_QUALITY', raising=False)
    _env.reset_settings_cache()
    try:
        cfg = _env.get_settings()
        assert cfg.brotli_quality == 4
    finally:
        _env.reset_settings_cache()
