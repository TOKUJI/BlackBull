"""Self-signed TLS helpers for the fault-injection example + tests.

The :class:`~blackbull.fault_injection.H2FaultServer` accepts an
:class:`ssl.SSLContext` so it can negotiate HTTP/2 over TLS with real
clients (httpx, curl, ...) that use ALPN.  This module exists so the
example and the unit tests don't each have to roll their own
self-signed cert generation.

The cert is RSA 2048, valid for 1 day, signed with SHA-256, and
carries ``DNS:localhost`` + ``IP:127.0.0.1`` SANs.  All of that is
fine for loopback tests and only loopback tests — never load one of
these certs into a public-facing process.
"""
from __future__ import annotations

import datetime
import ipaddress
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def make_self_signed_h2_context() -> ssl.SSLContext:
    """Return a server-side SSLContext that negotiates ``h2`` via ALPN.

    The returned context loads an ephemeral RSA self-signed cert from
    a tempdir and advertises ``h2`` (and ``http/1.1`` as a fallback)
    in the ALPN protocol list.  Use it with :class:`H2FaultServer`'s
    ``ssl_context=`` parameter.
    """
    cert_pem, key_pem = _generate_self_signed_pem()
    # SSLContext.load_cert_chain demands file paths, not bytes.  We
    # drop the PEMs into a tempdir; both files are removed when the
    # context goes out of scope at process exit.
    tmp = Path(tempfile.mkdtemp(prefix='blackbull-fault-tls-'))
    cert_path = tmp / 'cert.pem'
    key_path = tmp / 'key.pem'
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    # Real clients pick the strongest ALPN entry both sides advertise;
    # offering http/1.1 too keeps the example friendly to clients that
    # don't enable HTTP/2.
    ctx.set_alpn_protocols(['h2', 'http/1.1'])
    return ctx


def _generate_self_signed_pem() -> tuple[bytes, bytes]:
    """Generate (cert_pem, key_pem) for a localhost-only TLS handshake."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'localhost'),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


__all__ = ['make_self_signed_h2_context']
