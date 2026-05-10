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
    for marker_name, option_name in DEFAULT_SKIPPED_MARKERS.items():
        parser.addoption(
            option_name,
            action="store_true",
            default=False,
            help=f"run {marker_name} tests",
        )


def _is_explicit_vscode_selected_run(items):
    """
    VSCode Test Explorer execution では、-m が実行フェーズに見えず、
    RUN_TEST_IDS_PIPE 経由で選択テストだけが実行されることがある。

    その場合、現在の収集対象が「すべて skip-by-default マーカー付き」
    なら、ユーザーが明示的にその種別のテストを選んだとみなして実行を許可する。
    """
    if not os.environ.get("RUN_TEST_IDS_PIPE"):
        return False

    if not items:
        return False

    restricted = set(DEFAULT_SKIPPED_MARKERS)

    return all(
        any(mark.name in restricted for mark in item.iter_markers())
        for item in items
    )


def pytest_collection_modifyitems(config, items):
    markexpr = (config.option.markexpr or "").strip()

    # 1. CLIで -m を明示指定した場合は pytest 標準選別を優先
    if markexpr:
        return

    # 2. --run-* が1つでも付いていれば、その marker は許可
    enabled_markers = {
        marker_name
        for marker_name, option_name in DEFAULT_SKIPPED_MARKERS.items()
        if config.getoption(option_name)
    }

    # 3. VSCode Test Explorer が選択済み restricted tests のみを実行している場合
    #    （実行時に -m が見えなくても）許可
    if _is_explicit_vscode_selected_run(items):
        return

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
    cert_path = pathlib.Path(__file__).parent / "cert.pem"
    key_path = pathlib.Path(__file__).parent / "key.pem"

    if not cert_path.exists() or not key_path.exists():
        raise FileNotFoundError(
            "tests/cert.pem and tests/key.pem must exist. "
            "Generate them with: openssl req -x509 -newkey rsa:2048 "
            "-keyout tests/key.pem -out tests/cert.pem -days 365 -nodes"
        )

    yield
