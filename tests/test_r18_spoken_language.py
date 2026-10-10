"""Round 18 — the "Spoken language" setting: ONE preference, ONE resolver, every transcription
entry point.

- `GET/PUT /api/gateway/accounts/{me|id}/preferences` carry `spoken_language` ("auto" or a code
  the speech engines support, THE list being AbstractVoice's); an unknown code is refused with
  the voice layer's sentence; admins set any account's, a user only their own.
- `POST /runs/{run_id}/audio/transcribe` tells the engine the request's hint, else the account's
  preference, else nothing (auto); the response and the transcript's ledger evidence carry
  `language`, `language_source` and `detected_language`.
- The OpenAI-compatible `/v1/audio/transcriptions` form gains the account's language when it
  names none.
- The STT capability descriptor lists the languages.

Each test goes red when its seam is deleted (the resolver call in the route, the preference key,
the evidence in the ledger, the multipart append).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from abstractvoice.stt import languages as voice_languages
from test_openai_api import USER_TOKEN, _set as _set_openai_api, gw as openai_gw  # noqa: F401 - the OpenAI-lane fixture

ADMIN = {"Authorization": "Bearer admin-token"}
SENTENCE_XX = voice_languages.refusal_sentence("xx")


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import abstractgateway.config as cfg

    shared = tmp_path / "bundles"
    shared.mkdir(parents=True)
    (shared / "basic-agent.flow").write_bytes((Path(cfg._default_flows_dir()) / "basic-agent.flow").read_bytes())
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(shared))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"], runtime_id="alice")
    _b, bob = reg.create_user(user_id="bob", roles=["user"], runtime_id="bob")
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield {"c": c, "alice": {"Authorization": f"Bearer {alice}"}, "bob": {"Authorization": f"Bearer {bob}"}, "data": data}


def _get(c, who, account="me"):
    r = c.get(f"/api/gateway/accounts/{account}/preferences", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def _put(c, who, body, account="me"):
    return c.put(f"/api/gateway/accounts/{account}/preferences", headers=who, json=body)


def _store(gateway) -> dict:
    path = gateway["data"] / "runtime_config.json"
    if not path.exists():
        path = next(gateway["data"].rglob("runtime_config.json"))
    return json.loads(path.read_text()).get("account_preferences") or {}


# ------------------------------------------------------------------ the preference


def test_get_me_serves_the_spoken_language_block_on_auto(gateway):
    out = _get(gateway["c"], gateway["alice"])
    assert out["preferences"]["spoken_language"] == "auto"
    block = out["spoken_language"]
    assert block["value"] == "auto"
    assert block["label"] == "Spoken language"
    assert block["help"] == (
        "The language spoken to the microphone. Auto lets the speech engine detect it; naming it skips "
        "detection, so short phrases and mixed-language speech transcribe reliably and a little faster."
    )
    assert block["choices"][0] == {"value": "auto", "label": "Auto (detected)"}
    assert [c["value"] for c in block["choices"][1:]] == [c["value"] for c in voice_languages.choices()[1:]]
    assert {"value": "fr", "label": "French"} in block["choices"]
    assert out["declared"]["spoken_language"]["label"] == "Spoken language"


def test_put_round_trips_a_code_and_auto_clears_it(gateway):
    c, alice = gateway["c"], gateway["alice"]
    r = _put(c, alice, {"spoken_language": "FR"})
    assert r.status_code == 200, r.text
    assert r.json()["spoken_language"]["value"] == "fr"
    assert r.json()["preferences"]["spoken_language"] == "fr"
    assert _store(gateway)["default:alice"]["spoken_language"] == "fr"
    assert _get(c, alice)["spoken_language"]["value"] == "fr", "the stored value survives a fresh read"
    for back in ("auto", None, ""):
        assert _put(c, alice, {"spoken_language": "fr"}).status_code == 200
        r = _put(c, alice, {"spoken_language": back})
        assert r.status_code == 200, r.text
        assert r.json()["spoken_language"]["value"] == "auto"
        assert "spoken_language" not in _store(gateway).get("default:alice", {}), back
    # The other keys keep: a time zone set earlier is untouched by a language write.
    assert _put(c, alice, {"time_zone": "Asia/Tokyo"}).status_code == 200
    assert _put(c, alice, {"spoken_language": "de"}).json()["time_zone"]["value"] == "Asia/Tokyo"


def test_an_unknown_code_is_refused_with_the_voice_layers_sentence(gateway):
    c, alice = gateway["c"], gateway["alice"]
    assert _put(c, alice, {"spoken_language": "fr"}).status_code == 200
    r = _put(c, alice, {"spoken_language": "xx"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "preference_refused" and detail["key"] == "spoken_language"
    assert detail["message"] == SENTENCE_XX
    assert "Choose auto or one of: " in detail["message"] and ", fr, " in detail["message"]
    assert _store(gateway)["default:alice"]["spoken_language"] == "fr", "a refusal changes nothing"
    r = _put(c, alice, {"spoken_language": 7})
    assert r.status_code == 400 and r.json()["detail"]["key"] == "spoken_language"


def test_an_admin_sets_any_accounts_language_and_a_user_only_their_own(gateway):
    c = gateway["c"]
    r = _put(c, ADMIN, {"spoken_language": "ja"}, account="alice")
    assert r.status_code == 200, r.text
    assert _get(c, gateway["alice"])["spoken_language"]["value"] == "ja"
    assert _get(c, gateway["bob"])["spoken_language"]["value"] == "auto", "each account's own"
    r = _put(c, gateway["bob"], {"spoken_language": "fr"}, account="alice")
    assert r.status_code == 403


# -------------------------------------------------------------- the transcribe route


def _fake_facade(monkeypatch, calls: list, *, detected: Any = "fr"):
    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    class _RunFacade:
        def transcribe_audio(self, parent_run_id: str, *, media, prompt=None, output=None, params=None, child_vars=None):
            calls.append({"output": dict(output or {})})
            return SimpleNamespace(
                run_id="child-stt-r18",
                status=RunStatus.COMPLETED,
                error=None,
                output={"result": {"content": "bonjour tout le monde", "metadata": {"detected_language": detected}}},
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (_RunFacade(), None))


def _upload(c, who, session_id="s-r18") -> Dict[str, Any]:
    up = c.post(
        "/api/gateway/attachments/upload",
        data={"session_id": session_id},
        files={"file": ("clip.wav", b"RIFF....", "audio/wav")},
        headers=who,
    )
    assert up.status_code == 200, up.text
    return up.json()["attachment"]


def _transcribe(c, who, audio_ref, **extra):
    body = {"audio_artifact": audio_ref, "request_id": "req-r18", **extra}
    return c.post("/api/gateway/runs/session_memory_s-r18/audio/transcribe", json=body, headers=who)


def _generation_of(c, who, run_id: str, artifact_id: str) -> Dict[str, Any]:
    r = c.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}", headers=who)
    assert r.status_code == 200, r.text
    body = r.json()
    for holder in (body.get("metadata"), body.get("descriptor")):
        if isinstance(holder, dict) and isinstance(holder.get("generation"), dict):
            return holder["generation"]
    raise AssertionError(f"no generation block in the transcript artifact metadata: {list(body)}")


def test_transcribe_tells_the_engine_the_account_language_and_reports_the_detected_one(gateway, monkeypatch):
    c, alice = gateway["c"], gateway["alice"]
    calls: list = []
    _fake_facade(monkeypatch, calls, detected="FR")
    audio = _upload(c, alice)

    # auto: nothing stored, no hint -> the engine gets NO language and detects it.
    r = _transcribe(c, alice, audio)
    assert r.status_code == 200, r.text
    assert "language" not in calls[-1]["output"]
    body = r.json()
    assert body["text"] == "bonjour tout le monde"
    assert (body["language"], body["language_source"], body["detected_language"]) == ("auto", "auto", "fr")
    gen = _generation_of(c, alice, body["run_id"], body["transcript_artifact"]["$artifact"])
    assert (gen["language"], gen["language_source"], gen["detected_language"]) == ("auto", "auto", "fr")

    # the account preference reaches the engine
    assert _put(c, alice, {"spoken_language": "fr"}).status_code == 200
    r = _transcribe(c, alice, audio)
    assert r.status_code == 200, r.text
    assert calls[-1]["output"]["language"] == "fr"
    body = r.json()
    assert (body["language"], body["language_source"], body["detected_language"]) == ("fr", "account", "fr")
    gen = _generation_of(c, alice, body["run_id"], body["transcript_artifact"]["$artifact"])
    assert (gen["language"], gen["language_source"]) == ("fr", "account")

    # a request hint wins over the preference; "auto" as a hint is no hint
    r = _transcribe(c, alice, audio, language="EN")
    assert r.status_code == 200, r.text
    assert calls[-1]["output"]["language"] == "en"
    assert (r.json()["language"], r.json()["language_source"]) == ("en", "request")
    r = _transcribe(c, alice, audio, language="auto")
    assert r.status_code == 200 and calls[-1]["output"]["language"] == "fr"

    # another account is not told alice's language
    bob_audio = _upload(c, gateway["bob"], session_id="s-r18-bob")
    r = c.post("/api/gateway/runs/session_memory_s-r18-bob/audio/transcribe", json={"audio_artifact": bob_audio}, headers=gateway["bob"])
    assert r.status_code == 200, r.text
    assert "language" not in calls[-1]["output"] and r.json()["language_source"] == "auto"


def test_transcribe_refuses_an_unknown_hint_before_touching_the_engine(gateway, monkeypatch):
    c, alice = gateway["c"], gateway["alice"]
    calls: list = []
    _fake_facade(monkeypatch, calls)
    audio = _upload(c, alice)
    r = _transcribe(c, alice, audio, language="xx")
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail == {"reason": "language_refused", "message": SENTENCE_XX, "key": "language"}
    assert calls == []


def test_detected_language_is_read_from_either_metadata_holder():
    """Split-server posture: the runtime copies the engine's report to the result's metadata;
    in-process posture: it stays in the text's own metadata (Core's generate()). Both answer."""
    from abstractgateway.spoken_language import detected_language_of

    assert detected_language_of({"metadata": {"detected_language": "FR"}}) == "fr"
    assert detected_language_of({"metadata": {}, "text": {"content": "x", "metadata": {"detected_language": "de"}}}) == "de"
    assert detected_language_of({"metadata": {"detected_language": None}, "text": {"metadata": {}}}) is None
    assert detected_language_of({"content": "x"}) is None
    assert detected_language_of("not a dict") is None


def test_transcribe_without_an_engine_report_answers_null_detected_language(gateway, monkeypatch):
    c, alice = gateway["c"], gateway["alice"]
    calls: list = []
    _fake_facade(monkeypatch, calls, detected=None)
    r = _transcribe(c, alice, _upload(c, alice))
    assert r.status_code == 200, r.text
    assert r.json()["detected_language"] is None and r.json()["language"] == "auto"


def test_the_stt_descriptor_lists_the_supported_languages(monkeypatch):
    """The capability contracts' STT descriptor names the languages a request may ask for (the
    same list the preference validates against), even on a gateway without the media helpers."""
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (None, "not wired"))
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (None, "not wired"))
    contracts = gateway_routes._build_client_capability_contracts(
        {
            "abstractvoice": {"installed": False, "error": "missing"},
            "abstractvision": {"installed": False, "error": "missing"},
            "voice": {"installed": False, "install_hint": "pip install abstractgateway"},
            "visualflow": {"installed": False, "error": "missing"},
            "capability_plugins": {
                "installed": True,
                "capabilities": {
                    "voice": {"available": False, "install_hint": "install voice"},
                    "audio": {"available": False, "install_hint": "install audio"},
                    "vision": {"available": False, "install_hint": "install vision"},
                    "music": {"available": False, "configured": False, "route_available": False, "config_hint": "install music"},
                },
            },
        }
    )
    for client in ("assistant", "flow_editor"):
        stt = contracts[client]["voice"]["stt"]
        assert stt["languages"] == voice_languages.supported_languages(), client
        assert "fr" in stt["languages"]


# ------------------------------------------------------- the OpenAI-compatible lane


def test_multipart_helpers_append_a_field_before_the_closing_delimiter():
    from abstractgateway import core_endpoint as ce

    ctype = 'multipart/form-data; boundary="xyz"'
    assert ce.multipart_boundary(ctype) == "xyz"
    assert ce.multipart_boundary("multipart/form-data; boundary=abc123") == "abc123"
    assert ce.multipart_boundary("application/json") is None
    body = b'--abc123\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFF\r\n--abc123--\r\n'
    out = ce.append_form_field(body, "abc123", "language", "fr")
    assert out.endswith(b'--abc123\r\nContent-Disposition: form-data; name="language"\r\n\r\nfr\r\n--abc123--\r\n')
    assert out.startswith(body[: body.rfind(b"--abc123--")])
    assert ce.append_form_field(b"no delimiter here", "abc123", "language", "fr") == b"no delimiter here"


def test_with_account_spoken_language_fills_only_a_languageless_form_of_an_account(tmp_path, monkeypatch):
    from abstractgateway import core_endpoint as ce
    from abstractgateway.account_preferences import write_preferences

    data = tmp_path / "data"
    data.mkdir()
    write_preferences(data, tenant_id="default", user_id="alice", changes={"spoken_language": "fr"}, validate=lambda i, v: None, actor="test")
    alice = SimpleNamespace(tenant_id="default", user_id="alice")
    bob = SimpleNamespace(tenant_id="default", user_id="bob")
    ctype = "multipart/form-data; boundary=abc123"
    body = b'--abc123\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n\r\nRIFF\r\n--abc123--\r\n'
    filled = ce.with_account_spoken_language(body, ctype, ["file"], alice, data)
    assert b'name="language"\r\n\r\nfr\r\n--abc123--' in filled
    assert ce.with_account_spoken_language(body, ctype, ["file", "language"], alice, data) == body, "the form's own language wins"
    assert ce.with_account_spoken_language(body, ctype, ["file"], None, data) == body, "no account, no preference"
    assert ce.with_account_spoken_language(body, ctype, ["file"], bob, data) == body, "bob is on auto"


def test_openai_lane_tells_core_the_accounts_language(openai_gw):
    """A gateway account's own token is its OpenAI-API key (test_openai_api); alice's form without a
    `language` reaches Core with her preference appended, a form naming one is left alone."""
    gw = openai_gw
    _set_openai_api(gw, enabled=True)
    from abstractgateway.account_preferences import write_preferences

    write_preferences(gw.data, tenant_id="default", user_id="alice", changes={"spoken_language": "fr"}, validate=lambda i, v: None, actor="test")
    alice = TestClient(gw.app, client=("127.0.0.1", 50000))
    bearer = {"Authorization": f"Bearer {USER_TOKEN}"}
    r = alice.post("/v1/audio/transcriptions", headers=bearer, files={"file": ("a.wav", b"RIFF", "audio/wav")}, data={"model": "faster-whisper/large-v3"})
    assert r.status_code == 200, r.text
    sent = gw.stub.calls[-1]
    assert sent["path"] == "/v1/audio/transcriptions"
    assert b'name="language"\r\n\r\nfr\r\n' in sent["body"], sent["body"][-200:]
    assert sent["headers"][b"content-length"] == str(len(sent["body"])).encode(), "the length follows the body"
    r = alice.post("/v1/audio/transcriptions", headers=bearer, files={"file": ("a.wav", b"RIFF", "audio/wav")}, data={"language": "en"})
    assert r.status_code == 200, r.text
    assert gw.stub.calls[-1]["body"].count(b'name="language"') == 1 and b"\r\n\r\nen\r\n" in gw.stub.calls[-1]["body"]
