"""A move of /api/health to "degraded" leaves one incident record on disk (OVH 2026-10-29)."""

from __future__ import annotations

import json

from abstractgateway import health_incidents as hi


def test_one_record_per_transition_with_reasons_in_words(tmp_path) -> None:
    hi.reset_for_tests()
    hi.configure(tmp_path)
    try:
        assert hi.note({"status": "healthy"}, now=1000.0) is None
        body = {"status": "degraded", "dead_workers": ["run-sweeper"], "runner": {"degraded": True, "reason": "tick lock held by nobody"}}
        path = hi.note(body, now=1001.0)
        assert path is not None and path.name.startswith("health-")
        record = json.loads(path.read_text())
        assert record["schema"] == hi.SCHEMA
        assert record["reasons"] == ["background workers stopped: run-sweeper", "the runner is degraded: tick lock held by nobody"]
        assert record["health"]["dead_workers"] == ["run-sweeper"]
        # Still degraded: no second record; healthy again, then degraded a minute later: a new one.
        assert hi.note(body, now=1002.0) is None
        assert hi.note({"status": "healthy"}, now=1003.0) is None
        assert hi.note(body, now=1030.0) is None, "at most one per minute"
        assert hi.note({"status": "healthy"}, now=1031.0) is None
        assert hi.note(body, now=1100.0) is not None
        assert len(list(tmp_path.glob("health-*.json"))) == 2
        assert hi.last_health_incident()["reasons"][0].startswith("background workers stopped")
        # A failed boot says so.
        assert hi.reasons({"status": "degraded", "boot": {"state": "failed", "error": "port in use"}}) == ["the gateway's boot failed: port in use"]
    finally:
        hi.reset_for_tests()


def test_the_health_route_records_a_degraded_status(tmp_path, monkeypatch) -> None:
    import asyncio

    from abstractgateway import app as app_module

    hi.reset_for_tests()
    hi.configure(tmp_path)
    monkeypatch.setattr("abstractgateway.service.gateway_runner_health_snapshot", lambda: {"initialized": True, "degraded": True, "reason": "no runner ticks"})
    try:
        body = asyncio.run(app_module.health_check())
        assert body["status"] == "degraded"
        files = list(tmp_path.glob("health-*.json"))
        assert len(files) == 1 and "no runner ticks" in files[0].read_text()
    finally:
        hi.reset_for_tests()
