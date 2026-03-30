import pathlib
import pytest_asyncio

CERT_CONTENT = '''\
-----BEGIN CERTIFICATE-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA7QIDAQAB
-----END CERTIFICATE-----
'''
KEY_CONTENT = '''\
-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQD
-----END PRIVATE KEY-----
'''

@pytest_asyncio.fixture(scope="session", autouse=True)
def manage_cert_and_key():
    cert_path = pathlib.Path(__file__).parent / 'cert.pem'
    key_path = pathlib.Path(__file__).parent / 'key.pem'
    cert_backup = cert_path.with_suffix('.pem.bak')
    key_backup = key_path.with_suffix('.pem.bak')

    # Backup if exists
    cert_existed = cert_path.exists()
    key_existed = key_path.exists()
    if cert_existed:
        cert_path.rename(cert_backup)
    if key_existed:
        key_path.rename(key_backup)

    # Create test cert/key
    cert_path.write_text(CERT_CONTENT)
    key_path.write_text(KEY_CONTENT)

    yield

    # Remove test cert/key
    try:
        cert_path.unlink()
    except FileNotFoundError:
        pass
    try:
        key_path.unlink()
    except FileNotFoundError:
        pass

    # Restore backups if existed
    if cert_existed:
        cert_backup.rename(cert_path)
    if key_existed:
        key_backup.rename(key_path)
