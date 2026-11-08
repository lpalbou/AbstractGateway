"""`GET /runs?root_only=true` reads the run store ONCE per request in steady state (2026-11-08).

The Observer's `root_only=true&kind=conversation&session_kind=automation,occurrence` refresh missed its
30 s deadline and Code's conversation list said "Loading conversations…" for 30-90 s on the operator's
14,000-run store. Where the time went: the gateway runs the store in `--run-index verify` mode, where
EVERY `list_run_index` call globs and stats the whole run folder (whatever its limit), and the listing
widened its scan by re-calling the store (1005 -> 4020 -> 16080 ...) whenever its own filters (internal
workflows, drafts, archived sessions, session purpose) discarded the newest rows. Each widening paid the
whole folder again; under a busy interpreter every stat waits for the GIL, so the page cost seconds.

The listing now remembers how deep the last identical listing had to read (`_runs_scan_depth`) and starts
there: one store pass per request once warm, the same rows in the same order.

Store shape (5,000 runs): the OLDEST turns are the visible ones (300 chat roots, each with a follow-up
child, then an automation controller and its 300 occurrence runs); the 4,098 NEWEST roots are all hidden
from a conversation list (1,100 occurrence runs of an AbstractNote automation, which are notes, then
internal `__session_memory__` runs, draft tests and roots of archived sessions). A root-only page of 200
therefore needs a deep read: three store passes widening, one with the depth memory; the Observer's
occurrence page two, one with the depth memory.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, controller_run, gateway_env, occurrence_run
from timing import patient, timing_budget  # noqa: E402 - tests/timing.py

pytestmark = pytest.mark.basic

DRAFT = {"source": "abstractflow.editor", "purpose": "draft_test", "visibility": "private",
         "retention": {"mode": "ephemeral", "ttl_s": "3600"}, "editor_session_id": "editor-session", "flow_id": "root"}
# Conversations AND automation sessions (backlog 1166: the plain default is conversations only,
# and this store's newest visible turns are the automation occurrences).
ROOT_PAGE = "/api/gateway/runs?limit=200&root_only=true&kind=conversation,automation"
OBSERVER_PAGE = "/api/gateway/runs?limit=200&root_only=true&kind=conversation&session_kind=automation,occurrence"


def _at(i: int) -> str:
    return f"2026-10-{1 + i // 86400:02d}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"


def _seed(data_dir: Path) -> Dict[str, Any]:
    """Write the store (oldest first, so file mtimes and updated_at agree) and the archive."""
    from abstractruntime import JsonFileRunStore

    from abstractgateway.session_archive import archive_session

    store = JsonFileRunStore(data_dir)
    n = 0

    def save(run: Any) -> Any:
        nonlocal n
        run.created_at = run.updated_at = _at(n)
        n += 1
        store.save(run)
        return run

    chat_roots: List[str] = []
    for i in range(300):
        root = save(chat_run(session_id=f"chat-{i}"))
        chat_roots.append(root.run_id)
        save(chat_run(session_id=f"chat-{i}", parent_run_id=root.run_id))
    controller = save(controller_run(title="Monitor"))
    occurrences: List[str] = []
    for i in range(1, 301):
        occ = save(occurrence_run(controller.run_id, index=i, session_id=f"occ-session:{controller.run_id}:{i}"))
        occurrences.append(occ.run_id)
    # An AbstractNote automation's occurrences: turns, but notes, never in a conversation list
    # (and the newest occurrence rows, so the Observer's occurrence page reads past them too).
    notes = save(controller_run(title="Note processing"))
    for i in range(1, 1101):
        occ = occurrence_run(notes.run_id, index=i, session_id=f"note-session:{i}")
        occ.workflow_id = "abstractnote@0.2.0:process"
        save(occ)
    archived = [f"archived-{k}" for k in range(10)]
    for i in range(2998):
        kind = i % 3
        if kind == 0:
            run = chat_run(session_id=f"memory-{i}")
            run.workflow_id = "__session_memory__"
        elif kind == 1:
            run = chat_run(session_id=f"draft-{i}")
            run.vars = {"_run_lifecycle": dict(DRAFT)}
        else:
            run = chat_run(session_id=archived[i % len(archived)])
        save(run)
    for sid in archived:
        archive_session(data_dir, sid, by="test")
    return {"runs": n, "chat_roots": chat_roots, "occurrences": occurrences}


def _store_passes(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """The limit of every listing pass `GET /runs` asks the store for (the boot's whole-index
    migration listing excluded)."""
    from abstractruntime import JsonFileRunStore

    passes: List[int] = []
    orig = JsonFileRunStore.list_run_index

    def spy(self: Any, **kw: Any) -> Any:
        if int(kw.get("limit") or 0) < 1_000_000:
            passes.append(int(kw.get("limit") or 0))
        return orig(self, **kw)

    monkeypatch.setattr(JsonFileRunStore, "list_run_index", spy)
    return passes


def _boot(client: TestClient) -> None:
    deadline = time.time() + patient(60.0)
    while client.get("/api/health").json().get("status") == "starting":
        assert time.time() < deadline, "gateway never finished booting"
        time.sleep(0.05)


def _page(client: TestClient, url: str, passes: List[int]) -> tuple:
    passes.clear()
    t0 = time.perf_counter()
    r = client.get(url, headers=HEADERS)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    return r.json(), list(passes), elapsed


def test_root_only_listing_reads_the_store_once_with_the_same_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = gateway_env(monkeypatch, tmp_path)
    seeded = _seed(data_dir)
    assert seeded["runs"] == 5000
    from abstractgateway.app import app

    passes = _store_passes(monkeypatch)
    with TestClient(app) as client:
        _boot(client)
        cold, cold_passes, _ = _page(client, ROOT_PAGE, passes)
        # The newest 200 visible turns are the 200 newest occurrence runs, newest first.
        expected = list(reversed(seeded["occurrences"]))[:200]
        assert [it["run_id"] for it in cold["items"]] == expected
        assert cold["has_more"] is True and cold["count"] == 200
        assert len(cold_passes) >= 2, f"the store shape must need a deep read: {cold_passes}"

        warm_s: List[float] = []
        for _ in range(5):
            warm, warm_passes, elapsed = _page(client, ROOT_PAGE, passes)
            warm_s.append(elapsed)
            assert warm == cold, "the depth memory changed the page"
            assert len(warm_passes) == 1, f"a warm root-only page must read the store once, read it {len(warm_passes)} times: {warm_passes}"

        # The Observer's refresh: its own depth memory, the same answer shape.
        obs_cold, obs_cold_passes, _ = _page(client, OBSERVER_PAGE, passes)
        assert len(obs_cold_passes) >= 2, f"the store shape must need a deep occurrence read: {obs_cold_passes}"
        obs_s: List[float] = []
        for _ in range(5):
            obs_warm, obs_passes, elapsed = _page(client, OBSERVER_PAGE, passes)
            obs_s.append(elapsed)
            assert obs_warm == obs_cold
            assert [it["run_id"] for it in obs_warm["items"]] == expected
            assert len(obs_passes) == 1, f"the Observer's warm page read the store {len(obs_passes)} times: {obs_passes}"

        # Deeper pages keep working off the same memory (offset scales the depth).
        page2, page2_passes, _ = _page(client, ROOT_PAGE + "&offset=200", passes)
        assert [it["run_id"] for it in page2["items"]] == list(reversed(seeded["occurrences"]))[200:] + list(reversed(seeded["chat_roots"]))[:100]
        last, _, _ = _page(client, ROOT_PAGE + "&offset=400", passes)
        assert [it["run_id"] for it in last["items"]] == list(reversed(seeded["chat_roots"]))[100:300]
        assert last["has_more"] is False
        # Medians of 5 measured on the operator's Mac under memory pressure (verify run index,
        # 2026-11-08), four runs each: widening (the depth memory removed) root-only 164-281 ms and
        # Observer 98-181 ms; one pass root-only 67-132 ms and Observer 55-92 ms. The pass counts
        # above are the guard; these budgets are enforced only under ABSTRACTGATEWAY_TIMING_STRICT=1.
        timing_budget(statistics.median(warm_s), 0.15, "warm root-only page over 5,000 runs (median of 5)")
        timing_budget(statistics.median(obs_s), 0.10, "warm Observer page over 5,000 runs (median of 5)")
        assert len(page2_passes) <= 2, page2_passes


def test_depth_memory_is_a_floor_and_bounded() -> None:
    from abstractgateway.routes import gateway as gw

    key = ("plane-x", None, None, None, True, None, None, False, True, False, "conversation", True)
    gw._RUNS_SCAN_DEPTH.pop(key, None)
    assert gw._runs_scan_depth(key, 201, 1005) == 1005  # nothing remembered: the default
    gw._note_runs_scan_depth(key, rows_per_item=0.5)
    assert gw._runs_scan_depth(key, 201, 1005) == 1005  # never shallower than the default
    gw._note_runs_scan_depth(key, rows_per_item=20.0)
    assert gw._runs_scan_depth(key, 201, 1005) > 201 * 20  # deeper than last time's need
    gw._note_runs_scan_depth(key, exhausted_at=3000)
    assert gw._runs_scan_depth(key, 201, 1005) > 3000  # a short read still proves exhaustion
    gw._note_runs_scan_depth(key, rows_per_item=10_000.0)
    assert gw._runs_scan_depth(key, 201, 1005) == gw._RUNS_SCAN_CAP
    for i in range(gw._RUNS_SCAN_DEPTH_MAX + 10):
        gw._note_runs_scan_depth(("bound", i), rows_per_item=1.0)
    assert len(gw._RUNS_SCAN_DEPTH) <= gw._RUNS_SCAN_DEPTH_MAX
