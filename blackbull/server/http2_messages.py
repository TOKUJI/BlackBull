"""Message types for the HTTP/2 actor protocol.

These dataclasses are the internal messages exchanged between
``HTTP2Actor``, ``StreamActor``, and their callers.
"""
from dataclasses import dataclass, field

from ..actor import Message
from .headers import HeaderList
from .recipient import AbstractReader
from .sender import AbstractWriter


@dataclass
class ConnectionAccepted(Message):
    reader: AbstractReader | None = field(default=None, compare=False, repr=False)
    writer: AbstractWriter | None = field(default=None, compare=False, repr=False)
    peername: tuple[str, int] | None = field(default=None, compare=False, repr=False)

@dataclass
class StreamHeadersReceived(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    headers: HeaderList | None = field(default=None, compare=False, repr=False)
    end_stream: bool | None = field(default=None, compare=False, repr=False)

@dataclass
class WindowUpdate(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    increment: int | None = field(default=None, compare=False, repr=False)

@dataclass
class Header(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    headers: HeaderList | None = field(default=None, compare=False, repr=False)
    end_stream: bool | None = field(default=None, compare=False, repr=False)

@dataclass
class Data(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    data: bytes | None = field(default=None, compare=False, repr=False)
    end_stream: bool | None = field(default=None, compare=False, repr=False)

@dataclass
class SettingsReceived(Message):
    settings: dict[int, int] | None = field(default=None, compare=False, repr=False)

@dataclass
class Goaway(Message):
    last_stream_id: int | None = field(default=None, compare=False, repr=False)
    error_code: int | None = field(default=None, compare=False, repr=False)
    debug_data: bytes | None = field(default=None, compare=False, repr=False)

@dataclass
class WindowRequested(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    increment: int | None = field(default=None, compare=False, repr=False)
