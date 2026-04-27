"""Auto-generate API reference pages from blackbull source modules.

Executed by mkdocs-gen-files at every build.  Walks ``blackbull/`` and writes
one Markdown file per public Python module containing a single mkdocstrings
``:::`` directive.  A ``SUMMARY.md`` is also written for mkdocs-literate-nav
to build the navigation tree automatically.

Adding or removing a source file is reflected in the next build without any
manual changes to ``mkdocs.yml`` or stub files.
"""
from pathlib import Path
import mkdocs_gen_files

src_root = Path("blackbull")
nav = mkdocs_gen_files.Nav()

for path in sorted(src_root.rglob("*.py")):
    module_parts = list(path.with_suffix("").parts)

    # Skip private modules (names starting with '_'), except __init__
    if any(part.startswith("_") and part != "__init__" for part in module_parts):
        continue

    # __init__ → represents the package itself; drop the trailing __init__
    if module_parts[-1] == "__init__":
        module_parts = module_parts[:-1]

    if not module_parts:
        continue

    doc_path = Path(*module_parts).with_suffix(".md")
    full_doc_path = Path("api", doc_path)
    module_ident = ".".join(module_parts)

    with mkdocs_gen_files.open(full_doc_path, "w") as f:
        f.write(f"# `{module_ident}`\n\n")
        f.write(f"::: {module_ident}\n")

    mkdocs_gen_files.set_edit_path(full_doc_path, path)
    nav[module_parts] = str(doc_path)

# Write the navigation summary file consumed by mkdocs-literate-nav
with mkdocs_gen_files.open("api/SUMMARY.md", "w") as f:
    f.writelines(nav.build_literate_nav())
