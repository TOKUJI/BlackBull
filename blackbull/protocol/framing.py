"""RFC 9112 §6 — how a message declares its body length.

Whatever side reads or writes it answers ``Content-Length`` identically, so
the answer is here once: one value, written once, in ``1*DIGIT``, agreed on by
every occurrence.  A message that carries two boundaries does not have one.
"""
from collections.abc import Iterable
from http import HTTPMethod


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
