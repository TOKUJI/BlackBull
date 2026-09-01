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


class ResponseTooLarge(ClientError):
    """The peer's response head passed a byte budget the client set.

    Distinct from :class:`ProtocolError`: the response was well-formed as far
    as it was read.  What failed is a limit this client chose, so a caller that
    wants the peer's output anyway can raise the budget rather than conclude
    the peer is broken.
    """

    def __init__(self, message: str, seen: bytes = b'') -> None:
        super().__init__(message)
        #: What had been read when the budget was passed — enough to identify
        #: the peer and the field, and deliberately not the whole overrun.
        self.seen = seen


class HandshakeError(ClientError):
    """A WebSocket or HTTP/2 handshake failed."""


class StreamReset(ClientError):
    """The HTTP/2 stream was reset by the peer (RST_STREAM)."""

    def __init__(self, stream_id: int, error_code: int):
        super().__init__(f'stream {stream_id} reset (error_code={error_code})')
        self.stream_id = stream_id
        self.error_code = error_code
