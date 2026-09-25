"""RFC 9112 §6 — how an outbound message declares its body length.

A request sender and a response sender answer ``Content-Length`` identically,
so the answer is here once: one value, written once, in ``1*DIGIT``, agreed on
by every occurrence.  A message that carries two boundaries does not have one.
"""
from collections.abc import Iterable


def parse_content_length(fields: Iterable[tuple[bytes, bytes]]
                         ) -> int | None:
    """The length every ``Content-Length`` occurrence agrees on, or ``None``.

    Occurrences are ``#field-value`` lists too, so each member counts.  Raises
    ``ValueError`` when a member is not ``1*DIGIT`` or the members disagree.
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
        raise ValueError('conflicting Content-Length values')
    return values[0]
