from http import HTTPStatus

from blackbull.utils import Scheme, pop_safe, is_client_error, is_server_error


# ---------------------------------------------------------------------------
# Scheme
# ---------------------------------------------------------------------------

def test_scheme_values():
    assert Scheme.http == 'http'
    assert Scheme.websocket == 'websocket'


# ---------------------------------------------------------------------------
# pop_safe
# ---------------------------------------------------------------------------

def test_pop_safe_moves_key():
    src = {'a': 1, 'b': 2}
    dst = {}
    pop_safe('a', src, dst)
    assert dst == {'a': 1}
    assert 'a' not in src


def test_pop_safe_with_new_key():
    src = {'old': 42}
    dst = {}
    pop_safe('old', src, dst, new_key='new')
    assert dst == {'new': 42}
    assert 'old' not in src


def test_pop_safe_missing_key_is_noop():
    src = {'x': 1}
    dst = {}
    pop_safe('missing', src, dst)
    assert dst == {}
    assert src == {'x': 1}


# ---------------------------------------------------------------------------
# is_client_error / is_server_error — 3.11-safe HTTP status classifiers
# (regression guard for the requires-python >=3.11 mismatch: HTTPStatus's
# own .is_client_error / .is_server_error are 3.12-only and crash on 3.11)
# ---------------------------------------------------------------------------

def test_is_client_error_true_for_4xx():
    for s in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND,
              HTTPStatus.IM_A_TEAPOT, HTTPStatus.UNAVAILABLE_FOR_LEGAL_REASONS):
        assert is_client_error(s)
        assert not is_server_error(s)


def test_is_server_error_true_for_5xx():
    for s in (HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY,
              HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.HTTP_VERSION_NOT_SUPPORTED):
        assert is_server_error(s)
        assert not is_client_error(s)


def test_non_error_statuses_are_neither():
    for s in (HTTPStatus.CONTINUE, HTTPStatus.OK, HTTPStatus.CREATED,
              HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.NOT_MODIFIED):
        assert not is_client_error(s)
        assert not is_server_error(s)


def test_classifiers_accept_plain_int():
    # HTTPStatus is an IntEnum, but the helpers must also accept raw ints
    # (custom/unregistered status codes on the error paths).
    assert is_client_error(418) and not is_server_error(418)
    assert is_server_error(503) and not is_client_error(503)
    assert not is_client_error(200) and not is_server_error(200)


def test_classifiers_match_312_semantics_on_boundaries():
    assert is_client_error(400) and is_client_error(499)
    assert not is_client_error(399) and not is_client_error(500)
    assert is_server_error(500) and is_server_error(599)
    assert not is_server_error(499) and not is_server_error(600)
