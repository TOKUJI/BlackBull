"""Ordered, multi-valued byte headers with case-insensitive lookup.
"""
from collections.abc import Iterable
from typing import TypeAlias

from .protocol import structured_fields as sf
from .protocol.field_grammar import (
    FIELD_VALUE_ALLOWED_OCTETS, LOWERCASE_TCHAR_OCTETS, TCHAR_OCTETS)

HeaderList: TypeAlias = Iterable[tuple[bytes, bytes]]


def _validate_response_header_field(name: bytes, value: bytes) -> None:
    """Reject a response field that cannot remain one field on the wire.

    The grammar is HTTP's, not this layer's: the same token alphabet and the
    same field-content octets the two transports validate against.
    """
    if not isinstance(name, bytes) or not isinstance(value, bytes):
        raise TypeError('HTTP response header name and value must be bytes')
    if not name or name.translate(None, TCHAR_OCTETS):
        raise ValueError('invalid HTTP response header name')
    if value.translate(None, FIELD_VALUE_ALLOWED_OCTETS):
        raise ValueError('invalid HTTP response header value')


class _MinimalResponseHeaders(list):
    """A response field section that keeps its contract: every name lowercase
    tchar, every value free of CTL, and the framing fields located —
    ``content_length`` (the Content-Length fields, or ``None``),
    ``transfer_encoding`` and ``date`` (whether present).

    Build one with [`_as_response_fields`][]; add or remove fields only with
    [`add`][], [`extend`][] and [`discard`][], which keep the framing facts
    true.  A value replaced in place must still keep the contract.
    """

    __slots__ = ('content_length', 'transfer_encoding', 'date')

    def add(self, name: bytes, value: bytes) -> None:
        """Append one field, validated, with its name lowercased."""
        _validate_response_header_field(name, value)
        field = (name.lower(), value)
        self.append(field)
        self._locate(field)

    def extend(self, fields: Iterable) -> None:
        """Append *fields*, validated unless they already keep the contract."""
        for field in _as_response_fields(fields):
            self.append(field)
            self._locate(field)

    def discard(self, name: bytes) -> None:
        """Remove every field named *name* (lowercase)."""
        self[:] = [field for field in self if field[0] != name]
        if name == b'content-length':
            self.content_length = None
        elif name == b'transfer-encoding':
            self.transfer_encoding = False
        elif name == b'date':
            self.date = False

    def copy(self) -> '_MinimalResponseHeaders':
        """A copy with the same fields and framing facts."""
        out = _MinimalResponseHeaders(self)
        out.content_length = (None if self.content_length is None
                              else list(self.content_length))
        out.transfer_encoding = self.transfer_encoding
        out.date = self.date
        return out

    def _locate(self, field: tuple[bytes, bytes]) -> None:
        name = field[0]
        size = len(name)
        if size == 14:
            if name == b'content-length':
                if self.content_length is None:
                    self.content_length = []
                self.content_length.append(field)
        elif size == 17:
            if name == b'transfer-encoding':
                self.transfer_encoding = True
        elif size == 4:
            if name == b'date':
                self.date = True


def _as_response_fields(fields: Iterable) -> _MinimalResponseHeaders:
    """Return *fields* when they already keep the response-field contract,
    else a validated copy with names lowercased.

    *fields* holds ``(name, value)`` pairs in any two-item form ASGI allows.
    Raises ``ValueError``/``TypeError`` for a field that cannot remain one
    field on the wire.
    """
    if isinstance(fields, _MinimalResponseHeaders):
        return fields
    head = _MinimalResponseHeaders(fields)
    content_length = None
    transfer_encoding = date = False
    for i, field in enumerate(head):
        name, value = field
        if (type(name) is not bytes or type(value) is not bytes
                or not name or name.translate(None, LOWERCASE_TCHAR_OCTETS)
                or value.translate(None, FIELD_VALUE_ALLOWED_OCTETS)):
            _validate_response_header_field(name, value)
            name = name.lower()
            head[i] = field = (name, value)
        size = len(name)
        if size == 14:
            if name == b'content-length':
                if content_length is None:
                    content_length = []
                content_length.append(field)
        elif size == 17:
            if name == b'transfer-encoding':
                transfer_encoding = True
        elif size == 4:
            if name == b'date':
                date = True
    head.content_length = content_length
    head.transfer_encoding = transfer_encoding
    head.date = date
    return head


def _owned_response_fields(fields: Iterable) -> _MinimalResponseHeaders:
    """Return a copy of *fields* the caller may mutate, keeping the
    response-field contract; validated only when *fields* does not keep it."""
    if isinstance(fields, _MinimalResponseHeaders):
        return fields.copy()
    return _as_response_fields(fields)


class Headers:
    """Ordered multi-valued headers with bytes names and values.

    Lookups ignore name casing; iteration preserves input casing and duplicate
    order. get returns the first value or its default; getlist returns all
    matching (name, value) pairs, or an empty list.
    """

    def __init__(self, pairs: Iterable[tuple[bytes, bytes]]):
        self._list: list[tuple[bytes, bytes]] = list(pairs)
        self._index: dict[bytes, list[tuple[bytes, bytes]]] = {}
        for pair in self._list:
            self._index.setdefault(pair[0].lower(), []).append(pair)

    @classmethod
    def from_lowered(cls, pairs: list[tuple[bytes, bytes]]) -> 'Headers':
        """Adopt pairs whose names the caller guarantees are lowercase.

        Uppercase names would become unreachable through lookup. Do not mutate the
        list after handing it over; this path takes ownership instead of copying.
        """
        self = cls.__new__(cls)
        self._list = pairs
        index: dict[bytes, list[tuple[bytes, bytes]]] = {}
        for pair in pairs:
            index.setdefault(pair[0], []).append(pair)
        self._index = index
        return self

    @classmethod
    def _adopt(cls, pairs: list[tuple[bytes, bytes]],
               index: dict[bytes, list[tuple[bytes, bytes]]]) -> 'Headers':
        """Take *pairs* and the index [`from_lowered`][] would build from them."""
        self = cls.__new__(cls)
        self._list = pairs
        self._index = index
        return self

    # ---- ASGI-compliant iterable ----------------------------------------

    def __iter__(self):
        return iter(self._list)

    def __len__(self) -> int:
        return len(self._list)

    def __eq__(self, other: object) -> bool:
        """Value equality on the ordered ``(name, value)`` pair list.

        Two ``Headers`` are equal when they carry the same fields in the same
        order (RFC 7230 §3.2.2 — order is significant for repeated fields).
        Enables ``Connection`` round-trip equality."""
        if isinstance(other, Headers):
            return self._list == other._list
        return NotImplemented

    # Defining __eq__ drops the inherited __hash__; Headers is a mutable
    # multi-valued store and is never used as a dict key or set member.
    __hash__ = None

    # ---- dict-like lookup (returns list of pairs) -----------------------

    # The index uses lowercase bytes keys; mixed-case lookups normalize on a miss.

    def __contains__(self, name: bytes) -> bool:
        return name in self._index or name.lower() in self._index

    def __getitem__(self, name: bytes) -> list[tuple[bytes, bytes]]:
        """Return all pairs for *name*.  Raises ``KeyError`` if absent."""
        pairs = self._index.get(name)
        if pairs is None:
            return self._index[name.lower()]
        return pairs

    def getlist(self, name: bytes) -> list[tuple[bytes, bytes]]:
        """Return all pairs for *name*, or ``[]`` if the header is absent."""
        pairs = self._index.get(name)
        if pairs is None:
            pairs = self._index.get(name.lower())
        return pairs if pairs is not None else []

    def get(self, name: bytes, default: bytes = b'') -> bytes:
        """Return the first value for *name*, or *default* if absent.

        Mirrors ``dict.get(key, default)``: single value, optional default.
        For headers that may repeat use ``getlist(name)``.
        """
        pairs = self._index.get(name)
        if pairs is None:
            pairs = self._index.get(name.lower())
        return pairs[0][1] if pairs else default

    def append(self, name_or_pairs, value: bytes | None = None) -> None:
        """Append header(s) to the end of the list.

        Two-argument form: ``append(name, value)`` — adds a single pair.
        One-argument form: ``append(pairs)`` — adds every pair in the iterable.
        """
        if value is not None:
            pair = (name_or_pairs, value)
            self._list.append(pair)
            self._index.setdefault(name_or_pairs.lower(), []).append(pair)
        else:
            for name, val in name_or_pairs:
                pair = (name, val)
                self._list.append(pair)
                self._index.setdefault(name.lower(), []).append(pair)

    def __add__(self, other: 'Headers') -> 'Headers':
        """Return a new Headers containing all pairs from *self* then *other*."""
        return Headers(list(self._list) + list(other._list))

    def get_combined(self, name: bytes) -> bytes | None:
        """Return values joined by ``, ``, or ``None`` when absent.

        Use only for fields allowing comma combination. For fields such as
        Set-Cookie, use ``getlist`` instead. A single empty value returns ``b''``.
        """
        pairs = self._index.get(name)
        if pairs is None:
            pairs = self._index.get(name.lower())
        if not pairs:
            return None
        if len(pairs) == 1:
            return pairs[0][1]
        return b', '.join(value for _, value in pairs)

    # ---- Structured Fields accessors (RFC 9651) --------------------------

    def get_sf_item(self, name: bytes) -> sf.Item | None:
        """Parse *name* as a Structured Field Item (RFC 9651).

        Returns ``(bare_item, parameters)``, or ``None`` if the field is
        absent or fails strict parsing (per RFC 9651 §4.2 the whole field
        is then ignored).

        Example::

            headers.get_sf_item(b'deprecation')   # (Date(1659578233), {})
        """
        value = self.get_combined(name)
        if value is None:
            return None
        try:
            return sf.parse_item(value)
        except ValueError:
            return None

    def get_sf_list(self, name: bytes) -> sf.SFList | None:
        """Parse *name* as a Structured Field List (RFC 9651).

        Multiple field lines are combined first.  Returns a list of Items /
        Inner Lists, or ``None`` if the field is absent or fails strict
        parsing (per RFC 9651 §4.2 the whole field is then ignored).

        Example::

            headers.get_sf_list(b'accept-query')  # [('a', {}), ('b', {})]
        """
        value = self.get_combined(name)
        if value is None:
            return None
        try:
            return sf.parse_list(value)
        except ValueError:
            return None

    def get_sf_dict(self, name: bytes) -> sf.SFDictionary | None:
        """Parse *name* as a Structured Field Dictionary (RFC 9651).

        Multiple field lines are combined first.  Returns an ordered
        ``dict`` of member name → Item / Inner List, or ``None`` if the
        field is absent or fails strict parsing (per RFC 9651 §4.2 the
        whole field is then ignored).

        Example::

            headers.get_sf_dict(b'priority')      # {'u': (2, {}), 'i': (True, {})}
        """
        value = self.get_combined(name)
        if value is None:
            return None
        try:
            return sf.parse_dictionary(value)
        except ValueError:
            return None
