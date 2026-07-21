"""The opt-in ``Connection`` context object for HTTP handlers.

Declare a parameter annotated ``Connection`` (any name) — or a bare
parameter named ``conn``/``request`` — and the router injects the
per-request ``Connection``, BlackBull's single internal request
representation (the ASGI scope is a derived view of it).

``Connection`` replaces the old ``Request`` object; ``blackbull.Request``
remains a deprecated alias (removal no earlier than 2027-08-01).

Try it:

    python examples/connection_object.py

    curl localhost:8000/whoami
    curl -b 'session=abc' localhost:8000/whoami
    curl -X POST -H 'Content-Type: application/json' \
         -d '{"title": "buy milk"}' localhost:8000/notes/7
"""
from http import HTTPMethod

from blackbull import BlackBull, Connection

app = BlackBull()


@app.route(path='/whoami')
async def whoami(conn: Connection):
    return {
        'method': conn.method,
        'path': conn.path,
        'client': conn.client,
        'user_agent': conn.headers.get(b'user-agent', b'').decode(),
        'session': conn.cookies.get('session', ''),
    }


@app.route(path='/notes/{note_id:int}', methods=[HTTPMethod.POST])
async def create_note(note_id: int, conn: Connection):
    data = await conn.json()          # buffered + parsed once, cached
    if data is None:
        return {'error': 'invalid JSON'}
    return {'id': note_id, 'note': data}


if __name__ == '__main__':
    app.run(port=8000)
