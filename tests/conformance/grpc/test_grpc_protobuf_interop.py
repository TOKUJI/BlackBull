"""Interop conformance for the **protobuf layer** (`blackbull-protobuf`,
Sprint 66): a real grpcio client + the official grpcio client-side packages
driving BlackBull's reflection, health, and rich-error services over a real
h2c socket.

What each leg proves against a spec-strict, independent implementation:

- **Reflection (D2)** — ``grpc_reflection``'s
  ``ProtoReflectionDescriptorDatabase`` (the grpcurl flow: list services,
  fetch descriptors, then invoke a method using *only* reflection-derived
  dynamic message classes — no local ``.proto``).
- **Health (D3)** — ``grpc_health``'s generated ``HealthStub`` gencode.  Note
  that this registers ``grpc/health/v1/health.proto`` in the *default*
  descriptor pool of the very process serving our vendored copy — passing
  proves the private-pool design prevents the duplicate-file collision.
- **Rich errors (D4)** — ``grpc_status.rpc_status.from_call``, which also
  *verifies* that the packed ``google.rpc.Status`` agrees with the
  ``grpc-status`` / ``grpc-message`` trailers (it raises on any mismatch).
- **Adapter (D1)** — the serving app is wired via ``add_servicer`` with
  object-typed handlers; every leg above rides it.

Skips when the optional packages are missing (core BlackBull must work
without them).  Set ``BB_GRPC_INTEROP_REQUIRE_PROTOBUF=1`` (the grpc-interop
CI job does, once blackbull-protobuf is published) to turn a missing package
into a hard failure so the gate cannot silently pass on nothing.
"""
from __future__ import annotations

import asyncio
import os
import threading

import pytest
import pytest_asyncio

_REQUIRED = ('grpc', 'blackbull_protobuf', 'grpc_reflection', 'grpc_health',
             'grpc_status')
if os.environ.get('BB_GRPC_INTEROP_REQUIRE_PROTOBUF'):
    import importlib
    for _mod in _REQUIRED:
        importlib.import_module(_mod)       # missing dep = loud failure
for _mod in _REQUIRED:
    pytest.importorskip(
        _mod, reason=f'{_mod} not installed; the protobuf interop suite '
                     f'needs blackbull-protobuf plus the grpcio client packages')

import grpc
from google.protobuf import descriptor_pool, message_factory
from google.rpc import error_details_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha.proto_reflection_descriptor_database import (
    ProtoReflectionDescriptorDatabase,
)
from grpc_status import rpc_status

from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, GrpcStatus
from blackbull.server.server import ASGIServer
from blackbull_protobuf import (
    add_servicer, enable_health, enable_reflection, abort_with_details,
    HealthService,
)

from . import bbinterop_pb2

_SERVER_STARTUP_WAIT_SECONDS = 0.15
_CALL_TIMEOUT = 5.0


class Greeter:
    async def SayHello(self, request, context):
        return bbinterop_pb2.HelloReply(message=f'Hello, {request.name}!')

    async def LotsOfReplies(self, request, context):
        for i in range(3):
            yield bbinterop_pb2.HelloReply(message=f'{request.name} #{i}')

    async def AlwaysInvalid(self, request, context):
        bad = error_details_pb2.BadRequest()
        bad.field_violations.add(field='name', description='must not be empty')
        abort_with_details(context, GrpcStatus.INVALID_ARGUMENT,
                           'name is required', [bad])


def _make_app():
    app = BlackBull()
    registry = GrpcServiceRegistry()
    add_servicer(registry, Greeter(), bbinterop_pb2)
    health = enable_health(registry)
    health.set('bbinterop.Greeter', HealthService.SERVING)
    enable_reflection(registry)
    app.enable_grpc(registry)
    return app, health


@pytest_asyncio.fixture
async def grpc_server():
    """(port, HealthService) of a protobuf-enabled BlackBull app on h2c."""
    app, health = _make_app()
    server = ASGIServer(app)
    server.open_socket(port=0)
    port = server.port

    task = asyncio.create_task(server.run())
    await asyncio.sleep(_SERVER_STARTUP_WAIT_SECONDS)
    try:
        yield port, health
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Reflection — the grpcurl flow, no local .proto
# ---------------------------------------------------------------------------

def _reflection_flow(port: int):
    """List services, then invoke SayHello using only reflection-derived
    descriptors (dynamic message classes)."""
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        db = ProtoReflectionDescriptorDatabase(channel)
        services = list(db.get_services())

        pool = descriptor_pool.DescriptorPool(db)
        service = pool.FindServiceByName('bbinterop.Greeter')
        method = service.methods_by_name['SayHello']
        request_class = message_factory.GetMessageClass(method.input_type)
        response_class = message_factory.GetMessageClass(method.output_type)

        call = channel.unary_unary(
            '/bbinterop.Greeter/SayHello',
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=response_class.FromString,
        )
        reply = call(request_class(name='reflection'), timeout=_CALL_TIMEOUT)
        return services, reply.message


@pytest.mark.asyncio
async def test_reflection_lists_and_invokes_without_proto(grpc_server):
    port, _ = grpc_server
    services, message = await asyncio.to_thread(_reflection_flow, port)
    assert 'bbinterop.Greeter' in services
    assert 'grpc.health.v1.Health' in services
    assert 'grpc.reflection.v1alpha.ServerReflection' in services
    assert message == 'Hello, reflection!'


def _describe_service(port: int, symbol: str):
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        pool = descriptor_pool.DescriptorPool(
            ProtoReflectionDescriptorDatabase(channel))
        service = pool.FindServiceByName(symbol)
        return service.file.name, sorted(service.methods_by_name)


@pytest.mark.asyncio
async def test_reflection_describes_health_service(grpc_server):
    """The health service is fully describable through reflection.  (The
    exact file *name* is not pinned: this test process also imports the
    grpcio client gencode, which registers an equivalent copy in the default
    pool under grpcio's renamed path, and the server may serve either.)"""
    port, _ = grpc_server
    file_name, methods = await asyncio.to_thread(
        _describe_service, port, 'grpc.health.v1.Health')
    assert file_name.endswith('health.proto')
    assert methods == ['Check', 'Watch']


# ---------------------------------------------------------------------------
# Health — official gencode client stub
# ---------------------------------------------------------------------------

def _health_check(port: int, service: str):
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        stub = health_pb2_grpc.HealthStub(channel)
        try:
            resp = stub.Check(health_pb2.HealthCheckRequest(service=service),
                              timeout=_CALL_TIMEOUT)
            return (True, resp.status)
        except grpc.RpcError as exc:  # noqa: BLE001 — surface the status
            return (False, exc)


@pytest.mark.asyncio
async def test_health_check_overall_and_per_service(grpc_server):
    port, _ = grpc_server
    ok, status = await asyncio.to_thread(_health_check, port, '')
    assert ok and status == health_pb2.HealthCheckResponse.SERVING
    ok, status = await asyncio.to_thread(_health_check, port, 'bbinterop.Greeter')
    assert ok and status == health_pb2.HealthCheckResponse.SERVING


@pytest.mark.asyncio
async def test_health_check_unknown_service_not_found(grpc_server):
    port, _ = grpc_server
    ok, exc = await asyncio.to_thread(_health_check, port, 'no.such.Svc')
    assert not ok
    assert exc.code() == grpc.StatusCode.NOT_FOUND


def _health_watch_two(port: int, service: str, got_first: threading.Event):
    """Read the first two Watch updates for *service*, signalling
    *got_first* once the initial status arrived (so the flip can't race the
    subscription)."""
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        stub = health_pb2_grpc.HealthStub(channel)
        stream = stub.Watch(health_pb2.HealthCheckRequest(service=service),
                            timeout=_CALL_TIMEOUT)
        first = next(stream).status
        got_first.set()
        second = next(stream).status
        stream.cancel()
        return first, second


@pytest.mark.asyncio
async def test_health_watch_observes_status_flip(grpc_server):
    port, health = grpc_server
    got_first = threading.Event()

    async def flip_after_first():
        # Wait (off-loop) until the client has the initial status, then flip
        # on the server's loop — HealthService.set is loop-affine.
        await asyncio.to_thread(got_first.wait, _CALL_TIMEOUT)
        health.set('bbinterop.Greeter', HealthService.NOT_SERVING)

    flipper = asyncio.create_task(flip_after_first())
    try:
        first, second = await asyncio.to_thread(
            _health_watch_two, port, 'bbinterop.Greeter', got_first)
    finally:
        await flipper
    assert first == health_pb2.HealthCheckResponse.SERVING
    assert second == health_pb2.HealthCheckResponse.NOT_SERVING


# ---------------------------------------------------------------------------
# Rich error details — grpc_status decodes (and cross-checks) the trailer
# ---------------------------------------------------------------------------

def _call_always_invalid(port: int):
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.unary_unary(
            '/bbinterop.Greeter/AlwaysInvalid',
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=bbinterop_pb2.HelloReply.FromString,
        )
        try:
            call(bbinterop_pb2.HelloRequest(name=''), timeout=_CALL_TIMEOUT)
            return None
        except grpc.RpcError as exc:  # noqa: BLE001 — the expected outcome
            return exc


@pytest.mark.asyncio
async def test_rich_error_details_decoded_by_grpc_status(grpc_server):
    port, _ = grpc_server
    exc = await asyncio.to_thread(_call_always_invalid, port)
    assert exc is not None
    assert exc.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert exc.details() == 'name is required'

    # from_call also *verifies* code/message consistency between the packed
    # google.rpc.Status and the plain trailers — a mismatch raises ValueError.
    status = rpc_status.from_call(exc)
    assert status is not None
    assert status.message == 'name is required'
    bad = error_details_pb2.BadRequest()
    assert status.details[0].Unpack(bad)
    assert bad.field_violations[0].field == 'name'


# ---------------------------------------------------------------------------
# Adapter — streaming through generated messages
# ---------------------------------------------------------------------------

def _server_stream(port: int, name: str):
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.unary_stream(
            '/bbinterop.Greeter/LotsOfReplies',
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=bbinterop_pb2.HelloReply.FromString,
        )
        return [r.message for r in
                call(bbinterop_pb2.HelloRequest(name=name),
                     timeout=_CALL_TIMEOUT)]


@pytest.mark.asyncio
async def test_adapter_server_streaming_messages(grpc_server):
    port, _ = grpc_server
    replies = await asyncio.to_thread(_server_stream, port, 'bb')
    assert replies == ['bb #0', 'bb #1', 'bb #2']
