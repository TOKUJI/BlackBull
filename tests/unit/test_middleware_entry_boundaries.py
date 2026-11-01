"""Framework request conversion and direct-call fallback are distinct boundaries."""

import pytest

from blackbull.middleware import CORS, Compression, StaticFiles


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['http', 'websocket'])
@pytest.mark.parametrize('middleware_type', [CORS, Compression, StaticFiles])
async def test_direct_raw_scope_with_next_preserves_fallback(kind, middleware_type, tmp_path):
    middleware = middleware_type(str(tmp_path)) if middleware_type is StaticFiles else middleware_type()
    scope = {'type': kind, 'method': 'GET', 'path': '/', 'headers': []}
    calls = []
    sent = []

    async def send(event):
        sent.append(event)

    async def next_(request, receive, inner_send):
        calls.append(request)

    await middleware(scope, None, send, next_)
    assert calls == [scope]
    assert calls[0] is scope
    assert sent == []
