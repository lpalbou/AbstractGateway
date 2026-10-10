"""Named API keys after the sealing key moved from the OS keychain to the data folder (round 16,
operator ruling 2026-10-10): keys sealed with the old keychain key are never read — their rows say
"Reveal unavailable: created before the key moved — create a new key", Reveal answers that
sentence, Revoke still works, and a new key is sealed under the data folder's key and revealable.

Hermetic: the `gw` fixture of test_openai_api; no keychain (conftest + the store is never opened)."""
from __future__ import annotations

import base64
import json

import pytest

from test_openai_api import USER_TOKEN, _set, gw  # noqa: F401 - gw is a fixture

pytestmark = pytest.mark.basic

ALICE = {"Authorization": f"Bearer {USER_TOKEN}"}
SENTENCE = "Reveal unavailable: created before the key moved — create a new key"
SECRETS = ("auth", "openai_key_secrets")


def _new_key(gw, label):
    r = gw.admin.post("/api/gateway/me/openai-keys", headers=ALICE, json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


def _keys(gw):
    r = gw.admin.get("/api/gateway/me/openai-keys", headers=ALICE)
    assert r.status_code == 200, r.text
    return {k["label"]: k for k in r.json()["keys"]}


def _keychain_sealed(path):
    path.write_text(json.dumps({
        "v": 1, "alg": "AES-256-GCM", "key": "keyring", "key_id": "0" * 24,
        "nonce": base64.b64encode(b"\0" * 12).decode(), "ct": base64.b64encode(b"\1" * 48).decode(),
    }))


@pytest.mark.parametrize("at_boot", [True, False])
def test_keys_sealed_with_the_old_key_are_unrevealable_and_revoke_still_works(gw, at_boot):
    from abstractgateway import openai_keys

    _set(gw, enabled=True)
    old = _new_key(gw, "old laptop")
    fp = old["item"]["fingerprint"]
    store = gw.data.joinpath(*SECRETS)
    _keychain_sealed(store / "secret.enc")  # as gateway <= 0.13 left it (key in the keychain)
    if at_boot:
        assert openai_keys.migrate_legacy_store(gw.data) is True
        assert openai_keys.migrate_legacy_store(gw.data) is False  # once
        row = _keys(gw)["old laptop"]
        assert row["revealable"] is False and row["reveal_unavailable"] == SENTENCE
    r = gw.admin.post(f"/api/gateway/me/openai-keys/{fp}/reveal", headers=ALICE)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["reason_code"] == "reveal_unavailable_key_moved"
    assert SENTENCE in r.text and old["key"] not in r.text
    row = _keys(gw)["old laptop"]
    assert row["revealable"] is False and row["reveal_unavailable"] == SENTENCE
    assert (store / "secret.keychain-old.enc").exists()
    lines = [json.loads(x) for x in (gw.data / "audit_log.jsonl").read_text().splitlines() if "secret_key_migrated_to_file" in x]
    assert len(lines) == 1 and lines[0]["store"] == "openai_keys"

    # A new key is sealed under the data folder's key, listed revealable, and reveals.
    new = _new_key(gw, "new laptop")
    assert new["item"]["revealable"] is True and "reveal_unavailable" not in new["item"]
    assert json.loads((store / "secret.enc").read_text())["key"] == "sealing-key"
    r = gw.admin.post(f"/api/gateway/me/openai-keys/{new['item']['fingerprint']}/reveal", headers=ALICE)
    assert r.status_code == 200 and r.json()["key"] == new["key"]

    # Revoke of the old key works (the key itself still worked until then).
    assert gw.admin.delete(f"/api/gateway/me/openai-keys/{fp}", headers=ALICE).status_code == 200
    assert set(_keys(gw)) == {"new laptop"}
