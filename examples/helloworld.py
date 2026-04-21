import asyncio
from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello(scope, receive, send):
    assert scope['type'] == 'http'

    response_body = b'Hello, world!'

    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [
            (b'content-type', b'text/plain'),
        ],
    })

    await send({
        'type': 'http.response.body',
        'body': response_body,
    })

if __name__ == '__main__':
    asyncio.run(app.run(port=8000))