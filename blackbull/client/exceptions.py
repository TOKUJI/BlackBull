"""Client errors; timeouts use the built-in TimeoutError."""


class ClientError(Exception):
    """Base for protocol client errors; built-in TimeoutError is separate.
    """


class ProtocolError(ClientError):
    """A request or peer response violates the protocol.
    """


class ConnectionError(ClientError):  # noqa: A001 — shadows builtin intentionally
    """The connection is closed or unusable.
    """


class ResponseTooLarge(ClientError):
    """A response head or body exceeds a configured client budget.

    This is a local limit, distinct from a protocol violation. Raise the budget
    only if the caller can safely accept more data.
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
