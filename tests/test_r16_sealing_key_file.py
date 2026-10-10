"""Sealed secrets with ONE key file per data folder, never the OS keychain (round 16, operator
ruling 2026-10-10: "abstractgateway must be compatible ACROSS OS — keychains are a macOS feature").

- the key: `<data dir>/secrets/sealing.key` 0600 in `secrets/` 0700, 32 random bytes, made on first
  use, ONE key for every sealed store (mailbox, OAuth clients, MCP headers, named API keys);
- seal / unseal round trip; a sealed file copied into another store does not open; a data folder
  moved with its `secrets/` keeps working; a missing key is a typed sentence;
- an older AbstractCore key-file store opens and is re-sealed under the data folder's key;
- a store sealed with the old keychain key is NEVER opened: the mailbox reads `needs_reconnect`
  with THE sentence (GET /me/email mailbox.reason), one audit line per store, and an "Email
  result" notice fails ONCE with that sentence (automation `last_notification`);
- `keyring` is never imported: the autouse guard (email_fixtures.memory_keyring) fails any test
  during which something imports it, and no gateway source file imports it.

Hermetic: scratch data dir, AbstractCore's fake IMAP/SMTP servers, no keychain anywhere."""

from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures (the keyring guard is autouse)
from email_fixtures import ALICE, connect_body, plane_of

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parent.parent / "src" / "abstractgateway"
SENTENCE = (
    "The mailbox credentials were sealed with the old macOS keychain key; connect the mailbox again "
    "— the new key lives in the data folder."
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _keychain_sealed(path: Path) -> None:
    """A secret.enc exactly as gateway <= 0.13 wrote it with the key in the macOS keychain."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "v": 1, "alg": "AES-256-GCM", "key": "keyring", "key_id": "0" * 24,
        "nonce": base64.b64encode(b"\0" * 12).decode(), "ct": base64.b64encode(b"\1" * 48).decode(),
    }))


def _audit(data_dir: Path, event: str) -> list:
    path = data_dir / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if f'"{event}"' in line]


# ---- the key -----------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows is best effort (docs/security.md)")
def test_the_key_is_made_on_first_use_0600_in_a_0700_folder(tmp_path) -> None:
    from abstractgateway.security.sealing import ensure_sealing_key, read_sealing_key, sealing_key_path

    data = tmp_path / "data"
    assert read_sealing_key(data) is None and not (data / "secrets").exists()  # nothing made by a read
    key = ensure_sealing_key(data)
    path = sealing_key_path(data)
    assert path == data / "secrets" / "sealing.key"
    assert len(key) == 32 and base64.b64decode(path.read_bytes().strip()) == key
    assert _mode(path) == 0o600 and _mode(data / "secrets") == 0o700
    assert ensure_sealing_key(data) == key and read_sealing_key(data) == key  # made once
    assert ensure_sealing_key(tmp_path / "other") != key  # one key PER data folder


def test_seal_unseal_round_trip_bound_to_its_store_and_movable_with_the_folder(tmp_path) -> None:
    from abstractgateway.mail.core_mail import EmailError
    from abstractgateway.security.sealing import sealed_vault

    data = tmp_path / "data"
    a = sealed_vault(data / "auth" / "a", data_dir=data)
    b = sealed_vault(data / "config" / "b", data_dir=data)
    assert a.key_file == data / "secrets" / "sealing.key"
    assert a.load() is None and a.location() == ""
    assert a.store({"password": "sentinel-pw-123"}) == "sealing-key"
    raw = a.sealed_path.read_text()
    assert "sentinel-pw-123" not in raw and json.loads(raw)["key"] == "sealing-key"
    if os.name != "nt":
        assert _mode(a.sealed_path) == 0o600 and _mode(a.directory) == 0o700
    assert a.load() == {"password": "sentinel-pw-123"} and a.location() == "sealing-key"
    # A sealed file copied into another store does not open (its place is the associated data).
    b.directory.mkdir(parents=True)
    shutil.copy(a.sealed_path, b.sealed_path)
    with pytest.raises(EmailError) as err:
        b.load()
    assert err.value.code == "email_secret_unavailable" and "copied from another store" in err.value.cause
    # The whole folder moved (a restore elsewhere, WITH secrets/): still opens.
    moved = tmp_path / "restored"
    shutil.copytree(data, moved)
    assert sealed_vault(moved / "auth" / "a", data_dir=moved).load() == {"password": "sentinel-pw-123"}
    # The folder copied WITHOUT secrets/: a typed sentence, never a stack.
    shutil.rmtree(moved / "secrets")
    with pytest.raises(EmailError) as err:
        sealed_vault(moved / "auth" / "a", data_dir=moved).load()
    assert err.value.code == "email_secret_unavailable" and "sealing.key" in err.value.cause
    a.delete()
    assert not a.exists() and (data / "secrets" / "sealing.key").exists()  # other stores keep the key


def test_an_older_key_file_store_is_opened_and_resealed_under_the_folder_key(tmp_path) -> None:
    import secrets as _secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from abstractgateway.security.sealing import sealed_vault

    data = tmp_path / "data"
    store_dir = data / "email" / "oauth_clients"
    # Exactly what gateway <= 0.13 wrote on a host without a keychain: secret.key beside secret.enc.
    store_dir.mkdir(parents=True)
    key = AESGCM.generate_key(bit_length=256)
    (store_dir / "secret.key").write_bytes(base64.b64encode(key))
    nonce, key_id = _secrets.token_bytes(12), "e" * 24
    ct = AESGCM(key).encrypt(nonce, json.dumps({"google": {"client_id": "cid"}}).encode(), b"abstractcore-email-secret-v1|" + key_id.encode())
    (store_dir / "secret.enc").write_text(json.dumps({"v": 1, "alg": "AES-256-GCM", "key": "file", "key_id": key_id,
                                                      "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode()}))
    readonly = sealed_vault(store_dir, data_dir=data, reseal_legacy=False)
    assert readonly.load() == {"google": {"client_id": "cid"}}
    assert json.loads(readonly.sealed_path.read_text())["key"] == "file"  # untouched
    vault = sealed_vault(store_dir, data_dir=data)
    assert vault.load() == {"google": {"client_id": "cid"}}
    assert json.loads(vault.sealed_path.read_text())["key"] == "sealing-key"
    assert not (store_dir / "secret.key").exists()
    assert vault.load() == {"google": {"client_id": "cid"}}


def test_every_sealed_store_uses_the_one_key(gateway, imap, smtp) -> None:
    from abstractgateway import mcp_registry
    from abstractgateway.mail.accounts import set_oauth_client
    from abstractgateway.security.sealing import key_id, read_sealing_key

    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    set_oauth_client("google", client_id="cid.apps.example.test", client_secret="s3cret", actor="admin")
    mcp_registry._store_secrets(gateway["data_dir"], {"srv": {"Authorization": "Bearer x"}})
    data = gateway["data_dir"]
    kid = key_id(read_sealing_key(data))
    sealed = [p for p in data.rglob("secret.enc")]
    assert len(sealed) == 3
    assert {json.loads(p.read_text())["kid"] for p in sealed} == {kid}
    assert not list(data.rglob("secret.key"))  # no per-store key file
    assert list(data.rglob("sealing.key")) == [data / "secrets" / "sealing.key"]
    assert mcp_registry._secrets(data) == {"srv": {"Authorization": "Bearer x"}}


# ---- the keychain-sealed mailbox ---------------------------------------------------------

def test_a_keychain_sealed_mailbox_needs_reconnect_without_touching_the_keychain(gateway, imap, smtp, memory_keyring) -> None:
    from abstractgateway.mail.accounts import migrate_keychain_sealed_mailboxes

    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    plane = plane_of("alice")
    _keychain_sealed(plane.email_dir / "account" / "email" / "secret.enc")

    notes = migrate_keychain_sealed_mailboxes()
    assert any(SENTENCE in n for n in notes)
    assert migrate_keychain_sealed_mailboxes() == []  # once
    lines = _audit(gateway["data_dir"], "email.secret_key_migrated_to_file")
    assert len(lines) == 1 and lines[0]["user_id"] == "alice" and lines[0]["store"] == "mailbox"

    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["mailbox"] == {"state": "needs_reconnect", "address": ALICE, "provider": "imap", "reason": SENTENCE}
    assert me["notifications_unavailable_reason"] == SENTENCE
    assert me["secret_storage"] == "old-keychain" and me["effective_enabled"] is True
    assert me["secret_warning"] == SENTENCE
    assert me["agent_tools"]["active"] is False
    # Test answers the sentence (409), and nothing was read from any keychain (the guard).
    r = c.post("/api/gateway/me/email/test", headers=gateway["alice"])
    assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "email_needs_reconnect"
    assert r.json()["detail"]["cause"] == SENTENCE

    # Connecting again seals under the data folder's key: connected.
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["mailbox"]["state"] == "connected" and me["secret_storage"] == "sealing-key"
    assert memory_keyring.attempts == []


def test_an_email_result_on_such_a_mailbox_fails_once_and_the_automation_says_so(gateway, imap, smtp) -> None:
    from abstractgateway.mail.notifications import NotificationOutbox, last_automation_notification, queue_notice

    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    plane = plane_of("alice")
    assert last_automation_notification(plane, "auto-1") is None
    _keychain_sealed(plane.email_dir / "account" / "email" / "secret.enc")

    sends = []
    queue_notice(plane, "automation_result", "k1", {"title": "Daily brief", "subject_ref": "automation:auto-1"})
    outbox = NotificationOutbox(plane)
    out = outbox.deliver(send=lambda ctx, msg: sends.append(msg))
    assert out == {"sent": 0, "failed": 1, "deferred": 0} and sends == []
    # Never retried: a second pass changes nothing.
    assert outbox.deliver(send=lambda ctx, msg: sends.append(msg)) == {"sent": 0, "failed": 0, "deferred": 0}
    rows = outbox.rows()
    assert len(rows) == 1 and rows[0]["state"] == "failed" and rows[0]["error_code"] == "email_needs_reconnect"
    assert len(_audit(gateway["data_dir"], "email.notification_failed")) == 1
    last = last_automation_notification(plane, "auto-1")
    assert last["state"] == "failed" and last["channel"] == "email" and last["code"] == "email_needs_reconnect"
    assert last["text"] == f"Email result failed — {SENTENCE}"
    # The mailbox's own status keeps no "send error" for it (its reason already says it).
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["status"].get("last_error") is None
    assert last_automation_notification(plane, "auto-2") is None


def test_the_automation_summary_carries_last_notification(gateway, imap, smtp) -> None:
    from types import SimpleNamespace

    from abstractgateway.mail.accounts import admin_plane
    from abstractgateway.mail.notifications import queue_notice
    from abstractgateway.routes.automations import _last_notification

    plane = admin_plane()
    svc = SimpleNamespace(config=SimpleNamespace(runtime_id="default", data_dir=plane.root, tenant_id="default", user_id="admin"))
    assert _last_notification(svc, "auto-9") is None
    queue_notice(plane, "automation_result", "k9", {"title": "T", "subject_ref": "automation:auto-9"})
    assert _last_notification(svc, "auto-9")["text"] == "Email result queued"


def test_boot_moves_keychain_sealed_oauth_and_mcp_stores_aside(gateway) -> None:
    from abstractgateway import mcp_registry
    from abstractgateway.mail.accounts import OAUTH_CLIENTS_KEY_MOVED, migrate_keychain_sealed_mailboxes, oauth_clients_raw

    data = gateway["data_dir"]
    _keychain_sealed(data / "email" / "oauth_clients" / "secret.enc")
    _keychain_sealed(data / "config" / "mcp_secrets" / "secret.enc")
    assert OAUTH_CLIENTS_KEY_MOVED in migrate_keychain_sealed_mailboxes()
    assert mcp_registry.migrate_legacy_store(data) is True and mcp_registry.migrate_legacy_store(data) is False
    assert oauth_clients_raw() == {} and mcp_registry._secrets(data) == {}
    assert (data / "email" / "oauth_clients" / "secret.keychain-old.enc").exists()
    assert (data / "config" / "mcp_secrets" / "secret.keychain-old.enc").exists()
    assert {line["store"] for line in _audit(data, "secret_key_migrated_to_file")} == {"oauth_clients", "mcp_secrets"}


# ---- keyring is gone ---------------------------------------------------------------------

def _keyring_imports(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
            [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        out += [f"{path.name}:{node.lineno} {n}" for n in names if n == "keyring" or n.startswith("keyring.")]
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            arg = str(node.args[0].value)
            if fn in ("import_module", "__import__", "find_spec") and (arg == "keyring" or arg.startswith("keyring.")):
                out.append(f"{path.name}:{node.lineno} {fn}({arg!r})")
    return out


def test_no_gateway_source_imports_keyring_and_it_is_no_dependency() -> None:
    offenders = [o for p in sorted(SRC.rglob("*.py")) for o in _keyring_imports(p)]
    assert offenders == []
    pyproject = (SRC.parent.parent / "pyproject.toml").read_text()
    assert '"keyring' not in pyproject  # no keyring requirement


def test_sealing_and_migration_never_import_keyring(tmp_path) -> None:
    """A fresh interpreter, a finder that REFUSES `keyring`: import the gateway's sealing users,
    seal, unseal, and read a keychain-sealed mailbox. Any attempt fails the test."""

    script = tmp_path / "probe.py"
    script.write_text(f"""
import base64, json, sys
from pathlib import Path
attempts = []
class Guard:
    def find_spec(self, name, path=None, target=None):
        if name == "keyring" or name.startswith("keyring."):
            attempts.append(name)
            raise ImportError(name)
sys.meta_path.insert(0, Guard())
for k in [k for k in sys.modules if k.startswith("keyring")]:
    del sys.modules[k]
import abstractgateway.service, abstractgateway.mail.worker, abstractgateway.openai_keys, abstractgateway.mcp_registry
from abstractgateway.security.sealing import sealed_vault
from abstractgateway.mail.accounts import mailbox_vault, migrate_keychain_sealed_mailboxes
data = Path({str(tmp_path / 'd')!r})
s = sealed_vault(data / "x", data_dir=data); s.store({{"a": 1}}); assert s.load() == {{"a": 1}}
v = mailbox_vault(data / "m")
v.directory.mkdir(parents=True)
v.sealed_path.write_text(json.dumps({{"v": 1, "alg": "AES-256-GCM", "key": "keyring", "key_id": "0", "nonce": "AA==", "ct": "AA=="}}))
assert v.legacy_keychain()
try:
    v.load()
except Exception as exc:
    assert getattr(exc, "code", "") == "email_needs_reconnect", exc
print(json.dumps(attempts))
""")
    env = dict(os.environ, ABSTRACTGATEWAY_DATA_DIR=str(tmp_path / "d"))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
