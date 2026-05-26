async def app(scope, receive, send):
    assert scope["type"] == "http"

    body = b""

    while True:
        message = await receive()
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(len(body)).encode()),
        ],
    })

    await send({
        "type": "http.response.body",
        "body": body,
    })