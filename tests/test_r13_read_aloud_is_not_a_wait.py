"""R13.1: Read aloud never puts the run into a wait (gateway + the REAL runtime facade).

The incident's second half: the Read-aloud stream created a child of the LIVE
run parked on `WAIT_EVENT abstractcore.voice.tts.stream:<uuid>`; the client
folded that waiting record into the run ("Waiting for an event › Streaming
voice synthesis is running." + "Event routing unavailable: the gateway did
not provide a canonical event wait key"). The model decided in R13.1: reading
a reply aloud is a voice request next to the run, never a wait of it — the
durable child run is recorded COMPLETED when the stream ends.

This drives `POST /runs/{id}/voice/tts/stream` through the gateway's own
runtime and run facade (only the speech engine is fake) and inspects the run
store while audio is streaming.
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic

TOKEN = "r13-wait-token"


def _waiting_children(rs: Any, parent_id: str) -> List[Any]:
    return [r for r in rs.list_runs(status=None, limit=1000) if getattr(r, "parent_run_id", None) == parent_id and str(getattr(r.status, "value", r.status)) == "waiting"]


def test_read_aloud_through_the_real_facade_never_creates_a_waiting_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    flows = tmp_path / "flows"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: {})
    observed: Dict[str, Any] = {}

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {TOKEN}"}
        client.get("/api/gateway/runs/session_memory_r13w", headers=h)  # boot gate
        svc = get_gateway_service()
        runtime = svc.host.runtime
        rs = svc.host.run_store

        class _FakeSpeech:
            def stream_tts(self, *, text, output=None, params=None):
                parent = (params or {}).get("trace_metadata", {}).get("parent_run_id")
                yield {"type": "start", "ok": True}
                observed["mid_stream_waiting_children"] = [r.run_id for r in _waiting_children(rs, parent)]
                observed["mid_stream_children"] = [r.run_id for r in rs.list_runs(limit=1000) if getattr(r, "parent_run_id", None) == parent]
                yield {"type": "audio", "sequence": 0, "content_type": "audio/wav", "audio_b64": base64.b64encode(b"RIFF0").decode()}
                yield {"type": "done", "ok": True, "chunks": 1}

        monkeypatch.setattr(runtime, "_abstractcore_llm_client", _FakeSpeech(), raising=False)
        resp = client.post(
            "/api/gateway/runs/session_memory_r13w/voice/tts/stream",
            headers=h,
            json={"text": "Found it. Here is what I can tell you.", "format": "wav", "provider": "supertonic"},
        )
        assert resp.status_code == 200, resp.text
        events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
        parent_id = events[0]["run_id"]
        children = [r for r in rs.list_runs(limit=1000) if getattr(r, "parent_run_id", None) == parent_id]
        ledger = []
        for c in children:
            ledger.extend(svc.host.ledger_store.list(c.run_id))

    assert [e["type"] for e in events] == ["runtime_start", "start", "audio", "done"], events
    assert "wait_key" not in events[0]
    assert observed["mid_stream_waiting_children"] == [], "the read-aloud stream parked a child run on a wait"
    assert observed["mid_stream_children"] == [], "a child run existed while audio streamed"
    assert [str(getattr(c.status, "value", c.status)) for c in children] == ["completed"]
    assert children[0].run_id == events[0]["child_run_id"] == events[-1]["child_run_id"]
    assert not [r for r in ledger if (r.get("status") if isinstance(r, dict) else None) == "waiting"], ledger
