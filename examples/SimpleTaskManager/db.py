"""
SQLite data layer for SimpleTaskManager.

All blocking sqlite3 calls are run in a thread via run_in_executor so they
do not block the asyncio event loop.  Compatible with Python 3.8+.
"""
import asyncio
import hashlib
import secrets
import sqlite3
from functools import partial
from pathlib import Path

DB_PATH = Path(__file__).parent / 'tasks.db'

DEFAULT_USER = 'admin'
DEFAULT_PASS = 'admin'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args))


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def _row_to_task(row) -> dict:
    return {
        'id': row[0],
        'title': row[1],
        'completed': bool(row[2]),
        'createdAt': row[3],
    }


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def _init_db_sync():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                salt          TEXT    NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                completed  INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
                           DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        ''')
        # Seed the default user if it doesn't exist yet.
        salt = secrets.token_hex(16)
        password_hash = _hash_password(DEFAULT_PASS, salt)
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)',
                (DEFAULT_USER, password_hash, salt),
            )
        except sqlite3.IntegrityError:
            pass  # already seeded
        conn.commit()


async def init_db() -> None:
    await _run(_init_db_sync)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _verify_user_sync(username: str, password: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT password_hash, salt FROM users WHERE username = ?',
            (username,),
        ).fetchone()
    if row is None:
        return False
    password_hash, salt = row
    return _hash_password(password, salt) == password_hash


async def verify_user(username: str, password: str) -> bool:
    return await _run(_verify_user_sync, username, password)


def _create_user_sync(username: str, password: str) -> bool:
    """Insert a new user. Returns True if created, False if username taken."""
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)',
                (username, password_hash, salt),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


async def create_user(username: str, password: str) -> bool:
    return await _run(_create_user_sync, username, password)


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

def _get_tasks_sync(username: str) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            'SELECT id, title, completed, created_at FROM tasks '
            'WHERE username = ? ORDER BY id DESC',
            (username,),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


async def get_tasks(username: str) -> list:
    return await _run(_get_tasks_sync, username)


def _create_task_sync(username: str, title: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            'INSERT INTO tasks (username, title) VALUES (?, ?)',
            (username, title),
        )
        conn.commit()
        row = conn.execute(
            'SELECT id, title, completed, created_at FROM tasks WHERE id = ?',
            (cur.lastrowid,),
        ).fetchone()
    return _row_to_task(row)


async def create_task(username: str, title: str) -> dict:
    return await _run(_create_task_sync, username, title)


def _update_task_sync(username: str, task_id: int,
                      title, completed) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            'SELECT id, title, completed, created_at FROM tasks '
            'WHERE id = ? AND username = ?',
            (task_id, username),
        ).fetchone()
        if row is None:
            return None
        new_title = title if title is not None else row[1]
        new_completed = int(completed) if completed is not None else row[2]
        conn.execute(
            'UPDATE tasks SET title = ?, completed = ? WHERE id = ?',
            (new_title, new_completed, task_id),
        )
        conn.commit()
        row = conn.execute(
            'SELECT id, title, completed, created_at FROM tasks WHERE id = ?',
            (task_id,),
        ).fetchone()
    return _row_to_task(row)


async def update_task(username: str, task_id: int,
                      title, completed) -> dict | None:
    return await _run(_update_task_sync, username, task_id, title, completed)


def _delete_task_sync(username: str, task_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            'DELETE FROM tasks WHERE id = ? AND username = ?',
            (task_id, username),
        )
        conn.commit()
    return cur.rowcount > 0


async def delete_task(username: str, task_id: int) -> bool:
    return await _run(_delete_task_sync, username, task_id)
