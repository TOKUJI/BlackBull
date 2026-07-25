# BlackBull development commands — run with `just` (https://github.com/casey/just)
# Install: uv tool install just

# Install all dependencies including optional extras
install:
    uv sync --all-extras

# Run the full test suite
test:
    uv run pytest -q

# Type-check with beartype instrumentation
typecheck:
    uv run pytest --beartype-packages=blackbull --timeout=30 -q --tb=short

# Build and serve docs locally
docs:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

# Build docs strictly (CI mode)
docs-build:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict
