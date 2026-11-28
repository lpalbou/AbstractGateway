"""SESSION TITLE CONTRACT (backlog 1343): a person renames a conversation.

- `PUT /sessions/{id}/title {"title"}` stores a normalized title in the caller's plane
  (one line, whitespace folded, trimmed, cut at 120 characters); empty / blank / null /
  missing clears it; `{session_id, title, changed}`, a repeat is `changed: false`;
- `GET /runs` rows of a titled session carry `session_title` (the archived listing too),
  rows of an untitled one have no such key; the keyword search matches the title;
- another account's session or an unknown one answers 404 `session_not_found`;
  a body that is not an object or a non-string title is 422 `invalid_request`;
- every change is audited AFTER it is saved (`session.titled`, set|cleared, never the text);
- an unreadable titles file never takes a listing down (`session_titles_error`, no titles);
  a damaged one (not JSON) heals at the next rename, a foreign one is never touched;
- the store: a folder that refuses new files, a file held open, orphan temp files, two
  processes (json_store.py, shared with the archive);
- titles survive a restart; concurrent writers never lose one;
- `GET /discovery/capabilities` says `capabilities.session_titles: true`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, gateway_env, save_runs
from abstractgateway import workflow_folder


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


def _forget(c: TestClient) -> None:
    """A hand edit within the read cache's second: read the file again."""
    from abstractgateway import session_titles

    session_titles._STORE.forget(c.data_dir)  # type: ignore[attr-defined]


def _no_absolute_path(message: str, data_dir: Path) -> None:
    assert str(data_dir) not in message and str(data_dir.parent) not in message and ":\\" not in message, message


def _listings_report_the_error(c: TestClient, data_dir: Path) -> None:
    """Every /runs variant and the search answer 200 without titles and say why."""
    for path in ("/api/gateway/runs?root_only=true&include_ledger_len=false",
                 "/api/gateway/runs?session_id=s1&include_ledger_len=false",
                 "/api/gateway/runs?include_ledger_len=false",
                 "/api/gateway/runs?root_only=true&archived_only=true&include_ledger_len=false",
                 "/api/gateway/runs?parent_run_id=nope&include_ledger_len=false",
                 "/api/gateway/sessions/search?q=x"):
        r = c.get(path, headers=HEADERS)
        assert r.status_code == 200, (path, r.text)
        body = r.json()
        err = body.get("session_titles_error")
        assert err and err["reason_code"] == "session_titles_unreadable" and set(err) == {"reason_code", "message"}, (path, body)
        _no_absolute_path(err["message"], data_dir)
        assert all("session_title" not in i for i in body["items"]), path


def test_a_damaged_titles_file_never_takes_the_list_down_and_heals_at_the_next_rename(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    _title(client, "s1", {"title": "Before"})
    assert "session_titles_error" not in _roots(client)
    path = data_dir / "session_titles.json"
    damaged = b'{"schema": "gateway_session_titles_v1", "sessions": {"s1": {"tit'
    path.write_bytes(damaged)
    _forget(client)
    _listings_report_the_error(client, data_dir)

    r = _put(client, "s1", {"title": "After the repair"})
    asides = sorted(p.name for p in data_dir.glob("session_titles.damaged-*.json"))
    assert len(asides) == 1 and (data_dir / asides[0]).read_bytes() == damaged
    assert r.status_code == 200 and r.json() == {"session_id": "s1", "title": "After the repair", "changed": True,
                                                 "store_reset": asides[0]}, r.text
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"]["s1"]["title"] == "After the repair"
    assert _audit(client)[-1]["store_reset"] == asides[0]
    page = _roots(client)
    assert "session_titles_error" not in page and _row_titles(page)["s1"] == "After the repair"
    assert "store_reset" not in _title(client, "s1", {"title": "Next"})  # only the rename that reset it


def test_a_valid_file_of_another_schema_is_never_touched(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    path = data_dir / "session_titles.json"
    valid = json.dumps({"schema": "gateway_session_titles_v1", "sessions": {"s1": {"title": "Café"}}}, ensure_ascii=False)
    for foreign in (json.dumps({"schema": "gateway_session_titles_v2", "sessions": {}}).encode("utf-8"),
                    json.dumps({"schema": "gateway_session_titles_v1", "sessions": []}).encode("utf-8"), b"[1, 2]",
                    # A valid store re-saved in another encoding (PowerShell 5 `>`, Notepad "ANSI"): never "damaged".
                    b"\xff\xfe" + valid.encode("utf-16-le"), valid.encode("cp1252")):
        path.write_bytes(foreign)
        _forget(client)
        _listings_report_the_error(client, data_dir)
        r = _put(client, "s1", {"title": "x"})
        assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unreadable", r.text
        _no_absolute_path(r.json()["detail"]["message"], data_dir)
        assert path.read_bytes() == foreign
        assert not list(data_dir.glob("session_titles.damaged-*"))
    assert _audit(client) == []


def test_a_transient_read_failure_is_not_damage(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import json_store

    save_runs(chat_run(session_id="s1"))
    _title(client, "s1", {"title": "Kept"})
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    path = data_dir / "session_titles.json"
    before = path.read_bytes()
    real = Path.read_bytes

    def refused(self: Path) -> bytes:
        if self.name == "session_titles.json":
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(self)

    monkeypatch.setattr(json_store, "RETRY_S", 0.1)
    monkeypatch.setattr(Path, "read_bytes", refused)
    r = _put(client, "s1", {"title": "New"})
    assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unreadable", r.text
    monkeypatch.setattr(Path, "read_bytes", real)
    assert path.read_bytes() == before and not list(data_dir.glob("session_titles.damaged-*"))


def test_a_bom_file_and_hand_edited_titles_read_as_stored_titles(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"), chat_run(session_id="s2"), chat_run(session_id="s3"))
    path = client.data_dir / "session_titles.json"  # type: ignore[attr-defined]
    doc = {"schema": "gateway_session_titles_v1", "sessions": {
        "s1": {"title": "Notepad édit"},
        "s2": {"title": "x\n\u0000‮" + "y" * 1_000_000},
        "s3": {"title": 7}}}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    page = _roots(client)
    assert "session_titles_error" not in page
    assert _row_titles(page) == {"s1": "Notepad édit", "s2": "x " + "y" * 118, "s3": "<absent>"}


def test_the_listing_logs_a_broken_store_once_per_change(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    save_runs(chat_run(session_id="s1"))
    path = client.data_dir / "session_titles.json"  # type: ignore[attr-defined]
    caplog.set_level(logging.INFO, logger="abstractgateway.session_titles")
    path.write_text("{broken", encoding="utf-8")
    _forget(client)
    for _ in range(3):
        _roots(client)
        time.sleep(0.01)
    path.unlink()
    from abstractgateway import session_titles

    session_titles._STORE.forget(client.data_dir)  # type: ignore[attr-defined]
    _roots(client)
    _roots(client)
    lines = [r.getMessage() for r in caplog.records if r.name == "abstractgateway.session_titles"]
    assert len([m for m in lines if "could not be read" in m]) == 1 and len([m for m in lines if "readable again" in m]) == 1, lines


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
    data_dir = gateway_env(monkeypatch, tmp_path)
    from abstractgateway import session_titles
    from abstractgateway.app import app

    with TestClient(app) as c:
        save_runs(chat_run(session_id="s-keep"))
        _title(c, "s-keep", {"title": "Kept across restarts"})
    session_titles._STORE.forget(data_dir)  # the new app must read the file, not this process's memory
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
    monkeypatch.setattr(workflow_folder, "chosen", Path(tmp_path / "bundles").resolve())
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


def _plane_dir(reg, headers: dict) -> Path:
    from abstractgateway.service import get_gateway_service_for_principal

    return Path(get_gateway_service_for_principal(reg.authenticate(headers["Authorization"].split(" ", 1)[1])).config.data_dir)


def test_two_accounts_titles_never_meet_and_who_renamed_is_recorded(accounts) -> None:
    c, reg, alice, bob, _root = accounts
    _save_in_plane_of(reg, alice, _turn("s-same", "alice's", "a", 1))
    _save_in_plane_of(reg, bob, _turn("s-same", "bob's", "b", 2))
    assert _put(c, "s-same", {"title": "Alice's name"}, headers=alice).status_code == 200
    assert _put(c, "s-same", {"title": "Bob's name"}, headers=bob).status_code == 200
    assert _row_titles(_roots(c, headers=alice)) == {"s-same": "Alice's name"}
    assert _row_titles(_roots(c, headers=bob)) == {"s-same": "Bob's name"}
    a_dir, b_dir = _plane_dir(reg, alice), _plane_dir(reg, bob)
    assert a_dir != b_dir
    a_doc = json.loads((a_dir / "session_titles.json").read_text(encoding="utf-8"))
    b_doc = json.loads((b_dir / "session_titles.json").read_text(encoding="utf-8"))
    assert a_doc["sessions"]["s-same"]["title"] == "Alice's name" and a_doc["sessions"]["s-same"]["set_by"] == {"kind": "person", "user_id": "alice"}
    assert b_doc["sessions"]["s-same"]["title"] == "Bob's name" and b_doc["sessions"]["s-same"]["set_by"]["user_id"] == "bob"
    from abstractgateway.users import gateway_data_dir_from_env

    root_file = Path(gateway_data_dir_from_env()) / "session_titles.json"
    assert root_file not in (a_dir / "session_titles.json", b_dir / "session_titles.json") and not root_file.exists()
    # The audit line names who renamed.
    audit_lines = []
    for p in Path(gateway_data_dir_from_env()).rglob("audit_log.jsonl"):
        audit_lines += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    actors = sorted(e["actor"] for e in audit_lines if e.get("event") == "session.titled")
    assert actors == ["alice", "bob"], audit_lines


# --- the store (json_store.py, shared with the archive) ----------------------------------------


def _icacls(path: Path, *args: str) -> None:
    subprocess.run(["icacls", str(path), *args], capture_output=True, text=True, check=True)


def _call_with_deadline(fn, timeout_s: float = 30.0):
    """A request that hangs fails the test instead of hanging the suite."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn()), daemon=True)
    t.start()
    t.join(timeout_s)
    assert not t.is_alive(), f"no answer within {timeout_s} s (the gateway hangs)"
    return box["r"]


@pytest.mark.skipif(os.name != "nt", reason="Windows access lists (icacls)")
@pytest.mark.parametrize("store", ["titles", "archive"])
def test_a_folder_that_refuses_new_files_is_an_immediate_error_not_a_hang(client: TestClient, store: str) -> None:
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"), chat_run(session_id="s2"))
    # One successful write first: the store and its lock file exist, only NEW files are refused.
    if store == "titles":
        _title(client, "s1", {"title": "Before"})
        call = lambda: _put(client, "s1", {"title": "After"})  # noqa: E731
        reason = "session_titles_unwritable"
        name = "session_titles.json"
    else:
        assert client.post("/api/gateway/sessions/s2/archive", headers=HEADERS).status_code == 200
        call = lambda: client.post("/api/gateway/sessions/s1/archive", headers=HEADERS)  # noqa: E731
        reason = "session_archive_unwritable"
        name = "session_archive.json"
    before = (data_dir / name).read_bytes()
    user = os.environ["USERNAME"]
    _icacls(data_dir, "/deny", f"{user}:(WD,AD)")
    try:
        t0 = time.monotonic()
        r = _call_with_deadline(call)
        assert time.monotonic() - t0 < 10
        assert r.status_code == 500 and r.json()["detail"]["reason_code"] == reason, r.text
        _no_absolute_path(r.json()["detail"]["message"], data_dir)
        # The gateway keeps answering.
        assert _call_with_deadline(lambda: client.get("/api/gateway/ping", headers=HEADERS)).status_code == 200
        assert _call_with_deadline(lambda: client.get("/api/gateway/runs?root_only=true&include_ledger_len=false", headers=HEADERS)).status_code == 200
    finally:
        _icacls(data_dir, "/remove:d", user)
    assert (data_dir / name).read_bytes() == before
    assert not list(data_dir.glob(name + ".*.tmp"))
    if store == "titles":
        assert [e["action"] for e in _audit(client)] == ["set"]  # the refused write is not audited


@pytest.mark.skipif(os.name != "nt", reason="Windows access lists (icacls)")
def test_the_first_listing_on_a_folder_that_refuses_new_files_answers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (round 2): the first listing of a plane runs the one-time session-kind migrations,
    whose save (session_kinds.py, then tempfile.mkstemp) retried a refused create up to TMP_MAX times
    while holding the migration lock: every later listing waited on it. It now uses json_store.py,
    a refused lock-file create fails at once, and a failed migration is not retried for a minute."""
    from abstractgateway import session_kinds

    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"))
    attempts: list = []
    real = session_kinds.migrate_sessions
    monkeypatch.setattr(session_kinds, "migrate_sessions", lambda *a, **k: (attempts.append(1), real(*a, **k))[1])
    user = os.environ["USERNAME"]
    _icacls(data_dir, "/deny", f"{user}:(WD,AD)")
    try:
        for n in range(3):
            t0 = time.monotonic()
            r = _call_with_deadline(lambda: client.get("/api/gateway/runs?root_only=true&include_ledger_len=false", headers=HEADERS))
            assert r.status_code == 200 and [i["session_id"] for i in r.json()["items"]] == ["s1"], r.text
            if n:
                assert time.monotonic() - t0 < 0.5, "a later listing pays no retry"
        rename_t0 = time.monotonic()
        r = _call_with_deadline(lambda: _put(client, "s1", {"title": "Refused"}))
        assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unwritable", r.text
        assert time.monotonic() - rename_t0 < 0.5, "a refused lock-file create fails at once"
        assert len(attempts) == 1, attempts  # not retried within the minute
        # The search answers too (its own index file may be refused: an answer, never a hang).
        _call_with_deadline(lambda: client.get("/api/gateway/sessions/search?q=x", headers=HEADERS))
    finally:
        _icacls(data_dir, "/remove:d", user)
    assert not list(data_dir.glob("session_kinds.json.*.tmp"))
    # Lifted, and the retry delay over: the next listing runs (and saves) the migrations.
    monkeypatch.setattr(session_kinds, "MIGRATION_RETRY_S", 0.0)
    assert _roots(client)["items"][0]["session_id"] == "s1"
    assert (data_dir / "session_kinds.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to replace a file another handle holds open")
def test_a_file_held_open_is_retried_then_refused_cleanly(client: TestClient) -> None:
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"))
    _title(client, "s1", {"title": "Kept"})
    path = data_dir / "session_titles.json"
    before = path.read_bytes()
    # Held for the whole retry window: 500, store intact, no temp file, no audit line.
    with open(path, "rb"):
        r = _call_with_deadline(lambda: _put(client, "s1", {"title": "Lost?"}))
    assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unwritable", r.text
    _no_absolute_path(r.json()["detail"]["message"], data_dir)
    assert "Internal error" not in r.text
    assert path.read_bytes() == before and not list(data_dir.glob("session_titles.json.*.tmp"))
    assert [e["action"] for e in _audit(client)] == ["set"]
    # Held briefly (a scanner): the bounded retry gets through.
    fh = open(path, "rb")
    threading.Timer(0.3, fh.close).start()
    r = _call_with_deadline(lambda: _put(client, "s1", {"title": "After the scan"}))
    assert r.status_code == 200 and r.json()["title"] == "After the scan", r.text


def test_a_failed_replace_leaves_the_store_whole_and_unaudited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import json_store

    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"))
    _title(client, "s1", {"title": "Whole"})
    path = data_dir / "session_titles.json"
    before = path.read_bytes()
    seen: list = []

    def broken_replace(src: str, dst: str) -> None:
        seen.append((Path(src).name, Path(dst).name))
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(json_store, "_os_replace", broken_replace)  # the module's own reference only
    r = _put(client, "s1", {"title": "Half written?"})
    assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_titles_unwritable", r.text
    # The new content only ever went to a temp file, replaced in one step.
    assert len(seen) == 1 and seen[0][1] == "session_titles.json" and seen[0][0].startswith("session_titles.json.") and seen[0][0].endswith(".tmp")
    assert path.read_bytes() == before and not list(data_dir.glob("session_titles.json.*.tmp"))
    assert [e["action"] for e in _audit(client)] == ["set"]


def test_a_no_op_never_rewrites_the_file(client: TestClient) -> None:
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"))
    _title(client, "s1", {"title": "Same"})
    path = data_dir / "session_titles.json"
    st = path.stat()
    time.sleep(0.05)
    for body in ({"title": "Same"}, {"title": "  Same "}):
        assert _title(client, "s1", body)["changed"] is False
    st2 = path.stat()
    assert (st2.st_mtime_ns, st2.st_ino) == (st.st_mtime_ns, st.st_ino)


def test_orphan_temp_files_are_removed_by_the_next_write_and_nothing_else(client: TestClient) -> None:
    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"))
    old = data_dir / "session_titles.json.4242.0123456789ab.tmp"     # this module's shape, a minute old
    fresh = data_dir / "session_titles.json.4243.0123456789ac.tmp"   # this module's shape, a write in flight
    others = [data_dir / "session_titles.json.backup.tmp",            # an operator's copy
              data_dir / "session_titles.json.27o0qd29.tmp",          # not this module's name
              data_dir / "session_titles.json.4242.deadbeef.tmp"]
    for p in (old, fresh, *others):
        p.write_text("{}", encoding="utf-8")
    past = time.time() - 120
    for p in (old, *others):
        os.utime(p, (past, past))
    _title(client, "s1", {"title": "Clean"})
    assert not old.exists() and fresh.exists() and all(p.exists() for p in others)


_WRITER = """
import sys
from abstractgateway.session_titles import set_session_title
d, n = sys.argv[1], sys.argv[2]
for i in range(40):
    out = set_session_title(d, f"p{n}-{i}", f"Process {n} title {i}", by=f"proc-{n}")
    assert out["changed"], out
print("ok")
"""


def test_two_processes_on_one_data_dir_lose_no_acknowledged_rename(tmp_path: Path) -> None:
    from abstractgateway import session_titles

    script = tmp_path / "writer.py"
    script.write_text(_WRITER, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), str(tmp_path), str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for n in (1, 2, 3)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0 and "ok" in out, err[-2000:]
    session_titles._STORE.forget(tmp_path)
    titles = session_titles.session_titles(tmp_path)
    assert len(titles) == 120, len(titles)
    assert not list(tmp_path.glob("session_titles.json.*.tmp"))


# --- normalisation -------------------------------------------------------------------------------


def test_hiding_characters_are_removed_joiners_and_marks_kept_and_input_is_bounded(client: TestClient) -> None:
    save_runs(_turn("s1", "an opening", "a", 1))
    # Zero-width space, word joiner, BOM, soft hyphen, Mongolian vowel separator, bidi controls: removed.
    hidden = "Sna​ke⁠ pl﻿a­n᠎ ‮RLO‬ ⁦iso⁩"
    assert _title(client, "s1", {"title": hidden})["title"] == "Snake plan RLO iso"
    # The search finds it by the visible word (a zero-width space no longer hides it).
    assert [i["session_id"] for i in _search(client, "snake")["items"]] == ["s1"]
    # Kept: ZWJ / ZWNJ (emoji sequences, Persian) and the direction marks.
    kept = "\U0001F469‍\U0001F4BB می‌خواهم ‎A‏"
    assert _title(client, "s1", {"title": kept})["title"] == kept
    # C1 controls that are not whitespace (CSI U+009B) become spaces too.
    assert _title(client, "s1", {"title": "a\u009bb"})["title"] == "a b"
    # Every other format character (Cf) goes too: tag characters, invisible operators, the deprecated
    # format controls, interlinear annotation; the Arabic letter mark U+061C is kept.
    others = ("a\U000E0041\U000E007Fb", "a⁡⁢⁣⁤b", "a⁪⁫⁬⁭⁮⁯b", "a￹￺￻b")
    for raw in others:
        assert _title(client, "s1", {"title": raw})["title"] == "ab", repr(raw)
    assert _title(client, "s1", {"title": "ا؜1"})["title"] == "ا؜1"
    # Visible format characters are kept: the Arabic end-of-ayah and number signs.
    ayah = "سورة ۝١٢ ؀٣"
    assert _title(client, "s1", {"title": ayah})["title"] == ayah
    assert _title(client, "s1", {"title": "۝"})["title"] == "۝"  # visible on its own
    # Emoji tag sequences are kept whole: the flags of England, Scotland and Wales.
    tags = lambda s: "".join(chr(0xE0000 + ord(ch)) for ch in s)  # noqa: E731
    flags = " ".join("\U0001F3F4" + tags(code) + "\U000E007F" for code in ("gbeng", "gbsct", "gbwls"))
    assert _title(client, "s1", {"title": f"Rugby {flags}"})["title"] == f"Rugby {flags}"
    # A tag run anywhere else (after a letter, or a flag run without its cancel tag) is removed.
    assert _title(client, "s1", {"title": "Plan" + tags("hidden") + "\U000E007F"})["title"] == "Plan"
    assert _title(client, "s1", {"title": "\U0001F3F4" + tags("gbeng") + " x"})["title"] == "\U0001F3F4 x"
    # A title with no visible character clears: marks, separators, kept joiners and marks, variation
    # selectors and the letters that render blank (Hangul fillers, Braille blank) show nothing.
    _title(client, "s1", {"title": "Visible"})
    for blank in ("ㅤ", "ᅟᅠ", "ﾠ", "⠀", "́⃝", "️\U000E0100", "‍‌‎‏؜",
                  "　 ", "​⁠﻿", "ㅤ ́ ⠀"):
        assert _title(client, "s1", {"title": blank})["title"] is None, repr(blank)
        _title(client, "s1", {"title": "Visible"})
    # A visible character with them is a title (an emoji with its variation selector is kept whole).
    assert _title(client, "s1", {"title": "❤️"})["title"] == "❤️"
    # The cut is applied AFTER whitespace is collapsed: a padded title keeps its end.
    assert _title(client, "s1", {"title": "start" + " " * 300 + "end"})["title"] == "start end"
    # Only the first 4,096 characters are read.
    assert _title(client, "s1", {"title": " " * 4096 + "beyond"})["title"] is None
    assert _title(client, "s1", {"title": "x" * 10_000_000})["title"] == "x" * 120


def test_a_session_id_is_trimmed(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    r = client.put("/api/gateway/sessions/%20s1%20/title", headers=HEADERS, json={"title": "Trimmed"})
    assert r.status_code == 200 and r.json()["session_id"] == "s1", r.text
    assert _row_titles(_roots(client)) == {"s1": "Trimmed"}


# --- /runs and search ----------------------------------------------------------------------------


def test_children_rows_carry_the_title(client: TestClient) -> None:
    root = chat_run(session_id="s-tree", created_at="2026-11-24T08:00:00+00:00")
    child = chat_run(session_id="s-tree", created_at="2026-11-24T08:01:00+00:00", parent_run_id=root.run_id)
    save_runs(root, child)
    _title(client, "s-tree", {"title": "Tree"})
    page = client.get(f"/api/gateway/runs?parent_run_id={root.run_id}&include_ledger_len=false", headers=HEADERS).json()
    assert [(i["run_id"], i.get("session_title")) for i in page["items"]] == [(child.run_id, "Tree")]


def test_search_filters_apply_to_title_hits(client: TestClient) -> None:
    save_runs(_turn("s-a", "first opening", "a", 1), _turn("s-b", "second opening", "b", 2), _turn("s-c", "third opening", "c", 3),
              _turn("s-arch", "fourth opening", "d", 4), _turn("s-words", "fifth opening", "e", 5))
    for sid in ("s-a", "s-b", "s-c", "s-arch"):
        _title(client, sid, {"title": f"Budget {sid}"})
    assert client.post("/api/gateway/sessions/s-arch/archive", headers=HEADERS).status_code == 200
    # Default: archived out; newest activity first; limit applies after the title hits.
    assert [i["session_id"] for i in _search(client, "budget")["items"]] == ["s-c", "s-b", "s-a"]
    assert [i["session_id"] for i in _search(client, "budget", "&limit=2")["items"]] == ["s-c", "s-b"]
    # The limit applies to title hits and content hits together.
    _title(client, "s-a", {"title": "Opening budget"})
    both = [i["session_id"] for i in _search(client, "opening", "&limit=2")["items"]]
    assert len(both) == 2 and both[0] == "s-a", both
    _title(client, "s-a", {"title": "Budget s-a"})
    # archived=true: only archived sessions' title hits.
    assert [i["session_id"] for i in _search(client, "budget", "&archived=true")["items"]] == ["s-arch"]
    # The session-kind filter applies to title hits (these are chats).
    assert _search(client, "budget", "&session_kind=discussion")["items"] == []
    # Short words never match inside words.
    _title(client, "s-words", {"title": "Standup notes up"})
    assert all(i["session_id"] != "s-words" for i in _search(client, "st")["items"])
    assert [i["session_id"] for i in _search(client, "up")["items"]] == ["s-words"]


def test_search_with_an_unreadable_store_answers_without_title_hits(client: TestClient) -> None:
    save_runs(_turn("s-t", "an opening about zebras", "z", 1), _turn("s-u", "unrelated", "u", 2))
    _title(client, "s-u", {"title": "Zebra crossing"})
    assert [i["session_id"] for i in _search(client, "zebra")["items"]] == ["s-u", "s-t"]
    (client.data_dir / "session_titles.json").write_text("{x", encoding="utf-8")  # type: ignore[attr-defined]
    _forget(client)
    body = _search(client, "zebra")
    assert body["session_titles_error"]["reason_code"] == "session_titles_unreadable"
    assert [(i["session_id"], i["matched_in"]) for i in body["items"]] == [("s-t", "question")]
    assert "session_title" not in body["items"][0]


# --- purge, OpenAPI ------------------------------------------------------------------------------


def test_purging_a_sessions_last_run_drops_its_title(client: TestClient) -> None:
    lifecycle = {"purpose": "draft_test", "retention": {"mode": "ephemeral", "ttl_s": 1}}
    draft = _turn("s-draft", "draft test", "a", 9)
    draft.vars["_run_lifecycle"] = lifecycle
    keep = _turn("s-keep", "kept", "k", 10)
    save_runs(draft, keep)
    _title(client, "s-draft", {"title": "Draft name"})
    _title(client, "s-keep", {"title": "Kept name"})
    assert client.post("/api/gateway/sessions/s-draft/archive", headers=HEADERS).status_code == 200
    r = client.post("/api/gateway/runs/purge_drafts", headers=HEADERS, json={"dry_run": False, "force": True, "run_ids": [draft.run_id]})
    assert r.status_code == 200 and r.json()["purged"] == 1, r.text
    assert r.json()["items"][0]["purge"]["session_title_dropped"] is True
    stored = json.loads((client.data_dir / "session_titles.json").read_text(encoding="utf-8"))["sessions"]  # type: ignore[attr-defined]
    assert "s-draft" not in stored and stored["s-keep"]["title"] == "Kept name"
    archive = json.loads((client.data_dir / "session_archive.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert "s-draft" not in archive["sessions"] and archive["history"][-1]["event"] == "purged"
    # A new conversation reusing the id neither inherits the name nor is born archived.
    save_runs(_turn("s-draft", "a brand new conversation", "n", 11))
    page = _roots(client)
    assert _row_titles(page)["s-draft"] == "<absent>" and page["archived_sessions"] == 0


@pytest.fixture()
def echo_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A gateway with a runnable bundle (no runner): `POST /runs/start` marks a session's kind."""
    from automations_fixtures import write_echo_bundle

    data_dir = gateway_env(monkeypatch, tmp_path)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.data_dir = data_dir  # type: ignore[attr-defined]
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def test_no_store_call_runs_on_the_event_loop(echo_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every read and write of the three per-session stores (titles, archive, kinds) runs in a worker
    thread: a store call made on the event loop's thread is recorded and fails the test."""
    import asyncio

    from abstractgateway import json_store
    from automations_fixtures import ECHO_BUNDLE_ID, ECHO_FLOW_ID

    c = echo_client
    on_loop: list = []

    def guard(name: str, fn):
        def wrapped(self, *args, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop.append(f"{self.filename}.{name}")
            except RuntimeError:
                pass
            return fn(self, *args, **kwargs)
        return wrapped

    for name in ("load", "read", "derived", "update"):
        monkeypatch.setattr(json_store.JsonStore, name, guard(name, getattr(json_store.JsonStore, name)))
    lifecycle = {"purpose": "draft_test", "retention": {"mode": "ephemeral", "ttl_s": 1}}
    draft = _turn("s-draft", "draft", "d", 3)
    draft.vars["_run_lifecycle"] = lifecycle
    save_runs(_turn("s1", "an opening", "a", 1), _turn("s2", "another", "b", 2), draft)
    assert _put(c, "s1", {"title": "Loop free"}).status_code == 200
    for path in ("/api/gateway/runs?root_only=true&include_ledger_len=false", "/api/gateway/runs?root_only=true&archived_only=true",
                 "/api/gateway/runs?session_id=s1", "/api/gateway/runs?parent_run_id=x", "/api/gateway/sessions/search?q=loop"):
        assert c.get(path, headers=HEADERS).status_code == 200, path
    assert c.post("/api/gateway/sessions/s2/archive", headers=HEADERS).status_code == 200
    assert c.post("/api/gateway/sessions/s2/unarchive", headers=HEADERS).status_code == 200
    r = c.post("/api/gateway/runs/start", headers=HEADERS, json={"bundle_id": ECHO_BUNDLE_ID, "flow_id": ECHO_FLOW_ID,
                                                                  "input_data": {"prompt": "p"}, "session_id": "s-docs", "kind": "docs"})
    assert r.status_code == 200, r.text
    r = c.post("/api/gateway/runs/purge_drafts", headers=HEADERS, json={"dry_run": False, "force": True, "run_ids": [draft.run_id]})
    assert r.status_code == 200, r.text
    assert on_loop == [], on_loop


def test_a_failed_read_is_remembered_not_retried_on_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import session_titles

    (tmp_path / "session_titles.json").write_text("{broken", encoding="utf-8")
    loads = []
    real = session_titles._load
    monkeypatch.setattr(session_titles, "_load", lambda d: (loads.append(1), real(d))[1])
    monkeypatch.setattr(session_titles, "REVALIDATE_S", 3600.0)
    session_titles._STORE.forget(tmp_path)
    for _ in range(5):
        with pytest.raises(session_titles.SessionTitlesUnreadable):
            session_titles.session_titles(tmp_path)
    assert len(loads) == 1


def test_a_reader_overlapping_a_write_never_caches_the_old_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic: a reader loads the old file, a write lands, then the reader finishes. The next
    read (well within the cache window) returns the written title, not the reader's old copy."""
    from abstractgateway import session_titles

    session_titles.set_session_title(tmp_path, "s", "Old", by="u")
    session_titles._STORE.forget(tmp_path)
    monkeypatch.setattr(session_titles, "REVALIDATE_S", 3600.0)
    loaded, go = threading.Event(), threading.Event()
    real = session_titles._STORE.load

    def slow_load(d):
        doc = real(d)
        loaded.set()
        assert go.wait(10)
        return doc

    monkeypatch.setattr(session_titles, "_load", slow_load)
    reader = threading.Thread(target=lambda: session_titles.session_titles(tmp_path))
    reader.start()
    assert loaded.wait(10)
    monkeypatch.setattr(session_titles, "_load", real)
    assert session_titles.set_session_title(tmp_path, "s", "New", by="u")["changed"] is True
    go.set()
    reader.join(10)
    assert session_titles.session_titles(tmp_path)["s"] == "New"


_LOCK_HOLDER = """
import msvcrt, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
"""


def test_a_held_lock_answers_busy_within_the_deadline(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractgateway import json_store, session_titles

    data_dir: Path = client.data_dir  # type: ignore[attr-defined]
    save_runs(chat_run(session_id="s1"))
    _title(client, "s1", {"title": "Before"})
    monkeypatch.setattr(json_store, "LOCK_WAIT_S", 0.5)
    # Held by another request of this process.
    lock = session_titles._STORE._write_lock(str(data_dir / "session_titles.json"))
    assert lock.acquire(timeout=5)
    try:
        t0 = time.monotonic()
        r = _call_with_deadline(lambda: _put(client, "s1", {"title": "Busy?"}))
        assert time.monotonic() - t0 < 5
        assert r.status_code == 503 and r.json()["detail"]["reason_code"] == "session_titles_busy", r.text
    finally:
        lock.release()
    if os.name == "nt":
        # Held by another process (its lock file locked).
        script = tmp_path / "holder.py"
        script.write_text(_LOCK_HOLDER, encoding="utf-8")
        holder = subprocess.Popen([sys.executable, str(script), str(data_dir / "session_titles.json.lock"), "20"], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
            r = _call_with_deadline(lambda: _put(client, "s1", {"title": "Busy?"}))
            assert r.status_code == 503 and r.json()["detail"]["reason_code"] == "session_titles_busy", r.text
        finally:
            holder.kill()
            holder.wait(10)
    assert _title(client, "s1", {"title": "After"})["title"] == "After"
    assert [e["action"] for e in _audit(client)] == ["set", "set"]


def test_the_openapi_body_is_declared(client: TestClient) -> None:
    spec = client.get("/openapi.json", headers=HEADERS).json()
    schema = spec["paths"]["/api/gateway/sessions/{session_id}/title"]["put"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["type"] == "object" and "title" in schema["properties"], schema
