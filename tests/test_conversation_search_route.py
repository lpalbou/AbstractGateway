"""`GET /api/gateway/sessions/search`: keyword search over the caller's conversations.

Questions and answers are found (words, substrings, accents, a typo), filtered like the
conversation listing (archived out unless asked, docs chats out), one item per session; and an
account never finds another account's conversations (planes are per principal).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env, save_runs


def turn(session_id: str, prompt: str, answer: str, minute: int):
    from abstractruntime.core.models import RunState, RunStatus

    ts = f"2026-10-18T10:{minute:02d}:00+00:00"
    return RunState(run_id=str(uuid.uuid4()), workflow_id="bundle-target@0.0.0:root", status=RunStatus.COMPLETED, current_node="end",
                    vars={"prompt": prompt, "context": {"messages": []}}, output={"answer": answer},
                    session_id=session_id, created_at=ts, updated_at=ts)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        save_runs(turn("s-km", "Compare overall survival by treatment arm", "Arm B: HR 0.78.", 1),
                  turn("s-km", "Now split it by age group", "Under 65: HR 0.71.", 2),
                  turn("s-qc", "Did the nightly assay QC pass?", "Yes: all 96 wells within range.", 3),
                  turn("s-fr", "Quelle est la météo à Paris ?", "Il fait beau.", 4))
        yield c


def search(c: TestClient, q: str, extra: str = "", headers=HEADERS) -> dict:
    r = c.get(f"/api/gateway/sessions/search?q={q}{extra}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_questions_and_answers_are_found(client: TestClient) -> None:
    body = search(client, "survival")
    assert [i["session_id"] for i in body["items"]] == ["s-km"] and body["indexing"]["complete"] is True
    item = body["items"][0]
    assert item["matched_in"] == "question" and item["archived"] is False and item["highlights"]
    assert [i["session_id"] for i in search(client, "wells")["items"]] == ["s-qc"]
    assert [i["session_id"] for i in search(client, "meteo")["items"]] == ["s-fr"]
    assert [i["session_id"] for i in search(client, "survivl")["items"]] == ["s-km"]
    assert search(client, "HR")["count"] == 1                      # one item per session


def test_archived_conversations_are_left_out_unless_asked(client: TestClient) -> None:
    assert client.post("/api/gateway/sessions/s-qc/archive", headers=HEADERS).status_code == 200
    assert search(client, "wells")["items"] == []
    found = search(client, "wells", "&archived=true")["items"]
    assert [i["session_id"] for i in found] == ["s-qc"] and found[0]["archived"] is True


def test_a_new_turn_is_found_on_the_next_search(client: TestClient) -> None:
    assert search(client, "plate")["items"] == []
    save_runs(turn("s-qc", "And plate P07?", "P07 was re-read.", 5))
    assert [i["session_id"] for i in search(client, "plate")["items"]] == ["s-qc"]


def test_a_query_is_required(client: TestClient) -> None:
    assert client.get("/api/gateway/sessions/search?q=", headers=HEADERS).status_code == 422
    assert client.get("/api/gateway/sessions/search?q=x", headers={}).status_code in (401, 403)


@pytest.fixture()
def two_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    (tmp_path / "bundles").mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"])
    _b, bob = reg.create_user(user_id="bob", roles=["user"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}


def test_an_account_never_finds_another_accounts_conversations(two_users) -> None:
    from abstractgateway.service import get_gateway_service_for_principal
    from abstractgateway.users import GatewayUserRegistry

    c, alice, bob = two_users
    assert search(c, "zebra", headers=alice)["items"] == []
    # A turn in Alice's own plane (the plane a request as Alice selects).
    principal = GatewayUserRegistry().authenticate(alice["Authorization"].split(" ", 1)[1])
    assert principal is not None and principal.user_id == "alice"
    get_gateway_service_for_principal(principal).host.run_store.save(turn("s-zebra", "Zebra migration patterns", "They follow the rains.", 1))
    assert [i["session_id"] for i in search(c, "zebra", headers=alice)["items"]] == ["s-zebra"]
    assert search(c, "zebra", headers=bob)["items"] == []          # Bob's plane has no such conversation


def test_archived_matches_are_found_behind_many_listed_ones(client: TestClient) -> None:
    save_runs(*[turn(f"s-many-{i}", f"quarterly budget review {i}", "ok", i % 60) for i in range(90)])
    save_runs(turn("s-old", "quarterly budget review archived", "ok", 59))
    assert client.post("/api/gateway/sessions/s-old/archive", headers=HEADERS).status_code == 200
    archived = search(client, "budget", "&archived=true")["items"]
    assert [i["session_id"] for i in archived] == ["s-old"]
    listed = search(client, "budget", "&limit=50")
    assert listed["count"] == 50 and "s-old" not in [i["session_id"] for i in listed["items"]]
    assert listed["indexing"]["errors"] == 0


def test_a_generated_image_is_found_by_what_was_asked_of_it(client: TestClient) -> None:
    from abstractgateway.service import get_gateway_service
    from abstractruntime.storage.artifacts import build_artifact_descriptor_payload

    run = turn("s-art", "Make me something for the hall", "Here is your picture.", 6)
    save_runs(run)
    descriptor, metadata = build_artifact_descriptor_payload(semantic_kind="image", render_kind="image", modality="image",
                                                              session_id="s-art", run_id=run.run_id,
                                                              generation={"prompt": "A lighthouse at dusk, watercolour"})
    get_gateway_service().host.artifact_store.store(
        b"\x89PNG", content_type="image/png", run_id=run.run_id, artifact_id="img-lighthouse",
        tags={"kind": "generated_media", "modality": "image", "run_id": run.run_id, "session_id": "s-art"},
        metadata=metadata, descriptor=descriptor)
    item = search(client, "lighthouse")["items"][0]
    assert item["session_id"] == "s-art" and item["matched_in"] == "artifact"
    assert item["artifact"]["artifact_id"] == "img-lighthouse" and item["artifact"]["run_id"] == run.run_id
    assert item["artifact"]["content_type"] == "image/png"
    # The owning run serves it on the gateway's own route.
    r = client.get(f"/api/gateway/runs/{run.run_id}/artifacts/img-lighthouse/content", headers=HEADERS)
    assert r.status_code == 200 and r.content == b"\x89PNG"
