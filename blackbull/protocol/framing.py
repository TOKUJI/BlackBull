"""RFC 9112 §6 — how a message declares its body length.

Whatever side reads or writes it answers ``Content-Length`` identically, so
the answer is here once: one value, written once, in ``1*DIGIT``, agreed on by
every occurrence.  A message that carries two boundaries does not have one.
"""
from collections.abc import Iterable


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
