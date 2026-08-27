"""A self-referencing dataclass must terminate the schema walk.

`_type_to_schema` recurses into `_dataclass_to_schema`, which walks each
field back through `_type_to_schema`.  With no record of what is already
being walked, a type that refers to itself never bottoms out.

A single self-reference (`Node.next: Node | None`) happened to stop: the
recursion hit a `RecursionError` deep inside `get_type_hints`, which the
bare `except Exception` swallowed, leaving a 332-level, 53 KB schema.  A
double self-reference (a binary tree) has no such accident — it branches
2^depth.

The gate is **termination**, not speed.  Asserting "under N seconds" would
pass on a slow-but-exponential walk on a fast machine and fail on a fast
linear one under load; asserting the shape is bounded says what is meant.
"""
from __future__ import annotations

import dataclasses

import pytest

from blackbull.openapi import _type_to_schema


# Module level on purpose.  ``get_type_hints`` resolves a string annotation
# against the *defining module's* namespace: a dataclass declared inside a
# test function cannot resolve its own name, so the walk never recurses and
# the test passes while measuring nothing.  The first draft of this file did
# exactly that.
@dataclasses.dataclass
class Node:
    value: int
    next: 'Node | None' = None


@dataclasses.dataclass
class Tree:
    value: int
    left: 'Tree | None' = None
    right: 'Tree | None' = None


@dataclasses.dataclass
class B:
    a: 'A | None' = None


@dataclasses.dataclass
class A:
    b: 'B | None' = None


@dataclasses.dataclass
class Inner:
    x: int


@dataclasses.dataclass
class Outer:
    inner: Inner
    label: str


@dataclasses.dataclass
class Item:
    id: int
    name: str
    price: float
    tags: list[str]


@dataclasses.dataclass
class Point:
    x: int
    y: int


@dataclasses.dataclass
class Line:
    start: Point
    end: Point


def _measure(schema, depth=0, limit=200):
    """(max nesting depth, node count) of a schema object."""
    if depth > limit or not isinstance(schema, (dict, list)):
        # Bounded so the *measurement* cannot blow the stack on the very
        # input it is measuring, which is what happened when this was
        # unbounded against a 1000-deep schema.
        return depth, 1
    items = schema.values() if isinstance(schema, dict) else schema
    best, total = depth, 1
    for v in items:
        d, n = _measure(v, depth + 1, limit)
        best = max(best, d)
        total += n
    return best, total


class TestSelfReferenceTerminates:
    def test_a_single_self_reference(self):
        schema = _type_to_schema(Node)

        depth, nodes = _measure(schema)
        assert depth < 40, f'schema nested {depth} deep — the walk did not fold'
        assert nodes < 500, f'{nodes} schema nodes for a two-field dataclass'

    def test_a_double_self_reference(self):
        """The exponential case: a binary tree branches 2^depth."""
        schema = _type_to_schema(Tree)

        depth, nodes = _measure(schema)
        assert depth < 40, f'schema nested {depth} deep'
        assert nodes < 500, f'{nodes} schema nodes — the walk branched'

    def test_a_mutual_reference(self):
        """A cycle through two types, which neither type can see alone."""
        schema = _type_to_schema(A)

        depth, nodes = _measure(schema)
        assert depth < 40 and nodes < 500


class TestOrdinaryDataclassesAreUnchanged:
    """The cycle guard must not truncate schemas that do terminate."""

    def test_a_flat_dataclass_keeps_every_field(self):
        schema = _type_to_schema(Item)

        assert schema['type'] == 'object'
        assert set(schema['properties']) == {'id', 'name', 'price', 'tags'}
        assert schema['properties']['price']['type'] == 'number'
        assert schema['properties']['tags']['type'] == 'array'

    def test_nesting_that_terminates_is_kept(self):
        schema = _type_to_schema(Outer)

        assert schema['properties']['inner']['type'] == 'object'
        assert 'x' in schema['properties']['inner']['properties']

    def test_the_same_type_twice_is_not_mistaken_for_a_cycle(self):
        """Two sibling fields of one type are not recursion."""
        schema = _type_to_schema(Line)

        for side in ('start', 'end'):
            assert schema['properties'][side]['type'] == 'object', (
                f'{side} was folded away as if it were a cycle')
            assert 'x' in schema['properties'][side]['properties']
