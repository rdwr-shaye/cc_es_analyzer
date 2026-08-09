"""
TLS helper — resolve a cert/key pair for serving HTTPS.

If the user supplies a cert + key (env SSL_CERTFILE / SSL_KEYFILE), those are
used as-is. Otherwise a self-signed certificate is generated once into
`certs/` next to the app (browsers will warn — fine for internal use; mount a
real cert for production).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs")


def ensure_cert(certfile: str = "", keyfile: str = "") -> tuple[str, str]:
    """Return (certfile, keyfile), generating a self-signed pair if none given.

    Raises RuntimeError with a clear message if a self-signed cert is needed but
    `cryptography` is unavailable.
    """
    if certfile and keyfile and os.path.isfile(certfile) and os.path.isfile(keyfile):
        return certfile, keyfile

    os.makedirs(_DEFAULT_DIR, exist_ok=True)
    cert_path = certfile or os.path.join(_DEFAULT_DIR, "selfsigned.crt")
    key_path  = keyfile or os.path.join(_DEFAULT_DIR, "selfsigned.key")
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path

    _generate_self_signed(cert_path, key_path)
    logger.info("[tls] generated self-signed certificate at %s", cert_path)
    return cert_path, key_path


def _generate_self_signed(cert_path: str, key_path: str) -> None:
    try:
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import ipaddress
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "HTTPS requested but no cert provided and `cryptography` is not "
            "installed. Add it to requirements.txt or set SSL_CERTFILE/SSL_KEYFILE."
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cc-es-analyzer")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
