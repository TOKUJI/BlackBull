import multiprocessing
import os
import pathlib
import subprocess
import sys
import pytest
import pytest_asyncio


# The live-server fixtures bind the listening socket in the parent and then
# start a worker that serves on it, so the child needs the *inherited* socket
# — plus an app whose handlers are locally-defined closures.  Neither can be
# pickled, which is what any start method other than ``fork`` requires of a
# process target.  CPython 3.14 made ``forkserver`` the POSIX default, which
# turns every such fixture into a setup error; pin the method these fixtures
# were written against instead of restating it at each call site.
multiprocessing.set_start_method('fork', force=True)


DEFAULT_SKIPPED_MARKERS = {
    "integration": "--run-integration",
    "system": "--run-system",
    "production": "--run-production",
    "slow": "--run-slow",
    "docker": "--run-docker",
    "network": "--run-network",
}


def pytest_addoption(parser):
    parser.addoption(
        "--run-all",
        action="store_true",
        default=False,
        help="run all tests including default-skipped marker tests",
    )

    for marker_name, option_name in DEFAULT_SKIPPED_MARKERS.items():
        parser.addoption(
            option_name,
            action="store_true",
            default=False,
            help=f"run {marker_name} tests",
        )


def pytest_collection_modifyitems(config, items):
    markexpr = (config.option.markexpr or "").strip()

    # -m 指定時は pytest 標準の marker selection を優先
    if markexpr:
        return

    # 全量実行オプション
    if config.getoption("--run-all"):
        return

    enabled_markers = {
        marker_name
        for marker_name, option_name in DEFAULT_SKIPPED_MARKERS.items()
        if config.getoption(option_name)
    }

    disabled_markers = set(DEFAULT_SKIPPED_MARKERS) - enabled_markers
    if not disabled_markers:
        return

    for item in items:
        item_markers = {mark.name for mark in item.iter_markers()}
        blocked_markers = sorted(item_markers & disabled_markers)

        if blocked_markers:
            required_options = ", ".join(
                DEFAULT_SKIPPED_MARKERS[name] for name in blocked_markers
            )
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        f"skipped by default for marker(s): {', '.join(blocked_markers)}; "
                        f"enable with {required_options} or run with --run-all"
                    )
                )
            )


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear ``blackbull.env.get_settings`` cache around each test.

    ``get_settings()`` is ``@functools.cache``-decorated for performance;
    tests that mutate env via ``monkeypatch.setenv`` or ``os.environ[...]``
    need a fresh parse, so clear before and after each test.
    """
    from blackbull.env import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest_asyncio.fixture(scope="session", autouse=True)
def manage_cert_and_key():
    cert_path = pathlib.Path(__file__).parent / "cert.pem"
    key_path = pathlib.Path(__file__).parent / "key.pem"

    if not cert_path.exists() or not key_path.exists():
        raise FileNotFoundError(
            "tests/cert.pem and tests/key.pem must exist. "
            "Generate them with: openssl req -x509 -newkey rsa:2048 "
            "-keyout tests/key.pem -out tests/cert.pem -days 365 -nodes"
        )

    yield


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def child_env():
    """Environment for a test's child process (BLA-448).

    A neighbouring worktree's editable install can answer a child's
    ``import blackbull`` for the tree under test, and a green run then
    proves nothing about that tree.  ``make`` pins ``PYTHONPATH`` ahead of
    the caller's entries and probes the resolution the child will see,
    failing if it lands outside the repository root.  ``BB_TEST_TREE_ROOT``
    lets a child assert the same in its own process — a console script
    runs the interpreter its shebang names, which the probe cannot share.
    """

    def make(extra: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        want = (extra or {}).get('PYTHONPATH', '').split(os.pathsep)
        have = env.get('PYTHONPATH', '').split(os.pathsep)
        env['PYTHONPATH'] = os.pathsep.join(
            [str(REPO_ROOT)] + [p for p in want + have if p])
        env['BB_TEST_TREE_ROOT'] = str(REPO_ROOT)
        probe = subprocess.run(
            [sys.executable, '-c',
             "import importlib.util as u; print(u.find_spec('blackbull').origin)"],
            env=env, capture_output=True, text=True, errors='replace',
            timeout=30)
        found = (probe.stdout or '').strip()
        assert probe.returncode == 0 and found, probe.stderr
        resolved = pathlib.Path(found)
        assert resolved.resolve().is_relative_to(REPO_ROOT), (
            f'child would import blackbull from {resolved}, not this checkout')
        env.update({k: v for k, v in (extra or {}).items()
                    if k != 'PYTHONPATH'})
        return env

    return make
