class StreamingAwareMiddleware:
    """Base class for middlewares that inspect or transform the response body.

    Subclasses override one or both hooks:

    * ``on_response_start(start)`` — called when streaming is detected (first
      ``more_body=True`` body event); return the (optionally modified) start
      event.  Default: pass-through.
    * ``on_response_body(start, body)`` — called with the *complete* body for
      non-streaming responses; return ``(start, body)``.  Default: pass-through.

    The base class handles all streaming detection internally.  Subclasses
    never deal with ``more_body`` directly.

    Example::

        class TagMiddleware(StreamingAwareMiddleware):
            async def on_response_body(self, start, body):
                return start, b'<tagged>' + body + b'</tagged>'
    """

    async def __call__(self, scope, receive, send, call_next):
        if scope.get('type') != 'http':
            await call_next(scope, receive, send)
            return

        start_event: dict = {}
        body_parts: list[bytes] = []
        streaming = False

        async def intercepting_send(event: dict) -> None:
            nonlocal streaming
            event_type = event.get('type')

            if event_type == 'http.response.start':
                start_event.update(event)

            elif event_type == 'http.response.body':
                chunk = event.get('body', b'')
                more_body = event.get('more_body', False)

                if streaming:
                    await send(event)
                elif more_body:
                    streaming = True
                    modified = await self.on_response_start(start_event)
                    await send(modified)
                    if chunk:
                        await send({'type': 'http.response.body',
                                    'body': chunk, 'more_body': True})
                else:
                    body_parts.append(chunk)

            else:
                await send(event)  # trailers, etc.

        await call_next(scope, receive, intercepting_send)

        if streaming:
            return

        body = b''.join(body_parts)
        modified_start, modified_body = await self.on_response_body(start_event, body)
        await send(modified_start)
        await send({'type': 'http.response.body', 'body': modified_body, 'more_body': False})

    async def on_response_start(self, start: dict) -> dict:
        """Override to modify the start event for streaming responses."""
        return start

    async def on_response_body(self, start: dict, body: bytes) -> tuple[dict, bytes]:
        """Override to transform the full body for non-streaming responses."""
        return start, body
