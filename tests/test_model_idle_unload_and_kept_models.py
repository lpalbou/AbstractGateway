"""Idle unload setting and kept-loaded persistence (root backlog 1115, operator rulings 2026-11-03).

- `models.idle_unload_minutes`: `serve --model-idle-unload MINUTES` > stored > 60; 0 = never; whole
  minutes up to a week; no environment variable.
- Keep loaded persists: a confirmed lock is recorded (`models.kept_loaded`), an unlock (whatever the
  runtime answers) or a forced unload removes the record; the gateway sends the list
  (`set_model_residency_locks`, replayed by the model host after a child restart) and the idle-unload
  seconds (`set_model_idle_unload`) to the runtime after its start and after every change.
- The Models page and the TUI say one plain line per loaded model, from what the runtime reports.

Each test goes red without the rule it names.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from node_requirement import require_node
from test_gateway_console_voice_provider_states import _function, _html

pytestmark = pytest.mark.basic


# ------------------------------------------------------------------ the setting


def test_idle_unload_resolves_flag_over_stored_over_sixty(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import resolve_model_idle_unload

    assert resolve_model_idle_unload(tmp_path, stored={}, launch={}) == {"value": 60, "source": "default"}
    assert resolve_model_idle_unload(tmp_path, stored={"models.idle_unload_minutes": 15}, launch={}) == {"value": 15, "source": "stored"}
    flag = resolve_model_idle_unload(tmp_path, stored={"models.idle_unload_minutes": 15}, launch={"model_idle_unload_minutes": 0})
    assert flag == {"value": 0, "source": "flag", "stored_value": 15}


@pytest.mark.parametrize("raw", [-1, 10081, "abc", "", 1.5, True, None])
def test_idle_unload_refuses_what_is_not_whole_minutes_up_to_a_week(raw: Any) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, parse_idle_unload_minutes

    with pytest.raises(RuntimeConfigError):
        parse_idle_unload_minutes(raw)


def test_idle_unload_accepts_zero_and_a_week() -> None:
    from abstractgateway.runtime_config import parse_idle_unload_minutes

    assert parse_idle_unload_minutes(0) == 0 and parse_idle_unload_minutes("10080") == 10080 and parse_idle_unload_minutes(30.0) == 30


def test_the_settings_door_writes_reads_and_clears_the_value(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, read_runtime_config, write_runtime_config

    entry = read_runtime_config(tmp_path, is_admin=True)["models.idle_unload_minutes"]
    assert entry["value"] == 60 and entry["source"] == "default" and entry["flag"] == "--model-idle-unload MINUTES"
    out = write_runtime_config(tmp_path, {"models.idle_unload_minutes": "45"}, actor="admin")
    assert out["models.idle_unload_minutes"]["value"] == 45 and out["models.idle_unload_minutes"]["source"] == "stored"
    with pytest.raises(RuntimeConfigError):
        write_runtime_config(tmp_path, {"models.idle_unload_minutes": -5}, actor="admin")
    assert read_runtime_config(tmp_path, is_admin=True)["models.idle_unload_minutes"]["value"] == 45  # a refusal never lands
    write_runtime_config(tmp_path, {"models.idle_unload_minutes": None}, actor="admin")
    assert read_runtime_config(tmp_path, is_admin=True)["models.idle_unload_minutes"]["source"] == "default"


def test_serve_records_the_flag_for_this_run_and_refuses_a_bad_value(tmp_path: Path) -> None:
    from abstractgateway import cli
    from abstractgateway.runtime_config import read_launch_settings, resolve_model_idle_unload

    cli._apply_backlog_launch_flags(argparse.Namespace(backlog_root=None, exec_runner=None, model_idle_unload=20), tmp_path)
    assert read_launch_settings(tmp_path) == {"model_idle_unload_minutes": 20}
    assert resolve_model_idle_unload(tmp_path) == {"value": 20, "source": "flag"}
    with pytest.raises(SystemExit):
        cli._apply_backlog_launch_flags(argparse.Namespace(backlog_root=None, exec_runner=None, model_idle_unload=-3), tmp_path)


def test_serve_has_the_flag_and_no_environment_variable() -> None:
    import sys

    from abstractgateway import runtime_config

    out = subprocess.run([sys.executable, "-m", "abstractgateway", "serve", "--help"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "--model-idle-unload MINUTES" in out.stdout
    assert "ABSTRACTGATEWAY_MODEL_IDLE" not in Path(runtime_config.__file__).read_text()


# ------------------------------------------------------------------ kept loaded: the record


def test_kept_models_are_recorded_once_and_forgotten(tmp_path: Path) -> None:
    from abstractgateway.kept_models import forget_kept, kept_models, remember_kept

    assert remember_kept(tmp_path, "mlx", "qwen") is True
    assert remember_kept(tmp_path, "MLX", "qwen") is False  # same model, recorded once
    assert remember_kept(tmp_path, "lmstudio", "gemma", "http://127.0.0.1:1234/v1") is True
    assert kept_models(tmp_path) == [
        {"provider": "mlx", "model": "qwen"},
        {"provider": "lmstudio", "model": "gemma", "base_url": "http://127.0.0.1:1234/v1"},
    ]
    assert forget_kept(tmp_path, "mlx", "qwen") is True
    assert forget_kept(tmp_path, "mlx", "qwen") is False
    assert kept_models(tmp_path) == [{"provider": "lmstudio", "model": "gemma", "base_url": "http://127.0.0.1:1234/v1"}]


def test_the_settings_door_never_writes_the_kept_list(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, write_runtime_config

    with pytest.raises(RuntimeConfigError):
        write_runtime_config(tmp_path, {"models.kept_loaded": [{"provider": "mlx", "model": "x"}]}, actor="admin")


class _PolicyFacade:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def set_model_idle_unload(self, seconds: Any) -> None:
        self.calls.append(("idle", seconds))

    def set_model_residency_locks(self, locks: Any) -> None:
        self.calls.append(("locks", locks))


def test_the_policy_sends_minutes_as_seconds_and_the_whole_kept_list(tmp_path: Path) -> None:
    from abstractgateway.kept_models import apply_model_policy, remember_kept
    from abstractgateway.runtime_config import write_runtime_config

    remember_kept(tmp_path, "mlx", "qwen")
    facade = _PolicyFacade()
    apply_model_policy(tmp_path, facade, why="a test")
    assert facade.calls == [("idle", 3600), ("locks", [{"provider": "mlx", "model": "qwen"}])]
    write_runtime_config(tmp_path, {"models.idle_unload_minutes": 0}, actor="admin")
    facade.calls.clear()
    apply_model_policy(tmp_path, facade, why="a test")
    assert facade.calls[0] == ("idle", 0)  # 0 = never unloads


def test_a_runtime_without_the_setters_is_reported_loudly(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from abstractgateway.kept_models import RUNTIME_NEEDED, apply_model_policy

    with caplog.at_level("ERROR", logger="abstractgateway.kept_models"):
        report = apply_model_policy(tmp_path, object(), why="a test")
    assert report["missing"] == ["set_model_idle_unload", "set_model_residency_locks"]
    assert sum(RUNTIME_NEEDED in r.getMessage() for r in caplog.records) == 2


# ------------------------------------------------------------------ kept loaded: the routes


class _Facade:
    def __init__(self, *, lock_ok: bool = True) -> None:
        self.lock_ok = lock_ok
        self.calls: List[tuple] = []

    def lock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("lock", dict(payload)))
        if not self.lock_ok:
            return {"ok": False, "success": False, "error": "model_not_resident", "provider": "mlx", "model": "qwen"}
        return {"ok": True, "locked": True, "runtime_id": "local:text_generation:mlx:qwen", "provider": "mlx", "model": "qwen"}

    def unlock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("unlock", dict(payload)))
        return {"ok": False, "success": False, "error": "not_found"}

    def unload_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("unload", dict(kwargs)))
        return {"ok": True, "unloaded": True, "provider": kwargs.get("provider"), "model": kwargs.get("model")}


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, facade: _Facade) -> tuple[TestClient, dict]:
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app
    from abstractgateway.routes import gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (facade, None))
    from abstractgateway import kept_models

    applied: List[str] = []
    monkeypatch.setattr(kept_models, "apply_in_background", lambda why, **k: applied.append(why))
    facade.applied = applied  # type: ignore[attr-defined]
    return TestClient(app), {"Authorization": "Bearer t"}


def test_a_confirmed_lock_is_recorded_and_an_unlock_removes_it_whatever_the_runtime_says(tmp_path, monkeypatch) -> None:
    from abstractgateway.kept_models import kept_models

    facade = _Facade()
    client, h = _client(tmp_path, monkeypatch, facade)
    with client:
        assert client.post("/api/gateway/models/lock", json={"runtime_id": "local:text_generation:mlx:qwen"}, headers=h).json()["ok"] is True
        assert kept_models(tmp_path / "runtime") == [{"provider": "mlx", "model": "qwen"}]
        assert facade.applied[-1] == "a model was kept loaded"  # the runtime gets the new list
        client.post("/api/gateway/models/unlock", json={"runtime_id": "local:text_generation:mlx:qwen"}, headers=h)
        assert kept_models(tmp_path / "runtime") == []
        assert facade.applied[-1] == "a model was released from Keep loaded"


def test_a_refused_lock_records_nothing(tmp_path, monkeypatch) -> None:
    from abstractgateway.kept_models import kept_models

    client, h = _client(tmp_path, monkeypatch, _Facade(lock_ok=False))
    with client:
        client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=h)
        assert kept_models(tmp_path / "runtime") == []


def test_a_forced_unload_ends_the_keep(tmp_path, monkeypatch) -> None:
    from abstractgateway.kept_models import kept_models, remember_kept

    client, h = _client(tmp_path, monkeypatch, _Facade())
    with client:
        remember_kept(tmp_path / "runtime", "mlx", "qwen")
        client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "qwen", "force": True}, headers=h)
        assert kept_models(tmp_path / "runtime") == []


# ------------------------------------------------------------------ when the policy is sent


def test_a_settings_change_sends_the_policy_at_once(tmp_path, monkeypatch) -> None:
    facade = _Facade()
    client, h = _client(tmp_path, monkeypatch, facade)
    with client:
        r = client.post("/api/gateway/admin/runtime-config", json={"models.idle_unload_minutes": 30}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["models.idle_unload_minutes"]["value"] == 30
        assert facade.applied[-1] == "the idle-unload setting changed"
        bad = client.post("/api/gateway/admin/runtime-config", json={"models.idle_unload_minutes": "soon"}, headers=h)
        assert bad.status_code == 400


def test_the_gateway_start_sends_the_policy() -> None:
    from abstractgateway import service

    src = Path(service.__file__).read_text()
    assert src.count("_restore_kept_models_after_boot()") >= 2  # both start paths (single and multi user)
    assert 'apply_in_background("the gateway started")' in src


def test_the_rows_carry_what_the_runtime_reports() -> None:
    from abstractgateway.routes.gateway import _normalize_model_residency_row_v1

    row = _normalize_model_residency_row_v1({"task": "text_generation", "provider": "mlx", "model": "qwen", "resident": True,
                                             "idle_unload_at": "2026-11-03T10:48:00+00:00", "in_use": False,
                                             "locked_by": ["admin"], "last_used_at": "2026-11-03T09:48:00+00:00"})
    assert row["idle_unload_at"] == "2026-11-03T10:48:00+00:00" and row["in_use"] is False and row["locked_by"] == ["admin"]


# ------------------------------------------------------------------ the line on the Models page


def _line(row: Dict[str, Any], now_ms: int) -> str:
    html = _html()
    fns = "\n".join(_function(html, n) for n in ("idleUnloadMinutes", "idleUnloadLine"))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(fns + f"\nconsole.log(JSON.stringify(idleUnloadLine({json.dumps(row)}, {now_ms})));")
    proc = subprocess.run([require_node(), f.name], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


NOW = 1_762_000_000_000  # ms


def _iso(ms: int) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


def test_the_models_page_line_says_what_the_runtime_reports() -> None:
    assert _line({"resident": True, "locked_by": ["admin"], "idle_unload_at": None}, NOW) == "Kept loaded: never unloads"
    assert _line({"resident": True, "locked": True}, NOW) == "Kept loaded: never unloads"
    assert _line({"resident": True, "last_used_at": _iso(NOW - 12 * 60_000), "idle_unload_at": _iso(NOW + 48 * 60_000)}, NOW) == (
        "Unused for 12 min, unloads in 48 min")
    assert _line({"resident": True, "idle_unload_at": _iso(NOW + 90 * 60_000)}, NOW) == "Unloads in 1 h 30 min"
    # Off, in use, nothing reported, or not resident: no line (never computed from the gateway's setting).
    assert _line({"resident": True, "idle_unload_at": None, "locked_by": []}, NOW) == ""
    assert _line({"resident": True}, NOW) == ""
    assert _line({"resident": False, "locked_by": ["admin"]}, NOW) == ""
