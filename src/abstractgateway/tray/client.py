"""Tiny loopback HTTP client for the tray helper (stdlib only, never raises to the UI).

Every call returns a `Result` (`ok`, `status`, `data`, `error`). Timeouts are
short and per-purpose: a metrics poll must fail fast, an unload may take a
while. The bearer token is the per-process ephemeral admin token the gateway
handed over on stdin; it is only ever sent to the base URL it came with.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Result:
    ok: bool
    status: int
    data: Any = None
    error: Optional[str] = None

    @property
    def detail(self) -> str:
        """Best human-readable message for a failure."""
        if isinstance(self.data, dict):
            hint = self.data.get("hint")
            tail = f" {hint.strip()}" if isinstance(hint, str) and hint.strip() else ""
            for key in ("detail", "error", "message", "reason"):
                value = self.data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip() + tail
                if isinstance(value, dict):
                    inner = value.get("detail") or value.get("error") or value.get("message")
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip() + tail
        return str(self.error or f"HTTP {self.status}")

    @property
    def model_locked(self) -> bool:
        """The one 409 the unload flow may answer with a 'force' offer."""
        if self.status != 409:
            return False
        blob = json.dumps(self.data) if self.data is not None else str(self.error or "")
        return "model_locked" in blob


class GatewayClient:
    def __init__(self, base_url: str, token: str, *, api_prefix: str = "/api/gateway") -> None:
        self.base_url = str(base_url).rstrip("/")
        self.api_prefix = api_prefix
        self._token = str(token or "")

    # -- plumbing -----------------------------------------------------------

    def _request(self, method: str, path: str, *, body: Any = None, timeout: float = 4.0, api: bool = True) -> Result:
        url = self.base_url + (self.api_prefix if api else "") + path
        data = None
        headers = {"Accept": "application/json", "User-Agent": "abstractgateway-tray"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=float(timeout)) as resp:  # noqa: S310 - loopback URL from the parent
                raw = resp.read()
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            parsed = _parse_json(raw)
            return Result(False, int(exc.code), parsed, error=parsed.get("detail") if isinstance(parsed, dict) else (raw.decode("utf-8", "replace")[:300] or str(exc)))
        except urllib.error.URLError as exc:
            return Result(False, 0, None, error=f"unreachable: {getattr(exc, 'reason', exc)}")
        except Exception as exc:  # noqa: BLE001 - timeouts, resets
            return Result(False, 0, None, error=f"{type(exc).__name__}: {exc}")
        parsed = _parse_json(raw)
        ok = 200 <= status < 300 and not (isinstance(parsed, dict) and parsed.get("ok") is False)
        err = None
        if not ok and isinstance(parsed, dict):
            err = str(parsed.get("error") or parsed.get("detail") or f"HTTP {status}")
        return Result(ok, status, parsed, error=err)

    # -- reads ---------------------------------------------------------------

    def health(self) -> Result:
        return self._request("GET", "/api/health", timeout=2.0, api=False)

    def runner(self) -> Result:
        return self._request("GET", "/host/runner", timeout=2.0)

    def live(self) -> Result:
        """GPU + memory + execution state in one call (gateway 2026-09-05+)."""
        return self._request("GET", "/host/metrics/live", timeout=2.5)

    def gpu(self) -> Result:
        return self._request("GET", "/host/metrics/gpu", timeout=2.0)

    def memory(self) -> Result:
        return self._request("GET", "/host/metrics/memory", timeout=2.0)

    def host_state(self) -> Result:
        # Residency walks every provider; a host mid-load answers late, not never.
        return self._request("GET", "/host/state", timeout=20.0)

    def update_state(self) -> Result:
        return self._request("GET", "/host/update", timeout=4.0)

    def recent_runs(self, *, limit: int = 25, window_hours: float = 24.0) -> Result:
        """Recent runs on this MACHINE — `/host/runs`, not `/runs`.

        `/runs` answers for the calling principal's data plane, so the tray
        reported an idle machine while a conversation was running on the
        gateway's default plane. Everything else on this menu (memory, GPU,
        loaded models) is host-wide; the run list is too. Root runs only —
        a deep-research run spawns dozens of children and this is a glance.
        """
        return self._request(
            "GET",
            f"/host/runs?limit={int(limit)}&window_hours={float(window_hours)}",
            timeout=8.0,
        )

    def models_installed(self) -> Result:
        """Every model the local engines hold, with sizes (`models_installed_v1`).
        Walks the HF cache and asks LM Studio/Ollama: slow on a big machine."""
        return self._request("GET", "/models/installed", timeout=60.0)

    def model_availability(self) -> Result:
        """Configured capability routes (the defaults) + local availability."""
        return self._request("GET", "/models/availability", timeout=60.0)

    def apps(self) -> Result:
        """Mission O's apps overview; `latest=false` keeps the npm registry out of a poll."""
        return self._request("GET", "/apps?latest=false", timeout=15.0)

    def network(self) -> Result:
        """Mission R's network settings + every address this gateway answers on."""
        return self._request("GET", "/network", timeout=6.0)

    # -- actions -------------------------------------------------------------

    def set_network(self, mode: str, *, port: Optional[int] = None, acknowledge_internet: bool = False) -> Result:
        body: Dict[str, Any] = {"mode": str(mode)}
        if port is not None:
            body["port"] = int(port)
        if acknowledge_internet:
            body["acknowledge_internet"] = True
        return self._request("POST", "/network", body=body, timeout=15.0)

    def network_restart(self) -> Result:
        return self._request("POST", "/network/restart", body={}, timeout=10.0)

    def load_model(self, *, provider: str, model: str, task: str = "text_generation", timeout_s: float = 900.0) -> Result:
        """Preload one model (the gateway pins it resident). Big models take minutes."""
        return self._request("POST", "/models/load", body={"provider": provider, "model": model, "task": task}, timeout=timeout_s)

    def app_launch(self, app_id: str) -> Result:
        return self._request("POST", f"/apps/{app_id}/launch", body={}, timeout=45.0)

    def app_open(self, app_id: str) -> Result:
        """A one-time signed-in handover link (`open_url`, relative to the gateway)."""
        return self._request("POST", f"/apps/{app_id}/open", body={"remember": True}, timeout=15.0)

    def app_launch_tui(self, app_id: str) -> Result:
        """Mission Y: a new terminal window on this machine, the app's
        terminal version signed in through a one-time handover."""
        return self._request("POST", f"/apps/{app_id}/launch-tui", body={}, timeout=30.0)

    def app_install(self, app_id: str, *, launch: bool = True) -> Result:
        return self._request("POST", f"/apps/{app_id}/install", body={"launch": bool(launch)}, timeout=30.0)

    def apps_job(self, job_id: str) -> Result:
        return self._request("GET", f"/apps/jobs/{job_id}", timeout=10.0)

    def pause(self, reason: Optional[str] = None) -> Result:
        return self._request("POST", "/host/pause", body={"reason": reason} if reason else {}, timeout=6.0)

    def resume(self) -> Result:
        return self._request("POST", "/host/resume", body={}, timeout=6.0)

    def unload_model(self, target: Dict[str, Any], *, force: bool = False) -> Result:
        body = dict(target)
        if force:
            body["force"] = True
        return self._request("POST", "/models/unload", body=body, timeout=120.0)

    def unlock_model(self, target: Dict[str, Any]) -> Result:
        return self._request("POST", "/models/unlock", body=dict(target), timeout=30.0)

    def restart(self, reason: Optional[str] = None) -> Result:
        return self._request("POST", "/host/restart", body={"reason": reason} if reason else {}, timeout=6.0)

    def shutdown(self, reason: Optional[str] = None) -> Result:
        return self._request("POST", "/host/shutdown", body={"reason": reason} if reason else {}, timeout=6.0)

    def set_setting(self, key: str, value: Any) -> Result:
        """Write ONE runtime-config knob (never more: a wider body could erase maps)."""
        return self._request("POST", "/admin/runtime-config", body={str(key): value}, timeout=6.0)

    def check_update(self) -> Result:
        return self._request("POST", "/host/update/check", body={}, timeout=15.0)

    def start_update(self) -> Result:
        return self._request("POST", "/host/update/start", body={}, timeout=10.0)


def _parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
