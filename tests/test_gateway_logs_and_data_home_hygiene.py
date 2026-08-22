"""Logs surface + data-home hygiene (operator 2026-08-19: "a cache is a
cache" — logs are their own readable category; stale registrations get an
honest label and a Forget; "?" never renders as a size again).

Pins:
- GET /admin/logs lists ONLY kind=logs homes' files (symlinks never list);
  stale log homes appear as missing.
- GET /admin/logs/read tails with the house contract and REFUSES: non-logs
  homes (the arbitrary-file-reader hole, adversary B1), path traversal,
  symlinks, and files outside the home.
- POST /admin/data-homes/forget removes ONLY rows whose path is gone; a
  live row refuses (it would re-register at boot — zombie UX, B3);
  all_stale sweeps in one call.
- list_homes stamps `exists` on the fast (no-sizes) pass too.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "logs-admin-secret"


@pytest.fixture()
def registry_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """An isolated data registry + a registered live log home and a stale one."""
    monkeypatch.setenv("ABSTRACTFRAMEWORK_DATA_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    from abstractcore.utils.data_registry import register_data_home

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "gateway.log").write_text("line one\nline two\nline three\n", encoding="utf-8")
    (logs_dir / "stack.log").write_text("s1\ns2\n", encoding="utf-8")
    secret_target = tmp_path / "secret.txt"
    secret_target.write_text("out-of-home secret\n", encoding="utf-8")
    os.symlink(secret_target, logs_dir / "sneaky.log")

    other_dir = tmp_path / "artifacts-store"
    other_dir.mkdir()
    (other_dir / "deliverable.txt").write_text("precious\n", encoding="utf-8")

    stale_dir = tmp_path / "gone-logs"
    stale_dir.mkdir()

    register_data_home("t-logs", path=str(logs_dir), kind="logs", owner="t", safe_to_purge=True, description="test logs")
    register_data_home("t-artifacts", path=str(other_dir), kind="artifacts", owner="t", safe_to_purge=False, description="durable")
    register_data_home("t-stale-logs", path=str(stale_dir), kind="logs", owner="t", safe_to_purge=True, description="stale")
    register_data_home("t-stale-store", path=str(tmp_path / "gone-store"), kind="artifacts", owner="t", safe_to_purge=False, description="stale durable")
    # Now the stale rows' paths vanish.
    stale_dir.rmdir()
    return {"logs_dir": logs_dir, "tmp": tmp_path}


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_logs_listing_serves_only_log_homes_and_skips_symlinks(registry_env) -> None:
    with _client() as client:
        r = client.get("/api/gateway/admin/logs")
        assert r.status_code == 200, r.text
        homes = {h["home"]: h for h in r.json()["homes"]}
        assert "t-logs" in homes and "t-stale-logs" in homes
        assert "t-artifacts" not in homes, "non-logs homes must never appear on the Logs surface"
        names = {f["name"] for f in homes["t-logs"]["files"]}
        assert names == {"gateway.log", "stack.log"}, "symlinks never list"
        assert homes["t-stale-logs"]["missing"] is True


def test_log_read_tails_and_refuses_escapes(registry_env) -> None:
    with _client() as client:
        ok = client.get(
            "/api/gateway/admin/logs/read",
            params={"home": "t-logs", "file": "gateway.log", "max_bytes": 1024},
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert "line three" in body["content"]
        assert body["truncated"] is False

        # A bounded tail cuts the head, honestly flagged.
        small = client.get(
            "/api/gateway/admin/logs/read",
            params={"home": "t-logs", "file": "gateway.log", "max_bytes": 1024, "after_bytes": 9},
        )
        assert small.status_code == 200
        assert small.json()["truncated"] is True
        assert small.json()["content"].endswith("line three\n")

        # HARD SCOPE (adversary B1): a non-logs home refuses — this route
        # must never become an arbitrary-file reader over durable stores.
        deny = client.get(
            "/api/gateway/admin/logs/read",
            params={"home": "t-artifacts", "file": "deliverable.txt"},
        )
        assert deny.status_code == 404, deny.text

        # Traversal and symlink escapes refuse.
        for bad in ("../secret.txt", "a/b.log", "..", ""):
            resp = client.get(
                "/api/gateway/admin/logs/read",
                params={"home": "t-logs", "file": bad},
            )
            assert resp.status_code in (400, 422), f"{bad!r} must refuse, got {resp.status_code}"
        sneaky = client.get(
            "/api/gateway/admin/logs/read",
            params={"home": "t-logs", "file": "sneaky.log"},
        )
        assert sneaky.status_code == 400, "symlinked files are never served"


def test_forget_is_stale_only_and_bulk(registry_env) -> None:
    with _client() as client:
        # A live row refuses with the boot-resurrection reason.
        live = client.post(
            "/api/gateway/admin/data-homes/forget",
            json={"name": "t-logs"},
        )
        assert live.status_code == 409, live.text
        assert "next boot" in live.json()["detail"]

        # One stale row forgets.
        one = client.post(
            "/api/gateway/admin/data-homes/forget",
            json={"name": "t-stale-logs"},
        )
        assert one.status_code == 200, one.text
        assert one.json()["forgotten"] == ["t-stale-logs"]

        # Bulk sweeps the remaining stale row — protection is irrelevant
        # (rows are registrations, not stores; disk is untouched).
        sweep = client.post(
            "/api/gateway/admin/data-homes/forget",
            json={"all_stale": True},
        )
        assert sweep.status_code == 200, sweep.text
        assert sweep.json()["forgotten"] == ["t-stale-store"]

        # The registry no longer serves either stale row.
        after = client.get("/api/gateway/admin/data-homes", params={"sizes": 0})
        names = {r["name"] for r in after.json()["homes"]}
        assert "t-stale-logs" not in names and "t-stale-store" not in names
        assert "t-logs" in names and "t-artifacts" in names


def test_fast_listing_carries_exists(registry_env, tmp_path) -> None:
    from abstractgateway.data_homes import list_homes

    rows, _ = list_homes(include_sizes=False)
    by_name = {r["name"]: r for r in rows}
    assert by_name["t-logs"]["exists"] is True
    assert by_name["t-stale-logs"]["exists"] is False
