"""Write the environment-variable tables in the reference page from the code.

``blackbull/_env_vars.py`` holds every configurable variable: its default as
the value, its description as the attribute docstring.  This renders those
into ``docs/reference/env-vars.md`` between the markers, leaving the page's
own prose -- the intros, the client narrative, the recommendations table --
untouched.

Run it with ``--check`` to fail when the committed page is not what the code
would produce.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'blackbull' / '_env_vars.py'
PAGE = ROOT / 'docs' / 'reference' / 'env-vars.md'

BEGIN = '<!-- generated: {} -->'
END = '<!-- /generated -->'
HEADER = '| Variable | Default | Controls |\n|---|---|---|'


def sections() -> list[tuple[str, list[tuple[str, object, str]]]]:
    """``(section, [(variable, default, description)])`` in source order."""
    src = SOURCE.read_text(encoding='utf-8')
    tree = ast.parse(src)
    lines = src.splitlines()
    ns: dict[str, object] = {}
    exec(compile(tree, str(SOURCE), 'exec'), ns)  # noqa: S102 - our own module
    COMPUTED.update(ns.get('COMPUTED_DEFAULTS', {}))

    out: list[tuple[str, list]] = []
    current: str | None = None
    body = tree.body
    for index, node in enumerate(body):
        if not isinstance(node, ast.Assign):
            continue
        name = node.targets[0].id
        if not name.startswith(('BB_', 'BLACKBULL_')):
            continue
        for above in range(node.lineno - 2, 0, -1):
            text = lines[above].strip()
            if text.startswith('# --- '):
                current = text[6:].rstrip('- ').strip()
                break
            if text and not text.startswith('#'):
                break
        # The description is the string statement that follows the assignment
        # -- an attribute docstring, which is what griffe reads too.
        nxt = body[index + 1] if index + 1 < len(body) else None
        doc = (nxt.value.value if isinstance(nxt, ast.Expr)
               and isinstance(nxt.value, ast.Constant)
               and isinstance(nxt.value.value, str) else '')
        if not out or out[-1][0] != current:
            out.append((current or '', []))
        out[-1][1].append((name, ns[name], doc.strip()))
    return out


COMPUTED: dict[str, str] = {}


def spelling(value: object) -> str:
    """The value as a reader would type it into a shell.

    The module holds what the code parses to; the page documents what you set.
    A ``bool`` is ``1`` or ``0`` on the command line, never ``True``.
    """
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def gloss(value: object) -> str:
    """``(30 MiB)`` for a byte count a reader would otherwise have to divide.

    Mebibytes only.  Glossing every multiple of 1024 puts ``(8 KiB)`` beside
    ``8192``, which tells a reader nothing they could not see.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return ''
    if value >= 1024 ** 2 and value % (1024 ** 2) == 0:
        return f' ({value // 1024 ** 2} MiB)'
    return ''


def cell(text: str) -> str:
    return text.replace('|', r'\|')


def table(entries: list[tuple[str, object, str]]) -> str:
    rows = [HEADER]
    for name, value, doc in entries:
        computed = COMPUTED.get(name)
        shown = computed if computed is not None else spelling(value)
        default = f'`{shown}`{gloss(value)}' if shown != '' else '*(unset)*'
        rows.append(f'| `{name}` | {default} | {cell(doc)} |')
    return '\n'.join(rows)


def render(page: str) -> str:
    for section, entries in sections():
        begin = BEGIN.format(section)
        if begin not in page:
            raise SystemExit(f'{PAGE.name} has no marker for section {section!r}.\n'
                             f'Add:\n{begin}\n{END}')
        head, rest = page.split(begin, 1)
        _, tail = rest.split(END, 1)
        page = f'{head}{begin}\n{table(entries)}\n{END}{tail}'
    return page


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='fail if the page is not what the code produces')
    args = ap.parse_args()
    current = PAGE.read_text(encoding='utf-8')
    wanted = render(current)
    if args.check:
        if current == wanted:
            print(f'{PAGE.relative_to(ROOT)} matches {SOURCE.relative_to(ROOT)}')
            return 0
        print(f'{PAGE.relative_to(ROOT)} is not what {SOURCE.relative_to(ROOT)} '
              f'produces.  Run: uv run python scripts/gen_env_docs.py')
        return 1
    PAGE.write_text(wanted, encoding='utf-8')
    print(f'wrote {PAGE.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
