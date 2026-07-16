"""The canonical gRPC *Greeter* — real protobuf on the wire, zero dependencies.

This is gRPC's universally-recognised "hello world": the ``helloworld.Greeter``
service.  Unlike ``examples/grpc_server.py`` (which echoes raw bytes to stay
serialisation-agnostic), this example speaks **actual protobuf**, so a stock
``grpcurl`` or ``grpcio`` client using the standard ``helloworld.proto`` talks
to it unmodified — the interop the design promises, shown end to end.

To keep the example self-contained there is no ``protoc`` step and no
``protobuf`` dependency: the two message types here are each a single ``string``
field, whose protobuf encoding is trivial, so we hand-roll it in ~20 lines
below.  In real code you would drop in ``grpc_tools.protoc`` output and call
``HelloRequest.FromString`` / ``reply.SerializeToString`` instead — the handler
contract (``bytes`` in, ``bytes`` out) is identical.

The equivalent ``.proto`` (what a client compiles against):

    syntax = "proto3";
    package helloworld;

    service Greeter {
      rpc SayHello (HelloRequest) returns (HelloReply);
      rpc SayManyHellos (HelloRequest) returns (stream HelloReply);
    }
    message HelloRequest { string name = 1; }
    message HelloReply   { string message = 1; }

Run it — no TLS needed to try it (BlackBull accepts cleartext HTTP/2 / h2c):

    python examples/grpc_greeter.py --port 50051

Call it with grpcurl (needs the .proto above saved as helloworld.proto):

    grpcurl -plaintext -proto helloworld.proto \
      -d '{"name": "BlackBull"}' \
      localhost:50051 helloworld.Greeter/SayHello
    # -> { "message": "Hello, BlackBull!" }

For a production-like TLS + ALPN setup, generate a dev cert and pass it in
(grpcurl then wants ``-insecure`` instead of ``-plaintext``):

    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
      -days 365 -nodes -subj '/CN=localhost'
    python examples/grpc_greeter.py --port 8443 --cert cert.pem --key key.pem

Or with the standard grpcio client + generated stubs:

    channel = grpc.secure_channel('localhost:8443', creds)
    stub = helloworld_pb2_grpc.GreeterStub(channel)
    stub.SayHello(helloworld_pb2.HelloRequest(name='BlackBull'))
    for reply in stub.SayManyHellos(helloworld_pb2.HelloRequest(name='BlackBull')):
        print(reply.message)
"""
import argparse

from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, GrpcStatus


# --- Minimal protobuf codec for single-`string` messages ------------------
# Real projects use protoc-generated classes; this is here only so the example
# needs no build step.  A proto3 `string name = 1` encodes as: a key varint
# (field_number << 3 | wire_type 2), a length varint, then the UTF-8 bytes.

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _string_field(field_number: int, value: str) -> bytes:
    """Encode ``value`` as protobuf length-delimited field ``field_number``."""
    data = value.encode('utf-8')
    key = (field_number << 3) | 2  # wire type 2 = length-delimited
    return bytes([key]) + _write_varint(len(data)) + data


def _write_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_string_field_1(buf: bytes) -> str:
    """Return field 1 (a string) from a protobuf message, or '' if absent.

    Skips any other fields so the parser tolerates messages with extra fields.
    """
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_number, wire_type = key >> 3, key & 0x07
        if wire_type == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            chunk = buf[pos:pos + length]
            pos += length
            if field_number == 1:
                return chunk.decode('utf-8')
        elif wire_type == 0:  # varint — skip
            _, pos = _read_varint(buf, pos)
        else:
            raise ValueError(f'unsupported wire type {wire_type}')
    return ''


# --- The Greeter service --------------------------------------------------

app = BlackBull()
grpc = GrpcServiceRegistry()


@grpc.method('/helloworld.Greeter/SayHello')
async def say_hello(request: bytes, context) -> bytes:
    """Unary RPC: HelloRequest{name} -> HelloReply{message}."""
    name = _read_string_field_1(request)
    if not name:
        context.abort(GrpcStatus.INVALID_ARGUMENT, 'name is required')
    return _string_field(1, f'Hello, {name}!')


@grpc.method('/helloworld.Greeter/SayManyHellos')
async def say_many_hellos(request: bytes, context):
    """Server-streaming RPC: yield three greetings for one request.

    An async generator handler is auto-detected as server-streaming; each
    ``yield`` becomes one HelloReply message on the wire.
    """
    name = _read_string_field_1(request) or 'world'
    for i in range(1, 4):
        yield _string_field(1, f'Hello #{i}, {name}!')


app.enable_grpc(grpc)


@app.route(path='/')
async def index():
    return 'REST here; gRPC service helloworld.Greeter on the same port.'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert')
    parser.add_argument('--key')
    args = parser.parse_args()
    app.run(port=args.port, certfile=args.cert, keyfile=args.key)
