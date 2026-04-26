"""Exception hierarchy for the protocol-layer client.

All client-side errors derive from ``ClientError`` so callers can catch the
whole family with one ``except`` clause and still distinguish specific causes.
"""


class ClientError(Exception):
    """Base class for all client-side errors."""


class ProtocolError(ClientError):
    """The client refused to send a request that violates the protocol."""


class ConnectionError(ClientError):  # noqa: A001 — shadows builtin intentionally
    """The connection was closed unexpectedly (e.g. server sent GOAWAY)."""


class HandshakeError(ClientError):
    """A WebSocket or HTTP/2 handshake failed."""


class StreamReset(ClientError):
    """The HTTP/2 stream was reset by the peer (RST_STREAM)."""

    def __init__(self, stream_id: int, error_code: int):
        super().__init__(f'stream {stream_id} reset (error_code={error_code})')
        self.stream_id = stream_id
        self.error_code = error_code
