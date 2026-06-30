"""Postgres + Redis backing for the HttpArena ``async-db`` and ``crud`` profiles.

The HttpArena harness starts a seeded ``postgres:18`` sidecar and a
``redis:7-alpine`` sidecar and hands the framework container their addresses
via environment variables (see ``launcher.py`` for the names we read).  This
module owns:

  * a lazily-created :mod:`asyncpg` pool, sized from ``DATABASE_MAX_CONN``
    (HttpArena's rule: size from the env var, **not** CPU count);
  * the four queries the two profiles need (price-range select, paginated
    list, get-by-id, upsert/update);
  * an optional Redis cache for ``GET /crud/items/{id}`` (invalidated on
    update), used only when ``REDIS_URL`` is set.

Everything degrades gracefully: with no database configured (or one that is
unreachable) the read paths return empty results rather than erroring, which
is exactly what the ``async-db`` contract requires
("Returns ``{"items":[],"count":0}`` ... when the database is unavailable").
The pool and Redis client are created inside the running event loop on first
use, so this is import-safe in ``launcher.py``'s pre-fork parent.
"""
from __future__ import annotations

import json
import os

try:
    import asyncpg  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - asyncpg is a bench-only dependency
    asyncpg = None  # type: ignore[assignment]

try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

# Connection strings supplied by the harness.  DATABASE_URL is the primary
# knob; if unset we fall back to libpq's standard PG* environment variables
# (PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE), which asyncpg also honours.
DATABASE_URL = os.environ.get('DATABASE_URL') or None
REDIS_URL = os.environ.get('REDIS_URL') or None
# HttpArena async-db rule: pool size comes from the env var, not nproc.
try:
    DATABASE_MAX_CONN = int(os.environ.get('DATABASE_MAX_CONN', '256'))
except ValueError:
    DATABASE_MAX_CONN = 256

_CACHE_TTL = 30  # seconds; get-by-id cache entries

# Lazily-initialised, per-process singletons.  ``_pool_ready`` distinguishes
# "not tried yet" from "tried and failed" so we don't reconnect on every call.
_pool = None
_pool_ready = False
_redis = None
_redis_ready = False


async def get_pool():
    """Return the asyncpg pool, or ``None`` if no DB is configured/reachable."""
    global _pool, _pool_ready
    if _pool_ready:
        return _pool
    _pool_ready = True
    if asyncpg is None or (DATABASE_URL is None and 'PGHOST' not in os.environ):
        _pool = None
        return None
    try:
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=DATABASE_MAX_CONN,
        )
    except Exception:  # noqa: BLE001 - any connect failure → DB-less mode
        _pool = None
    return _pool


async def _get_redis():
    """Return the Redis client, or ``None`` if caching is not configured."""
    global _redis, _redis_ready
    if _redis_ready:
        return _redis
    _redis_ready = True
    if aioredis is None or REDIS_URL is None:
        _redis = None
        return None
    try:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    except Exception:  # noqa: BLE001
        _redis = None
    return _redis


def _row_to_item(row) -> dict:
    """Map an ``items`` row to the wire shape: nest the rating columns and
    decode the ``tags`` column.

    HttpArena's schema stores ``tags`` as a ``TEXT`` column holding a
    JSON-encoded array (e.g. ``'["fast","new"]'``), but the wire contract — and
    the validator — require ``tags`` to be a JSON *array*
    (``isinstance(item['tags'], list)``).  So decode the string back to a list.
    """
    item = dict(row)
    score = item.pop('rating_score', None)
    count = item.pop('rating_count', None)
    item['rating'] = {'score': score, 'count': count}
    tags = item.get('tags')
    if isinstance(tags, str):
        try:
            item['tags'] = json.loads(tags)
        except (ValueError, TypeError):
            item['tags'] = []
    return item


# ---------------------------------------------------------------------------
# async-db profile
# ---------------------------------------------------------------------------

async def async_db(min_price: int, max_price: int, limit: int) -> list[dict]:
    """``SELECT ... FROM items WHERE price BETWEEN $1 AND $2 LIMIT $3``.

    Returns ``[]`` when no rows match or the database is unavailable.
    """
    pool = await get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT id, name, category, price, quantity, active, tags, '
                'rating_score, rating_count FROM items '
                'WHERE price BETWEEN $1 AND $2 LIMIT $3',
                min_price, max_price, limit)
    except Exception:  # noqa: BLE001 - contract: empty result, never an error
        return []
    return [_row_to_item(r) for r in rows]


# ---------------------------------------------------------------------------
# crud profile
# ---------------------------------------------------------------------------

async def crud_list(category: str | None, page: int, limit: int) -> list[dict]:
    """Paginated item list, optionally filtered by ``category``."""
    pool = await get_pool()
    if pool is None:
        return []
    offset = (max(page, 1) - 1) * limit
    try:
        async with pool.acquire() as conn:
            if category:
                rows = await conn.fetch(
                    'SELECT id, name, category, price, quantity, active, tags, '
                    'rating_score, rating_count FROM items WHERE category = $1 '
                    'ORDER BY id LIMIT $2 OFFSET $3', category, limit, offset)
            else:
                rows = await conn.fetch(
                    'SELECT id, name, category, price, quantity, active, tags, '
                    'rating_score, rating_count FROM items '
                    'ORDER BY id LIMIT $1 OFFSET $2', limit, offset)
    except Exception:  # noqa: BLE001
        return []
    return [_row_to_item(r) for r in rows]


async def crud_get(item_id: int) -> dict | None:
    """Get a single item by id, served from the Redis cache when available."""
    import json  # noqa: PLC0415 - local to keep the import graph minimal
    rds = await _get_redis()
    key = b'item:%d' % item_id
    if rds is not None:
        try:
            cached = await rds.get(key)
            if cached is not None:
                return json.loads(cached)
        except Exception:  # noqa: BLE001 - cache miss on any Redis error
            pass
    pool = await get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, name, category, price, quantity, active, tags, '
                'rating_score, rating_count FROM items WHERE id = $1', item_id)
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    item = _row_to_item(row)
    if rds is not None:
        try:
            await rds.set(key, json.dumps(item).encode(), ex=_CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
    return item


async def crud_create(data: dict) -> bool:
    """Insert (or upsert on id conflict) an item.  Returns success."""
    pool = await get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO items (id, name, category, price, quantity) '
                'VALUES ($1, $2, $3, $4, $5) '
                'ON CONFLICT (id) DO UPDATE SET '
                'name = EXCLUDED.name, category = EXCLUDED.category, '
                'price = EXCLUDED.price, quantity = EXCLUDED.quantity',
                int(data['id']), data['name'], data['category'],
                int(data['price']), int(data['quantity']))
    except Exception:  # noqa: BLE001
        return False
    await _invalidate(int(data['id']))
    return True


async def crud_update(item_id: int, data: dict) -> bool:
    """Update name/price/quantity for an item and invalidate its cache."""
    pool = await get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                'UPDATE items SET name = $2, price = $3, quantity = $4 '
                'WHERE id = $1', item_id, data['name'],
                int(data['price']), int(data['quantity']))
    except Exception:  # noqa: BLE001
        return False
    await _invalidate(item_id)
    # asyncpg returns e.g. "UPDATE 1"; treat a non-zero rowcount as success.
    return result.rsplit(' ', 1)[-1] != '0'


async def _invalidate(item_id: int) -> None:
    rds = await _get_redis()
    if rds is not None:
        try:
            await rds.delete(b'item:%d' % item_id)
        except Exception:  # noqa: BLE001
            pass
