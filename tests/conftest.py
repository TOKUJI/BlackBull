import pathlib
import pytest_asyncio
import subprocess
from blackbull.logger import get_logger_set

logger, _ = get_logger_set("conftest")

@pytest_asyncio.fixture(scope="session", autouse=True)
async def manage_cert_and_key():
    cert_path = pathlib.Path(__file__).parent / 'cert.pem'
    key_path = pathlib.Path(__file__).parent / 'key.pem'
    cert_backup = cert_path.with_suffix('.pem.bak')
    key_backup = key_path.with_suffix('.pem.bak')

    logger.info(f"[manage_cert_and_key] START: cert_path={cert_path}, key_path={key_path}")

    # Backup if exists
    cert_existed = cert_path.exists()
    key_existed = key_path.exists()
    if cert_existed:
        cert_path.rename(cert_backup)
        logger.info("[manage_cert_and_key] Backed up existing cert.pem")
    if key_existed:
        key_path.rename(key_backup)
        logger.info("[manage_cert_and_key] Backed up existing key.pem")

    # Generate real cert/key using openssl
    logger.info("[manage_cert_and_key] Generating real cert.pem and key.pem with openssl")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_path), "-out", str(cert_path),
        "-days", "365", "-nodes", "-subj", "/CN=localhost"
    ], check=True)
    logger.info("[manage_cert_and_key] Created cert.pem and key.pem")

    yield

    # Remove test cert/key
    try:
        cert_path.unlink()
        logger.info("[manage_cert_and_key] Removed cert.pem")
    except FileNotFoundError:
        logger.info("[manage_cert_and_key] cert.pem not found at cleanup")
    try:
        key_path.unlink()
        logger.info("[manage_cert_and_key] Removed key.pem")
    except FileNotFoundError:
        logger.info("[manage_cert_and_key] key.pem not found at cleanup")

    # Restore backups if existed
    if cert_existed:
        cert_backup.rename(cert_path)
        logger.info("[manage_cert_and_key] Restored cert.pem from backup")
    if key_existed:
        key_backup.rename(key_path)
        logger.info("[manage_cert_and_key] Restored key.pem from backup")
