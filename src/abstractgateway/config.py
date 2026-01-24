from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(raw: Optional[str], default: int) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _as_float(raw: Optional[str], default: float) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except Exception:
        return default


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return v
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return v2
    return None


@dataclass(frozen=True)
class GatewayHostConfig:
    """Process-level configuration for the AbstractGateway host."""

    data_dir: Path
    flows_dir: Path
    store_backend: str = "file"  # file|sqlite
    db_path: Optional[Path] = None

    runner_enabled: bool = True
    poll_interval_s: float = 0.25
    command_batch_limit: int = 200
    tick_max_steps: int = 100
    tick_workers: int = 2
    run_scan_limit: int = 200

    @staticmethod
    def from_env() -> "GatewayHostConfig":
        # NOTE: We intentionally use ABSTRACTGATEWAY_* as the canonical namespace.
        # For a transition period, we accept legacy ABSTRACTFLOW_* names as fallbacks.
        data_dir_raw = _env("ABSTRACTGATEWAY_DATA_DIR", "ABSTRACTFLOW_RUNTIME_DIR") or "./runtime"
        flows_dir_raw = (
            _env("ABSTRACTGATEWAY_FLOWS_DIR")
            or _env("ABSTRACTFRAMEWORK_WORKFLOWS_DIR")
            or _env("ABSTRACTFLOW_FLOWS_DIR")
            or "./flows"
        )

        store_backend = str(_env("ABSTRACTGATEWAY_STORE_BACKEND") or "file").strip().lower() or "file"
        db_path_raw = _env("ABSTRACTGATEWAY_DB_PATH")

        enabled_raw = _env("ABSTRACTGATEWAY_RUNNER", "ABSTRACTFLOW_GATEWAY_RUNNER") or "1"
        runner_enabled = _as_bool(enabled_raw, True)

        poll_s = _as_float(_env("ABSTRACTGATEWAY_POLL_S", "ABSTRACTFLOW_GATEWAY_POLL_S"), 0.25)
        tick_workers = _as_int(_env("ABSTRACTGATEWAY_TICK_WORKERS", "ABSTRACTFLOW_GATEWAY_TICK_WORKERS"), 2)
        tick_steps = _as_int(_env("ABSTRACTGATEWAY_TICK_MAX_STEPS", "ABSTRACTFLOW_GATEWAY_TICK_MAX_STEPS"), 100)
        batch = _as_int(_env("ABSTRACTGATEWAY_COMMAND_BATCH_LIMIT", "ABSTRACTFLOW_GATEWAY_COMMAND_BATCH_LIMIT"), 200)
        scan = _as_int(_env("ABSTRACTGATEWAY_RUN_SCAN_LIMIT", "ABSTRACTFLOW_GATEWAY_RUN_SCAN_LIMIT"), 200)

        return GatewayHostConfig(
            data_dir=Path(data_dir_raw).expanduser().resolve(),
            flows_dir=Path(flows_dir_raw).expanduser().resolve(),
            store_backend=store_backend,
            db_path=Path(db_path_raw).expanduser().resolve() if db_path_raw else None,
            runner_enabled=bool(runner_enabled),
            poll_interval_s=float(poll_s),
            command_batch_limit=max(1, int(batch)),
            tick_max_steps=max(1, int(tick_steps)),
            tick_workers=max(1, int(tick_workers)),
            run_scan_limit=max(1, int(scan)),
        )
