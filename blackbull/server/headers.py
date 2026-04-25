from collections.abc import Iterable
from typing import TypeAlias

HeaderList: TypeAlias = Iterable[tuple[bytes, bytes]]


class Headers:
    """Ordered multi-valued HTTP header store.

    Satisfies the ASGI ``Iterable[[byte string, byte string]]`` contract
    while also providing O(1) dict-like lookup.

    **Invariants**:

    - Header names and values are always ``bytes`` (per ASGI spec).
    - Lookups are case-insensitive: the internal index is keyed on
      ``name.lower()`` (RFC 7230 §3.2 — header field names are case-insensitive).
      ``__contains__``, ``__getitem__``, ``getlist``, and ``get`` all lowercase
      the requested name; iteration preserves the original casing of the input.
    - Insertion order of duplicate names is preserved (RFC 7230 §3.2.2).

    Examples::

        headers = Headers([(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')])

        list(headers)
        # [(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]   # ASGI iteration

        headers.getlist(b'set-cookie')
        # [(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]

        headers.getlist(b'missing')
        # []

        headers.get(b'host')          # first value, or default
        # b'localhost:8000'
    """

    def __init__(self, pairs: Iterable[tuple[bytes, bytes]]):
        self._list: list[tuple[bytes, bytes]] = list(pairs)
        self._index: dict[bytes, list[tuple[bytes, bytes]]] = {}
        for pair in self._list:
            self._index.setdefault(pair[0].lower(), []).append(pair)

    # ---- ASGI-compliant iterable ----------------------------------------

    def __iter__(self):
        return iter(self._list)

    def __len__(self) -> int:
        return len(self._list)

    # ---- dict-like lookup (returns list of pairs) -----------------------

    def __contains__(self, name: bytes) -> bool:
        return name.lower() in self._index

    def __getitem__(self, name: bytes) -> list[tuple[bytes, bytes]]:
        """Return all pairs for *name*.  Raises ``KeyError`` if absent."""
        return self._index[name.lower()]

    def getlist(self, name: bytes) -> list[tuple[bytes, bytes]]:
        """Return all pairs for *name*, or ``[]`` if the header is absent."""
        return self._index.get(name.lower(), [])

    def get(self, name: bytes, default: bytes = b'') -> bytes:
        """Return the first value for *name*, or *default* if absent.

        Mirrors ``dict.get(key, default)``: single value, optional default.
        For headers that may repeat use ``getlist(name)``.
        """
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