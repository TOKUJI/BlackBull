"""Tests for blackbull.openapi — spec generation and Swagger UI host page."""
import json
import pytest

from blackbull import BlackBull
from blackbull.openapi import generate_spec, swagger_ui_html
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
