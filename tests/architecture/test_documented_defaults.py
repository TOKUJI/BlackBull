"""Every default this tree states in prose is the default it ships.

A setting's default lives in its declaration, and the reference page is
generated from it; what is left to check is that the committed page and the
package docstring say what the code does.
"""
from __future__ import annotations

import pathlib

import blackbull


def test_package_docstring_states_the_import_side_effect_it_has():
    """``blackbull/__init__.py`` says the server stack loads; check that it does.

    This is the claim that drifted furthest — the docstring asserted the exact
    opposite for as long as nothing executed it.  The test does not require the
    coupling to stay: making the import lazy is a fine change, and this failing
    is how the sentence gets rewritten with it rather than a year later.
    """
    import subprocess
    import sys

    loaded = subprocess.run(
        [sys.executable, '-c',
         'import sys, blackbull; '
         "print(len([m for m in sys.modules if m.startswith('blackbull.server')]))"],
        capture_output=True, text=True, check=True).stdout.strip()

    doc = blackbull.__doc__ or ''
    claims_loaded = 'loads the server stack' in doc
    assert claims_loaded == (int(loaded) > 0), (
        f'`import blackbull` loads {loaded} blackbull.server.* modules, but '
        f'the package docstring {"claims it does" if claims_loaded else "does not say so"}.  '
        'Whichever of the two changed, change the other.')


def test_env_vars_md_is_what_the_code_produces():
    """The reference page is generated; check the committed copy is current.

    Every variable, its default and its description live in its ``Settings``
    or ``AppConfig`` declaration.  The page is an output of
    ``scripts/gen_env_docs.py``, so "does the page state the shipped default"
    is true by construction and the only question left is whether the file in
    git is the file the generator writes.
    """
    import importlib.util

    script = pathlib.Path(__file__).resolve().parents[2] / 'scripts' / 'gen_env_docs.py'
    spec = importlib.util.spec_from_file_location('gen_env_docs', script)
    gen = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gen)

    committed = gen.PAGE.read_text(encoding='utf-8')
    assert committed == gen.render(committed), (
        'docs/reference/env-vars.md is not what the declarations produce.  '
        'Run: uv run python scripts/gen_env_docs.py')
