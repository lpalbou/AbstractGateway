"""The voice library routes (`routes/voice_library.py`): reads for every account, changes for admins.

The facade is AbstractRuntime's `AbstractCoreVoiceFacade` (tested there against real
AbstractVoice, local and over a real AbstractCore server); here a fake stands in so the routes'
own contract is pinned: the paths, the arguments that reach the facade, the status a refusal keeps,
the admin gate on every change, `language` on the voice catalog and `one_shot` on speech.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


class _FakeFacade:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def _record(self, op: str, /, *args: Any, **kwargs: Any) -> None:
        self.calls.append((op, args, kwargs))

    def list_languages(self, **kw):
        self._record("list_languages", **kw)
        return {"languages": {"tts": {"qwen3-tts": {"languages": ["en", "fr"]}}}}

    def list_cloned_voices(self, **kw):
        self._record("list_cloned_voices", **kw)
        return {"voices": [{"voice_id": "v1", "name": "Ada"}], "count": 1}

    def get_cloned_voice(self, voice_id, **kw):
        from abstractruntime.integrations.abstractcore import VoiceLibraryError

        self._record("get_cloned_voice", voice_id, **kw)
        if voice_id != "v1":
            raise VoiceLibraryError(404, "Unknown voice_id: " + voice_id)
        return {"voice_id": "v1", "name": "Ada"}

    def update_cloned_voice(self, voice_id, **kw):
        self._record("update_cloned_voice", voice_id, **kw)
        return {"voice_id": voice_id, "name": kw.get("name")}

    def delete_cloned_voice(self, voice_id, **kw):
        from abstractruntime.integrations.abstractcore import VoiceLibraryError

        self._record("delete_cloned_voice", voice_id, **kw)
        if voice_id.startswith("builtin"):
            raise VoiceLibraryError(422, "Built-in voices are read-only.")
        return {"voice_id": voice_id, "deleted": True}

    def design_voice(self, description, **kw):
        self._record("design_voice", description, **kw)
        return {"voice_id": "d1", "name": kw.get("name")}

    def clone_voice(self, audio, **kw):
        self._record("clone_voice", audio, **kw)
        return {"voice_id": "c1", "ok": True}

    def compatibility_catalog(self, **kw):
        self._record("compatibility_catalog", **kw)
        return {"providers": {"tts": {}, "stt": {}, "cloning": {}}}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, facade: _FakeFacade) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    (tmp_path / "flows").mkdir(parents=True, exist_ok=True)

    from abstractgateway.routes import voice_library

    monkeypatch.setattr(voice_library, "_facade", lambda: facade)
    monkeypatch.setattr(voice_library, "_keys", lambda request: {})
    from abstractgateway.app import app

    return TestClient(app)


ADMIN = {"Authorization": "Bearer admin-token"}


def _user(client: TestClient) -> Dict[str, str]:
    created = client.post(
        "/api/gateway/admin/users", headers=ADMIN, json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]}
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['token']}"}


def test_reads_are_open_to_every_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _FakeFacade()
    with _client(tmp_path, monkeypatch, facade) as client:
        user = _user(client)
        langs = client.get("/api/gateway/voice/languages?kind=tts&provider=qwen3-tts", headers=user)
        assert langs.status_code == 200 and langs.json()["languages"]["tts"]["qwen3-tts"]["languages"] == ["en", "fr"]
        assert ("list_languages", (), {"kind": "tts", "provider": "qwen3-tts"}) in facade.calls
        clones = client.get("/api/gateway/voice/clones?language=fr", headers=user)
        assert clones.status_code == 200 and clones.json()["count"] == 1
        assert ("list_cloned_voices", (), {"provider": None, "model": None, "language": "fr"}) in facade.calls
        assert client.get("/api/gateway/voice/clones/v1", headers=user).json()["name"] == "Ada"
        assert client.get("/api/gateway/voice/clones/nope", headers=user).status_code == 404
        assert "cloning" in client.get("/api/gateway/voice/compatibility", headers=user).json()["providers"]


def _runtime_with(voice_store_dir):
    client = type("Client", (), {"_llm_kwargs": {"voice_store_dir": voice_store_dir} if voice_store_dir else {}})()
    return type("Runtime", (), {"_abstractcore_llm_client": client})()


def test_changes_are_refused_to_a_non_admin_when_the_store_is_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _FakeFacade()
    from abstractgateway.routes import gateway as gateway_routes

    with _client(tmp_path, monkeypatch, facade) as client:
        user = _user(client)
        monkeypatch.setattr(gateway_routes, "_gateway_runtime", lambda: (_runtime_with(None), None))
        refused = [
            client.patch("/api/gateway/voice/clones/v1", headers=user, json={"name": "X"}),
            client.delete("/api/gateway/voice/clones/v1", headers=user),
            client.post("/api/gateway/voice/design", headers=user, json={"description": "a calm voice"}),
            client.post("/api/gateway/voice/clones", headers=user, files={"file": ("me.wav", b"RIFF", "audio/wav")}),
        ]
        assert [r.status_code for r in refused] == [403, 403, 403, 403]
        assert "administrator" in refused[0].json()["detail"]
        assert not [c for c in facade.calls if c[0] in {"update_cloned_voice", "delete_cloned_voice", "design_voice", "clone_voice"}]


def test_an_account_with_its_own_store_changes_its_voices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _FakeFacade()
    from abstractgateway.routes import gateway as gateway_routes

    with _client(tmp_path, monkeypatch, facade) as client:
        user = _user(client)
        monkeypatch.setattr(gateway_routes, "_gateway_runtime", lambda: (_runtime_with(str(tmp_path / "mallory" / "voices")), None))
        assert client.patch("/api/gateway/voice/clones/v1", headers=user, json={"name": "Mine"}).status_code == 200
        assert client.post("/api/gateway/voice/design", headers=user, json={"description": "a calm voice"}).status_code == 200
        created = client.post(
            "/api/gateway/voice/clones", headers=user, files={"file": ("me.wav", b"RIFF", "audio/wav")}, data={"language": "fr"}
        )
        assert created.status_code == 200
        assert [c for c in facade.calls if c[0] == "clone_voice"][-1][2]["language"] == "fr"


def test_admin_changes_reach_the_facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _FakeFacade()
    with _client(tmp_path, monkeypatch, facade) as client:
        up = client.patch("/api/gateway/voice/clones/v1", headers=ADMIN, json={"name": "Grace", "reference_text": "hello"})
        assert up.status_code == 200 and up.json()["name"] == "Grace"
        assert ("update_cloned_voice", ("v1",), {"name": "Grace", "reference_text": "hello"}) in facade.calls
        assert client.delete("/api/gateway/voice/clones/v1", headers=ADMIN).json()["deleted"] is True
        builtin = client.delete("/api/gateway/voice/clones/builtin-x", headers=ADMIN)
        assert builtin.status_code == 422 and "read-only" in builtin.json()["detail"]
        designed = client.post("/api/gateway/voice/design", headers=ADMIN, json={"description": "a calm voice", "name": "Calm", "language": "en"})
        assert designed.status_code == 200 and designed.json()["voice_id"] == "d1"
        assert ("design_voice", ("a calm voice",), {"name": "Calm", "language": "en", "text": None}) in facade.calls
        assert client.post("/api/gateway/voice/design", headers=ADMIN, json={"description": "  "}).status_code == 422
        cloned = client.post(
            "/api/gateway/voice/clones",
            headers=ADMIN,
            files={"file": ("me.wav", b"RIFF-bytes", "audio/wav")},
            data={"name": "Mine", "provider": "qwen3-tts", "reference_text": "hello"},
        )
        assert cloned.status_code == 200 and cloned.json()["voice_id"] == "c1"
        name, args, kwargs = [c for c in facade.calls if c[0] == "clone_voice"][0]
        assert args == (b"RIFF-bytes",) and kwargs["provider"] == "qwen3-tts" and kwargs["filename"] == "me.wav"
        assert client.post("/api/gateway/voice/clones", headers=ADMIN, files={"file": ("e.wav", b"", "audio/wav")}).status_code == 422


def test_voice_catalog_forwards_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Dict[str, Any]] = []

    class _Discovery:
        def get_voice_catalog(self, **kwargs):
            seen.append(kwargs)
            return {"available": True, "voices": [], "profiles": [], "cloned_voices": [], "tts_languages_by_provider": {"qwen3-tts": ["fr"]}}

    from abstractgateway.routes import gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (_Discovery(), None))
    with _client(tmp_path, monkeypatch, _FakeFacade()) as client:
        resp = client.get("/api/gateway/voice/voices?language=fr&compact=true", headers=ADMIN)
        assert resp.status_code == 200, resp.text
        assert seen[-1]["language"] == "fr"
        assert resp.json().get("tts_languages_by_provider") == {"qwen3-tts": ["fr"]}  # compact keeps it
        client.get("/api/gateway/voice/voices", headers=ADMIN)
        assert seen[-1].get("language") is None


def test_one_shot_is_refused_on_the_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch, _FakeFacade()) as client:
        resp = client.post(
            "/api/gateway/runs/some-run/voice/tts/stream", headers=ADMIN, json={"text": "hello", "one_shot": True}
        )
        assert resp.status_code == 422 and "one_shot" in resp.text


def test_one_shot_reaches_the_speech_output_spec() -> None:
    import inspect

    from abstractgateway.routes import gateway as gateway_routes

    assert "one_shot" in gateway_routes.VoiceTTSRequest.model_fields
    source = inspect.getsource(gateway_routes.voice_tts)
    assert 'output_spec["one_shot"]' in source
