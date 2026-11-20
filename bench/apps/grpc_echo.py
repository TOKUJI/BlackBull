"""``native_app`` with gRPC enabled: one unary echo method, ``/svc/Echo``.

Its ordinary routes measure HTTP with a gRPC selector installed; the gRPC lane
posts ``bench/ab/grpc/unary.bin`` to it (``bench/ab/ab_commit_h2.sh``).
"""
from blackbull.grpc import GrpcServiceRegistry

from bench.apps.native_app import app

registry = GrpcServiceRegistry()


@registry.method('/svc/Echo')
async def echo(request, context):
    return request


app.enable_grpc(registry)
