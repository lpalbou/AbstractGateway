from types import SimpleNamespace
import pytest
from abstractgateway.message_stats import message_stats, stats_from_records

pytestmark = pytest.mark.basic


def rec(kind, result, key=None, status="completed"):
    return dict(status=status, effect={"type": kind}, result=result, idempotency_key=key)


def test_stats_count_completed_usage_and_executed_calls_once():
    llm = rec("llm_call", {"usage": {"prompt_tokens": 123, "completion_tokens": 45}}, "a")
    denied = {"success": False, "error": "Denied by user", "output": None}
    blocked = {"success": False, "error": "Tool 'x' is not allowed for this run"}
    completed = rec("tool_calls", {"results": [{"success": True}, denied, blocked, {"success": False, "error": "Connection failed"}]}, "b")
    assert stats_from_records([llm, llm, completed, completed, rec("tool_calls", {}, status="waiting")]) == dict(input_tokens=123, output_tokens=45, tool_calls=2)


def test_missing_split_is_not_invented_from_total():
    assert stats_from_records([rec("llm_call", {"usage": {"total_tokens": 40}})]) == dict(input_tokens=None, output_tokens=None, tool_calls=0)


def test_subtree_stats_wall_time_is_not_sum_of_overlapping_runs():
    root = SimpleNamespace(run_id="root", created_at="2026-10-18T00:00:00Z", updated_at="2026-10-18T00:00:10Z", status="completed")
    child = SimpleNamespace(run_id="child")
    store = SimpleNamespace(load=lambda rid: root if rid == "root" else None, list_children=lambda parent_run_id: [child] if parent_run_id == "root" else [])
    ledger = SimpleNamespace(list=lambda rid: [rec("llm_call", {"usage": {"input_tokens": 20, "output_tokens": 5}}, "same-local-key")])
    assert message_stats(store, ledger, "root") == dict(wall_time_seconds=10, input_tokens=40, output_tokens=10, tool_calls=0)
    with pytest.raises(KeyError):
        message_stats(store, ledger, "foreign")
    assert message_stats(store, None, "root")["input_tokens"] is None


def test_visit_statistics_belong_to_latest_turn_and_exclude_idle_time():
    from abstractgateway.message_stats import visit_message_stats
    def park(time):
        return dict(status="waiting", result={"wait": {"wait_key": "visitor_input"}}, started_at=time, ended_at=time)
    records = [park("2026-10-18T00:00:00Z"),
               rec("llm_call", {"usage": {"input_tokens": 90, "output_tokens": 20}}),
               park("2026-10-18T00:00:05Z"),
               dict(status="completed", result={"resumed": True}, started_at="2026-10-18T01:00:00Z", ended_at="2026-10-18T01:00:00Z"),
               rec("llm_call", {"usage": {"prompt_tokens": 30, "completion_tokens": 7}}),
               park("2026-10-18T01:00:03Z")]
    ledger = SimpleNamespace(list=lambda rid: records)
    assert visit_message_stats(ledger, "v") == dict(input_tokens=30, output_tokens=7, tool_calls=0, wall_time_seconds=3)
