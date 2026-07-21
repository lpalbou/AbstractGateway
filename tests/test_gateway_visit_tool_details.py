"""Tool details on the durable visit lane (2026-07-18, laurent's
"unacceptable error with tools").

The incident: the entity ran 19 successful web lookups in one visit turn,
every result rested in the run's own ledger — and the app rendered "No
result recorded for this tool — the gateway did not return what the lookup
produced" for all of them, because the durable lane served
`tool_details: []` as a named follow-up that never landed. The truth was in
the ledger the whole time; these tests pin the fold that serves it.

Attribution contract (adversary F1/F2): details bind to the turn_id on the
next completed answer_user record — NEVER positional resume counting, which
breaks on the history sliding window and on empty-message resumes.
Results serve VERBATIM (maintainer 2026-07-09 transparency ruling — never
gated, never truncated; hosted-lane parity).
"""

from __future__ import annotations

from abstractgateway.entity_visits import (
    _compose_turn_probe,
    _ledger_tool_details,
)


def _tool_record(calls, results):
    """A completed tool_calls ledger record (the started twin carries no
    result and must not double-render)."""
    return {
        "effect": {"type": "tool_calls", "payload": {"tool_calls": calls}},
        "result": {"mode": "executed", "results": results},
    }


def _answer(turn_id):
    return {
        "effect": {"type": "answer_user", "payload": {"turn_id": turn_id, "message": "x"}},
        "result": {"delivered": True},
    }


def test_details_bind_to_the_answer_turn_id() -> None:
    calls_t1 = [{"name": "web_search", "arguments": {"query": "France news"}, "call_id": "c1"}]
    calls_t2 = [
        {"name": "web_search", "arguments": {"query": "SF weather"}, "call_id": "c2"},
        {"name": "fetch_url", "arguments": {"url": "https://x.test"}, "call_id": "c3"},
    ]
    records = [
        {"effect": {"type": "wait_event"}},   # open ceremony
        {"effect": {"type": "resume"}},        # turn 1 message
        {"effect": {"type": "tool_calls", "payload": {"tool_calls": calls_t1}}, "result": None},  # started twin
        _tool_record(calls_t1, [{"call_id": "c1", "name": "web_search", "success": True,
                                 "output": "Search results for: France news\n- headline"}]),
        _answer("t-0001"),
        {"effect": {"type": "resume"}},        # turn 2 message
        _tool_record(calls_t2, [
            {"call_id": "c2", "name": "web_search", "success": True, "output": "fog"},
            {"call_id": "c3", "name": "fetch_url", "success": False, "error": "HTTP 404"},
        ]),
        _answer("t-0002"),
    ]
    buckets, order, tail = _ledger_tool_details(records)
    assert order == ["t-0001", "t-0002"]
    assert tail == []
    t1 = buckets["t-0001"]
    assert len(t1) == 1  # started twin contributed args, not a second row
    assert t1[0]["name"] == "web_search"
    assert "France news" in t1[0]["arg"]
    assert t1[0]["success"] is True
    assert t1[0]["result"].startswith("Search results for: France news")
    t2 = buckets["t-0002"]
    assert [d["name"] for d in t2] == ["web_search", "fetch_url"]
    # A failed call serves its error text — a real reach that failed
    # loudly, never a blank.
    assert t2[1]["success"] is False
    assert "HTTP 404" in t2[1]["result"]


def test_empty_message_resume_does_not_shift_attribution() -> None:
    """Adversary F2: an empty-text resume parks without a turn (no answer
    record) — positional resume counting would shift every later turn; the
    turn-id key must not."""
    calls = [{"name": "web_search", "arguments": {"query": "q"}, "call_id": "c1"}]
    records = [
        {"effect": {"type": "resume"}},        # turn 1
        _tool_record(calls, [{"call_id": "c1", "name": "web_search", "success": True, "output": "hits"}]),
        _answer("t-0001"),
        {"effect": {"type": "resume"}},        # EMPTY message: parks, no answer
        {"effect": {"type": "resume"}},        # turn 2 (real)
        _answer("t-0002"),
    ]
    buckets, order, tail = _ledger_tool_details(records)
    assert order == ["t-0001", "t-0002"]
    assert len(buckets["t-0001"]) == 1
    assert buckets["t-0002"] == []  # turn 2 ran no tools — honest empty
    assert tail == []


def test_results_serve_verbatim_never_truncated() -> None:
    """Maintainer 2026-07-09: operator transparency is never gated, never
    truncated (hosted-lane parity). fetch_url self-caps at source; the
    serving layer must not cut further."""
    big = "x" * 50_000
    records = [
        {"effect": {"type": "resume"}},
        _tool_record(
            [{"name": "fetch_url", "arguments": {"url": "https://big.test"}, "call_id": "c1"}],
            [{"call_id": "c1", "name": "fetch_url", "success": True, "output": big}],
        ),
        _answer("t-0001"),
    ]
    buckets, _, _ = _ledger_tool_details(records)
    assert buckets["t-0001"][0]["result"] == big


def test_slimmed_completed_payload_args_come_from_the_started_twin() -> None:
    """Adversary F5: $slim replaces >4KB payload fields on COMPLETED records
    with a marker — args must be harvested from the STARTED twin."""
    calls = [{"name": "web_search", "arguments": {"query": "big batch"}, "call_id": "c1"}]
    records = [
        {"effect": {"type": "resume"}},
        {"effect": {"type": "tool_calls", "payload": {"tool_calls": calls}}, "result": None},  # started: full payload
        {"effect": {"type": "tool_calls", "payload": {"tool_calls": {"$slim": {"kind": "unchanged-payload-field"}}}},
         "result": {"mode": "executed",
                    "results": [{"call_id": "c1", "name": "web_search", "success": True, "output": "hits"}]}},
        _answer("t-0001"),
    ]
    buckets, _, _ = _ledger_tool_details(records)
    [d] = buckets["t-0001"]
    assert "big batch" in d["arg"]
    assert d["result"] == "hits"


def test_diary_result_serves_verbatim_and_empty_output_and_failed() -> None:
    """Post act-only-deletion (runtime c273, laurent's A ruling): the ref
    layer is gone — the HOME is the privacy boundary, so diary tool results
    rest AS SERVED and surface verbatim in the operator's turn-detail modal
    (the operator diary-door right covers the read). Empty output serves a
    present marker (adversary F7); a failed call serves its error (F8)."""
    records = [
        {"effect": {"type": "resume"}},
        _tool_record(
            [{"name": "diary_read", "arguments": {"entry_id": "diary_ab12"}, "call_id": "c1"},
             {"name": "web_search", "arguments": {"query": "q"}, "call_id": "c2"},
             {"name": "diary_read", "arguments": {"entry_id": "diary_nope"}, "call_id": "c3"}],
            [{"call_id": "c1", "name": "diary_read", "success": True,
              "output": "the entry text, served as it rests in the home"},
             {"call_id": "c2", "name": "web_search", "success": True, "output": ""},
             {"call_id": "c3", "name": "diary_read", "success": False,
              "error": "diary entry not found: diary_nope"}],
        ),
        _answer("t-0001"),
    ]
    buckets, _, _ = _ledger_tool_details(records)
    [diary, empty, failed] = buckets["t-0001"]
    # Diary result serves verbatim — no act-only label anymore.
    assert diary["result"] == "the entry text, served as it rests in the home"
    assert "act-only" not in diary["result"]
    # Executed-but-empty output: a present marker, not a missing field the
    # app would render as "no result recorded" (adversary F7).
    assert empty["result"] == "(the tool returned empty output)"
    # Failed call serves the error text (F8).
    assert "not found" in failed["result"]


def test_interrupted_turn_details_land_in_the_tail() -> None:
    """A turn that died before its ANSWER record: its tools accumulate in
    the tail (served as the current turn's honest best), never silently
    folded into the previous answered turn."""
    calls = [{"name": "web_search", "arguments": {"query": "q"}, "call_id": "c1"}]
    records = [
        {"effect": {"type": "resume"}},
        _answer("t-0001"),
        {"effect": {"type": "resume"}},
        _tool_record(calls, [{"call_id": "c1", "name": "web_search", "success": True, "output": "hits"}]),
        # no answer record — crashed mid-turn
    ]
    buckets, order, tail = _ledger_tool_details(records)
    assert order == ["t-0001"]
    assert buckets["t-0001"] == []
    assert len(tail) == 1 and tail[0]["result"] == "hits"


def test_probe_carries_the_folded_details() -> None:
    details = [{"name": "web_search", "arg": '{"query": "x"}', "success": True, "result": "hits"}]
    probe = _compose_turn_probe({"_visit": {}, "_turn": {"tools_ran": ["web_search"]}},
                                tool_details=details)
    assert probe["tool_details"] == details
    # Absent fold still serves the honest empty list, never None.
    probe2 = _compose_turn_probe({"_visit": {}, "_turn": {}})
    assert probe2["tool_details"] == []
