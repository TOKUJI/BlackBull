"""RFC 9112 §6 — how a message declares its body length.

Whatever side reads or writes it answers ``Content-Length`` identically, so
the answer is here once: one value, written once, in ``1*DIGIT``, agreed on by
every occurrence.  A message that carries two boundaries does not have one.

"""
import re
from collections.abc import Iterable
from http import HTTPMethod

from .field_grammar import FIELD_VALUE_ALLOWED_OCTETS, TCHAR_OCTETS

__all__ = ('NO_CONTENT_GENERATED_STATUSES', 'NO_CONTENT_STATUSES',
           'TransferCoding', 'is_informational', 'method_is',
           'parse_content_length', 'parse_status', 'response_has_content',
           'split_transfer_codings')


def parse_content_length(fields: Iterable[tuple[bytes, bytes]]
                         ) -> int | None:
    """The length every ``Content-Length`` occurrence agrees on, or ``None``.

    Occurrences are ``#field-value`` lists too, so each member counts.  Raises
    ``ValueError`` when a member is not ``1*DIGIT`` or the members disagree;
    the message names the values it could not reconcile.
    """
    values: list[int] = []
    for _name, raw in fields:
        for member in raw.split(b','):
            value = member.strip(b' \t')
            if not value or not value.isdigit():
                raise ValueError(f'invalid Content-Length value: {value!r}')
            values.append(int(value))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValueError(
            f'conflicting Content-Length values: {sorted(set(values))!r}')
    return values[0]


#: One ``Transfer-Encoding`` member: the lowered coding and its parameters,
#: each value as written — a token or a quoted string, quotes kept.
TransferCoding = tuple[bytes, tuple[tuple[bytes, bytes], ...]]

#: RFC 9110 §5.6.1 lets a recipient ignore "a reasonable number" of empty
#: list members.  16, shared by every side that reads the list.
_MAX_EMPTY_TRANSFER_MEMBERS = 16

# RFC 9112 §5.5: transfer-coding = token *( OWS ";" OWS token BWS "=" BWS
# ( token / quoted-string ) ) — spelled once from the shared alphabets.
_TOKEN = rb'[' + re.escape(TCHAR_OCTETS) + rb']+'
_QUOTED = (rb'"(?:[' + re.escape(bytes(c for c in FIELD_VALUE_ALLOWED_OCTETS
                                  if c not in b'"\\')) + rb']|\\['
           + re.escape(FIELD_VALUE_ALLOWED_OCTETS) + rb'])*"')
_PLAIN_LIST_OCTETS = TCHAR_OCTETS + b' \t,'
_CODING = re.compile(_TOKEN)
_PARAMETER = re.compile(rb'[ \t]*;[ \t]*(' + _TOKEN + rb')[ \t]*=[ \t]*('
                        + _TOKEN + rb'|' + _QUOTED + rb')')


def split_transfer_codings(
        fields: Iterable[tuple[bytes, bytes]]) -> list[TransferCoding]:
    """Every ``Transfer-Encoding`` member as RFC 9112 §5.5 writes one, or
    ``ValueError``.

    Commas split, OWS off, the coding token lowered, the parameters kept
    rather than dropped — a policy that refuses ``chunked`` carrying a
    parameter sees one, a policy that reads only the names drops them as its
    own decision.  An empty member stays ``(b'', ())`` (``chunked, `` is not
    ``chunked`` to a sender rewriting the field; a reader ignores empties
    per RFC 9110 §5.6.1).  A member outside the grammar raises — a
    parameter this reading cannot see through must not hide a different
    final coding — as does a list over the empty-member bound.
    """
    members: list[TransferCoding] = []
    empties = 0
    for _name, value in fields:
        if not value.translate(None, _PLAIN_LIST_OCTETS):
            # Every octet is a token octet, OWS or a comma: no parameter and
            # no quoted string can hide behind one, so the plain comma split
            # reads the same members as the grammar below; the
            # OWS inside a member is what the strip and check are for.
            for raw in (value.split(b',') if b',' in value else (value,)):
                member = raw.strip(b' \t')
                if member.translate(None, TCHAR_OCTETS):
                    raise ValueError(
                        f'invalid Transfer-Encoding token {member!r}')
                if member:
                    members.append((member.lower(), ()))
                    continue
                empties += 1
                if empties > _MAX_EMPTY_TRANSFER_MEMBERS:
                    raise ValueError(
                        'too many empty Transfer-Encoding list members')
                members.append((b'', ()))
            continue
        pos = 0
        while True:
            pos = _skip_ows(value, pos)
            if pos == len(value) or value[pos] == 0x2c:  # empty member
                empties += 1
                if empties > _MAX_EMPTY_TRANSFER_MEMBERS:
                    raise ValueError(
                        'too many empty Transfer-Encoding list members')
                members.append((b'', ()))
                if pos == len(value):
                    break
                pos += 1
                continue
            coding = _CODING.match(value, pos)
            if coding is None:
                raise ValueError(
                    f'invalid Transfer-Encoding member at position {pos}')
            pos = coding.end()
            params: list[tuple[bytes, bytes]] = []
            while (param := _PARAMETER.match(value, pos)) is not None:
                params.append((param[1], param[2]))
                pos = param.end()
            pos = _skip_ows(value, pos)
            if pos < len(value) and value[pos] != 0x2c:  # comma
                raise ValueError(
                    f'invalid Transfer-Encoding list separator at position {pos}')
            members.append((coding[0].lower(), tuple(params)))
            if pos == len(value):
                break
            pos += 1
    return members


def _skip_ows(value: bytes, pos: int) -> int:
    while pos < len(value) and value[pos] in (0x20, 0x09):
        pos += 1
    return pos


def parse_status(value: str | bytes) -> int | None:
    """A status code as RFC 9110 §15 writes one, or ``None`` if it is not.

    Three ASCII digits and nothing else.  There is deliberately no 100-599
    bound: a range one transport enforces and the other does not is an accept
    set the two disagree on, and neither protocol draws one here.
    """
    if isinstance(value, bytes):
        try:
            value = value.decode('ascii')
        except UnicodeDecodeError:
            return None
    if len(value) != 3 or not value.isdigit() or not value.isascii():
        return None
    return int(value)


#: RFC 9112 §6.3 rule 1 — a response to HEAD and any of these has no body and
#: no trailer section, whatever the field sections say.
NO_CONTENT_STATUSES: frozenset[int] = frozenset(range(100, 200)) | {204, 304}
#: What a server may not generate content in. RFC 9110 §15.3.6 adds 205 to
#: the set above, and only to what it sends.
NO_CONTENT_GENERATED_STATUSES: frozenset[int] = NO_CONTENT_STATUSES | {205}


def is_informational(status: int) -> bool:
    """Whether *status* is an informational (1xx) response — RFC 9110 §15.

    An informational response is provisional: it shares its sender with the
    final response that must still follow, so it commits no status, completes
    no exchange, and carries no content framing.
    """
    return 100 <= status < 200


def method_is(method: str | bytes | HTTPMethod | None, expected: str) -> bool:
    """Whether *method* is exactly *expected*.

    RFC 9110 §9.1 makes a method name case-sensitive, so `head` is not
    `HEAD` and folding the two together changes whether a response may
    carry content.
    """
    if method.__class__ is str:
        return method == expected
    if method.__class__ is bytes:
        return method == expected.encode('ascii')
    return method is not None and str(method) == expected


def response_has_content(method: str | bytes | HTTPMethod | None,
                         status: int) -> bool:
    """How the body length of a response to *method* with *status* is found.

    RFC 9112 §6.3 rule 1: a response to HEAD (RFC 9110 §9.3.2) and any 1xx
    (§15.2), 204 (§15.3.5) or 304 (§15.4.5) has none whatever the field
    sections say, and an informational response is not the response yet.

    This is the framing question and it stops there. RFC 9110 §15.3.6 also
    forbids a *server* to generate content in a 205, but RFC 9112 §6.3 still
    frames one normally, so a peer that sends one has to be read to the
    length it declared — leaving those octets behind would desynchronise the
    reader from the very peer that was already misbehaving. The generation
    rule lives on the sender beside it, not in here.
    """
    return status not in NO_CONTENT_STATUSES and not method_is(method, 'HEAD')
