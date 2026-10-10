"""Hermetic fixtures for the per-user email tests (framework backlog 0992 WP3).

Nothing here reaches a real mail service, OAuth provider or the OS keychain:

- IMAP / SMTP / OAuth servers are AbstractCore's test fakes (`abstractcore.testing.mailserver`),
  bound to 127.0.0.1 on free ports, with a throwaway CA generated per session; every address
  is under `example.test`;
- `SSL_CERT_FILE` points the default trust store at that CA, so accounts connect with
  VERIFIED TLS and no CA file (the CA file is an admin-only setting);
- `keyring` may not even be imported: the gateway seals with the data folder's key file, and
  the `memory_keyring` guard fails any test during which something imports it;
- the legacy `ABSTRACT_EMAIL_*` variables and every `*_KEY` / `*_TOKEN` / `*PASSWORD` are removed;
- OAuth HTTP may only reach localhost.
"""

from __future__ import annotations

import email
import email.policy
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractcore.testing.mailserver import FakeImapServer, FakeOAuthServer, FakeSmtpServer, TestCA, TokenRegistry, build_message

ADMIN_TOKEN = "admin-token-email-tests"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

ALICE = "alice@example.test"
BOB = "bob@example.test"
ADMIN_ADDR = "admin@example.test"
PASSWORDS = {ALICE: "alice-app-password", BOB: "bob-app-password", ADMIN_ADDR: "admin-app-password"}


class KeychainImportGuard:
    """A meta-path finder that refuses `keyring` (and records who asked): the gateway seals with
    the data folder's key file and must never reach an OS keychain (round 16)."""

    def __init__(self) -> None:
        self.attempts: list = []

    def find_spec(self, name, path=None, target=None):  # noqa: D401 - importlib protocol
        if name == "keyring" or name.startswith("keyring."):
            self.attempts.append(name)
            raise ImportError(f"{name}: the gateway must never import keyring (tests/email_fixtures.py guard)")
        return None


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[KeychainImportGuard]:
    """Historic name (tests import it): no keychain at all now. Any attempt to import `keyring`
    during the test fails it — nothing in the gateway may reach the OS keychain."""

    import sys

    guard = KeychainImportGuard()
    stashed = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "keyring" or k.startswith("keyring.")}
    sys.meta_path.insert(0, guard)
    try:
        yield guard
    finally:
        try:
            sys.meta_path.remove(guard)
        except ValueError:
            pass
        sys.modules.update(stashed)
    assert not guard.attempts, f"keyring was imported during the test: {guard.attempts}"


@pytest.fixture(autouse=True)
def hermetic_email_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if (
            name.startswith("ABSTRACT_EMAIL_")
            or name.endswith("_KEY")
            or name.endswith("_TOKEN")
            or "PASSWORD" in name
            or name.startswith("ABSTRACT_BACKLOG_EMAIL")
            or name.startswith("ABSTRACT_TRIAGE_EMAIL")
        ):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.parse

    import httpx

    real_send = httpx.Client.send

    def guarded_send(self, request, *args, **kwargs):
        host = urllib.parse.urlsplit(str(request.url)).hostname or ""
        if host not in {"localhost", "127.0.0.1", "testserver"}:
            raise AssertionError(f"a test tried to reach {host!r}; email tests stay on localhost")
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", guarded_send)


@pytest.fixture(scope="session")
def ca(tmp_path_factory) -> TestCA:
    return TestCA.create(tmp_path_factory.mktemp("email-ca"))


@pytest.fixture(autouse=True)
def trust_test_ca(ca: TestCA, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(ca.ca_pem))


@pytest.fixture
def tokens() -> TokenRegistry:
    return TokenRegistry()


@pytest.fixture
def imap(ca: TestCA, tokens: TokenRegistry) -> Iterator[FakeImapServer]:
    server = FakeImapServer(ca, users=dict(PASSWORDS), security="ssl", tokens=tokens)
    yield server
    server.close()


@pytest.fixture
def imap_bob(ca: TestCA, tokens: TokenRegistry) -> Iterator[FakeImapServer]:
    server = FakeImapServer(ca, users=dict(PASSWORDS), security="ssl", tokens=tokens)
    yield server
    server.close()


def _start_smtp(ca: TestCA, tokens: TokenRegistry) -> FakeSmtpServer:
    """FakeSmtpServer picks a free port and then binds it; under parallel workers another process
    can take that port in between (EADDRINUSE). Only that error is retried, a few times."""
    import errno

    for attempt in range(5):
        try:
            return FakeSmtpServer(ca, users=dict(PASSWORDS), security="starttls", tokens=tokens, refuse={"blocked@example.test": 550})
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or attempt == 4:
                raise
    raise AssertionError("unreachable")


@pytest.fixture
def smtp(ca: TestCA, tokens: TokenRegistry) -> Iterator[FakeSmtpServer]:
    server = _start_smtp(ca, tokens)
    yield server
    server.close()


@pytest.fixture
def oauth_server(ca: TestCA, tokens: TokenRegistry) -> Iterator[FakeOAuthServer]:
    server = FakeOAuthServer(ca, tokens, user=ALICE)
    yield server
    server.close()


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A user-accounts gateway (multi-user) with the real middleware and routers, runner off.
    Registry users: alice and bob (with registered emails); the static admin token."""

    data_dir = tmp_path / "gw"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_LOCKOUT_AFTER", "1000")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "core" / "abstractcore.json"))
    for name in ("ABSTRACTGATEWAY_MULTI_USER", "ABSTRACTGATEWAY_AUTH_MODE", "ABSTRACTFLOW_GATEWAY_USER_AUTH"):
        monkeypatch.delenv(name, raising=False)

    from abstractgateway.routes import email_router, gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env
    from abstractgateway.users import GatewayUserRegistry

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(email_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")

    registry = GatewayUserRegistry()
    _a, alice_token = registry.create_user(user_id="alice", roles=["user"], email=ALICE)
    _b, bob_token = registry.create_user(user_id="bob", roles=["user"], email=BOB)
    return {
        "app": app,
        "client": TestClient(app),
        "data_dir": data_dir,
        "alice": {"Authorization": f"Bearer {alice_token}"},
        "bob": {"Authorization": f"Bearer {bob_token}"},
        "alice_token": alice_token,
        "bob_token": bob_token,
    }


def connect_body(address: str, imap: FakeImapServer, smtp: FakeSmtpServer, *, password: str = "") -> Dict[str, Any]:
    return {
        "address": address,
        "password": password or PASSWORDS[address],
        "imap": {"host": "localhost", "port": imap.port, "security": "ssl"},
        "smtp": {"host": "localhost", "port": smtp.port, "security": "starttls"},
    }


def plane_of(user_id: str):
    from abstractgateway.mail.accounts import plane_for_user

    return plane_for_user(user_id)


def smtp_bodies(smtp: FakeSmtpServer) -> list:
    out = []
    for m in smtp.messages:
        msg = email.message_from_bytes(m["data"], policy=email.policy.default)
        body = msg.get_body(preferencelist=("plain",))
        out.append({"to": list(m["rcpt_tos"]), "subject": str(msg["Subject"] or ""), "text": body.get_content() if body else "", "from": m["mail_from"]})
    return out


def code_from(text: str) -> str:
    """The 8-digit code line of a recovery mail (a fixed template: the code alone on its line)."""

    for line in text.splitlines():
        s = line.strip()
        if len(s) == 8 and s.isdigit():
            return s
    raise AssertionError("no code in the mail")


def all_files_bytes(root: Path) -> Iterator[Tuple[Path, bytes]]:
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                yield p, p.read_bytes()
            except OSError:
                continue


def message(subject: str, *, from_: str = "sender@example.test", to: str = ALICE, message_id: str = "", text: str = "hello") -> bytes:
    return build_message(from_=from_, to=to, subject=subject, text=text, message_id=message_id)


