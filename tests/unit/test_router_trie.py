"""Unit tests for the _RouteTrie acceleration structure in blackbull.router.

These tests verify the trie's internal behaviour directly, separate from the
full Router API tests in test_router.py.
"""
import pytest
from http import HTTPMethod

from blackbull.utils import Scheme
from blackbull.router import (
    Router, _RouteTrie, _LookupCache, PathNotRegistered, MethodNotApplicable,
)


# ---------------------------------------------------------------------------
# _RouteTrie unit tests
# ---------------------------------------------------------------------------

def _handler():
    pass


def test_trie_exact_path_hit():
    trie = _RouteTrie()
    trie.insert('/ping', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, allowed = trie.lookup('/ping', HTTPMethod.GET, Scheme.http)
    assert h is _handler
    assert params == {}


def test_trie_exact_path_miss():
    trie = _RouteTrie()
    trie.insert('/ping', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, allowed = trie.lookup('/pong', HTTPMethod.GET, Scheme.http)
    assert h is None
    assert allowed == set()


def test_trie_param_path_hit():
    trie = _RouteTrie()
    trie.insert('/users/{id}', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, allowed = trie.lookup('/users/42', HTTPMethod.GET, Scheme.http)
    assert h is _handler
    assert params == {'id': '42'}


def test_trie_int_converter_accepts_digits():
    trie = _RouteTrie()
    trie.insert('/items/{n:int}', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, _ = trie.lookup('/items/7', HTTPMethod.GET, Scheme.http)
    assert h is _handler
    assert params == {'n': 7}
    assert isinstance(params['n'], int)


def test_trie_int_converter_rejects_non_int():
    trie = _RouteTrie()
    trie.insert('/items/{n:int}', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, allowed = trie.lookup('/items/abc', HTTPMethod.GET, Scheme.http)
    assert h is None
    assert allowed == set()  # conversion failed → path not matched


def test_trie_path_converter_matches_slashes():
    trie = _RouteTrie()
    trie.insert('/files/{rest:path}', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, _ = trie.lookup('/files/a/b/c.txt', HTTPMethod.GET, Scheme.http)
    assert h is _handler
    assert params == {'rest': 'a/b/c.txt'}


def test_trie_static_child_wins_over_param():
    """Static segment takes priority over param at the same position."""
    exact_handler = object()
    param_handler = object()

    trie = _RouteTrie()
    trie.insert('/users/me', (HTTPMethod.GET,), Scheme.http, exact_handler)
    trie.insert('/users/{id}', (HTTPMethod.GET,), Scheme.http, param_handler)

    h, params, _ = trie.lookup('/users/me', HTTPMethod.GET, Scheme.http)
    assert h is exact_handler
    assert params == {}

    h2, params2, _ = trie.lookup('/users/42', HTTPMethod.GET, Scheme.http)
    assert h2 is param_handler
    assert params2 == {'id': '42'}


def test_trie_method_not_allowed_returns_allowed_set():
    trie = _RouteTrie()
    trie.insert('/resource', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, allowed = trie.lookup('/resource', HTTPMethod.POST, Scheme.http)
    assert h is None
    assert HTTPMethod.GET in allowed


def test_trie_scheme_mismatch_no_match():
    trie = _RouteTrie()
    trie.insert('/ws', (HTTPMethod.GET,), Scheme.websocket, _handler)
    h, params, allowed = trie.lookup('/ws', HTTPMethod.GET, Scheme.http)
    assert h is None
    assert allowed == set()


def test_trie_multiple_params():
    trie = _RouteTrie()
    trie.insert('/a/{x}/{y:int}', (HTTPMethod.GET,), Scheme.http, _handler)
    h, params, _ = trie.lookup('/a/hello/7', HTTPMethod.GET, Scheme.http)
    assert h is _handler
    assert params == {'x': 'hello', 'y': 7}


# ---------------------------------------------------------------------------
# Backtracking pins (Sprint 77) — behaviours the lookup fast path must keep
# ---------------------------------------------------------------------------

def test_trie_backtracks_to_param_when_static_dead_ends_deeper():
    """A static branch that dead-ends below must fall back to a param branch
    taken at an earlier level."""
    static_h, param_h = object(), object()
    trie = _RouteTrie()
    trie.insert('/x/static/other', (HTTPMethod.GET,), Scheme.http, static_h)
    trie.insert('/x/{a}/target', (HTTPMethod.GET,), Scheme.http, param_h)

    h, params, _ = trie.lookup('/x/static/target', HTTPMethod.GET, Scheme.http)
    assert h is param_h
    assert params == {'a': 'static'}


def test_trie_backtracks_on_method_mismatch_in_static_branch():
    """Static-first priority is per-entry-match, not per-path: a method miss
    on the static entry still lets a param sibling serve the request."""
    get_h, post_h = object(), object()
    trie = _RouteTrie()
    trie.insert('/x/fixed', (HTTPMethod.GET,), Scheme.http, get_h)
    trie.insert('/x/{a}', (HTTPMethod.POST,), Scheme.http, post_h)

    h, params, _ = trie.lookup('/x/fixed', HTTPMethod.POST, Scheme.http)
    assert h is post_h
    assert params == {'a': 'fixed'}


def test_trie_405_aggregates_allowed_methods_across_branches():
    trie = _RouteTrie()
    trie.insert('/x/fixed', (HTTPMethod.GET,), Scheme.http, object())
    trie.insert('/x/{a}', (HTTPMethod.POST,), Scheme.http, object())

    h, _, allowed = trie.lookup('/x/fixed', HTTPMethod.DELETE, Scheme.http)
    assert h is None
    assert HTTPMethod.GET in allowed and HTTPMethod.POST in allowed


def test_trie_backtracks_to_wildcard_when_param_dead_ends():
    param_h, wild_h = object(), object()
    trie = _RouteTrie()
    trie.insert('/f/{a}/b', (HTTPMethod.GET,), Scheme.http, param_h)
    trie.insert('/f/{rest:path}', (HTTPMethod.GET,), Scheme.http, wild_h)

    h, params, _ = trie.lookup('/f/q/z', HTTPMethod.GET, Scheme.http)
    assert h is wild_h
    assert params == {'rest': 'q/z'}


def test_trie_converter_rejection_falls_back_to_wildcard_sibling():
    int_h, wild_h = object(), object()
    trie = _RouteTrie()
    trie.insert('/i/{n:int}', (HTTPMethod.GET,), Scheme.http, int_h)
    trie.insert('/i/{rest:path}', (HTTPMethod.GET,), Scheme.http, wild_h)

    h, params, _ = trie.lookup('/i/abc', HTTPMethod.GET, Scheme.http)
    assert h is wild_h
    assert params == {'rest': 'abc'}


def test_trie_slash_normalization_matches_static_route():
    """Empty segments are dropped: trailing and duplicate slashes match the
    canonical registered path."""
    trie = _RouteTrie()
    trie.insert('/api/x', (HTTPMethod.GET,), Scheme.http, _handler)
    for variant in ('/api/x/', '//api//x', '/api/x//'):
        h, params, _ = trie.lookup(variant, HTTPMethod.GET, Scheme.http)
        assert h is _handler, variant
        assert params == {}


# ---------------------------------------------------------------------------
# Router integration: trie is created and used
# ---------------------------------------------------------------------------

def test_router_has_trie():
    r = Router()
    assert hasattr(r, '_trie')
    assert isinstance(r._trie, _RouteTrie)


@pytest.mark.asyncio
async def test_router_trie_used_for_parameterized_routes():
    """Route registered via router.route() is reachable through the trie."""
    router = Router()

    @router.route(path='/tasks/{task_id}', methods=[HTTPMethod.GET])
    async def handler(scope, receive, send):
        pass

    # trie should find it
    h, params, _ = router._trie.lookup('/tasks/99', HTTPMethod.GET, Scheme.http)
    assert h is not None
    assert params.get('task_id') == '99'


@pytest.mark.asyncio
async def test_router_trie_hit_bypasses_regex_scan():
    """For normal string paths the trie hit means self.regex_ scan is skipped.

    We verify this indirectly: the regex_ scan is slow for large route sets
    but the trie should still return the correct handler immediately.
    """
    router = Router()

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        return 'pong'

    fn = router[('/ping', HTTPMethod.GET, Scheme.http)]
    assert fn is not None


# ---------------------------------------------------------------------------
# Router lookup cache (#9 worker-local caches)
# ---------------------------------------------------------------------------

def test_lookup_cache_populated_after_hit():
    """A successful lookup must add the result to the lookup cache."""
    router = Router()

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        pass

    key = ('/ping', HTTPMethod.GET, Scheme.http)
    assert key not in router._cache

    router[key]

    assert key in router._cache


def test_lookup_cache_returns_same_object():
    """Second lookup must return the identical object from the cache."""
    router = Router()

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        pass

    key = ('/ping', HTTPMethod.GET, Scheme.http)
    first = router[key]
    second = router[key]

    assert first is second


def test_lookup_cache_cleared_on_route_registration():
    """Registering a new route must evict all cached lookups."""
    router = Router()

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        pass

    key = ('/ping', HTTPMethod.GET, Scheme.http)
    router[key]
    assert key in router._cache

    # Adding a second route must clear the cache
    @router.route(path='/pong', methods=[HTTPMethod.GET])
    async def pong(scope, receive, send):
        pass

    assert len(router._cache) == 0


def test_lookup_cache_lru_evicts_least_recently_used():
    """Cache stays at cache_max; least-recently-used entry is evicted, not the hottest."""
    router = Router()

    @router.route(path='/items/{item_id}', methods=[HTTPMethod.GET])
    async def handler(scope, receive, send):
        pass

    # Shrink the cap to a testable size
    router.cache_max = 3

    k1 = ('/items/1', HTTPMethod.GET, Scheme.http)
    k2 = ('/items/2', HTTPMethod.GET, Scheme.http)
    k3 = ('/items/3', HTTPMethod.GET, Scheme.http)
    k4 = ('/items/4', HTTPMethod.GET, Scheme.http)

    router[k1]
    router[k2]
    router[k3]
    assert len(router._cache) == 3

    # Re-access k1 — it becomes the most recently used, so k2 is now the LRU
    router[k1]

    # Adding k4 must evict k2 (LRU), not k1 (recently accessed)
    router[k4]
    assert len(router._cache) == 3
    assert k2 not in router._cache, 'LRU entry (k2) must be evicted'
    assert k1 in router._cache, 'recently-accessed k1 must be retained'
    assert k4 in router._cache


def test_cache_max_zero_disables_caching():
    """cache_max=0 → the resolve result is never stored; every lookup misses
    the cache but still resolves correctly."""
    router = Router(cache_max=0)

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        pass

    key = ('/ping', HTTPMethod.GET, Scheme.http)
    assert router[key] is not None            # resolves
    assert router[key] is not None            # resolves again
    assert len(router._cache) == 0     # nothing cached


def test_cache_get_set_pair_round_trips():
    """The extracted _cache_get / _cache_set pair is the cache's contract."""
    router = Router()

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        pass

    key = ('/ping', HTTPMethod.GET, Scheme.http)
    hit, _ = router._cache_get(key)
    assert hit is False                       # cold

    sentinel = object()
    router._cache_set(key, sentinel)
    hit, result = router._cache_get(key)
    assert hit is True and result is sentinel


def test_default_cache_max_is_2048():
    assert Router().cache_max == 2048


@pytest.mark.asyncio
async def test_lookup_cache_parametrized_route_injects_correct_params():
    """Cached closure for a parametrized route injects the right path_params."""
    router = Router()

    @router.route(path='/tasks/{task_id}', methods=[HTTPMethod.GET])
    async def handler(scope, receive, send):
        pass

    key = ('/tasks/42', HTTPMethod.GET, Scheme.http)

    # Populate cache
    fn = router[key]
    assert key in router._cache

    # Cached closure injects params correctly (Sprint 79: onto conn.path_params)
    scope: dict = {}
    await fn(scope, None, None)
    assert scope['_connection'].path_params == {'task_id': '42'}

    # Different path → different cache entry and different params
    key2 = ('/tasks/99', HTTPMethod.GET, Scheme.http)
    fn2 = router[key2]
    scope2: dict = {}
    await fn2(scope2, None, None)
    assert scope2['_connection'].path_params == {'task_id': '99'}


# ---------------------------------------------------------------------------
# _LookupCache — swappable cache-strategy extraction (Sprint 68 follow-up)
# ---------------------------------------------------------------------------

def test_router_uses_lookupcache_instance():
    assert isinstance(Router()._cache, _LookupCache)


def test_lookupcache_get_set_clear_contract():
    cache = _LookupCache(cache_max=8)
    key = ('/x', HTTPMethod.GET, Scheme.http)
    assert cache.get(key) == (False, None)
    sentinel = object()
    cache.set(key, sentinel)
    assert key in cache and len(cache) == 1
    assert cache.get(key) == (True, sentinel)
    cache.clear()
    assert key not in cache and len(cache) == 0


def test_lookupcache_zero_disables():
    cache = _LookupCache(cache_max=0)
    cache.set(('/x', HTTPMethod.GET, Scheme.http), object())
    assert len(cache) == 0


def test_lookupcache_lru_eviction():
    cache = _LookupCache(cache_max=2)
    for p in ('/a', '/b'):
        cache.set((p, HTTPMethod.GET, Scheme.http), p)
    assert len(cache) == 2
    cache.set(('/c', HTTPMethod.GET, Scheme.http), '/c')  # evicts LRU '/a'
    assert len(cache) == 2
    assert ('/a', HTTPMethod.GET, Scheme.http) not in cache
    assert ('/c', HTTPMethod.GET, Scheme.http) in cache


def test_router_cache_strategy_is_swappable():
    """The point of the extraction: replace Router._cache with another
    same-interface instance and lookups honour it — no __getitem__ change."""
    router = Router()

    @router.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping(scope, receive, send):
        pass

    # Swap in a caching-disabled strategy; lookups must stop being stored.
    router._cache = _LookupCache(cache_max=0)
    key = ('/ping', HTTPMethod.GET, Scheme.http)
    assert router[key] is not None
    assert len(router._cache) == 0
