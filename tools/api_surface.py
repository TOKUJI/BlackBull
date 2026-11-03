"""Report breaks in BlackBull's public API between two git refs.

The public API is the ``__all__`` of each package that ``pyproject.toml``
declares under ``[tool.blackbull] public-packages``; an exported module adds
its own ``__all__``.  The code and the declaration are both read from git at
each ref, and what BASE declares is what is compared, so a change cannot
narrow the surface it is judged by.

    python -m tools.api_surface diff BASE [HEAD]   # exit 1 on any break
    python -m tools.api_surface release TAG        # exit 1 when TAG keeps MINOR over a break

Exit 2 is an error, never a verdict.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tomllib

import griffe
# find_breaking_changes compares only an object's members; this compares the
# exported object itself, so a changed function signature is seen too.
from griffe._internal.diff import _type_based_yield


def declaration(pyproject: str) -> list[str] | None:
    return tomllib.loads(pyproject).get('tool', {}).get('blackbull', {}).get('public-packages')


def _declared(repo: Path, ref: str) -> list[str] | None:
    return declaration(subprocess.run(
        ['git', 'show', f'{ref}:pyproject.toml'], cwd=repo, check=True,
        capture_output=True, text=True, timeout=30).stdout)


def _load(repo: Path, ref: str, top: str):
    # Source only: the ref may be an untrusted pull request, whose code must not run.
    return griffe.load_git(top, ref=ref, repo=repo, allow_inspection=False,
                           resolve_aliases=True, resolve_external=False)


def _exports(obj) -> list[str] | None:
    return None if obj.exports is None else [str(name) for name in obj.exports]


def _explain(breakage) -> str:
    param = next((v for v in (breakage.old_value, breakage.new_value)
                  if isinstance(v, griffe.Parameter)), None)
    where = f'{breakage.obj.path}({param.name})' if param else breakage.obj.path
    return f'{where}: {breakage.kind.value}'


def _walk(old, new, path: str, out: list[str]) -> None:
    exported = set(_exports(new) or ())
    for name in _exports(old) or ():
        qualified = f'{path}.{name}'
        if name not in exported or name not in new.members:
            out.append(f'{qualified}: Public object was removed')
            continue
        before, after = old[name], new[name]
        if before.is_module:
            if _exports(before) is not None:
                _walk(before, after, qualified, out)
            continue
        out.extend(f'{qualified}: {_explain(b)}'
                   for b in _type_based_yield(before, after, seen_paths=set()))


def breaks(repo: Path, base: str, head: str, packages: list[str] | None = None) -> list[str]:
    out: list[str] = []
    if packages is None:
        after = _declared(repo, head) or []
        # Only the change that introduces the declaration is judged by its own.
        packages = _declared(repo, base) or after
        out += [f'{p}: no longer declared public' for p in packages if p not in after]
        if not packages:
            raise ValueError(f'neither {base} nor {head} declares [tool.blackbull] public-packages')
    top = packages[0].split('.')[0]
    old, new = _load(repo, base, top), _load(repo, head, top)
    for package in packages:
        inner = package.split('.')[1:]
        before, after = old, new
        for part in inner:
            before, after = before[part], after[part]
        if _exports(before) is None:
            raise ValueError(f'{package} declares no __all__ at {base}')
        _walk(before, after, package, out)
    return out


_VERSION = re.compile(r'v(\d+)\.(\d+)\.(\d+)')


def _version(tag: str) -> tuple[int, int]:
    match = _VERSION.match(tag)
    if match is None:
        raise ValueError(f'{tag} is not a vMAJOR.MINOR.PATCH tag')
    return int(match[1]), int(match[2])


def release(repo: Path, tag: str, packages: list[str] | None = None) -> tuple[str, list[str]]:
    """The previous release tag, and the breaks *tag* ships without raising MINOR."""
    previous = subprocess.run(
        ['git', 'describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*', f'{tag}^'],
        cwd=repo, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if _version(tag) > _version(previous):
        return previous, []
    return previous, breaks(repo, previous, tag, packages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m tools.api_surface')
    parser.add_argument('--repo', type=Path, default=Path('.'))
    parser.add_argument('--package', action='append', dest='packages')
    sub = parser.add_subparsers(dest='command', required=True)
    d = sub.add_parser('diff')
    d.add_argument('base')
    d.add_argument('head', nargs='?', default='HEAD')
    r = sub.add_parser('release')
    r.add_argument('tag')
    args = parser.parse_args(argv)

    try:
        if args.command == 'diff':
            found = breaks(args.repo, args.base, args.head, args.packages)
            heading = f'Public API breaks from {args.base} to {args.head}: the next release must raise MINOR.'
        else:
            previous, found = release(args.repo, args.tag, args.packages)
            heading = f'{args.tag} keeps the MINOR of {previous} but breaks the public API:'
    except Exception as exc:  # noqa: BLE001 — any failure is reported as an error, not a verdict
        target = f'{args.base}..{args.head}' if args.command == 'diff' else args.tag
        detail = getattr(exc, 'stderr', None) or exc
        print(f'Error: the public API of {target} could not be compared: {type(exc).__name__}: {detail}')
        return 2
    if found:
        print(heading, *(f'- {line}' for line in found), sep='\n')
        return 1
    print('No public API break.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
