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


_SECTION_RANK = {"active": 0, "paused": 1, "archived": 2}


def _by_the_rule(items: list) -> list:
    """The ids in the order ADR 0048 gives the rows' OWN recorded stamps: active first, then paused,
    each by last activity newest first, ties by id descending. Applied to the very response the test
    reads, so an activity stamp moving between two reads (a controller tick) can never race it."""
    rows = sorted(items, key=lambda s: s["automation_id"], reverse=True)
    rows = sorted(rows, key=lambda s: s["last_activity_at"], reverse=True)
    rows = sorted(rows, key=lambda s: _SECTION_RANK[s["section"]])
    return [s["automation_id"] for s in rows]


def _row(body: dict, aid: str) -> dict:
    return next(s for s in body["items"] if s["automation_id"] == aid)


def test_the_listing_is_active_by_last_activity_then_paused_and_pausing_moves_a_row(live: TestClient) -> None:
    """The listing order is checked against the stamps each response carries, never against the
    order of the test's own actions. CI main 8a12518: A, B and C were created within one stamp, so
    the tie rule (id descending) ordered them, not their creation order; and a controller tick can
    move an automation's activity at any time."""
    a, b, c = (_create(live, rid) for rid in ("A", "B", "C"))
    body = _listing(live)
    assert [s["automation_id"] for s in body["items"]] == _by_the_rule(body["items"]) and len(body["items"]) == 3
    assert all(s["section"] == "active" and s["last_activity_at"] for s in body["items"])
    assert body["paused_automations_count"] == 0 and body["archived_automations_count"] == body["archived_automations"] == 0
    created = {s["automation_id"]: s["last_activity_at"] for s in body["items"]}

    # A runs: its activity moves; the list follows the rule over the new stamps.
    _command(live, a, "automation.run_now", "run-a")

    def a_ran():
        row = _row(_listing(live), a)
        return (row.get("last_occurrence") or {}).get("status") == "completed" and row["last_activity_at"] > created[a] or None

    wait_until(a_ran, timeout_s=20)
    body = _listing(live)
    assert [s["automation_id"] for s in body["items"]] == _by_the_rule(body["items"])

    # C is paused: it moves to the paused section and closes the list, whatever its stamp.
    before_pause = _row(_listing(live), c)["last_activity_at"]
    _command(live, c, "automation.pause", "pause-c")

    def c_paused_and_recorded():
        # The pause is recorded when C is paused AND its activity moved (a save can flip the status
        # before the one that stamps the time: release gate 16f2a1aa).
        row = _row(_listing(live), c)
        return row["status"] == "paused" and row["last_activity_at"] > before_pause or None

    wait_until(c_paused_and_recorded)
    body = _listing(live)
    order = [s["automation_id"] for s in body["items"]]
    assert order == _by_the_rule(body["items"]) and order[-1] == c and set(order[:2]) == {a, b}
    assert [s["section"] for s in body["items"]] == ["active", "active", "paused"]
    assert body["paused_automations_count"] == 1 and body["items"][-1]["status"] == "paused"

    # Every page of a paged walk follows the same order, each row exactly once. A walk spans several
    # requests: it is compared with a listing taken right after it (a client refreshes from the
    # first page when a stamp moved under it, the listing's contract).
    for limit in (1, 2):
        def walk_matches():
            walked = [s["automation_id"] for s in _all_pages(live, limit)]
            return walked == [s["automation_id"] for s in _listing(live)["items"]] and len(set(walked)) == 3 or None

        wait_until(walk_matches)

    # `status=paused` is the paused section alone, same order and count.
    only_paused = _listing(live, "?status=paused")
    assert [s["automation_id"] for s in only_paused["items"]] == [c] and only_paused["paused_automations_count"] == 1

    # Resumed, C is active again, its resume is recorded activity, and the order is the rule's.
    paused_at = _row(_listing(live), c)["last_activity_at"]
    _command(live, c, "automation.resume", "resume-c")

    def c_resumed_and_recorded():
        row = _row(_listing(live), c)
        return row["status"] == "active" and row["last_activity_at"] > paused_at or None

    wait_until(c_resumed_and_recorded)
    body = _listing(live)
    assert [s["automation_id"] for s in body["items"]] == _by_the_rule(body["items"])
    assert all(s["section"] == "active" for s in body["items"]) and body["paused_automations_count"] == 0


def test_archived_rows_carry_their_section_and_a_foreign_cursor_is_refused(live: TestClient) -> None:
    keep, gone = _create(live, "keep"), _create(live, "gone")
    _command(live, gone, "automation.archive", "arch")
    wait_until(lambda: _status(live, gone) == "archived" or None)
    body = _listing(live)
    assert [s["automation_id"] for s in body["items"]] == [keep] and body["archived_automations_count"] == body["archived_automations"] == 1
    arch = _listing(live, "?archived_only=true")
    assert [(s["automation_id"], s["section"]) for s in arch["items"]] == [(gone, "archived")]
    # A cursor of the earlier creation order (runtime `auto1:`) is refused, never misread.
    r = live.get("/api/gateway/automations?cursor=auto1:WyIyMDI2IiwieCJd", headers=HEADERS)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason_code"] == "invalid_request" and r.json()["detail"]["field"] == "cursor"
