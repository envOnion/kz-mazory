"""Deterministic external provider in the isolated Docker E2E network."""
import datetime
import json
import ssl
from pathlib import Path
from http.server import ThreadingHTTPServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from .providers import ProviderHandler


def main():
    directory = Path('/e2e')
    directory.mkdir(exist_ok=True)
    directory.chmod(0o777)
    (directory / 'isolated-e2e.marker').touch()
    control = directory / 'provider-state.json'
    control.write_text('{}'); control.chmod(0o666)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.DNSName('provider')]), critical=False).sign(key, hashes.SHA256())
    (directory / 'localhost.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = directory / 'localhost.key'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    ProviderHandler.control = control
    server = ThreadingHTTPServer(('0.0.0.0', 9443), ProviderHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(directory / 'localhost.crt', key_path)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
