"""Real HTTPS on a non-loopback hostname, plus a separate plaintext Cookie witness."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import socket
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, Request
from fastapi.responses import Response
import uvicorn

from tests.browser.test_studio_submission import serving

HOST = "auth32.openmontage.test"
ORIGIN = "https://" + HOST + ":4766"
HTTP_WITNESS = "http://" + HOST + ":4767"


def private_bytes(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


@contextmanager
def https_and_cookie_witness(app, directory):
    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Private identity validation CA")])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name).public_key(root_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(root_key, hashes.SHA256()))
    certificate = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
                   .issuer_name(root_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(root_key, hashes.SHA256()))
    ca, chain, secret = directory / "test-ca.pem", directory / "test-chain.pem", directory / "test-server.key"
    private_bytes(ca, root.public_bytes(serialization.Encoding.PEM))
    private_bytes(chain, certificate.public_bytes(serialization.Encoding.PEM) + root.public_bytes(serialization.Encoding.PEM))
    private_bytes(secret, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 4766))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", proxy_headers=False, access_log=False,
                                         ssl_certfile=str(chain), ssl_keyfile=str(secret)))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Required TLS identity application did not start"
    witness, cookies = FastAPI(), []

    @witness.get("/")
    def record(request: Request):
        cookies.append(request.headers.get("cookie", ""))
        response = Response("Plaintext witness accepted the request", media_type="text/plain")
        response.set_cookie("auth32_plaintext_probe", "observed", httponly=True, samesite="lax")
        return response

    try:
        context = ssl.create_default_context(cafile=str(ca))
        with socket.create_connection(("127.0.0.1", 4766), timeout=5) as transport:
            with context.wrap_socket(transport, server_hostname=HOST) as secured:
                assert ("DNS", HOST) in secured.getpeercert()["subjectAltName"]
                assert secured.version() in {"TLSv1.2", "TLSv1.3"}
        with serving(witness, port=4767):
            yield cookies
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Required TLS identity application did not stop"
