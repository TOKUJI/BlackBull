"""Unary gRPC over BlackBull's HTTP/2 layer — REST and gRPC on one port.

gRPC is HTTP/2 with ``content-type: application/grpc``; BlackBull multiplexes
it onto the same port as your REST routes.  Handlers exchange raw message
bytes, so this example echoes bytes rather than pulling in protobuf — wire your
own ``MyRequest.FromString`` / ``response.SerializeToString`` in real code.

Run (gRPC needs HTTP/2, so TLS + ALPN):

    python examples/grpc_server.py --port 8443 --cert cert.pem --key key.pem

Then call ``/echo.Echo/Echo`` with any gRPC client pointed at the service.
"""
import argparse

from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, GrpcStatus

app = BlackBull()
grpc = GrpcServiceRegistry()


@grpc.method('/echo.Echo/Echo')
async def echo(request: bytes, context) -> bytes:
    if not request:
        context.abort(GrpcStatus.INVALID_ARGUMENT, 'empty request message')
    return request


@grpc.method('/echo.Echo/Reverse')
async def reverse(request: bytes, context) -> bytes:
    return request[::-1]


app.enable_grpc(grpc)


@app.route(path='/')
async def index():
    return 'REST here; gRPC service echo.Echo on the same port.'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert')
    parser.add_argument('--key')
    args = parser.parse_args()
    app.run(port=args.port, certfile=args.cert, keyfile=args.key)
