"""The load test's verdict (scripts/perf/ci_verdict.py) decides from the recorded numbers, never the
driver's printed text: correctness fails in both modes, limits only with --enforce (ADR 0050 rule 5)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.basic

_SPEC = importlib.util.spec_from_file_location("ci_verdict", Path(__file__).resolve().parents[1] / "scripts" / "perf" / "ci_verdict.py")
assert _SPEC and _SPEC.loader
ci_verdict = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_verdict)


def _doc(**over: Any) -> Dict[str, Any]:
    doc = {
        "probes": {"me": {"n": 20, "ok": 20, "codes": [200], "p50_s": 0.001, "p99_s": 0.002, "max_s": 0.003}},
        "idle": {"seconds": 120.0, "cpu_pct": 0.3},
        "gates": {"p99_s": {"me": 0.05}, "idle_cpu_pct": 5.0, "failures": [], "ok": True},
    }
    for key, value in over.items():
        doc[key] = value
    return doc


def test_within_limits_passes_in_both_modes() -> None:
    assert ci_verdict.verdict(_doc(), 0, enforce=False) == (0, "PASS")
    assert ci_verdict.verdict(_doc(), 0, enforce=True) == (0, "PASS")


def test_an_exceeded_limit_is_reported_on_a_pull_request_and_fails_when_enforced() -> None:
    slow = _doc(probes={"me": {"n": 20, "ok": 20, "codes": [200], "p50_s": 0.04, "p99_s": 0.09, "max_s": 0.1}})
    code, sentence = ci_verdict.verdict(slow, 1, enforce=False)
    assert code == 0 and sentence.startswith("PASS (limits reported") and "me: p99 90.0 ms > 50.0 ms" in sentence
    assert ci_verdict.verdict(slow, 1, enforce=True)[0] == 1
    busy = _doc(idle={"seconds": 120.0, "cpu_pct": 9.0})
    assert ci_verdict.verdict(busy, 1, enforce=False)[0] == 0 and ci_verdict.verdict(busy, 1, enforce=True)[0] == 1


def test_a_non_200_answer_fails_in_both_modes_whatever_the_text_says() -> None:
    bad = _doc(probes={"me": {"n": 20, "ok": 19, "codes": [200, 500], "p50_s": 0.001, "p99_s": 0.002, "max_s": 0.003}},
               gates={"p99_s": {"me": 0.05}, "idle_cpu_pct": 5.0, "failures": ["anything at all"], "ok": False})
    assert ci_verdict.verdict(bad, 1, enforce=False)[0] == 1
    assert "19 of 20 answers were 200" in ci_verdict.verdict(bad, 1, enforce=False)[1]


def test_a_missing_measurement_is_never_swallowed_as_a_limit() -> None:
    no_cpu = _doc(idle={"seconds": 120.0, "cpu_pct": None})
    assert ci_verdict.verdict(no_cpu, 1, enforce=False) == (1, "FAIL: CPU at rest: not measured")
    no_p99 = _doc(probes={"me": {"n": 20, "ok": 20, "codes": [200], "p50_s": None, "p99_s": None, "max_s": None}})
    assert ci_verdict.verdict(no_p99, 1, enforce=False)[0] == 1
    missing_probe = _doc(probes={})
    assert ci_verdict.verdict(missing_probe, 1, enforce=False) == (1, "FAIL: me: no measurement")


def test_no_result_or_no_gates_fails() -> None:
    assert ci_verdict.verdict(None, 1, enforce=False)[0] == 1
    assert ci_verdict.verdict({"probes": {}}, 0, enforce=False)[0] == 1


def test_the_script_reads_the_newest_result_of_this_run_and_writes_the_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "bench-1.json").write_text(json.dumps(_doc(idle={"seconds": 30.0, "cpu_pct": 9.0})))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    ci_verdict.main([str(tmp_path), "0", "1", "0"])
    out = capsys.readouterr().out.strip()
    assert out.startswith("0 PASS (limits reported, not enforced): over CPU at rest 9.0% > 5.0% of a core")
    text = summary.read_text()
    assert "limits reported (pull request)" in text and "| me | 1.0 ms | 2.0 ms | 50.0 ms | 3.0 ms | 20/20 |" in text
