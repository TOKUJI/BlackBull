"""``Depends`` — per-request dependency injection, on a pseudo database.

A *provider* declares a per-request resource; the framework constructs it
before the handler runs and tears it down **after the response is sent**.
This example fakes a DB pool with visible checkout accounting — watch the
``[pool]`` lines interleave with your requests in the terminal: the
``release`` line always prints after curl has already shown the response.

Try it:

    python examples/dependency_injection.py

    curl localhost:8000/tasks/1
    curl 'localhost:8000/tasks?done=false'
    curl -X POST -H 'Content-Type: application/json' \
         -d '{"title": "feed the bull"}' localhost:8000/tasks
    curl localhost:8000/report        # two Depends params, one connection
    curl localhost:8000/tasks/99      # 404 via HTTPException
"""
import asyncio
from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus

from blackbull import BlackBull, Depends, HTTPException

app = BlackBull()


# ---------------------------------------------------------------------------
# App-scoped engine — one pool per process, created at startup.  In a real
# app this is ``asyncpg.create_pool(...)`` / ``create_async_engine(...)``.
# ---------------------------------------------------------------------------

class FakeConnection:
    """One checked-out connection.  Real work would be SQL; here it's dicts."""

    def __init__(self, pool: 'FakePool', conn_id: int):
        self._tables = pool.tables
        self.id = conn_id

    async def fetch_task(self, task_id: int) -> dict | None:
        await asyncio.sleep(0)              # pretend to hit the wire
        return self._tables['tasks'].get(task_id)

    async def list_tasks(self, done: bool | None = None) -> list[dict]:
        await asyncio.sleep(0)
        return [t for t in self._tables['tasks'].values()
                if done is None or t['done'] == done]

    async def insert_task(self, title: str) -> dict:
        await asyncio.sleep(0)
        tasks = self._tables['tasks']
        task = {'id': max(tasks, default=0) + 1, 'title': title, 'done': False}
        tasks[task['id']] = task
        return task


class FakePool:
    """Stand-in for a DB pool, with visible checkout accounting."""

    def __init__(self):
        self.tables = {'tasks': {
            1: {'id': 1, 'title': 'water the bull', 'done': False},
            2: {'id': 2, 'title': 'polish the horns', 'done': True},
        }}
        self._seq = 0
        self.in_use = 0

    async def acquire(self) -> FakeConnection:
        self._seq += 1
        self.in_use += 1
        print(f'[pool] acquire conn#{self._seq}  (checked out: {self.in_use})',
              flush=True)
        return FakeConnection(self, self._seq)

    async def release(self, conn: FakeConnection) -> None:
        self.in_use -= 1
        print(f'[pool] release conn#{conn.id}  (checked out: {self.in_use})'
              '  <- after the response was sent', flush=True)


pool: FakePool | None = None


@app.on_startup
async def create_pool():
    global pool
    pool = FakePool()


# ---------------------------------------------------------------------------
# The provider — per-request lifetime.  Everything before ``yield`` runs
# before the handler; everything after ``yield`` runs once the response is
# on the wire (also on handler exceptions — ``finally`` always executes).
# ---------------------------------------------------------------------------

async def get_db():
    conn = await pool.acquire()
    try:
        yield conn
    finally:
        await pool.release(conn)


@dataclass
class NewTask:
    title: str


@app.route(path='/tasks/{task_id:int}')
async def get_task(task_id: int, db=Depends(get_db)):
    task = await db.fetch_task(task_id)
    if task is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f'no task {task_id}')
    return task


@app.route(path='/tasks')                       # ?done=true / ?done=false
async def list_tasks(done: bool | None = None, db=Depends(get_db)):
    return await db.list_tasks(done)


@app.route(path='/tasks', methods=[HTTPMethod.POST])
async def create_task(body: NewTask, db=Depends(get_db)):
    return await db.insert_task(body.title)


@app.route(path='/report')
async def report(a=Depends(get_db), b=Depends(get_db)):
    # use_cache=True (the default): parameters naming the same provider
    # share ONE instance per request — the terminal shows a single acquire.
    return {'same_connection': a is b, 'conn_id': a.id}


if __name__ == '__main__':
    app.run(port=8000)
