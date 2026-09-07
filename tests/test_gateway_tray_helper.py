"""The tray helper's pure layers (no display, no pystray import).

Pins:
- The gauge-ring icon renders at every master size and state, with the
  quantized signature that keeps idle noise from re-encoding a PNG.
- The sampler parses the combined live payload AND the legacy pair, turns
  residency rows into actionable model rows, and STOPS polling after two
  auth refusals (a stale token must never trip the loopback lockout).
- Menu copy: header/detail lines per state, the tooltip, the rebuild
  signature, model-row labels.
- The supervisor's readiness handshake against a real (tiny) child.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic

PIL = pytest.importorskip("PIL")


# ------------------------------------------------------------------- icons


def test_icon_renders_every_state_at_every_master_size() -> None:
    from abstractgateway.tray.icons import STATES, render_icon, render_wide_icon

    for size in (16, 22, 24, 32, 44):
        for state in STATES:
            img = render_icon(size, state=state, mem_pct=63.0, gpu_pct=34.0, active=True, frame=3, mode="neutral")
            assert img.size == (size, size) and img.mode == "RGBA"
            # Something was drawn (the alpha channel is not empty).
            assert img.getchannel("A").getbbox() is not None
    for mode in ("neutral", "light", "dark"):
        assert render_icon(22, state="running", mem_pct=10, gpu_pct=0, mode=mode).size == (22, 22)
    wide = render_wide_icon(44, mem_history=tuple(float(i % 100) for i in range(60)), gpu_history=(None,) * 30 + (50.0,) * 30, mem_pct=60, gpu_pct=30)
    assert wide.height == 44 and wide.width > 44


def test_icon_signature_quantizes_and_ignores_gauges_in_static_states() -> None:
    from abstractgateway.tray.icons import icon_signature, quantize

    assert quantize(None) is None and quantize(0) == 0.0 and quantize(1) == 4.0 and quantize(63.9) == 64.0 and quantize(200) == 100.0
    a = icon_signature(size=22, state="running", mem_pct=63.1, gpu_pct=33.9, active=False, frame=0, mode="neutral")
    b = icon_signature(size=22, state="running", mem_pct=64.9, gpu_pct=33.0, active=False, frame=5, mode="neutral")
    assert a == b  # same 4 % buckets, frame irrelevant while running
    assert icon_signature(size=22, state="paused", mem_pct=1, gpu_pct=1, active=True, frame=0, mode="neutral") == icon_signature(size=22, state="paused", mem_pct=99, gpu_pct=99, active=False, frame=9, mode="neutral")
    assert icon_signature(size=22, state="updating", mem_pct=None, gpu_pct=None, active=False, frame=1, mode="neutral") != icon_signature(size=22, state="updating", mem_pct=None, gpu_pct=None, active=False, frame=2, mode="neutral")


# ----------------------------------------------------------------- sampler


class _Res:
    def __init__(self, ok: bool, status: int, data: Any = None, error: str | None = None) -> None:
        self.ok, self.status, self.data, self.error = ok, status, data, error


class _FakeClient:
    def __init__(self, *, live: Any = None, gpu: Any = None, memory: Any = None, runner: Any = None, host_state: Any = None) -> None:
        self.calls: List[str] = []
        self._live, self._gpu, self._memory, self._runner, self._host_state = live, gpu, memory, runner, host_state

    def _r(self, name: str, value: Any) -> _Res:
        self.calls.append(name)
        if isinstance(value, _Res):
            return value
        return _Res(True, 200, value)

    def live(self) -> _Res:
        return self._r("live", self._live if self._live is not None else _Res(False, 404, None, "not found"))

    def gpu(self) -> _Res:
        return self._r("gpu", self._gpu if self._gpu is not None else _Res(False, 0, None, "unreachable"))

    def memory(self) -> _Res:
        return self._r("memory", self._memory if self._memory is not None else _Res(False, 0, None, "unreachable"))

    def runner(self) -> _Res:
        return self._r("runner", self._runner if self._runner is not None else _Res(False, 0, None, "unreachable"))

    def host_state(self) -> _Res:
        return self._r("host_state", self._host_state if self._host_state is not None else _Res(False, 0, None, "unreachable"))


_LIVE = {
    "ok": True,
    "gpu": {"supported": True, "utilization_gpu_pct": 20.0, "gpus": [{"name": "GPU A", "utilization_gpu_pct": 20.0}, {"name": "GPU B", "utilization_gpu_pct": 90.0}]},
    "memory": {"supported": True, "ram": {"total_bytes": 100, "available_bytes": 40, "used_bytes": 70, "percent": 70.0}, "process": {"rss_bytes": 10}, "device": {"backend": "metal"}},
    "runner": {"paused": False, "inflight_ticks": 2},
}


def test_sampler_prefers_the_live_payload_and_uses_max_gpu_and_available_memory() -> None:
    from abstractgateway.tray.sampler import Sampler

    client = _FakeClient(live=_LIVE)
    s = Sampler(client, version="0.2.29")  # type: ignore[arg-type]
    s._sample_fast()
    snap = s.snapshot()
    assert client.calls == ["live"]
    assert snap.gpu_pct == 90.0 and "GPU A (+1)" == snap.gpu_name  # busiest card, not the mean
    assert snap.mem_used == 60 and snap.mem_total == 100 and snap.mem_pct == 60.0  # total − available
    assert snap.inflight_ticks == 2 and snap.gateway_state == "running" and snap.reachable is True
    assert snap.gpu_history[-1] == 90.0 and snap.process_history[-1] == 10.0


def test_sampler_falls_back_to_the_legacy_pair_on_404_and_flags_unreachable_after_three_misses() -> None:
    from abstractgateway.tray.sampler import Sampler

    client = _FakeClient(gpu={"supported": False, "reason": "no gpu"}, memory={"supported": True, "ram": {"total_bytes": 10, "available_bytes": 5}, "process": {}})
    s = Sampler(client)  # type: ignore[arg-type]
    s._sample_fast()
    assert client.calls == ["live", "gpu", "memory"]
    assert s._live_supported is False
    snap = s.snapshot()
    assert snap.gpu_supported is False and snap.gpu_reason == "no gpu" and snap.mem_pct == 50.0
    s._sample_fast()
    assert client.calls[-2:] == ["gpu", "memory"]  # never asks /live again

    down = _FakeClient()
    s2 = Sampler(down)  # type: ignore[arg-type]
    for _ in range(2):
        s2._sample_fast()
    assert s2.snapshot().gateway_state == "starting"  # never reached: honest "starting", not red
    s2._sample_fast()
    assert s2.snapshot().reachable is False


def test_sampler_stops_polling_after_two_auth_refusals() -> None:
    from abstractgateway.tray.sampler import Sampler

    client = _FakeClient(live=_Res(False, 401, {"detail": "Unauthorized"}, "Unauthorized"))
    s = Sampler(client)  # type: ignore[arg-type]
    s._sample_fast()
    assert s.unauthorized is False
    s._sample_fast()
    assert s.unauthorized is True
    assert s.snapshot().unauthorized is True and s.snapshot().gateway_state == "unreachable"


def test_model_rows_keep_resident_models_only_and_sort_by_size() -> None:
    from abstractgateway.tray.sampler import model_rows_from_host_state

    rows, total = model_rows_from_host_state(
        {
            "models": [
                {"runtime_id": "r1", "provider": "lmstudio", "model": "small", "resident": True, "size_bytes": 100, "locked": False},
                {"runtime_id": "r2", "provider": "mlx", "model": "big", "resident": True, "est_weights_bytes": 5000, "locked": True},
                {"runtime_id": "r3", "provider": "mlx", "model": "cold", "resident": False, "size_bytes": 9999},
                {"provider": "ollama", "model": "unknown-residency", "resident": None},
                "junk",
            ]
        }
    )
    assert [r.name for r in rows] == ["big", "small"]
    assert rows[0].size_source == "estimated" and rows[0].locked is True and rows[0].target == {"runtime_id": "r2"}
    assert total == 5100


# ------------------------------------------------------------------- menu


def _snap(**over: Any):
    from abstractgateway.tray.sampler import Snapshot

    base: Dict[str, Any] = dict(
        ts=0.0, gateway_state="running", reachable=True, paused=False, inflight_ticks=0, paused_by=None, pause_reason=None,
        gpu_supported=True, gpu_reason=None, gpu_pct=34.0, gpu_name="GPU", gpu_history=(34.0,), mem_supported=True, mem_reason=None,
        mem_used=44_240_000_000, mem_total=68_720_000_000, mem_pct=64.0, mem_history=(64.0,), process_rss=3_300_000_000, process_history=(4.8,),
        device_backend="metal", models=(), models_total_bytes=None, models_error=None, can_restart=True, can_shutdown=True, restart_block_reason=None,
        update_job_running=False, step_gate_supported=True, version="0.2.29", last_error=None, consecutive_failures=0, unauthorized=False,
    )
    base.update(over)
    return Snapshot(**base)


def test_menu_copy_per_state_and_tooltip() -> None:
    from abstractgateway.tray.app import menu_signature, model_row_label, state_lines, tooltip_text
    from abstractgateway.tray.sampler import ModelRow

    row = ModelRow(key="r1", name="qwen3-30b-a3b-instruct-2507-mlx-4bit-long-name", provider="lmstudio", size_bytes=17_100_000_000, size_source="reported", locked=True, lockable=True, resident=True, target={"runtime_id": "r1"})
    label = model_row_label(row)
    assert "…" in label and "LM Studio" in label and label.endswith("kept in memory") and "🔒" not in label

    header, detail = state_lines(_snap(models=(row,), models_total_bytes=row.size_bytes))
    assert header == "AbstractGateway — Running" and detail.startswith("Ready · 1 model loaded")
    _, detail = state_lines(_snap(inflight_ticks=2, models=(row,)))
    assert detail.startswith("Working on 2 steps")
    header, detail = state_lines(_snap(gateway_state="paused", paused=True))
    assert header.endswith("Paused") and "Still running" in detail
    _, detail = state_lines(_snap(gateway_state="pausing", paused=True, inflight_ticks=1, step_gate_supported=False))
    assert "older runtime" in detail
    header, detail = state_lines(_snap(gateway_state="unreachable", reachable=False))
    assert header.endswith("Not responding") and "retrying" in detail
    _, detail = state_lines(_snap(gateway_state="updating"), update_phase="updating", update_latest="0.2.30")
    assert "Installing 0.2.30" in detail

    tip = tooltip_text(_snap())
    assert tip.startswith("AbstractGateway — Running\nMemory ") and "GPU 34% busy" in tip and len(tip) <= 120
    assert "Paused (still running)" in tooltip_text(_snap(gateway_state="paused", paused=True))

    a = menu_signature(_snap(mem_pct=64.0, gpu_pct=34.0), update_phase="idle", pending=None, tk_available=True)
    b = menu_signature(_snap(mem_pct=63.0, gpu_pct=38.0), update_phase="idle", pending=None, tk_available=True)
    c = menu_signature(_snap(mem_pct=71.0, gpu_pct=34.0), update_phase="idle", pending=None, tk_available=True)
    assert a == b and a != c  # 5-point memory buckets, 10-point GPU buckets


def test_fmt_helpers() -> None:
    from abstractgateway.tray.sampler import fmt_bytes, fmt_pct

    assert fmt_bytes(None) == "size unknown" and fmt_bytes(0) == "0 B" and fmt_bytes(1536) == "1.5 KB"
    assert fmt_bytes(17_100_000_000) == "15.9 GB" and fmt_bytes(137_438_953_472) == "128 GB"
    assert fmt_pct(None) == "—" and fmt_pct(63.6) == "64%"


# -------------------------------------------------------------- supervisor


def test_supervisor_readiness_handshake_with_a_real_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tiny stand-in child: reads the handshake, answers ready, then waits
    for stdin EOF (the liveness channel) and exits 0."""
    from abstractgateway import tray_supervisor

    child = (
        "import sys, json\n"
        "line = sys.stdin.readline()\n"
        "hs = json.loads(line)\n"
        "assert hs['token'] and hs['base_url'] and hs['parent_pid']\n"
        "sys.stdout.write(json.dumps({'ready': True}) + '\\n'); sys.stdout.flush()\n"
        "sys.stdin.read()\n"  # EOF = parent gone / stop requested
        "sys.exit(0)\n"
    )
    script = tmp_path / "child.py"
    script.write_text(child, encoding="utf-8")
    monkeypatch.setattr(tray_supervisor, "_child_python", lambda: sys.executable)
    real_popen = tray_supervisor.subprocess.Popen

    def _popen(cmd: List[str], **kw: Any):
        assert cmd[1:3] == ["-m", "abstractgateway.tray"]
        return real_popen([sys.executable, str(script)], **kw)

    monkeypatch.setattr(tray_supervisor.subprocess, "Popen", _popen)
    sup = tray_supervisor.TraySupervisor()
    st = sup.start(base_url="http://127.0.0.1:1", data_dir=tmp_path, version="0.0.0", decision=tray_supervisor.TrayDecision(True, "ok"))
    try:
        assert st["running"] is True and st["ready"] is True and st["pid"]
        assert (tmp_path / "logs" / "tray.log").exists()
        # The child's token is accepted from loopback while it lives...
        from abstractgateway.security.gateway_security import ephemeral_loopback_token_valid

        assert ephemeral_loopback_token_valid(sup._token or "", peer_ip="127.0.0.1") is True
        token = sup._token
    finally:
        st = sup.stop()
    # ...and stop() closes stdin (polite quit), waits, and revokes the token.
    assert st["running"] is False and st["exit_code"] == 0
    assert ephemeral_loopback_token_valid(token or "", peer_ip="127.0.0.1") is False


def test_supervisor_reports_a_child_that_fails_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import tray_supervisor

    script = tmp_path / "child.py"
    script.write_text("import sys, json\nsys.stdin.readline()\nsys.stdout.write(json.dumps({'ready': False, 'reason': 'no system tray here', 'hint': 'install the AppIndicator extension'}) + '\\n'); sys.stdout.flush()\nsys.exit(4)\n", encoding="utf-8")
    real_popen = tray_supervisor.subprocess.Popen
    monkeypatch.setattr(tray_supervisor.subprocess, "Popen", lambda cmd, **kw: real_popen([sys.executable, str(script)], **kw))
    sup = tray_supervisor.TraySupervisor()
    st = sup.start(base_url="http://127.0.0.1:1", data_dir=tmp_path, version="0.0.0", decision=tray_supervisor.TrayDecision(True, "ok"))
    assert st["ready"] is False or st["running"] is False
    assert st["failure"] and st["failure"]["reason"] == "no system tray here"
    assert "no system tray" in str(st["error"])
    import time as _time

    deadline = _time.time() + 5
    while sup.status()["running"] and _time.time() < deadline:
        _time.sleep(0.05)
    assert sup.status()["exit_code"] == 4
    sup.stop()
