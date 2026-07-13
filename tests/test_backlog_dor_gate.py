"""Definition-of-Ready gate (continuum c1088 ask 3; contract + vectors c1124).

The gateway mirror of continuum's client-side DoR checklist so a curl can't
bypass readiness; the operator outranks it via override=true. continuum's
board_model.ts is the reference impl; V1-V6 below are their vectors verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

from abstractgateway.maintenance.backlog_dor import evaluate_dor

_FULL = """# 001-abstractgateway: A ready item

> Type: feature

## Summary

Doors should open when the operator clicks the handle.

## Acceptance Criteria

- [ ] Doors open on click
- [ ] Closed doors stay closed

## Testing

Run `python -m pytest tests/test_doors.py -q`.
"""

_TEMPLATE_FRESH = """# 002-abstractgateway: Fresh template

> Type: task

## Summary

One paragraph describing the problem and the desired behavior.

## Acceptance Criteria

- [ ] Criterion 1 (clear, testable)

## Testing

`...`
"""

_H3_HEADINGS = """# 003-abstractgateway: H3 sections

> Type: bug

## Summary

The widget miscounts.

### Acceptance Criteria

- [ ] Count is correct

### Testing

`pytest tests/test_widget.py`
"""

_CHECKED_ONLY = """# 004-abstractgateway: Checked criteria

> Type: improvement

## Summary

Speed up the loader.

## Acceptance Criteria

- [x] Loader is 2x faster

## Testing

`pytest -k loader`
"""

_TESTS_PLACEHOLDER = """# 005-abstractgateway: Only placeholder tests

> Type: task

## Summary

A real summary line here.

## Acceptance Criteria

- [ ] Something works

## Testing

`...`
`n/a`
"""

_SUMMARY_SUFFIX_HEADING = """# 006-abstractgateway: Summary heading variant

> Type: feature

## Summary (short)

Real prose in a suffixed summary heading.

## Acceptance Criteria

- [ ] It works

## Testing

`pytest`
"""


def test_v1_full_spec_passes() -> None:
    ready, checks = evaluate_dor(_FULL, "feature")
    assert ready, [c.to_dict() for c in checks if not c.ok]


def test_v2_template_fresh_fails_summary_acceptance_tests() -> None:
    ready, checks = evaluate_dor(_TEMPLATE_FRESH, "task")
    assert not ready
    failed = {c.id for c in checks if not c.ok}
    assert failed == {"summary", "acceptance", "tests"}, failed


def test_v3_h3_headings_pass() -> None:
    ready, checks = evaluate_dor(_H3_HEADINGS, "bug")
    fails = {c.id for c in checks if not c.ok}
    assert not fails, f"H3 sections must be visible: {fails}"


def test_v4_checked_criteria_count() -> None:
    _, checks = evaluate_dor(_CHECKED_ONLY, "improvement")
    acc = next(c for c in checks if c.id == "acceptance")
    assert acc.ok, "checked boxes count toward acceptance (DoD is separate)"


def test_v5_placeholder_only_tests_fail() -> None:
    _, checks = evaluate_dor(_TESTS_PLACEHOLDER, "task")
    tests = next(c for c in checks if c.id == "tests")
    assert not tests.ok, "'...' and 'n/a' are not real test commands"


def test_v6_summary_suffix_heading_passes() -> None:
    _, checks = evaluate_dor(_SUMMARY_SUFFIX_HEADING, "feature")
    summary = next(c for c in checks if c.id == "summary")
    assert summary.ok, "'## Summary (short)' must match the summary heading"


def test_v7_improvement_type_passes() -> None:
    """continuum c1136 V7: the semantics-ruled 'improvement' type passes the
    type check (it's in the OFFER enum)."""
    _, checks = evaluate_dor(_FULL, "improvement")
    t = next(c for c in checks if c.id == "type")
    assert t.ok and t.evidence == "improvement"


def test_v8_enhancement_type_fails_naming_the_as_written_value() -> None:
    """continuum c1136 V8: an at-rest type OUTSIDE the ruled enum
    ('enhancement', the GitHub synonym semantics rejected) FAILS the
    write-side type check with the as-written value named in evidence."""
    _, checks = evaluate_dor(_FULL, "enhancement")
    t = next(c for c in checks if c.id == "type")
    assert not t.ok
    assert "enhancement" in t.evidence, "evidence must name the as-written value"


def test_dor_gate_through_the_execute_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes
    from dataclasses import dataclass

    repo_root = tmp_path / "repo"
    planned = repo_root / "docs" / "backlog" / "planned"
    planned.mkdir(parents=True, exist_ok=True)
    (planned / "002_abstractgateway_fresh.md").write_text(_TEMPLATE_FRESH, encoding="utf-8")
    (planned / "001_abstractgateway_ready.md").write_text(_FULL, encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    @dataclass
    class _Stores:
        base_dir: Path

    @dataclass
    class _Service:
        stores: _Stores

    gw = tmp_path / "gw"
    gw.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(stores=_Stores(base_dir=gw)))

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    with TestClient(app) as client:
        base = "/api/gateway/backlog/planned"

        # Not-ready item with dor=check → 409 naming the failing checks.
        r = client.post(f"{base}/002_abstractgateway_fresh.md/execute?dor=check")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "definition_of_ready_failed"
        failed = {c["id"] for c in detail["checks"] if not c["ok"]}
        assert {"summary", "acceptance", "tests"} <= failed

        # Operator override → queues despite failing checks, records the override.
        r2 = client.post(f"{base}/002_abstractgateway_fresh.md/execute?dor=check&override=true")
        assert r2.status_code == 200, r2.text
        import json as _json

        rid = r2.json()["request_id"]
        queued = _json.loads((gw / "backlog_exec_queue" / f"{rid}.json").read_text(encoding="utf-8"))
        assert queued["dor_overridden"] is True

        # Ready item passes the gate.
        r3 = client.post(f"{base}/001_abstractgateway_ready.md/execute?dor=check")
        assert r3.status_code == 200, r3.text
        rid3 = r3.json()["request_id"]
        queued3 = _json.loads((gw / "backlog_exec_queue" / f"{rid3}.json").read_text(encoding="utf-8"))
        assert queued3["dor_overridden"] is False

        # No dor param → gate is not evaluated (byte-unchanged default path).
        r4 = client.post(f"{base}/002_abstractgateway_fresh.md/execute")
        assert r4.status_code in (200, 409)  # 409 only if already queued; not a DoR refusal
        if r4.status_code == 409:
            assert "definition_of_ready" not in str(r4.json().get("detail", ""))
