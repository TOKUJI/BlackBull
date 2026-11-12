from blackbull import BlackBull, Response

app = BlackBull()

@app.route(path='/')
async def hello(conn, receive, send):   # full form: `conn` is a native Connection
    assert conn.type == 'http'

    response_body = b'Hello, world!'

    await send(Response(response_body, content_type='text/plain'))

if __name__ == '__main__':
    app.run(port=8000)
