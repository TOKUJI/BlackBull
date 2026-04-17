class Headers:
    """Ordered multi-valued HTTP header store.

    Satisfies the ASGI ``Iterable[[byte string, byte string]]`` contract
    while also providing O(1) dict-like lookup.

    Lookup returns the list of matching ``(name, value)`` pairs in insertion
    order, preserving all duplicate header names as required by RFC 7230 §3.2.2
    and the ASGI spec.

    Examples::

        headers = Headers([(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')])

        list(headers)
        # [(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]   # ASGI iteration

        headers.get(b'set-cookie')
        # [(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]

        headers.get(b'missing')
        # []

        headers.get_value(b'host')          # first value, or default
        # b'localhost:8000'
    """

    def __init__(self, pairs: list[tuple[bytes, bytes]]):
        self._list = pairs
        self._index: dict[bytes, list[tuple[bytes, bytes]]] = {}
        for pair in pairs:
            self._index.setdefault(pair[0], []).append(pair)

    # ---- ASGI-compliant iterable ----------------------------------------

    def __iter__(self):
        return iter(self._list)

    def __len__(self) -> int:
        return len(self._list)

    # ---- dict-like lookup (returns list of pairs) -----------------------

    def __contains__(self, name: bytes) -> bool:
        return name in self._index

    def __getitem__(self, name: bytes) -> list[tuple[bytes, bytes]]:
        """Return all pairs for *name*.  Raises ``KeyError`` if absent."""
        return self._index[name]

    def get(self, name: bytes) -> list[tuple[bytes, bytes]]:
        """Return all pairs for *name*, or ``[]`` if the header is absent."""
        return self._index.get(name, [])

    def get_value(self, name: bytes, default: bytes = b'') -> bytes:
        """Return the first value for *name*, or *default* if absent.

        Convenience helper for headers that are not expected to repeat
        (e.g. ``Host``, ``Expect``, ``Content-Length``).
        """
        pairs = self._index.get(name)
        return pairs[0][1] if pairs else default
