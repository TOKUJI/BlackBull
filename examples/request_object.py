"""The opt-in ``Request`` context object for HTTP handlers.

Declare a parameter annotated ``Request`` (any name) — or a bare
parameter named ``request`` — and the router injects a per-request
context object wrapping the ASGI scope + receive channel.

Try it:

    python examples/request_object.py

    curl localhost:8000/whoami
    curl -b 'session=abc' localhost:8000/whoami
    curl -X POST -H 'Content-Type: application/json' \
         -d '{"title": "buy milk"}' localhost:8000/notes/7
"""
from http import HTTPMethod

from blackbull import BlackBull, Request

app = BlackBull()


@app.route(path='/whoami')
async def whoami(request: Request):
    return {
        'method': request.method,
        'path': request.path,
        'client': request.client,
        'user_agent': request.headers.get(b'user-agent', b'').decode(),
        'session': request.cookies.get('session', ''),
    }


@app.route(path='/notes/{note_id:int}', methods=[HTTPMethod.POST])
async def create_note(note_id: int, request: Request):
    data = await request.json()          # buffered + parsed once, cached
    if data is None:
        return {'error': 'invalid JSON'}
    return {'id': note_id, 'note': data}


if __name__ == '__main__':
    app.run(port=8000)
