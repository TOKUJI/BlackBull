#!/usr/bin/env python3
"""Which comments could a name have carried, and which must stay?

`prose_census.py` counts prose.  This says what *kind* it is, so "renaming
does not pay" stops being an assertion.  Four reduction passes made that claim
without evidence; measured afterwards, it held for two shapes and not for the
third:

======================================  ===========  ====================
shape                                     population  real opportunities
======================================  ===========  ====================
comment above an assignment                112 blocks  ~2, both partial
a number whose meaning is in a comment              22  0 -- all already
                                                        named constants
comment above an ``if``/``while``            49 blocks  15 candidates,
                                                        5 taken so far
======================================  ===========  ====================

Only the third shape is worth a pass, so only it is classified here:

``DO-NOT-EXTRACT``
    the block records a decision *against* extraction (hot path, inlined
    deliberately).  The comment stays and no name is made -- it is the
    "trade-off decided against" class, and the decision is the content.
``CITATION``
    the block leads with an RFC section or a CVE.  The citation stays.
``CANDIDATE``
    neither.  A name may be able to carry it -- a reader still decides.

Two heuristics were wrong before they were right, and both cost a pass:

* **loop depth.** A site inside a loop is not per-request but per-iteration;
  binding a local there multiplies by the iteration count.  Reported.
* **suite attribution.** A comment that is the first thing inside an
  ``except``/``else``/``finally`` suite annotates *that suite* -- usually the
  reason an exception is swallowed -- and only looks like it belongs to the
  statement beneath.  Three false candidates came from binding it to a
  following ``if`` by adjacency.  Now excluded.

The output is a worklist for a human or an agent, never a verdict::

    comment_shapes.py blackbull/server/http1_actor.py
    comment_shapes.py blackbull/server/*.py
"""
import ast, io, pathlib, re, sys, tokenize

NO_EXTRACT = re.compile(r'\binlin(e|ed|ing)\b|\bhot path\b|per-request|per-frame|'
                        r'\bcall overhead\b|executed instructions|cheaper than',
                        re.I)
CITE = re.compile(r'RFC\s*\d+|§\s*[\d.]+|CVE-')


def own_blocks(src, lines):
    own = {}
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type == tokenize.COMMENT and lines[t.start[0]-1].strip().startswith('#'):
            own[t.start[0]] = t.string
    out, seen = [], set()
    for ln in sorted(own):
        if ln in seen:
            continue
        end = ln
        while end + 1 in own:
            end += 1
            seen.add(end)
        out.append((ln, end, [own[i] for i in range(ln, end + 1)]))
    return out


def annotate(tree):
    """line -> (enclosing def, loop depth at that line)."""
    info = {}

    def walk(node, fn, depth):
        for child in ast.iter_child_nodes(node):
            f, d = fn, depth
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f, d = child.name, 0
            elif isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
                d = depth + 1
            lo = getattr(child, 'lineno', None)
            if lo is not None:
                # Descend last, so an inner def/loop overwrites the outer one:
                # the innermost owner is the one that decides cost.
                for ln in range(lo, (getattr(child, 'end_lineno', lo) or lo) + 1):
                    info[ln] = (f, d)
            walk(child, f, d)

    walk(tree, '<module>', 0)
    return info


for path in sys.argv[1:]:
    src = pathlib.Path(path).read_text()
    lines = src.splitlines()
    tree = ast.parse(src)
    conds = {n.lineno for n in ast.walk(tree) if isinstance(n, (ast.If, ast.While))}
    info = annotate(tree)

    def opens_a_suite(ln):
        """Does the line before *ln* end a compound-statement header?

        A comment that is the first thing inside an ``except``/``else``/
        ``finally``/``with``/``for`` suite annotates *that suite* -- most
        often the reason an exception is swallowed -- and only appears to
        belong to the statement beneath it.  Binding it to a following
        ``if`` by adjacency produced three false candidates across passes 5
        and 6 before this check existed.
        """
        for prev in range(ln - 1, 0, -1):
            t = lines[prev - 1].strip()
            if not t:
                continue
            return t.endswith(':')
        return False

    rows = []
    for start, end, texts in own_blocks(src, lines):
        if end + 1 not in conds:
            continue
        if opens_a_suite(start):
            continue
        body = ' '.join(texts)
        fn, depth = info.get(end + 1, ('<module>', 0))
        verdict = ('DO-NOT-EXTRACT' if NO_EXTRACT.search(body)
                   else 'CITATION' if CITE.search(texts[0]) else 'CANDIDATE')
        rows.append((start, end - start + 1, fn, depth, verdict, texts[0].strip()[:62]))

    from collections import Counter
    tally = Counter(r[4] for r in rows)
    print(f'\n=== {path}   ({len(rows)} blocks above an if/while)')
    for v in ('DO-NOT-EXTRACT', 'CITATION', 'CANDIDATE'):
        print(f'    {v:16} {tally.get(v, 0)}')
    print(f'\n    {"line":7} {"L":3} {"loop":5} {"enclosing":30} first line')
    for start, n, fn, depth, verdict, txt in rows:
        if verdict == 'CANDIDATE':
            loop = f'x{depth}' if depth else '-'
            print(f'    :{start:<6} {n}L  {loop:5} {fn:30} {txt}')
