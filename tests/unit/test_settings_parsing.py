"""One parsing rule for every environment setting.

A value that parses and is within range is used; anything else falls back to
the setting's default.  Booleans are false only for 0, false, no and off.
"""
import pytest

from blackbull.env import (DerivedCap, Environment, Settings, get_settings,
                           read_setting, reset_settings_cache)

DEFAULTS = Settings()


@pytest.mark.parametrize('env,raw,field,expected', [
    # int with minimum 1
    ('BB_WORKERS', '4', 'workers', 4),
    ('BB_WORKERS', '0', 'workers', DEFAULTS.workers),
    ('BB_WORKERS', 'many', 'workers', DEFAULTS.workers),
    # int with minimum 0
    ('BB_SOCKET_SNDBUF', '0', 'socket_sndbuf', 0),
    ('BB_SOCKET_SNDBUF', '-1', 'socket_sndbuf', DEFAULTS.socket_sndbuf),
    ('BB_SOCKET_SNDBUF', '1.5', 'socket_sndbuf', DEFAULTS.socket_sndbuf),
    # float with minimum 0
    ('BB_HEADER_TIMEOUT', '2.5', 'header_timeout', 2.5),
    ('BB_HEADER_TIMEOUT', '0', 'header_timeout', 0.0),
    ('BB_HEADER_TIMEOUT', '-1', 'header_timeout', DEFAULTS.header_timeout),
    ('BB_HEADER_TIMEOUT', 'soon', 'header_timeout', DEFAULTS.header_timeout),
    # bool: false only for the four spellings, case and padding ignored
    ('BB_ACCESS_LOG', ' OFF ', 'access_log', False),
    ('BB_ACCESS_LOG', 'no', 'access_log', False),
    ('BB_ACCESS_LOG', '0', 'access_log', False),
    ('BB_ACCESS_LOG', 'false', 'access_log', False),
    ('BB_ACCESS_LOG', 'yes', 'access_log', True),
    ('BB_ACCESS_LOG', 'anything', 'access_log', True),
    # str: taken as given
    ('BB_LOG_FORMAT', ' json ', 'log_format', ' json '),
    # custom parsers
    ('BLACKBULL_ENV', 'PRODUCTION', 'env', Environment.PRODUCTION),
    ('BLACKBULL_ENV', 'staging', 'env', Environment.DEVELOPMENT),
    ('BB_MAX_CONNECTIONS', '7', 'max_connections', 7),
    ('BB_MAX_CONNECTIONS', '0', 'max_connections', 0),
])
def test_a_setting_is_its_value_or_its_default(monkeypatch, env, raw, field, expected):
    monkeypatch.setenv(env, raw)
    reset_settings_cache()
    value = getattr(get_settings(), field)
    assert (value, type(value)) == (expected, type(expected))
    assert read_setting(field) == expected


def test_an_unset_setting_is_its_default(monkeypatch):
    monkeypatch.delenv('BB_HEADER_TIMEOUT', raising=False)
    reset_settings_cache()
    assert get_settings().header_timeout == DEFAULTS.header_timeout


@pytest.mark.parametrize('raw', [None, 'auto', 'AUTO', '-1', 'lots'])
def test_max_connections_derives_its_cap_when_not_a_count(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv('BB_MAX_CONNECTIONS', raising=False)
    else:
        monkeypatch.setenv('BB_MAX_CONNECTIONS', raw)
    reset_settings_cache()
    cap = get_settings().max_connections
    assert isinstance(cap, DerivedCap) or cap == 0     # 0 only on an unlimited host


def test_the_snapshot_is_cached_until_reset(monkeypatch):
    monkeypatch.setenv('BB_HEADER_TIMEOUT', '3')
    reset_settings_cache()
    first = get_settings()
    monkeypatch.setenv('BB_HEADER_TIMEOUT', '4')
    assert get_settings() is first
    assert read_setting('header_timeout') == 4.0       # read_setting never caches
    reset_settings_cache()
    assert get_settings().header_timeout == 4.0
