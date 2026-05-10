import pathlib
import pytest
import pytest_asyncio


# 通常実行では skip し、明示指定時だけ実行するマーカー
DEFAULT_SKIPPED_MARKERS = {
    "integration": "--run-integration",
    "system": "--run-system",
    "production": "--run-production",
    "slow": "--run-slow",
    "docker": "--run-docker",
    "network": "--run-network",
}



def pytest_addoption(parser):
    for marker_name, option_name in DEFAULT_SKIPPED_MARKERS.items():
        parser.addoption(
            option_name,
            action="store_true",
            default=False,
            help=f"run {marker_name} tests",
        )


def pytest_collection_modifyitems(config, items):
    # -m が指定された場合は、pytest本来の marker selection を優先する
    # 例:
    #   pytest -m integration
    #   pytest -m "integration or system"
    #   pytest -m "slow and not docker"
    markexpr = (config.option.markexpr or "").strip()
    if markexpr:
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
                        f"enable with {required_options} or run with -m"
                    )
                )
            )

@pytest_asyncio.fixture(scope="session", autouse=True)
def manage_cert_and_key():
    """Ensure the real cert.pem / key.pem are present for the test session.

    Previously this fixture replaced the real files with stub (invalid) PEM
    content, which caused ssl.load_cert_chain() to fail with an SSLError.
    The real cert/key files that already live in tests/ are self-signed and
    valid; the test suite just needs them to be present, so this fixture now
    simply verifies they exist and leaves them untouched.
    """
    cert_path = pathlib.Path(__file__).parent / 'cert.pem'
    key_path = pathlib.Path(__file__).parent / 'key.pem'

    if not cert_path.exists() or not key_path.exists():
        raise FileNotFoundError(
            "tests/cert.pem and tests/key.pem must exist. "
            "Generate them with: openssl req -x509 -newkey rsa:2048 "
            "-keyout tests/key.pem -out tests/cert.pem -days 365 -nodes"
        )

    yield

