"""Query-param resolution in the simplified handler model (Sprint 74).

Handler parameters that are neither path params, ``body``, ``scope``,
``Request``, a dataclass body, nor ``Depends(...)`` resolve from
``scope['query_string']`` with the same annotation-coercion rules path
params use.  A default makes the param optional; a missing required param
or a failed coercion surfaces as a 400 (``HTTPException``), never a 500.
"""
import warnings
from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest

from blackbull import BlackBull
from blackbull.router import HTTPException, _adapt_handler
from blackbull.testing import TestClient


def _scope(query: bytes = b'') -> dict:
    return {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [],
            'query_string': query}


class TestQueryParamResolution:
    @pytest.mark.asyncio
    async def test_str_param(self):
        captured = {}
        async def fn(q: str): captured['q'] = q
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q=bull'), None, AsyncMock())
        assert captured['q'] == 'bull'

    @pytest.mark.asyncio
    async def test_unannotated_param_resolves_as_str(self):
        captured = {}
        async def fn(q): captured['q'] = q
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q=42'), None, AsyncMock())
        assert captured['q'] == '42'
        assert isinstance(captured['q'], str)

    @pytest.mark.asyncio
    async def test_int_param(self):
        captured = {}
        async def fn(page: int): captured['page'] = page
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'page=7'), None, AsyncMock())
        assert captured['page'] == 7
        assert isinstance(captured['page'], int)

    @pytest.mark.asyncio
    async def test_float_param(self):
        captured = {}
        async def fn(ratio: float): captured['ratio'] = ratio
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'ratio=0.5'), None, AsyncMock())
        assert captured['ratio'] == 0.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize('raw', ['1', 'true', 'yes', 'on', 'True', 'YES'])
    async def test_bool_true_forms(self, raw):
        captured = {}
        async def fn(active: bool): captured['active'] = active
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(f'active={raw}'.encode()), None, AsyncMock())
        assert captured['active'] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize('raw', ['0', 'false', 'no', 'off', 'False', 'NO'])
    async def test_bool_false_forms(self, raw):
        captured = {}
        async def fn(active: bool): captured['active'] = active
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(f'active={raw}'.encode()), None, AsyncMock())
        assert captured['active'] is False

    @pytest.mark.asyncio
    async def test_default_used_when_absent(self):
        captured = {}
        async def fn(q: str, page: int = 1): captured.update(q=q, page=page)
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q=bull'), None, AsyncMock())
        assert captured == {'q': 'bull', 'page': 1}

    @pytest.mark.asyncio
    async def test_default_overridden_when_present(self):
        captured = {}
        async def fn(page: int = 1): captured['page'] = page
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'page=3'), None, AsyncMock())
        assert captured['page'] == 3

    @pytest.mark.asyncio
    async def test_optional_annotation_missing_gives_none(self):
        captured = {}
        async def fn(limit: int | None = None): captured['limit'] = limit
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b''), None, AsyncMock())
        assert captured['limit'] is None

    @pytest.mark.asyncio
    async def test_optional_annotation_present_coerces(self):
        captured = {}
        async def fn(limit: int | None = None): captured['limit'] = limit
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'limit=9'), None, AsyncMock())
        assert captured['limit'] == 9

    @pytest.mark.asyncio
    async def test_repeated_key_last_occurrence_wins(self):
        captured = {}
        async def fn(q: str): captured['q'] = q
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q=first&q=second'), None, AsyncMock())
        assert captured['q'] == 'second'

    @pytest.mark.asyncio
    async def test_blank_value_is_empty_string(self):
        captured = {}
        async def fn(q: str): captured['q'] = q
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q='), None, AsyncMock())
        assert captured['q'] == ''

    @pytest.mark.asyncio
    async def test_percent_and_plus_decoding(self):
        captured = {}
        async def fn(q: str): captured['q'] = q
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q=hello%20big+world'), None, AsyncMock())
        assert captured['q'] == 'hello big world'

    @pytest.mark.asyncio
    async def test_query_alongside_path_params(self):
        captured = {}
        async def fn(item_id: int, q: str, page: int = 1):
            captured.update(item_id=item_id, q=q, page=page)
        wrapper = _adapt_handler(fn, '/items/{item_id}')
        scope = _scope(b'q=bull&page=2')
        scope['path_params'] = {'item_id': '5'}
        await wrapper(scope, None, AsyncMock())
        assert captured == {'item_id': 5, 'q': 'bull', 'page': 2}

    @pytest.mark.asyncio
    async def test_missing_query_string_key_entirely(self):
        # scope without a 'query_string' key at all (defensive parity with
        # minimal test scopes) — defaults still apply.
        captured = {}
        async def fn(page: int = 4): captured['page'] = page
        wrapper = _adapt_handler(fn, '/search')
        await wrapper({'type': 'http', 'headers': []}, None, AsyncMock())
        assert captured['page'] == 4


class TestQueryParam400s:
    @pytest.mark.asyncio
    async def test_missing_required_raises_400(self):
        async def fn(q: str): pass
        wrapper = _adapt_handler(fn, '/search')
        with pytest.raises(HTTPException) as exc_info:
            await wrapper(_scope(b''), None, AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST
        assert 'q' in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_int_coercion_failure_raises_400(self):
        async def fn(page: int): pass
        wrapper = _adapt_handler(fn, '/search')
        with pytest.raises(HTTPException) as exc_info:
            await wrapper(_scope(b'page=abc'), None, AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_blank_int_value_raises_400(self):
        async def fn(page: int): pass
        wrapper = _adapt_handler(fn, '/search')
        with pytest.raises(HTTPException) as exc_info:
            await wrapper(_scope(b'page='), None, AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_bool_bad_value_raises_400(self):
        async def fn(active: bool): pass
        wrapper = _adapt_handler(fn, '/search')
        with pytest.raises(HTTPException) as exc_info:
            await wrapper(_scope(b'active=maybe'), None, AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST


class TestQueryParamRegistration:
    def test_unsupported_annotation_raises_at_registration(self):
        async def fn(x: dict): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'x'"):
            _adapt_handler(fn, '/search')

    def test_container_annotation_raises_at_registration(self):
        async def fn(tags: list[str]): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'tags'"):
            _adapt_handler(fn, '/search')

    def test_path_param_with_default_warns_shadowing(self):
        async def fn(item_id: int = 3): pass
        with pytest.warns(UserWarning, match='shadow'):
            _adapt_handler(fn, '/items/{item_id}')

    def test_path_param_without_default_does_not_warn(self):
        async def fn(item_id: int): pass
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            _adapt_handler(fn, '/items/{item_id}')

    def test_query_param_never_warns(self):
        async def fn(page: int = 1): pass
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            _adapt_handler(fn, '/search')

    @pytest.mark.asyncio
    async def test_request_name_with_scalar_annotation_is_query_param(self):
        # Spec change (Sprint 74): a param named 'request' with a scalar
        # annotation used to be a registration-time TypeError; it is now an
        # ordinary query param.  Request injection still requires the Request
        # annotation or the bare name 'request' (unannotated).
        captured = {}
        async def fn(request: int): captured['request'] = request
        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'request=8'), None, AsyncMock())
        assert captured['request'] == 8

    @pytest.mark.asyncio
    async def test_query_params_do_not_touch_receive(self):
        async def fn(q: str): pass
        called = []

        async def should_not_be_called():
            called.append(True)
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        wrapper = _adapt_handler(fn, '/search')
        await wrapper(_scope(b'q=x'), should_not_be_called, AsyncMock())
        assert called == []


class TestQueryParamsEndToEnd:
    def test_query_params_through_the_full_stack(self):
        app = BlackBull()

        @app.route(path='/search')
        async def search(q: str, page: int = 1, active: bool = False):
            return {'q': q, 'page': page, 'active': active}

        with TestClient(app) as client:
            r = client.get('/search?q=bull&page=2&active=yes')
            assert r.status_code == 200
            assert r.json() == {'q': 'bull', 'page': 2, 'active': True}

    def test_missing_required_is_400_not_500(self):
        app = BlackBull()

        @app.route(path='/search')
        async def search(q: str):
            return {'q': q}

        with TestClient(app) as client:
            r = client.get('/search')
            assert r.status_code == 400

    def test_coercion_failure_is_400_not_500(self):
        app = BlackBull()

        @app.route(path='/search')
        async def search(page: int):
            return {'page': page}

        with TestClient(app) as client:
            r = client.get('/search?page=xyz')
            assert r.status_code == 400
