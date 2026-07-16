# Structured Fields (RFC 9651)

Every HTTP header defined by the IETF since ~2021 — `Priority`,
`Deprecation`, `Accept-Query`, rate-limit signalling, Client Hints —
uses **Structured Field Values** (RFC 9651) instead of a bespoke
grammar.  BlackBull ships a strict parser and serialiser for all of
it in `blackbull.protocol.structured_fields`, verified against the
HTTP Working Group's
[conformance suite](https://github.com/httpwg/structured-field-tests)
(2,135 cases).

## Reading structured headers

`Headers` exposes one accessor per top-level SF type.  Each returns
`None` when the field is absent **or** fails strict parsing — RFC 9651
requires a malformed field to be ignored in its entirety:

```python
@app.route(path='/', methods=[HTTPMethod.GET])
async def handler(request: Request):
    # Dictionary-typed field, e.g. Priority: u=2, i
    priority = request.headers.get_sf_dict(b'priority')
    # → {'u': (2, {}), 'i': (True, {})}   or None

    # Item-typed field, e.g. Deprecation: @1659578233
    deprecation = request.headers.get_sf_item(b'deprecation')
    # → (Date(1659578233), {})            or None

    # List-typed field, e.g. Example: sugar, tea;kind=green
    example = request.headers.get_sf_list(b'example')
    # → [(Token('sugar'), {}), (Token('tea'), {'kind': Token('green')})]
```

Multiple field lines of the same name are combined with `", "` before
parsing, exactly as §4.2 of the RFC specifies.

## The type mapping

Every member comes back as a `(value, parameters)` tuple, where
`parameters` is an ordered `dict[str, bare item]`.  An Inner List is a
`(list_of_items, parameters)` tuple.  Bare items map to Python as:

| SF type | Python type | Wire example |
|---|---|---|
| Integer | `int` | `42` |
| Decimal | `float` | `4.5` |
| String | `str` | `"hello"` |
| Token | `Token` (str subclass) | `text/html` |
| Byte Sequence | `bytes` | `:cHJl...:` |
| Boolean | `bool` | `?1` |
| Date | `Date` | `@1659578233` |
| Display String | `DisplayString` (str subclass) | `%"f%c3%bc"` |

`Token` and `DisplayString` compare equal to plain `str` but keep
their identity so re-serialisation stays faithful (`foo` vs `"foo"` vs
`%"foo"` are three different wire forms of the same Python string).

`Date` wraps an integer of Unix seconds rather than subclassing
`datetime` — the RFC's range (±10¹⁵−1 seconds) exceeds what `datetime`
can hold.  Use `Date.to_datetime()` / `Date.from_datetime()` for
values in the representable range.

## Producing structured headers

The serialisers return ASCII bytes ready to drop into a response
header list:

```python
from blackbull.protocol.structured_fields import (
    Date, Token, serialize_dictionary, serialize_item, serialize_list,
)

serialize_list([(Token('gzip'), {}), (Token('br'), {'q': 0.9})])
# b'gzip, br;q=0.9'

serialize_dictionary({'u': (2, {}), 'i': (True, {})})
# b'u=2, i'

serialize_item(Date(1659578233))
# b'@1659578233'
```

Serialisation validates its input — an integer beyond ±(10¹⁵−1), an
invalid Token, or a non-ASCII String raises `ValueError` instead of
emitting a malformed field.  An empty List or Dictionary serialises to
`b''`; per RFC 9651 §3.1 you should then omit the field entirely.

Parsing functions (`parse_item`, `parse_list`, `parse_dictionary`) are
also available directly when you have a raw value in hand; they raise
`ValueError` on any grammar violation.

## Where the framework itself uses it

The RFC 9218 priority surfaces are parsed with this module: the
`Priority` request header and the HTTP/2 `PRIORITY_UPDATE` frame
payload both flow through `parse_priority_field`, and the result lands
on the scope as described in [HTTP/2 → Priority hints](http2.md#priority-hints).
Out-of-range or mistyped members (`u=9`, `i=1`) are ignored per
RFC 9218 §4, and a field value that fails SF parsing falls back to the
defaults (`urgency=3`, `incremental=False`).
