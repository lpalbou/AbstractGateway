"""ADR 0048: `GET /api/gateway/automations` is served in the listing order every client draws.

Active automations first, then paused ones, each by `last_activity_at` (newest first), ties broken
by `automation_id`; every row carries `section` and `last_activity_at`; every page counts the
paused and archived ones; the keyset cursor pages that same order.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway.automation_order import (
    InvalidOrderCursor,
    automation_section,
    decode_cursor,
    encode_cursor,
    order_key,
    ordered,
    page,
    section_counts,
)
from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle

# --------------------------------------------------------------------- the rule, unit level


@pytest.mark.parametrize(
    "status,section",
    [("active", "active"), ("completed", "active"), ("failed", "active"), ("paused", "paused"), ("archived", "archived")],
)
def test_every_status_has_one_section(status: str, section: str) -> None:
    assert automation_section(status) == section


def _entries():
    rows = [
        ("paused", "2026-11-02T10:00:00+00:00", "p-old"),
        ("active", "2026-11-01T09:00:00+00:00", "a-old"),
        ("paused", "2026-11-02T12:00:00+00:00", "p-new"),
        ("active", "2026-11-02T08:00:00+00:00", "a-tie-1"),
        ("completed", "2026-11-02T11:00:00+00:00", "c-new"),
        ("active", "2026-11-02T08:00:00+00:00", "a-tie-2"),
        ("active", "", "a-never"),
    ]
    return [(order_key(status=s, last_activity_at=at, automation_id=aid), aid) for s, at, aid in rows]


def test_active_first_then_paused_each_newest_first_ties_by_id_desc() -> None:
    assert [aid for _k, aid in ordered(_entries())] == [
        "c-new", "a-tie-2", "a-tie-1", "a-old", "a-never",  # active section (completed included)
        "p-new", "p-old",  # paused section
    ]


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 50])
def test_pages_walk_the_same_order_exactly_once(limit: int) -> None:
    full = [aid for _k, aid in ordered(_entries())]
    seen, cursor = [], None
    while True:
        rows, cursor = page(ordered(_entries()), cursor=cursor, limit=limit)
        seen.extend(aid for _k, aid in rows)
        if cursor is None:
            break
    assert seen == full


def test_cursor_round_trip_and_refusals() -> None:
    key = order_key(status="paused", last_activity_at="2026-11-02T10:00:00+00:00", automation_id="x")
    assert decode_cursor(encode_cursor(key)) == key
    for bad in ("garbage", "auto1:WyIyMDI2IiwieCJd", "auto2:not-base64!", encode_cursor((9, "", "x"))):
        with pytest.raises(InvalidOrderCursor):
            decode_cursor(bad)


def test_section_counts() -> None:
    assert section_counts(["active", "paused", "failed", "archived", "paused"]) == {"active": 2, "paused": 2, "archived": 1}


# --------------------------------------------------------------------- the route, live gateway


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _create(c: TestClient, rid: str) -> str:
    body = {
        "request_id": rid,
        "title": f"Echo {rid}",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hello", "notify": False}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": "independent"},
    }
    r = c.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()["automation_id"]


def _command(c: TestClient, aid: str, typ: str, cid: str) -> None:
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": cid, "type": typ, "payload": {}})
    assert r.status_code == 200, r.text


def _status(c: TestClient, aid: str) -> str:
    return c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"]


def _listing(c: TestClient, query: str = "") -> dict:
    r = c.get(f"/api/gateway/automations{query}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _all_pages(c: TestClient, limit: int) -> list:
    seen, cursor = [], None
    while True:
        body = _listing(c, f"?limit={limit}" + (f"&cursor={cursor}" if cursor else ""))
        seen.extend(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            return seen


def test_the_listing_is_active_by_last_activity_then_paused_and_pausing_moves_a_row(live: TestClient) -> None:
    a, b, c = (_create(live, rid) for rid in ("A", "B", "C"))
    # Created A, B, C: the newest change first.
    body = _listing(live)
    assert [s["automation_id"] for s in body["items"]] == [c, b, a]
    assert all(s["section"] == "active" and s["last_activity_at"] for s in body["items"])
    assert body["paused_automations"] == 0 and body["archived_automations"] == 0

    # A runs: its last activity is now the newest, it leads the list.
    _command(live, a, "automation.run_now", "run-a")
    def a_ran():
        first = _listing(live)["items"][0]
        return first["automation_id"] == a and (first.get("last_occurrence") or {}).get("status") == "completed"

    wait_until(a_ran, timeout_s=20)
    assert [s["automation_id"] for s in _listing(live)["items"]] == [a, c, b]

    # C is paused: it leaves the active rows and closes the list, in the paused section,
    # even though its change is now the newest of all.
    _command(live, c, "automation.pause", "pause-c")
    wait_until(lambda: _status(live, c) == "paused" or None)
    body = _listing(live)
    assert [(s["automation_id"], s["section"]) for s in body["items"]] == [(a, "active"), (b, "active"), (c, "paused")]
    assert body["paused_automations"] == 1
    paused_row = body["items"][-1]
    assert paused_row["status"] == "paused" and paused_row["last_activity_at"] >= body["items"][0]["last_activity_at"]

    # Every page of a paged walk follows the same order, each row exactly once.
    for limit in (1, 2):
        assert [s["automation_id"] for s in _all_pages(live, limit)] == [a, b, c]

    # `status=paused` is the paused section alone, same order and count.
    only_paused = _listing(live, "?status=paused")
    assert [s["automation_id"] for s in only_paused["items"]] == [c] and only_paused["paused_automations"] == 1

    # Resumed, C is active again and its resume is the newest activity.
    _command(live, c, "automation.resume", "resume-c")
    wait_until(lambda: _status(live, c) == "active" or None)
    body = _listing(live)
    assert [(s["automation_id"], s["section"]) for s in body["items"]] == [(c, "active"), (a, "active"), (b, "active")]
    assert body["paused_automations"] == 0


def test_archived_rows_carry_their_section_and_a_foreign_cursor_is_refused(live: TestClient) -> None:
    keep, gone = _create(live, "keep"), _create(live, "gone")
    _command(live, gone, "automation.archive", "arch")
    wait_until(lambda: _status(live, gone) == "archived" or None)
    body = _listing(live)
    assert [s["automation_id"] for s in body["items"]] == [keep] and body["archived_automations"] == 1
    arch = _listing(live, "?archived_only=true")
    assert [(s["automation_id"], s["section"]) for s in arch["items"]] == [(gone, "archived")]
    # A cursor of the earlier creation order (runtime `auto1:`) is refused, never misread.
    r = live.get("/api/gateway/automations?cursor=auto1:WyIyMDI2IiwieCJd", headers=HEADERS)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason_code"] == "invalid_request" and r.json()["detail"]["field"] == "cursor"
