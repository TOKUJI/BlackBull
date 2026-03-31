import pathlib
import pytest_asyncio


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
