"""RFC 9651 — Structured Field Values for HTTP.

Strict parser and serialiser for the three top-level Structured Field
types (Item, List, Dictionary), implementing the normative algorithms of
RFC 9651 §4 (obsoletes RFC 8941; adds the Date and Display String types).

Python type mapping (bare items):

| SF type        | Python type          |
|----------------|----------------------|
| Integer        | ``int``              |
| Decimal        | ``float``            |
| String         | ``str``              |
| Token          | `Token` (str subclass) |
| Byte Sequence  | ``bytes``            |
| Boolean        | ``bool``             |
| Date           | `Date`               |
| Display String | `DisplayString` (str subclass) |

Structures:

- **Item** — ``(bare_item, parameters)`` tuple; parameters are a
  ``dict[str, bare_item]`` preserving order.
- **Inner List** — ``(list_of_items, parameters)`` tuple.
- **List** — ``list`` of Items / Inner Lists.
- **Dictionary** — ``dict[str, Item | InnerList]`` preserving order.

Parsing is strict per RFC 9651 §1.1: any violation raises ``ValueError``
and the caller is expected to ignore the entire field value (see
``Headers.get_sf_item`` / ``get_sf_list`` / ``get_sf_dict``, which return
``None`` in that case).  Serialisation likewise raises ``ValueError`` on
out-of-range or mistyped input.

An empty List / Dictionary serialises to ``b''``; per RFC 9651 §3.1 the
field should then be omitted entirely, which is the caller's job.
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TypeAlias

import base64
import binascii
import string


class Token(str):
    """RFC 9651 Token — a short textual identifier (str subclass).

    Kept distinct from `str` (SF String) because the two serialise
    differently and RFC 9651 requires the distinction to be preserved.
    """
    __slots__ = ()

    def __repr__(self) -> str:
        return f'Token({str.__repr__(self)})'


class DisplayString(str):
    """RFC 9651 Display String — Unicode text, percent-encoded on the wire."""
    __slots__ = ()

    def __repr__(self) -> str:
        return f'DisplayString({str.__repr__(self)})'


class Date:
    """RFC 9651 Date — an integer number of Unix seconds (may pre-date 1970).

    Deliberately not a ``datetime``: the SF Date range (±10¹⁵−1 seconds)
    exceeds what ``datetime`` can represent.  Use `to_datetime` for values
    in the representable range.
    """
    __slots__ = ('seconds',)

    def __init__(self, seconds: int):
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise TypeError(f'Date takes an int, not {type(seconds).__name__}')
        self.seconds = seconds

    def __eq__(self, other) -> bool:
        return isinstance(other, Date) and other.seconds == self.seconds

    def __hash__(self) -> int:
        return hash((Date, self.seconds))

    def __repr__(self) -> str:
        return f'Date({self.seconds})'

    def to_datetime(self) -> datetime:
        """Return the date as an aware UTC ``datetime``."""
        return datetime.fromtimestamp(self.seconds, tz=timezone.utc)

    @classmethod
    def from_datetime(cls, dt: datetime) -> 'Date':
        """Build a `Date` from a ``datetime`` (naive values are taken as UTC)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return cls(int(dt.timestamp()))


BareItem: TypeAlias = bool | int | float | str | bytes | Token | DisplayString | Date
Parameters: TypeAlias = dict[str, BareItem]
Item: TypeAlias = tuple[BareItem, Parameters]
InnerList: TypeAlias = tuple[list[Item], Parameters]
SFList: TypeAlias = list[Item | InnerList]
SFDictionary: TypeAlias = dict[str, Item | InnerList]

_DIGITS = frozenset(string.digits)
_LCALPHA = frozenset(string.ascii_lowercase)
_ALPHA = frozenset(string.ascii_letters)
_KEY_START = _LCALPHA | {'*'}
_KEY_CHARS = _LCALPHA | _DIGITS | {'_', '-', '.', '*'}
_TOKEN_START = _ALPHA | {'*'}
# tchar (RFC 9110 §5.6.2) plus ':' and '/' (RFC 9651 §3.3.4)
_TOKEN_CHARS = _ALPHA | _DIGITS | set("!#$%&'*+-.^_`|~") | {':', '/'}
_BASE64_CHARS = frozenset(string.ascii_letters + string.digits + '+/=')
_LC_HEXDIG = frozenset(string.hexdigits.lower()) - frozenset('ABCDEF')


# ---------------------------------------------------------------------------
# Parsing (RFC 9651 §4.2)
# ---------------------------------------------------------------------------

class _Parser:
    """Cursor over the ASCII text of a field value."""
    __slots__ = ('s', 'pos')

    def __init__(self, s: str):
        self.s = s
        self.pos = 0

    def eof(self) -> bool:
        return self.pos >= len(self.s)

    def peek(self) -> str:
        """Next character, or '' at end of input."""
        return self.s[self.pos] if self.pos < len(self.s) else ''

    def advance(self) -> str:
        c = self.s[self.pos]
        self.pos += 1
        return c

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise ValueError(f'expected {char!r} at position {self.pos} in {self.s!r}')
        self.pos += 1

    def discard_sp(self) -> None:
        while self.peek() == ' ':
            self.pos += 1

    def discard_ows(self) -> None:
        while self.peek() in (' ', '\t'):
            self.pos += 1


def _to_text(value: bytes | bytearray | str) -> str:
    """§4.2 step 1 — the field value must be ASCII."""
    if isinstance(value, str):
        if not value.isascii():
            raise ValueError('structured field value must be ASCII')
        return value
    try:
        return bytes(value).decode('ascii')
    except UnicodeDecodeError as exc:
        raise ValueError('structured field value must be ASCII') from exc


def _parse_top(value: bytes | bytearray | str, parse_fn):
    """§4.2 — trim SP (only SP), parse, and require full consumption."""
    p = _Parser(_to_text(value))
    p.discard_sp()
    result = parse_fn(p)
    p.discard_sp()
    if not p.eof():
        raise ValueError(f'trailing characters at position {p.pos} in {p.s!r}')
    return result


def parse_item(value: bytes | bytearray | str) -> Item:
    """Parse a Structured Field Item (RFC 9651 §4.2.3).

    Returns ``(bare_item, parameters)``.  Raises ``ValueError`` on any
    violation of the RFC grammar.
    """
    return _parse_top(value, _parse_item)


def parse_list(value: bytes | bytearray | str) -> SFList:
    """Parse a Structured Field List (RFC 9651 §4.2.1).

    Multiple field lines must be combined (joined with ``b', '``) before
    calling.  Returns a list of Items / Inner Lists; raises ``ValueError``
    on any violation of the RFC grammar.
    """
    return _parse_top(value, _parse_list)


def parse_dictionary(value: bytes | bytearray | str) -> SFDictionary:
    """Parse a Structured Field Dictionary (RFC 9651 §4.2.2).

    Multiple field lines must be combined (joined with ``b', '``) before
    calling.  Returns an ordered ``dict`` of member name → Item / Inner
    List; raises ``ValueError`` on any violation of the RFC grammar.
    """
    return _parse_top(value, _parse_dictionary)


def _parse_list(p: _Parser) -> SFList:
    members: SFList = []
    while not p.eof():
        members.append(_parse_item_or_inner_list(p))
        p.discard_ows()
        if p.eof():
            return members
        p.expect(',')
        p.discard_ows()
        if p.eof():
            raise ValueError('trailing comma in list')
    return members


def _parse_dictionary(p: _Parser) -> SFDictionary:
    dictionary: SFDictionary = {}
    while not p.eof():
        key = _parse_key(p)
        if p.peek() == '=':
            p.advance()
            member = _parse_item_or_inner_list(p)
        else:
            # §4.2.2 — a valueless member means Boolean true, but its
            # parameters are still parsed.
            member = (True, _parse_parameters(p))
        # Duplicate keys: the last value wins, in the first occurrence's
        # position (dict assignment preserves original insertion order).
        dictionary[key] = member
        p.discard_ows()
        if p.eof():
            return dictionary
        p.expect(',')
        p.discard_ows()
        if p.eof():
            raise ValueError('trailing comma in dictionary')
    return dictionary


def _parse_item_or_inner_list(p: _Parser) -> Item | InnerList:
    if p.peek() == '(':
        return _parse_inner_list(p)
    return _parse_item(p)


def _parse_inner_list(p: _Parser) -> InnerList:
    p.expect('(')
    inner: list[Item] = []
    while not p.eof():
        p.discard_sp()
        if p.peek() == ')':
            p.advance()
            return (inner, _parse_parameters(p))
        inner.append(_parse_item(p))
        if p.peek() not in (' ', ')'):
            raise ValueError(f'invalid character in inner list at position {p.pos}')
    raise ValueError('unterminated inner list')


def _parse_item(p: _Parser) -> Item:
    bare = _parse_bare_item(p)
    return (bare, _parse_parameters(p))


def _parse_parameters(p: _Parser) -> Parameters:
    params: Parameters = {}
    while p.peek() == ';':
        p.advance()
        p.discard_sp()
        key = _parse_key(p)
        value: BareItem = True
        if p.peek() == '=':
            p.advance()
            value = _parse_bare_item(p)
        params[key] = value          # duplicate keys: last value wins
    return params


def _parse_key(p: _Parser) -> str:
    if p.peek() not in _KEY_START:
        raise ValueError(f'invalid key at position {p.pos} in {p.s!r}')
    start = p.pos
    while p.peek() in _KEY_CHARS:
        p.advance()
    return p.s[start:p.pos]


def _parse_bare_item(p: _Parser) -> BareItem:
    c = p.peek()
    if c == '' :
        raise ValueError('empty bare item')
    if c == '-' or c in _DIGITS:
        return _parse_number(p)
    if c == '"':
        return _parse_string(p)
    if c in _TOKEN_START:
        return _parse_token(p)
    if c == ':':
        return _parse_byte_sequence(p)
    if c == '?':
        return _parse_boolean(p)
    if c == '@':
        return _parse_date(p)
    if c == '%':
        return _parse_display_string(p)
    raise ValueError(f'invalid bare item at position {p.pos} in {p.s!r}')


def _parse_number(p: _Parser) -> int | float:
    """§4.2.4 — Integer or Decimal."""
    sign = 1
    if p.peek() == '-':
        p.advance()
        sign = -1
    if p.peek() not in _DIGITS:
        raise ValueError(f'invalid number at position {p.pos} in {p.s!r}')
    digits = []
    is_decimal = False
    while not p.eof():
        c = p.peek()
        if c in _DIGITS:
            digits.append(p.advance())
        elif not is_decimal and c == '.':
            if len(digits) > 12:
                raise ValueError('decimal integer component too long')
            digits.append(p.advance())
            is_decimal = True
        else:
            break
        if not is_decimal and len(digits) > 15:
            raise ValueError('integer too long')
        if is_decimal and len(digits) > 16:
            raise ValueError('decimal too long')
    text = ''.join(digits)
    if not is_decimal:
        return sign * int(text)
    int_part, _, frac_part = text.partition('.')
    if not frac_part:
        raise ValueError('decimal ends in "."')
    if len(frac_part) > 3:
        raise ValueError('decimal fractional component too long')
    return sign * float(text)


def _parse_string(p: _Parser) -> str:
    p.expect('"')
    out = []
    while not p.eof():
        c = p.advance()
        if c == '\\':
            if p.eof():
                raise ValueError('unterminated escape in string')
            nxt = p.advance()
            if nxt not in ('"', '\\'):
                raise ValueError(f'invalid escape \\{nxt} in string')
            out.append(nxt)
        elif c == '"':
            return ''.join(out)
        elif not (0x20 <= ord(c) <= 0x7E):
            raise ValueError('invalid character in string')
        else:
            out.append(c)
    raise ValueError('unterminated string')


def _parse_token(p: _Parser) -> Token:
    if p.peek() not in _TOKEN_START:
        raise ValueError(f'invalid token at position {p.pos} in {p.s!r}')
    start = p.pos
    while p.peek() in _TOKEN_CHARS:
        p.advance()
    return Token(p.s[start:p.pos])


def _parse_byte_sequence(p: _Parser) -> bytes:
    p.expect(':')
    end = p.s.find(':', p.pos)
    if end == -1:
        raise ValueError('unterminated byte sequence')
    b64 = p.s[p.pos:end]
    p.pos = end + 1
    if not set(b64) <= _BASE64_CHARS:
        raise ValueError('invalid character in byte sequence')
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f'invalid base64 in byte sequence: {exc}') from exc


def _parse_boolean(p: _Parser) -> bool:
    p.expect('?')
    c = p.peek()
    if c == '1':
        p.advance()
        return True
    if c == '0':
        p.advance()
        return False
    raise ValueError(f'invalid boolean at position {p.pos} in {p.s!r}')


def _parse_date(p: _Parser) -> Date:
    p.expect('@')
    value = _parse_number(p)
    if isinstance(value, float):
        raise ValueError('date must be an integer number of seconds')
    return Date(value)


def _parse_display_string(p: _Parser) -> DisplayString:
    p.expect('%')
    p.expect('"')
    raw = bytearray()
    while not p.eof():
        c = p.advance()
        o = ord(c)
        if o <= 0x1F or o >= 0x7F:
            raise ValueError('invalid character in display string')
        if c == '%':
            if len(p.s) - p.pos < 2:
                raise ValueError('truncated percent escape in display string')
            hi, lo = p.advance(), p.advance()
            if hi not in _LC_HEXDIG or lo not in _LC_HEXDIG:
                raise ValueError('invalid percent escape in display string')
            raw.append(int(hi + lo, 16))
        elif c == '"':
            try:
                return DisplayString(raw.decode('utf-8'))
            except UnicodeDecodeError as exc:
                raise ValueError('display string is not valid UTF-8') from exc
        else:
            raw.append(o)
    raise ValueError('unterminated display string')


# ---------------------------------------------------------------------------
# Serialisation (RFC 9651 §4.1)
# ---------------------------------------------------------------------------

def serialize_item(bare_item: BareItem, params: Parameters | None = None) -> bytes:
    """Serialise a Structured Field Item (RFC 9651 §4.1.3) to ASCII bytes.

    Raises ``ValueError`` for out-of-range values (e.g. an integer beyond
    ±(10¹⁵−1)) or types with no SF mapping.
    """
    return (_ser_bare_item(bare_item) + _ser_parameters(params or {})).encode('ascii')


def serialize_list(members: SFList) -> bytes:
    """Serialise a Structured Field List (RFC 9651 §4.1.1) to ASCII bytes.

    An empty list yields ``b''`` — per RFC 9651 §3.1 the field should then
    not be emitted at all.
    """
    return ', '.join(_ser_item_or_inner_list(m) for m in members).encode('ascii')


def serialize_dictionary(members: SFDictionary) -> bytes:
    """Serialise a Structured Field Dictionary (RFC 9651 §4.1.2) to ASCII bytes.

    An empty dictionary yields ``b''`` — per RFC 9651 §3.1 the field should
    then not be emitted at all.
    """
    out = []
    for key, member in members.items():
        prefix = _ser_key(key)
        value, params = member
        if value is True:
            out.append(prefix + _ser_parameters(params))
        else:
            out.append(prefix + '=' + _ser_item_or_inner_list(member))
    return ', '.join(out).encode('ascii')


def _ser_item_or_inner_list(member: Item | InnerList) -> str:
    value, params = member
    if isinstance(value, list):
        inner = ' '.join(_ser_bare_item(b) + _ser_parameters(p) for b, p in value)
        return f'({inner})' + _ser_parameters(params)
    return _ser_bare_item(value) + _ser_parameters(params)


def _ser_parameters(params: Parameters) -> str:
    out = []
    for key, value in params.items():
        out.append(';' + _ser_key(key))
        if value is not True:
            out.append('=' + _ser_bare_item(value))
    return ''.join(out)


def _ser_key(key: str) -> str:
    if not key or key[0] not in _KEY_START or not set(key) <= _KEY_CHARS:
        raise ValueError(f'invalid key: {key!r}')
    return key


def _ser_bare_item(value: BareItem) -> str:
    # bool must be tested before int (bool is an int subclass), and
    # Token / DisplayString before str.
    if isinstance(value, bool):
        return '?1' if value else '?0'
    if isinstance(value, int):
        return _ser_integer(value)
    if isinstance(value, float):
        return _ser_decimal(value)
    if isinstance(value, Token):
        return _ser_token(value)
    if isinstance(value, DisplayString):
        return _ser_display_string(value)
    if isinstance(value, str):
        return _ser_string(value)
    if isinstance(value, (bytes, bytearray)):
        return _ser_byte_sequence(bytes(value))
    if isinstance(value, Date):
        return '@' + _ser_integer(value.seconds)
    raise ValueError(f'type {type(value).__name__} has no structured-field mapping')


def _ser_integer(value: int) -> str:
    if not -999_999_999_999_999 <= value <= 999_999_999_999_999:
        raise ValueError(f'integer out of structured-field range: {value}')
    return str(value)


def _ser_decimal(value: float) -> str:
    # §4.1.5 — round the fractional component to 3 digits, half to even.
    # Decimal(repr(...)) preserves the shortest-round-trip literal, avoiding
    # binary-float artifacts like 0.0025 * 1000 == 2.5000000000000004.
    try:
        quantized = Decimal(repr(value)).quantize(
            Decimal('0.001'), rounding=ROUND_HALF_EVEN)
    except ArithmeticError as exc:
        raise ValueError(f'cannot serialize decimal: {value!r}') from exc
    sign, digits, exponent = quantized.as_tuple()
    if len(digits) + exponent > 12:          # integer component length
        raise ValueError(f'decimal integer component too long: {value!r}')
    text = str(quantized)
    text = text.rstrip('0')
    if text.endswith('.'):
        text += '0'
    return text


def _ser_string(value: str) -> str:
    out = ['"']
    for c in value:
        if not (0x20 <= ord(c) <= 0x7E):
            raise ValueError('string contains characters outside %x20-7e')
        if c in ('"', '\\'):
            out.append('\\')
        out.append(c)
    out.append('"')
    return ''.join(out)


def _ser_token(value: Token) -> str:
    if not value or value[0] not in _TOKEN_START or not set(value) <= _TOKEN_CHARS:
        raise ValueError(f'invalid token: {str(value)!r}')
    return str(value)


def _ser_byte_sequence(value: bytes) -> str:
    return ':' + base64.b64encode(value).decode('ascii') + ':'


def _ser_display_string(value: DisplayString) -> str:
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as exc:      # lone surrogates
        raise ValueError('display string is not valid Unicode') from exc
    out = ['%"']
    for byte in encoded:
        if byte in (0x25, 0x22) or not 0x20 <= byte <= 0x7E:
            out.append(f'%{byte:02x}')
        else:
            out.append(chr(byte))
    out.append('"')
    return ''.join(out)
