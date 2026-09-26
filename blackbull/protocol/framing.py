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


def is_informational(status: int | str) -> bool:
    """Whether *status* is an informational (1xx) response — RFC 9110 §15."""
    return 100 <= int(status) < 200


def method_is(method: str | bytes | HTTPMethod | None, expected: str) -> bool:
    """Whether *method* is exactly *expected*.

    RFC 9110 §9.1 makes a method name case-sensitive, so `head` is not
    `HEAD` and folding the two together changes whether a response may
    carry content.
    """
    if isinstance(method, bytes):
        return method == expected.encode('ascii')
    return method is not None and str(method) == expected


def response_has_content(method: str | bytes | HTTPMethod | None,
                         status: int) -> bool:
    """Whether a response to *method* with *status* may carry content.

    RFC 9110 §9.3.2: a HEAD response and a 204 or 304 carry none, whatever
    they declare. An informational response carries none either (§15.2),
    but only because it is not the response at all yet.
    """
    return (not is_informational(status) and status not in (204, 304)
            and not method_is(method, 'HEAD'))
