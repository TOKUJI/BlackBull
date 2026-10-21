"""Write the environment-variable tables in the reference page from the code.

Each ``Settings`` and ``AppConfig`` field declared with ``blackbull.env.setting``
holds one variable: its name, default, section and description.  This renders
them into ``docs/reference/env-vars.md`` between the markers, leaving the
page's own prose untouched.

Run it with ``--check`` to fail when the committed page is not what the code
would produce.
"""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blackbull.config import AppConfig  # noqa: E402
from blackbull.env import Settings  # noqa: E402

PAGE = ROOT / 'docs' / 'reference' / 'env-vars.md'
SOURCE = 'the Settings and AppConfig declarations'

BEGIN = '<!-- generated: {} -->'
END = '<!-- /generated -->'
HEADER = '| Variable | Default | Controls |\n|---|---|---|'


def sections() -> list[tuple[str, list[tuple[str, str, object, str]]]]:
    """``(section, [(variable, shown, default, description)])`` in declaration order."""
    out: dict[str, list] = {}
    for cls in (Settings, AppConfig):
        for f in dataclasses.fields(cls):
            meta = f.metadata
            if 'env' not in meta:
                continue
            out.setdefault(meta['section'], []).append(
                (meta['env'], meta['shown'], f.default, meta['doc']))
    return list(out.items())


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


def table(entries: list[tuple[str, str, object, str]]) -> str:
    rows = [HEADER]
    for name, shown, value, doc in entries:
        shown = shown if shown is not None else spelling(value)
        default = f'`{shown}`{gloss(value)}' if shown not in ('', 'None') else '*(unset)*'
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
            print(f'{PAGE.relative_to(ROOT)} matches {SOURCE}')
            return 0
        print(f'{PAGE.relative_to(ROOT)} is not what {SOURCE} '
              f'produces.  Run: uv run python scripts/gen_env_docs.py')
        return 1
    PAGE.write_text(wanted, encoding='utf-8')
    print(f'wrote {PAGE.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
