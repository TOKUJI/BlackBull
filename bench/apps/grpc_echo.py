"""``native_app`` with gRPC enabled: a unary echo, ``/svc/Echo``, and a
server-streaming echo, ``/svc/Stream``, that answers with the request
``STREAM_MESSAGES`` times.

Its ordinary routes measure HTTP with a gRPC selector installed; the gRPC
lanes post ``bench/ab/grpc/unary.bin`` to it (``bench/ab/ab_commit_h2.sh``).
"""
from blackbull.grpc import GrpcServiceRegistry

from bench.apps.native_app import app

STREAM_MESSAGES = 8

registry = GrpcServiceRegistry()


@registry.method('/svc/Echo')
async def echo(request, context):
    return request


@registry.method('/svc/Stream', streaming=True)
async def stream(request, context):
    for _ in range(STREAM_MESSAGES):
        yield request


app.enable_grpc(registry)
