import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from shortener.settings import Settings


def generate_certificates(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    certificate_path = directory / "server.crt"
    key_path = directory / "server.key"
    ca_path = directory / "ca.crt"
    now = datetime.now(UTC)
    if all(path.exists() for path in (certificate_path, key_path, ca_path)):
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        if certificate.not_valid_after_utc > now + timedelta(days=1):
            return

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "INF326 local CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(name)
                    for name in ("localhost", "log", "shortener-v1", "shortener-v2")
                ]
                + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key_path.touch(mode=0o600, exist_ok=True)
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    certificate_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))


if __name__ == "__main__":
    generate_certificates(Settings.from_env().tls_directory)
