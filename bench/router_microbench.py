"""Router `_resolve` microbenchmark — Sprint 77 (router-cache Phase 2/3).

Measures the per-call cost of ``Router._resolve`` (cache bypassed) and
``Router.__getitem__`` (cache path) across the regimes the proposal names:

- fixed-route hit          (trie walk, no params)
- param-route hit          (trie walk + converter + injector)
- deep param-route hit     (5 segments)
- miss → PathNotRegistered (trie miss + regex-fallback scan)
- miss → MethodNotApplicable

Run:  python bench/router_microbench.py [--repeat 7] [--number 20000]

Numbers print as ns/call (min of repeats — standard timeit discipline).
Results for sprint records go under bench/results/router-microbench/.
"""
import argparse
import timeit
from http import HTTPMethod

from blackbull.router import (
    MethodNotApplicable, PathNotRegistered, Router,
)
from blackbull.utils import Scheme


async def _h(scope, receive, send):
    return None


def build_router(n_fixed: int = 30, n_param: int = 10) -> Router:
    """A route table shaped like a realistic small API (like Sprint 68 W2)."""
    r = Router()
    for i in range(n_fixed):
        r[(f'/api/resource{i}/list', HTTPMethod.GET)] = _h
    for i in range(n_param):
        r[(f'/api/resource{i}/{{item_id:int}}', HTTPMethod.GET)] = _h
    r[('/api/deep/{a}/{b}/{c}/{d:int}/{e}', HTTPMethod.GET)] = _h
    r[('/', HTTPMethod.GET)] = _h
    return r


CASES = {
    'fixed_hit':        ('/api/resource7/list', HTTPMethod.GET),
    'param_hit':        ('/api/resource7/12345', HTTPMethod.GET),
    'deep_param_hit':   ('/api/deep/a/b/c/42/e', HTTPMethod.GET),
    'miss_404':         ('/nope/not/here', HTTPMethod.GET),
    'miss_405':         ('/api/resource7/list', HTTPMethod.POST),
}


def bench(fn, number: int, repeat: int) -> float:
    best = min(timeit.repeat(fn, number=number, repeat=repeat))
    return best / number * 1e9


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--number', type=int, default=20000)
    ap.add_argument('--repeat', type=int, default=7)
    args = ap.parse_args()

    r = build_router()
    r.validate()
    scheme = Scheme.http

    print(f'{"case":<18} {"_resolve ns":>12} {"__getitem__ ns":>15}')
    for name, (path, method) in CASES.items():
        key = (path, method, scheme)
        if name.startswith('miss'):
            exc = (PathNotRegistered, MethodNotApplicable)

            def resolve_call(key=key, exc=exc):
                try:
                    r._resolve(key)
                except exc:
                    pass

            def getitem_call(key=key, exc=exc):
                try:
                    r[key]
                except exc:
                    pass
        else:
            def resolve_call(key=key):
                r._resolve(key)

            def getitem_call(key=key):
                r[key]

        resolve_ns = bench(resolve_call, args.number, args.repeat)
        # Prime the cache, then measure the cached path.
        getitem_call()
        getitem_ns = bench(getitem_call, args.number, args.repeat)
        print(f'{name:<18} {resolve_ns:>12.0f} {getitem_ns:>15.0f}')


if __name__ == '__main__':
    main()
