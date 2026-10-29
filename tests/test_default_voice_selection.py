"""A voice can be selected if and only if its model is downloaded (operator ruling 2026-10-29).

Setting the default voice (`PUT /config/capability-defaults/output/voice`) to a local model whose
weights are not on disk is refused at SELECTION time: 409 with the sentence as `detail` (a string
every client prints) and the download action under `error`. Synthesis itself never checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str):
    import abstractgateway.core_config as core_config

    store = tmp_path / "config" / "abstractcore.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    probes: list = []

    def availability(rows):
        probes.append([dict(r) for r in rows])
        return [dict(r, availability={"provider": r["provider"], "artifact": r["model"], "status": status, "downloadable": True}) for r in rows]

    monkeypatch.setattr(core_config.config_facade, "annotate_model_availability", availability)
    monkeypatch.setattr(core_config.config_facade, "model_catalog", lambda q=None, **kw: {
        "rows": [{"artifacts": [{"provider": "supertonic", "artifact": "supertonic-3", "download_bytes": 401_276_744}]}]})
    from abstractgateway.app import app

    return app, {"Authorization": "Bearer t"}, store, probes


def test_a_default_voice_that_is_not_downloaded_is_refused_with_its_download(tmp_path, monkeypatch) -> None:
    app, headers, store, _probes = _client(tmp_path, monkeypatch, "absent")
    with TestClient(app) as c:
        r = c.put("/api/gateway/config/capability-defaults/output/voice", headers=headers, json={"provider": "supertonic", "model": "supertonic-3"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], str) and "supertonic-3 (401 MB) is not downloaded on this computer yet, so it cannot be the default voice" in body["detail"]
    assert body["error"]["reason_code"] == "model_not_downloaded" and body["error"]["size_mb"] == 401
    assert body["error"]["download"] == {"method": "POST", "path": "/api/gateway/models/download", "body": {"provider": "supertonic", "artifact": "supertonic-3"}}
    routes = json.loads(store.read_text())["capability_defaults"]["routes"]
    assert "output.voice" not in routes, "nothing is saved"


def test_a_downloaded_voice_is_saved_and_other_routes_are_not_checked(tmp_path, monkeypatch) -> None:
    app, headers, store, probes = _client(tmp_path, monkeypatch, "installed")
    with TestClient(app) as c:
        r = c.put("/api/gateway/config/capability-defaults/output/voice", headers=headers, json={"provider": "supertonic", "model": "supertonic-3"})
        assert r.status_code == 200, r.text
        n = len(probes)
        r = c.put("/api/gateway/config/capability-defaults/output/text", headers=headers, json={"provider": "lmstudio", "model": "qwen3-30b"})
        assert r.status_code == 200, r.text
    assert json.loads(store.read_text())["capability_defaults"]["routes"]["output.voice"]["model"] == "supertonic-3"
    assert len(probes) == n, "the text route is not checked here"
