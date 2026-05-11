import os
import pathlib
import pytest
import pytest_asyncio


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
