from blackbull.utils import Scheme, pop_safe


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
