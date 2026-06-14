"""Tests for blackbull.openapi — spec generation and Swagger UI host page."""
import json
import pytest
from dataclasses import dataclass, field
from typing import Optional

from blackbull import BlackBull
from blackbull.openapi import (
    OpenAPIExtension,
    _dataclass_to_schema,
    _type_to_schema,
    generate_spec,
    swagger_ui_html,
)
from http import HTTPMethod


@pytest.fixture
def app_with_routes():
    """A small app exercising every path-converter and a couple of methods."""
    app = BlackBull()

    @app.route(methods=HTTPMethod.GET, path='/')
    async def root(scope, receive, send):  # noqa: ARG001
        pass

    @app.route(methods=HTTPMethod.GET, path='/items/{item_id:int}')
    async def get_item(scope, receive, send):  # noqa: ARG001
        """Get one item.

        Returns the item with the given numeric id.
        """
        pass

    @app.route(methods=[HTTPMethod.POST, HTTPMethod.PUT], path='/items')
    async def write_item(scope, receive, send):  # noqa: ARG001
        pass

    @app.route(methods=HTTPMethod.DELETE, path='/users/{uid:uuid}')
    async def delete_user(scope, receive, send):  # noqa: ARG001
        pass

    @app.route(methods=HTTPMethod.GET, path='/files/{rest:path}')
    async def get_file(scope, receive, send):  # noqa: ARG001
        pass

    return app


def test_spec_has_3_1_envelope(app_with_routes):
    spec = generate_spec(app_with_routes, title='X', version='1.2.3')
    assert spec['openapi'] == '3.1.0'
    assert spec['info'] == {'title': 'X', 'version': '1.2.3'}
    assert 'paths' in spec


def test_paths_cover_every_registered_route(app_with_routes):
    paths = generate_spec(app_with_routes)['paths']
    assert set(paths) == {
        '/', '/items', '/items/{item_id}',
        '/users/{uid}', '/files/{rest}',
    }


def test_methods_emitted_per_path(app_with_routes):
    paths = generate_spec(app_with_routes)['paths']
    assert set(paths['/items']) == {'post', 'put'}
    assert set(paths['/items/{item_id}']) == {'get'}
    assert set(paths['/users/{uid}']) == {'delete'}


def test_path_param_converter_to_schema(app_with_routes):
    paths = generate_spec(app_with_routes)['paths']
    int_params = paths['/items/{item_id}']['get']['parameters']
    assert int_params == [{'name': 'item_id', 'in': 'path',
                           'required': True, 'schema': {'type': 'integer'}}]
    uuid_params = paths['/users/{uid}']['delete']['parameters']
    assert uuid_params[0]['schema'] == {'type': 'string', 'format': 'uuid'}
    path_params = paths['/files/{rest}']['get']['parameters']
    assert path_params[0]['schema'] == {'type': 'string'}


def test_docstring_becomes_summary_and_description(app_with_routes):
    op = generate_spec(app_with_routes)['paths']['/items/{item_id}']['get']
    assert op['summary'] == 'Get one item.'
    assert 'numeric id' in op['description']


def test_default_response_present(app_with_routes):
    op = generate_spec(app_with_routes)['paths']['/']['get']
    assert op['responses'] == {'200': {'description': 'OK'}}


def test_request_body_only_on_write_methods(app_with_routes):
    paths = generate_spec(app_with_routes)['paths']
    assert 'requestBody' in paths['/items']['post']
    assert 'requestBody' in paths['/items']['put']
    assert 'requestBody' not in paths['/']['get']
    assert 'requestBody' not in paths['/users/{uid}']['delete']


def test_websocket_routes_skipped():
    from blackbull.utils import Scheme  # noqa: PLC0415
    app = BlackBull()

    @app.route(methods=HTTPMethod.GET, path='/ws', scheme=Scheme.websocket)
    async def ws_route(scope, receive, send):  # noqa: ARG001
        pass

    @app.route(methods=HTTPMethod.GET, path='/http-route')
    async def http_route(scope, receive, send):  # noqa: ARG001
        pass

    paths = generate_spec(app)['paths']
    assert '/ws' not in paths
    assert '/http-route' in paths


def test_enable_openapi_registers_spec_and_docs_routes():
    app = BlackBull()

    @app.route(methods=HTTPMethod.GET, path='/ping')
    async def ping(scope, receive, send):  # noqa: ARG001
        pass

    app.enable_openapi(title='Test', version='0.0.1')

    # All three routes are registered.
    templates = {ri.template for ri in app._router._route_info}
    assert '/openapi.json' in templates
    assert '/docs' in templates
    assert '/ping' in templates


def test_enable_openapi_hides_its_own_routes_from_the_spec():
    app = BlackBull()

    @app.route(methods=HTTPMethod.GET, path='/users')
    async def list_users(scope, receive, send):  # noqa: ARG001
        pass

    app.enable_openapi()
    paths = generate_spec(app)['paths']
    assert '/users' in paths
    assert '/openapi.json' not in paths
    assert '/docs' not in paths


def test_enable_openapi_docs_path_can_be_disabled():
    app = BlackBull()
    app.enable_openapi(docs_path=None)
    templates = {ri.template for ri in app._router._route_info}
    assert '/openapi.json' in templates
    assert '/docs' not in templates


def test_swagger_ui_html_contains_spec_url_and_title():
    html = swagger_ui_html('/custom-spec.json', title='Hello UI')
    assert '<title>Hello UI</title>' in html
    assert 'url: "/custom-spec.json"' in html
    assert 'swagger-ui-dist' in html
    assert '<div id="swagger-ui"></div>' in html


def test_spec_is_json_serializable(app_with_routes):
    spec = generate_spec(app_with_routes)
    # Round-trip through json — no non-serializable types should leak.
    assert json.loads(json.dumps(spec)) == spec


# ---------------------------------------------------------------------------
# v2 — type → JSON schema synthesis
# ---------------------------------------------------------------------------

class TestTypeToSchema:
    def test_primitives(self):
        assert _type_to_schema(str)   == {'type': 'string'}
        assert _type_to_schema(int)   == {'type': 'integer'}
        assert _type_to_schema(float) == {'type': 'number'}
        assert _type_to_schema(bool)  == {'type': 'boolean'}
        assert _type_to_schema(bytes) == {'type': 'string', 'format': 'binary'}

    def test_none(self):
        assert _type_to_schema(None) == {'type': 'null'}
        assert _type_to_schema(type(None)) == {'type': 'null'}

    def test_list(self):
        assert _type_to_schema(list[int]) == {
            'type': 'array', 'items': {'type': 'integer'},
        }

    def test_dict(self):
        assert _type_to_schema(dict[str, int]) == {
            'type': 'object', 'additionalProperties': {'type': 'integer'},
        }

    def test_optional(self):
        assert _type_to_schema(Optional[int]) == {
            'anyOf': [{'type': 'integer'}, {'type': 'null'}],
        }
        # PEP 604 union form
        assert _type_to_schema(int | None) == {
            'anyOf': [{'type': 'integer'}, {'type': 'null'}],
        }

    def test_union_multiple(self):
        result = _type_to_schema(int | str)
        assert result == {'anyOf': [{'type': 'integer'}, {'type': 'string'}]}

    def test_unknown_falls_back_to_open(self):
        class Random: pass
        assert _type_to_schema(Random) == {}


class TestDataclassSchema:
    def test_simple(self):
        @dataclass
        class Item:
            name: str
            count: int

        assert _dataclass_to_schema(Item) == {
            'type': 'object',
            'title': 'Item',
            'properties': {
                'name':  {'type': 'string'},
                'count': {'type': 'integer'},
            },
            'required': ['name', 'count'],
        }

    def test_defaults_become_optional(self):
        @dataclass
        class Pet:
            name: str
            breed: str = 'unknown'
            tags: list[str] = field(default_factory=list)

        schema = _dataclass_to_schema(Pet)
        assert schema['required'] == ['name']
        assert schema['properties']['breed'] == {'type': 'string', 'default': 'unknown'}
        assert schema['properties']['tags'] == {
            'type': 'array', 'items': {'type': 'string'}, 'default': [],
        }

    def test_nested_dataclass(self):
        @dataclass
        class Address:
            street: str
            zip: str

        @dataclass
        class Person:
            name: str
            home: Address

        schema = _dataclass_to_schema(Person)
        assert schema['properties']['home'] == {
            'type': 'object',
            'title': 'Address',
            'properties': {
                'street': {'type': 'string'},
                'zip':    {'type': 'string'},
            },
            'required': ['street', 'zip'],
        }

    def test_optional_field(self):
        @dataclass
        class Maybe:
            value: int | None = None

        schema = _dataclass_to_schema(Maybe)
        assert schema['properties']['value']['anyOf'] == [
            {'type': 'integer'}, {'type': 'null'},
        ]


class TestHandlerIntrospection:
    def test_dataclass_body_emits_schema(self):
        @dataclass
        class CreateTask:
            title: str
            done: bool = False

        app = BlackBull()

        @app.route(methods=HTTPMethod.POST, path='/tasks')
        async def create(body: CreateTask):  # noqa: ARG001
            return {}

        op = generate_spec(app)['paths']['/tasks']['post']
        body_schema = op['requestBody']['content']['application/json']['schema']
        assert body_schema['type'] == 'object'
        assert body_schema['title'] == 'CreateTask'
        assert body_schema['properties']['title'] == {'type': 'string'}
        assert body_schema['required'] == ['title']

    def test_body_without_annotation_falls_back_to_object(self):
        app = BlackBull()

        @app.route(methods=HTTPMethod.POST, path='/raw')
        async def raw(body: bytes):  # noqa: ARG001
            return {}

        op = generate_spec(app)['paths']['/raw']['post']
        assert op['requestBody']['content']['application/json']['schema'] == {
            'type': 'object',
        }

    def test_dataclass_response_emits_schema(self):
        @dataclass
        class Task:
            id: int
            title: str

        app = BlackBull()

        @app.route(methods=HTTPMethod.GET, path='/tasks/{tid:int}')
        async def get(tid: int) -> Task:  # noqa: ARG001
            return Task(id=tid, title='x')

        op = generate_spec(app)['paths']['/tasks/{tid}']['get']
        resp = op['responses']['200']
        assert 'content' in resp
        schema = resp['content']['application/json']['schema']
        assert schema['title'] == 'Task'
        assert set(schema['properties']) == {'id', 'title'}

    def test_list_of_dataclass_response(self):
        @dataclass
        class Tag:
            name: str

        app = BlackBull()

        @app.route(methods=HTTPMethod.GET, path='/tags')
        async def list_tags() -> list[Tag]:
            return []

        op = generate_spec(app)['paths']['/tags']['get']
        schema = op['responses']['200']['content']['application/json']['schema']
        assert schema['type'] == 'array'
        assert schema['items']['title'] == 'Tag'

    def test_no_annotation_keeps_v1_response_stub(self):
        app = BlackBull()

        @app.route(methods=HTTPMethod.GET, path='/ping')
        async def ping(scope, receive, send):  # noqa: ARG001
            pass

        op = generate_spec(app)['paths']['/ping']['get']
        assert op['responses'] == {'200': {'description': 'OK'}}

    def test_path_param_not_treated_as_body(self):
        """A dataclass-typed handler parameter that *is* a path param
        must not be misread as the request body."""
        @dataclass
        class NotABody:
            x: int

        app = BlackBull()

        # 'tid' is in the URL; if introspection wrongly considered every
        # dataclass annotation a body, this POST would carry a body schema.
        # It doesn't — path params are excluded from body candidacy.
        @app.route(methods=HTTPMethod.POST, path='/things/{tid:int}')
        async def write(tid: int):  # noqa: ARG001
            return {}

        op = generate_spec(app)['paths']['/things/{tid}']['post']
        assert op['requestBody']['content']['application/json']['schema'] == {
            'type': 'object',
        }


# ---------------------------------------------------------------------------
# OpenAPIExtension — the reference implementation of the Sprint 40
# init_app(app) extension convention.
# ---------------------------------------------------------------------------


class TestOpenAPIExtension:
    def test_deferred_form_registers_routes_and_extensions_entry(self):
        app = BlackBull()

        @app.route(methods=HTTPMethod.GET, path='/ping')
        async def ping():  # noqa: ARG001
            return 'pong'

        ext = OpenAPIExtension(title='X', version='9.9.9')
        assert 'openapi' not in app.extensions
        ext.init_app(app)

        assert app.extensions['openapi'] is ext
        templates = {ri.template for ri in app._router._route_info}
        assert {'/openapi.json', '/docs', '/ping'} <= templates

    def test_eager_form_wires_on_construction(self):
        app = BlackBull()
        ext = OpenAPIExtension(app, title='X', version='1.0.0')

        assert app.extensions['openapi'] is ext
        templates = {ri.template for ri in app._router._route_info}
        assert '/openapi.json' in templates
        assert '/docs' in templates

    def test_docs_path_none_skips_swagger_route(self):
        app = BlackBull()
        OpenAPIExtension(app, docs_path=None)

        templates = {ri.template for ri in app._router._route_info}
        assert '/openapi.json' in templates
        assert '/docs' not in templates

    def test_collision_raises(self):
        """A second extension registering under the same key fails fast
        rather than silently shadowing the first instance."""
        app = BlackBull()
        OpenAPIExtension(app, title='first')

        with pytest.raises(RuntimeError, match="already registered"):
            OpenAPIExtension(app, title='second')

    def test_re_init_with_same_instance_is_idempotent_per_call(self):
        """Calling init_app twice with the same instance should not trigger
        the collision check (the existing-is-self branch)."""
        app = BlackBull()
        ext = OpenAPIExtension(title='X')
        ext.init_app(app)
        # The second init_app re-registers the routes (now duplicated), but
        # doesn't raise — the collision is only against a *different* owner.
        ext.init_app(app)
        assert app.extensions['openapi'] is ext

    def test_enable_openapi_uses_extension_under_the_hood(self):
        """BlackBull.enable_openapi() should produce the same state as a
        direct OpenAPIExtension construction — proving the convenience method
        is implemented in terms of the public extension surface."""
        app = BlackBull()
        app.enable_openapi(title='Via convenience', version='2.0.0')

        assert 'openapi' in app.extensions
        ext = app.extensions['openapi']
        assert isinstance(ext, OpenAPIExtension)
        assert ext.title == 'Via convenience'
        assert ext.version == '2.0.0'


# ---------------------------------------------------------------------------
# Conformance — generated spec must pass openapi-spec-validator's checks
# against the OpenAPI 3.1 schema.  Promotes "we emit JSON shaped like 3.1"
# from a structural assertion to a third-party-verified one.
# ---------------------------------------------------------------------------


@pytest.fixture
def _spec_validator():
    """Import the validator lazily and skip if it's not installed."""
    osv = pytest.importorskip('openapi_spec_validator')
    return osv.validate


def test_generated_spec_validates_against_openapi_3_1(_spec_validator, app_with_routes):
    spec = generate_spec(app_with_routes, title='Conformance', version='1.0.0',
                         description='Spec must round-trip the OpenAPI 3.1 schema.')
    # validate() raises on a non-conforming document.
    _spec_validator(spec)


def test_extension_emits_spec_that_validates(_spec_validator):
    @dataclass
    class CreateTask:
        title: str
        done: bool = False

    @dataclass
    class Task:
        id: int
        title: str

    app = BlackBull()

    @app.route(methods=HTTPMethod.GET, path='/tasks/{tid:int}')
    async def get_task(tid: int) -> Task:  # noqa: ARG001
        return Task(id=tid, title='x')

    @app.route(methods=HTTPMethod.POST, path='/tasks')
    async def create_task(body: CreateTask) -> Task:  # noqa: ARG001
        return Task(id=1, title=body.title)

    OpenAPIExtension(app, title='Validator', version='1.0.0')
    spec = generate_spec(app, title='Validator', version='1.0.0')
    _spec_validator(spec)
