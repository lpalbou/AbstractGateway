"""The email bridge verifies TLS (framework backlog 0992 WP0).

Hermetic: a local IMAP server over implicit TLS on a random 127.0.0.1 port and a
throwaway test CA generated per module with the openssl CLI (the server certificate
names `DNS:localhost` only, so `127.0.0.1` exercises the host-name check). No real
mailbox, no network beyond loopback, no key committed.

The "refused" cases are the mutation check: drop `ssl_context=` from the bridge's
`IMAP4_SSL(...)` (the stdlib default verifies nothing on CPython 3.12) and the
connection succeeds, the server receives the password, and the test goes red.
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path
from typing import Dict, List

import pytest

PASSWORD_ENV = "WP0_TEST_MAIL_PASSWORD"
PASSWORD = "wp0-test-password"


def make_test_certs(out: Path, openssl: str = "openssl") -> Dict[str, Path]:
    """Generate a throwaway test CA and a server certificate for DNS:localhost only.

    Built at test time with the openssl CLI (no key is committed; `*.pem` is gitignored).
    The certificate names no IP address, so connecting to 127.0.0.1 exercises the host-name check.
    """
    exe = shutil.which(openssl)
    if exe is None:
        pytest.fail("the TLS tests need the openssl command line tool on PATH")
    out.mkdir(parents=True, exist_ok=True)
    (out / "ca.cnf").write_text(
        "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=AbstractFramework throwaway TEST CA\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n",
        encoding="utf-8",
    )
    (out / "leaf.cnf").write_text(
        "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=localhost\n"
        "[v3_leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n",
        encoding="utf-8",
    )

    def run(*args: str) -> None:
        subprocess.run([exe, *args], cwd=out, check=True, capture_output=True)

    run("genrsa", "-out", "ca.key", "2048")
    run("req", "-x509", "-new", "-key", "ca.key", "-sha256", "-days", "2", "-config", "ca.cnf",
        "-extensions", "v3_ca", "-out", "ca.pem")
    run("genrsa", "-out", "server.key", "2048")
    run("req", "-new", "-key", "server.key", "-config", "leaf.cnf", "-out", "server.csr")
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-sha256", "-days", "2", "-extfile", "leaf.cnf", "-extensions", "v3_leaf", "-out", "server.pem")
    return {"ca": out / "ca.pem", "cert": out / "server.pem", "key": out / "server.key"}


class _FakeMailServer:
    """One-connection-at-a-time IMAP server over implicit TLS (an empty mailbox)."""

    def __init__(self, kind: str, tls: Dict[str, Path]) -> None:
        assert kind == "imap"
        self.kind = kind
        self.logins: List[str] = []  # the password each successful LOGIN/AUTH carried
        self.handshake_errors: List[str] = []
        self._ctx = _server_context(tls)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(5)
            try:
                self._imap(conn)
            except ssl.SSLError as e:
                self.handshake_errors.append(str(e))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _wrap(self, conn: socket.socket) -> ssl.SSLSocket:
        return self._ctx.wrap_socket(conn, server_side=True)

    # --- IMAP (implicit TLS) -----------------------------------------------------------
    def _imap(self, raw: socket.socket) -> None:
        conn = self._wrap(raw)
        f = conn.makefile("rwb")

        def send(line: str) -> None:
            f.write(line.encode() + b"\r\n")
            f.flush()

        send("* OK IMAP4rev1 test server ready")
        while True:
            line = f.readline()
            if not line:
                return
            parts = line.decode().rstrip("\r\n").split(" ")
            tag, cmd, args = parts[0], parts[1].upper() if len(parts) > 1 else "", parts[2:]
            if cmd == "CAPABILITY":
                send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                send(f"{tag} OK CAPABILITY completed")
            elif cmd == "LOGIN":
                self.logins.append(args[1].strip('"') if len(args) > 1 else "")
                send(f"{tag} OK LOGIN completed")
            elif cmd in {"SELECT", "EXAMINE"}:
                send("* 0 EXISTS")
                send("* 0 RECENT")
                send("* OK [UIDVALIDITY 1] UIDs valid")
                send(f"{tag} OK [READ-ONLY] {cmd} completed")
            elif cmd == "UID" and args and args[0].upper() == "SEARCH":
                send("* SEARCH")
                send(f"{tag} OK SEARCH completed")
            elif cmd == "LOGOUT":
                send("* BYE logging out")
                send(f"{tag} OK LOGOUT completed")
                return
            else:
                send(f"{tag} BAD unsupported")


@pytest.fixture(scope="module")
def tls(tmp_path_factory) -> Dict[str, Path]:
    return make_test_certs(tmp_path_factory.mktemp("wp0-bridge-tls"))


def _server_context(tls: Dict[str, Path]) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls["cert"], tls["key"])
    return ctx


@pytest.fixture
def imap_server(tls):
    server = _FakeMailServer("imap", tls)
    yield server
    server.close()


def _bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, host: str, port: int, trust_ca: Path | None = None):
    from abstractruntime.storage.artifacts import InMemoryArtifactStore

    from abstractgateway.integrations.email_bridge import EmailBridge, EmailBridgeConfig

    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    cfg = EmailBridgeConfig(
        enabled=True,
        event_name="email.message",
        session_prefix="email:",
        account="me@example.invalid",
        imap_host=host,
        imap_port=port,
        imap_timeout_s=5.0,
        imap_username="me@example.invalid",
        imap_password_env_var=PASSWORD_ENV,
        imap_folder="INBOX",
        state_dir=tmp_path / "email_bridge",
    )
    bridge = EmailBridge(config=cfg, host=None, runner=None, artifact_store=InMemoryArtifactStore())
    if trust_ca is not None:
        # Still a verifying context (certificate + host name): only the test CA is added.
        monkeypatch.setattr(bridge, "_tls_context", lambda: ssl.create_default_context(cafile=str(trust_ca)))
    return bridge


def test_bridge_refuses_a_certificate_the_system_does_not_trust(imap_server, tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, host="localhost", port=imap_server.port)

    client, err = bridge._connect_imap()

    assert client is None
    assert err is not None and "IMAP TLS certificate verification failed for localhost" in err
    assert "Fix:" in err
    assert imap_server.logins == []  # the password never left the gateway


def test_bridge_connects_when_the_test_ca_is_trusted(imap_server, tls, tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, host="localhost", port=imap_server.port, trust_ca=tls["ca"])

    client, err = bridge._connect_imap()

    assert err is None and client is not None
    client.logout()
    assert imap_server.logins == [PASSWORD]
    assert bridge.poll_once() == 0  # the whole poll path works over the verified connection (empty mailbox)


def test_bridge_checks_the_host_name_even_with_the_ca_trusted(imap_server, tls, tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, host="127.0.0.1", port=imap_server.port, trust_ca=tls["ca"])

    client, err = bridge._connect_imap()

    assert client is None
    assert err is not None and "IMAP TLS certificate verification failed for 127.0.0.1" in err
    assert imap_server.logins == []


def test_bridge_default_context_verifies_certificate_and_host_name(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path, monkeypatch, host="localhost", port=993)
    ctx = bridge._tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_bridge_refuses_a_password_env_var_that_is_not_a_variable_name(tmp_path, monkeypatch) -> None:
    from abstractruntime.storage.artifacts import InMemoryArtifactStore

    from abstractgateway.integrations.email_bridge import EmailBridge, EmailBridgeConfig

    cfg = EmailBridgeConfig(
        enabled=True,
        event_name="email.message",
        session_prefix="email:",
        account="me@example.invalid",
        imap_host="localhost",
        imap_username="me@example.invalid",
        imap_password_env_var="hunter2 is my password",
        imap_folder="INBOX",
        state_dir=tmp_path / "email_bridge",
    )
    bridge = EmailBridge(config=cfg, host=None, runner=None, artifact_store=InMemoryArtifactStore())

    password, err = bridge._resolve_password()

    assert password is None
    assert err is not None and "must be the NAME of an environment variable" in err and "Fix:" in err
    assert "hunter2" not in err
