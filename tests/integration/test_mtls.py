"""Integration tests for mutual TLS (mTLS) — guide.md §10.3.

Each test runs its own server with a fresh CA + client-cert pair generated
via the `cryptography` library (already a transitive dependency). No Docker
or external tools are required.
"""
import asyncio
import datetime
import ipaddress
import ssl
import tempfile
from multiprocessing import Process
from pathlib import Path

import httpx
import pytest

from blackbull import BlackBull


# ---------------------------------------------------------------------------
# Cert generation helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _gen_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    return rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )


def _gen_ca_cert(key):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'Test CA'),
    ])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_utcnow())
        .not_valid_after(_utcnow() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )


def _gen_cert(key, ca_key, ca_cert, cn: str, is_server: bool = False):
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_utcnow())
        .not_valid_after(_utcnow() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if is_server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
                x509.DNSName('localhost'),
            ]),
            critical=False,
        )
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    else:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    return builder.sign(ca_key, hashes.SHA256())


def _pem(cert_or_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    if hasattr(cert_or_key, 'private_bytes'):
        return cert_or_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    return cert_or_key.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """Generate a CA, server cert, and client cert in a temp directory."""
    d = tmp_path_factory.mktemp('pki')
    ca_key  = _gen_key()
    ca_cert = _gen_ca_cert(ca_key)
    srv_key  = _gen_key()
    srv_cert = _gen_cert(srv_key, ca_key, ca_cert, 'localhost', is_server=True)
    cli_key  = _gen_key()
    cli_cert = _gen_cert(cli_key, ca_key, ca_cert, 'test-client')

    (d / 'ca.pem').write_bytes(_pem(ca_cert))
    (d / 'server.crt').write_bytes(_pem(srv_cert))
    (d / 'server.key').write_bytes(_pem(srv_key))
    (d / 'client.crt').write_bytes(_pem(cli_cert))
    (d / 'client.key').write_bytes(_pem(cli_key))
    return d


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/secret')
    async def secret():
        return {'ok': True}

    return app


def _run_mtls_server(app, certfile: str, keyfile: str, cafile: str):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    ctx.load_verify_locations(cafile=cafile)
    ctx.verify_mode = ssl.CERT_REQUIRED
    asyncio.run(app.run())


@pytest.fixture(scope="module")
def mtls_server(pki):
    """Spin up a TLS server that requires client certificates.

    Uses ASGIServer directly so the SSL context can be overridden to
    set ``verify_mode = CERT_REQUIRED`` before the worker forks.  The
    pre-Sprint-22 form mutated ``app.server.ssl_context``; the
    ``app.server`` attribute no longer exists, so this fixture holds
    the server reference itself.
    """
    import ssl as _ssl
    from blackbull.server import ASGIServer

    app = _make_app()
    server = ASGIServer(
        app,
        certfile=str(pki / 'server.crt'),
        keyfile=str(pki / 'server.key'),
    )
    server.open_socket(0)
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(pki / 'server.crt'),
                        keyfile=str(pki / 'server.key'))
    ctx.load_verify_locations(cafile=str(pki / 'ca.pem'))
    ctx.verify_mode = _ssl.CERT_REQUIRED
    server.ssl_context = ctx

    p = Process(target=lambda: asyncio.run(server.run()))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield {'app': app, 'pki': pki, 'port': server.port}
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mtls_valid_client_cert(mtls_server):
    pki = mtls_server['pki']
    port = mtls_server['port']
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(pki / 'ca.pem'))
    ctx.load_cert_chain(certfile=str(pki / 'client.crt'),
                        keyfile=str(pki / 'client.key'))
    ctx.check_hostname = False
    async with httpx.AsyncClient(verify=ctx) as c:
        r = await c.get(f'https://127.0.0.1:{port}/secret')
    assert r.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mtls_no_client_cert_rejected(mtls_server):
    pki = mtls_server['pki']
    port = mtls_server['port']
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(pki / 'ca.pem'))
    ctx.check_hostname = False
    # No client cert loaded — server must reject the handshake
    with pytest.raises(Exception):
        async with httpx.AsyncClient(verify=ctx) as c:
            await c.get(f'https://127.0.0.1:{port}/secret')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mtls_self_signed_client_cert_rejected(mtls_server, pki):
    port = mtls_server['port']
    # Generate a cert signed by a different (unknown) CA
    rogue_ca_key  = _gen_key()
    rogue_ca_cert = _gen_ca_cert(rogue_ca_key)
    rogue_cli_key  = _gen_key()
    rogue_cli_cert = _gen_cert(rogue_cli_key, rogue_ca_key, rogue_ca_cert, 'rogue')
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix='.crt', delete=False) as cf:
        cf.write(_pem(rogue_cli_cert))
        crt_path = cf.name
    with tempfile.NamedTemporaryFile(suffix='.key', delete=False) as kf:
        kf.write(_pem(rogue_cli_key))
        key_path = kf.name
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(pki / 'ca.pem'))
        ctx.load_cert_chain(certfile=crt_path, keyfile=key_path)
        ctx.check_hostname = False
        with pytest.raises(Exception):
            async with httpx.AsyncClient(verify=ctx) as c:
                await c.get(f'https://127.0.0.1:{port}/secret')
    finally:
        os.unlink(crt_path)
        os.unlink(key_path)
