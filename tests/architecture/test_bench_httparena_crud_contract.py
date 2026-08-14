"""Architecture guard — the HttpArena ``crud`` profile's create path must be 2xx.

For eleven releases the ``crud`` profile reported ``Per-template-ok`` index 0
== ``0``: **every** ``POST /crud/items`` in every run of every archived
HttpArena comparison failed, and ``crud`` was the only profile in the
20-profile matrix with non-2xx responses at all.

The cause was in the bench app, not the framework.  HttpArena's ``items``
table declares all nine columns ``NOT NULL`` **without defaults**
(``scripts/generate-pgdb.py`` upstream), while the profile's create body
carries only five of them (``id``/``name``/``category``/``price``/
``quantity``).  ``db.crud_create``'s ``INSERT`` named just those five, so
Postgres raised a not-null violation on ``active``, asyncpg propagated it,
the blanket ``except`` turned it into ``return False``, and the handler
answered ``503``.  The published throughput was never inflated — HttpArena
counts 2xx only — but a fifth of the profile's write mix was measuring an
error path.

These tests drive the real ``bench/httparena/db.py`` against a fake table
that enforces the two constraints that actually bit: the ``NOT NULL``
column set, and the primary key (gcannon's ``{SEQ:100001}`` counter resets
per invocation, so runs 2 and 3 replay run 1's ids and only survive because
the statement is an upsert).
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

BENCH_HTTPARENA = pathlib.Path(__file__).resolve().parents[2] / 'bench' / 'httparena'

# HttpArena's items schema — every column is NOT NULL and none declares a
# DEFAULT, so an INSERT must name all nine or Postgres rejects the row.
# Source: HttpArena ``scripts/generate-pgdb.py`` (``CREATE TABLE items``).
_NOT_NULL_COLUMNS = frozenset({
    'id', 'name', 'category', 'price', 'quantity',
    'active', 'tags', 'rating_score', 'rating_count',
})

# The body gcannon's crud-create template posts, after {SEQ} substitution.
_CREATE_BODY = {'id': 100001, 'name': 'New Product', 'category': 'test',
                'price': 150, 'quantity': 30}

_INSERT_COLUMNS = re.compile(r'INSERT\s+INTO\s+items\s*\(([^)]*)\)', re.IGNORECASE)


class _NotNullViolation(Exception):
    """Stands in for ``asyncpg.exceptions.NotNullViolationError``."""


class _UniqueViolation(Exception):
    """Stands in for ``asyncpg.exceptions.UniqueViolationError``."""


class _FakeItemsTable:
    """Enough of the seeded ``items`` table to reject the two real failures.

    Records the rejection before raising it, so a failing assertion can name
    the column that was left out instead of just reporting ``False``.
    """

    def __init__(self, seeded: tuple[int, ...] = (1, 2, 3)) -> None:
        self.ids = set(seeded)
        self.rejected: list[Exception] = []

    async def execute(self, sql: str, *args):
        match = _INSERT_COLUMNS.search(sql)
        if match is None:                       # the UPDATE path
            return 'UPDATE 1' if args[0] in self.ids else 'UPDATE 0'

        named = {c.strip() for c in match.group(1).split(',')}
        missing = _NOT_NULL_COLUMNS - named
        if missing:
            return self._reject(_NotNullViolation(
                'null value in NOT NULL column(s): ' + ', '.join(sorted(missing))))

        item_id = args[0]
        if item_id in self.ids and 'ON CONFLICT' not in sql.upper():
            return self._reject(_UniqueViolation(
                f'duplicate key value violates unique constraint items_pkey (id)={item_id}'))

        self.ids.add(item_id)
        return 'INSERT 0 1'

    def _reject(self, error: Exception):
        self.rejected.append(error)
        raise error


@pytest.fixture
def bench_db():
    """Import ``bench/httparena/db.py`` with its Redis client short-circuited."""
    spec = importlib.util.spec_from_file_location(
        '_bench_httparena_db', BENCH_HTTPARENA / 'db.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        # ``crud_create`` invalidates the cache on success; pin the lazy Redis
        # singleton to "tried, unavailable" so the test never opens a socket.
        module._redis_ready = True
        module._redis = None
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.asyncio
async def test_crud_create_supplies_every_not_null_column(bench_db):
    table = _FakeItemsTable()

    created = await bench_db.crud_create(table, dict(_CREATE_BODY))

    assert not table.rejected, f'the create INSERT was rejected: {table.rejected[0]}'
    assert created is True


@pytest.mark.asyncio
async def test_crud_create_upserts_when_the_id_sequence_replays(bench_db):
    # gcannon restarts {SEQ:100001} on every invocation, so run 2 posts the
    # ids run 1 created.  A plain INSERT would 5xx for the whole of runs 2+.
    table = _FakeItemsTable()

    first = await bench_db.crud_create(table, dict(_CREATE_BODY))
    second = await bench_db.crud_create(table, dict(_CREATE_BODY))

    assert not table.rejected, f'the replayed create was rejected: {table.rejected[0]}'
    assert (first, second) == (True, True)
