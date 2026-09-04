# BlackBull development commands — run with `just` (https://github.com/casey/just)
# Install: uv tool install just

# Install all dependencies including optional extras
install:
    uv sync --all-extras

# Run the full test suite
test:
    uv run pytest -q -n auto

# Type-check with beartype instrumentation
typecheck:
    uv run pytest --beartype-packages=blackbull --timeout=30 -q --tb=short -n auto

# Build and serve docs locally
docs:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

# Build docs strictly (CI mode)
docs-build:
    DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict

# YouTrack REST access. Credentials are read only by scripts/youtrack.sh.
yt-search query='project: BLA #Unresolved':
    scripts/youtrack.sh search "{{query}}"

yt-show issue:
    scripts/youtrack.sh show "{{issue}}"

yt-create summary description:
    scripts/youtrack.sh create "{{summary}}" "{{description}}"

yt-comment issue text:
    scripts/youtrack.sh comment "{{issue}}" "{{text}}"

# Replace an issue's description with the contents of a file
yt-update issue description_file:
    scripts/youtrack.sh update "{{issue}}" "{{description_file}}"

# Read a Knowledge Base article (BLA-A-<n>); no id lists them all
yt-article article='':
    scripts/youtrack.sh article {{article}}

yt-command issue command:
    scripts/youtrack.sh command "{{issue}}" "{{command}}"

yt-close issue:
    scripts/youtrack.sh close "{{issue}}"
