"""SESSION TITLE CONTRACT (backlog 1343): a person renames a conversation.

- `PUT /sessions/{id}/title {"title"}` stores a normalized title in the caller's plane
  (one line, whitespace folded, trimmed, cut at 120 characters); empty / blank / null /
  missing clears it; `{session_id, title, changed}`, a repeat is `changed: false`;
- `GET /runs` rows of a titled session carry `session_title` (the archived listing too),
  rows of an untitled one have no such key; the keyword search matches the title;
- another account's session or an unknown one answers 404 `session_not_found`;
  a body that is not an object or a non-string title is 422 `invalid_request`;
- every change is audited (`session.titled`, set|cleared, never the text); an unreadable
  titles file is a 500 on the route and on `/runs`, never "no titles";
- titles survive a restart; concurrent writers never lose one;
- `GET /discovery/capabilities` says `capabilities.session_titles: true`.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, gateway_env, save_runs


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _put(c: TestClient, sid: str, body, headers=HEADERS):
    return c.put(f"/api/gateway/sessions/{sid}/title", headers=headers, json=body)


def _title(c: TestClient, sid: str, body) -> dict:
    r = _put(c, sid, body)
    assert r.status_code == 200, r.text
    return r.json()


def _roots(c: TestClient, query: str = "", headers=HEADERS) -> dict:
    r = c.get(f"/api/gateway/runs?root_only=true&include_ledger_len=false{query}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _row_titles(page: dict) -> dict:
    return {r["session_id"]: r.get("session_title", "<absent>") for r in page["items"]}


def _audit(c: TestClient) -> list:
    path = c.data_dir / "audit_log.jsonl"  # type: ignore[attr-defined]
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [e for e in (json.loads(x) for x in lines) if e.get("event") == "session.titled"]


def test_set_repeat_and_clear_and_the_runs_listing(client: TestClient) -> None:
    save_runs(chat_run(session_id="s-snake", created_at="2026-11-24T08:00:00+00:00"),
              chat_run(session_id="s-snake", created_at="2026-11-24T09:00:00+00:00"),
              chat_run(session_id="s-other", created_at="2026-11-24T10:00:00+00:00"))
    assert _row_titles(_roots(client)) == {"s-snake": "<absent>", "s-other": "<absent>"}

    assert _title(client, "s-snake", {"title": "Snake game"}) == {"session_id": "s-snake", "title": "Snake game", "changed": True}
    assert _title(client, "s-snake", {"title": "  Snake   game "}) == {"session_id": "s-snake", "title": "Snake game", "changed": False}

    page = _roots(client)
    assert [r.get("session_title") for r in page["items"] if r["session_id"] == "s-snake"] == ["Snake game", "Snake game"]
    assert "session_title" not in next(r for r in page["items"] if r["session_id"] == "s-other")
    direct = client.get("/api/gateway/runs?session_id=s-snake&include_ledger_len=false", headers=HEADERS).json()
    assert {r.get("session_title") for r in direct["items"]} == {"Snake game"}

    stored = json.loads((client.data_dir / "session_titles.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert stored["schema"] == "gateway_session_titles_v1"
    rec = stored["sessions"]["s-snake"]
    assert rec["title"] == "Snake game" and rec["set_at"] and rec["set_by"]["kind"] == "person"

    # Every way of clearing: empty, blank, null, no `title` key.
    for body in ({"title": ""}, {"title": " \n\t "}, {"title": None}, {}):
        _title(client, "s-snake", {"title": "Again"})
        assert _title(client, "s-snake", body) == {"session_id": "s-snake", "title": None, "changed": True}, body
        assert _row_titles(_roots(client))["s-snake"] == "<absent>"
    assert _title(client, "s-snake", {}) == {"session_id": "s-snake", "title": None, "changed": False}
    stored = json.loads((client.data_dir / "session_titles.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert stored["sessions"] == {}


def test_normalisation_keeps_any_script_and_cuts_at_120_characters(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    assert _title(client, "s1", {"title": "Line one\nline\rtwo three\x00\x1b[31mred\x7f\x85end"})["title"] == \
        "Line one line two three [31mred end"
    rtl = "مرحبا שלום"
    assert _title(client, "s1", {"title": f"  {rtl}  "})["title"] == rtl
    family = "\U0001F468‍\U0001F469‍\U0001F467"  # one emoji: ZWJ joiners kept
    assert _title(client, "s1", {"title": f"Trip {family} \U0001F680"})["title"] == f"Trip {family} \U0001F680"

    # 200 astral characters (each two UTF-16 units, four UTF-8 bytes): exactly 120 kept, none split.
    long = _title(client, "s1", {"title": "\U0001F680" * 200})["title"]
    assert long == "\U0001F680" * 120
    assert len(long.encode("utf-16-le")) // 2 == 240 and long.encode("utf-8").decode("utf-8") == long
    mixed = _title(client, "s1", {"title": "a" * 119 + "ééé"})["title"]
    assert mixed == "a" * 119 + "é"
    # A cut that lands on a space is trimmed.
    assert _title(client, "s1", {"title": "b" * 119 + " tail"})["title"] == "b" * 119
    # An unpaired surrogate (valid JSON escape, not a character) never reaches the file.
    r = client.put("/api/gateway/sessions/s1/title", headers={**HEADERS, "Content-Type": "application/json"},
                   content=b'{"title": "x\\ud83dy"}')
    assert r.status_code == 200 and r.json()["title"] == "x y"
    json.loads((client.data_dir / "session_titles.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]


def test_wrong_bodies_are_422_and_change_nothing(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    for body in (["Snake"], "Snake", 7, None):
        r = _put(client, "s1", body)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["detail"]["reason_code"] == "invalid_request"
    for title in (7, True, ["a"], {"t": "a"}):
        r = _put(client, "s1", {"title": title})
        assert r.status_code == 422 and r.json()["detail"]["reason_code"] == "invalid_request", title
    assert not (client.data_dir / "session_titles.json").exists()  # type: ignore[attr-defined]
    assert _audit(client) == []


def test_an_unknown_session_is_404(client: TestClient) -> None:
    r = _put(client, "no-such", {"title": "x"})
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "session_not_found"
    assert not (client.data_dir / "session_titles.json").exists()  # type: ignore[attr-defined]


def test_every_change_is_audited_without_the_title_text(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    _title(client, "s1", {"title": "Private plans"})
    _title(client, "s1", {"title": "Private plans"})  # no-op: not audited
    _title(client, "s1", {"title": "Other"})
    _title(client, "s1", {"title": ""})
    _title(client, "s1", {"title": None})  # no-op
    events = _audit(client)
    assert [(e["session_id"], e["changed"], e["action"]) for e in events] == [("s1", True, "set"), ("s1", True, "set"), ("s1", True, "cleared")]
    assert all("actor" in e for e in events)
    raw = (client.data_dir / "audit_log.jsonl").read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "Private plans" not in raw


def test_an_unreadable_titles_file_is_an_error_not_no_titles(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    path = client.data_dir / "session_titles.json"  # type: ignore[attr-defined]
    for broken in ("{not json", json.dumps({"schema": "something_else", "sessions": {}}), json.dumps({"schema": "gateway_session_titles_v1", "sessions": []})):
        path.write_text(broken, encoding="utf-8")
        r = client.get("/api/gateway/runs?root_only=true&include_ledger_len=false", headers=HEADERS)
        assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unreadable", r.text
        r = _put(client, "s1", {"title": "x"})
        assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unreadable", r.text
        r = client.get("/api/gateway/sessions/search?q=x", headers=HEADERS)
        assert r.status_code == 500, r.text
        assert path.read_text(encoding="utf-8") == broken  # never overwritten


def test_an_archived_session_keeps_its_title(client: TestClient) -> None:
    save_runs(chat_run(session_id="s-arch"), chat_run(session_id="s-keep"))
    _title(client, "s-arch", {"title": "Old project"})
    assert client.post("/api/gateway/sessions/s-arch/archive", headers=HEADERS).status_code == 200
    archived = _roots(client, "&archived_only=true")
    assert _row_titles(archived) == {"s-arch": "Old project"} and archived["items"][0]["archived"] is True
    assert _row_titles(_roots(client)) == {"s-keep": "<absent>"}
    # An archived session can still be renamed (its owner may tidy the archive).
    _title(client, "s-arch", {"title": "Older project"})
    assert _row_titles(_roots(client, "&archived_only=true")) == {"s-arch": "Older project"}


def _turn(session_id: str, prompt: str, answer: str, minute: int):
    from abstractruntime.core.models import RunState, RunStatus

    ts = f"2026-11-24T10:{minute:02d}:00+00:00"
    return RunState(run_id=str(uuid.uuid4()), workflow_id="bundle-target@0.0.0:root", status=RunStatus.COMPLETED, current_node="end",
                    vars={"prompt": prompt, "context": {"messages": []}}, output={"answer": answer},
                    session_id=session_id, created_at=ts, updated_at=ts)


def _search(c: TestClient, q: str, extra: str = "") -> dict:
    r = c.get(f"/api/gateway/sessions/search?q={q}{extra}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_keyword_search_finds_a_conversation_by_its_title(client: TestClient) -> None:
    save_runs(_turn("s-game", "write me a game in python", "Here is pong.", 1),
              _turn("s-game", "make it faster", "Done.", 2),
              _turn("s-mail", "draft a reply about the snake exhibit", "Dear team...", 3),
              _turn("s-plain", "what is the weather", "Sunny.", 4))
    assert [i["session_id"] for i in _search(client, "snake")["items"]] == ["s-mail"]
    _title(client, "s-game", {"title": "Snake game été"})

    items = _search(client, "snake")["items"]
    assert [i["session_id"] for i in items] == ["s-game", "s-mail"]  # the title match first
    hit = items[0]
    assert hit["matched_in"] == "title" and hit["session_title"] == "Snake game été"
    assert hit["snippet"] == "Snake game été" and hit["highlights"] == [[0, 5]]
    assert hit["prompt"] == "make it faster" and hit["run_id"] and hit["archived"] is False
    assert "session_title" not in items[1]
    # Folded like the index: case and accents ignored, every word must be there.
    assert [i["session_id"] for i in _search(client, "SNAKE%20ete")["items"]] == ["s-game"]
    assert _search(client, "snake%20chess")["items"] == []
    # Matching both its title and a turn: one item, the turn's own snippet, the title carried.
    both = _search(client, "pong")["items"]
    assert [i["session_id"] for i in both] == ["s-game"] and both[0]["session_title"] == "Snake game été"
    # Archived: out of the default search, found with archived=true.
    assert client.post("/api/gateway/sessions/s-game/archive", headers=HEADERS).status_code == 200
    assert [i["session_id"] for i in _search(client, "snake")["items"]] == ["s-mail"]
    found = _search(client, "snake", "&archived=true")["items"]
    assert [i["session_id"] for i in found] == ["s-game"] and found[0]["archived"] is True


def test_titles_survive_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway import session_titles
    from abstractgateway.app import app

    with TestClient(app) as c:
        save_runs(chat_run(session_id="s-keep"))
        _title(c, "s-keep", {"title": "Kept across restarts"})
    session_titles._READ_CACHE.clear()  # the new app must read the file, not this process's memory
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    with TestClient(app) as c:
        assert _row_titles(_roots(c)) == {"s-keep": "Kept across restarts"}


def test_two_concurrent_writers_lose_nothing(tmp_path: Path) -> None:
    from abstractgateway.session_titles import session_titles, set_session_title

    errors: list = []

    def writer(n: int) -> None:
        try:
            for i in range(25):
                set_session_title(tmp_path, f"s-{n}-{i}", f"Title {n} {i}", by=f"user-{n}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    titles = session_titles(tmp_path)
    assert len(titles) == 50 and titles["s-1-24"] == "Title 1 24" and titles["s-2-0"] == "Title 2 0"


def test_the_capability_is_advertised(client: TestClient) -> None:
    r = client.get("/api/gateway/discovery/capabilities", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["capabilities"]["session_titles"] is True


@pytest.fixture()
def accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    _c, root = reg.create_user(user_id="root", roles=["admin"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, reg, {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}, {"Authorization": f"Bearer {root}"}


def _save_in_plane_of(reg, headers: dict, run) -> None:
    from abstractgateway.service import get_gateway_service_for_principal

    principal = reg.authenticate(headers["Authorization"].split(" ", 1)[1])
    assert principal is not None
    get_gateway_service_for_principal(principal).host.run_store.save(run)


def test_the_owner_or_an_admin_titles_a_session_another_account_gets_404(accounts) -> None:
    c, reg, alice, bob, root = accounts
    _save_in_plane_of(reg, alice, _turn("s-alice", "alice's question", "answer", 1))
    _save_in_plane_of(reg, root, _turn("s-root", "the operator's question", "answer", 2))

    r = _put(c, "s-alice", {"title": "Bob was here"}, headers=bob)
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "session_not_found"
    r = _put(c, "s-alice", {"title": "Mine"}, headers=alice)
    assert r.status_code == 200 and r.json()["title"] == "Mine"
    assert _row_titles(_roots(c, headers=alice)) == {"s-alice": "Mine"}
    assert _roots(c, headers=bob)["items"] == []
    # An admin: their own plane's sessions, exactly like archive (another account's plane is not theirs).
    r = _put(c, "s-root", {"title": "Ops"}, headers=root)
    assert r.status_code == 200 and r.json()["changed"] is True
    assert _row_titles(_roots(c, headers=root)) == {"s-root": "Ops"}
    r = _put(c, "s-alice", {"title": "Admin rename"}, headers=root)
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "session_not_found"
    assert _row_titles(_roots(c, headers=alice)) == {"s-alice": "Mine"}
