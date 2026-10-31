# Python support

BlackBull supports the CPython versions that CPython itself still maintains:

- a new version is supported from the first BlackBull release after its
  final release;
- a version stops being supported in the first BlackBull release after its
  end of life.

The end of a version's support is announced on the
[Deprecations](deprecations.md) page and in `CHANGELOG.md`; running on that
version does not warn.

| Python | CPython end of life | BlackBull |
|---|---|---|
| 3.11 | 2027-10 | supported until the first release from October 2027 ([BBD009](deprecations.md)) |
| 3.12 | 2028-10 | supported |
| 3.13 | 2029-10 | supported |
| 3.14 | 2030-10 | supported |
| 3.15 | 2031-10 | supported from v0.81.1 |

The supported versions are the `Programming Language :: Python` classifiers
in `pyproject.toml`; a test keeps them, `requires-python`, the CI matrix and
this table in step.
