"""Per-principal attention preferences and `POST /automations/{id}/seen` (contracts C7, F)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway.automation_attention import (
    AttentionCursorError,
    AttentionPreferenceStore,
    AttentionStoreCorrupt,
    parse_attention_cursor,
    principal_key,
)
from automations_fixtures import HEADERS, chat_run, controller_run, gateway_env, save_runs


# --------------------------------------------------------------------- store


def test_principal_tuple_is_unambiguous(tmp_path: Path) -> None:
    a = AttentionPreferenceStore(tmp_path, tenant="a_b", user="c")
    b = AttentionPreferenceStore(tmp_path, tenant="a", user="b_c")
    assert a.path != b.path
    a.mark_seen("auto-1", 3)
    assert b.seen_seq("auto-1") == 0
    assert a.seen_seq("auto-1") == 3


def test_traversal_user_id_stays_in_the_directory(tmp_path: Path) -> None:
    store = AttentionPreferenceStore(tmp_path, tenant="default", user="../../etc/passwd")
    store.mark_seen("auto-1", 1)
    assert store.path.parent == tmp_path / "automations" / "attention"
    assert store.path.name == principal_key("default", "../../etc/passwd") + ".json"
    assert [p.name for p in (tmp_path / "automations" / "attention").iterdir()] == [store.path.name]
    data = json.loads(store.path.read_text())
    assert data["principal"] == ["default", "../../etc/passwd"]
    assert data["schema_version"] == 1


def test_seen_is_monotonic(tmp_path: Path) -> None:
    store = AttentionPreferenceStore(tmp_path, tenant="default", user="u")
    assert store.mark_seen("a", 5) == (5, True)
    assert store.mark_seen("a", 2) == (5, False)
    assert store.mark_seen("a", 5) == (5, False)
    assert store.mark_seen("a", 7) == (7, True)
    assert store.seen_seq("a") == 7
    assert store.seen_seq("other") == 0


def test_a_file_of_another_principal_is_refused(tmp_path: Path) -> None:
    store = AttentionPreferenceStore(tmp_path, tenant="default", user="u")
    store.mark_seen("a", 1)
    data = json.loads(store.path.read_text())
    data["principal"] = ["default", "someone-else"]
    store.path.write_text(json.dumps(data))
    with pytest.raises(AttentionStoreCorrupt):
        store.seen_seq("a")


def test_concurrent_writers_keep_the_max(tmp_path: Path) -> None:
    store = AttentionPreferenceStore(tmp_path, tenant="default", user="u")
    threads = [threading.Thread(target=store.mark_seen, args=("a", n)) for n in range(1, 41)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.seen_seq("a") == 40


@pytest.mark.parametrize("bad", ["", "att1:", "att1:-1", "att1:01", "att2:3", "3", None, 3])
def test_cursor_format(bad) -> None:
    with pytest.raises(AttentionCursorError):
        parse_attention_cursor(bad)
    assert parse_attention_cursor("att1:0") == 0
    assert parse_attention_cursor("att1:12") == 12


# --------------------------------------------------------------------- route


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def test_seen_route_is_monotonic_and_bounded(client: TestClient) -> None:
    controller = controller_run(attention_seq=4)
    save_runs(controller)
    url = f"/api/gateway/automations/{controller.run_id}/seen"

    r = client.post(url, headers=HEADERS, json={"attention_cursor": "att1:3"})
    assert r.status_code == 200, r.text
    assert r.json() == {"attention_cursor": "att1:3"}
    # Older cursor: ignored, the stored one comes back.
    assert client.post(url, headers=HEADERS, json={"attention_cursor": "att1:1"}).json() == {"attention_cursor": "att1:3"}
    assert client.post(url, headers=HEADERS, json={"attention_cursor": "att1:4"}).json() == {"attention_cursor": "att1:4"}

    # Beyond the latest attention item: refused.
    r = client.post(url, headers=HEADERS, json={"attention_cursor": "att1:5"})
    assert r.status_code == 422
    assert r.json() == {"detail": {"reason_code": "invalid_request", "message": r.json()["detail"]["message"], "field": "attention_cursor"}}

    r = client.post(url, headers=HEADERS, json={"attention_cursor": "latest"})
    assert r.status_code == 422 and r.json()["detail"]["reason_code"] == "invalid_request"

    # Stored in the plane, under the principal's hashed file.
    from abstractgateway.service import get_gateway_service

    files = list((Path(get_gateway_service().config.data_dir) / "automations" / "attention").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["automations"][controller.run_id]["attention_cursor"] == "att1:4"


def test_seen_on_a_run_that_is_not_an_automation_is_404(client: TestClient) -> None:
    chat = chat_run(session_id="s")
    save_runs(chat)
    for target in (chat.run_id, "does-not-exist"):
        r = client.post(f"/api/gateway/automations/{target}/seen", headers=HEADERS, json={"attention_cursor": "att1:0"})
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["reason_code"] == "automation_not_found"
