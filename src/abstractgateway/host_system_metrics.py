"""Bounded, read-only system observations for the Resources snapshot.

Called on the host-state worker, never on the event loop. No threads, subprocesses,
model imports, persistence, or blocking CPU sampling. Memory residency stays in the
Runtime facade. Failures are independent and a first rate sample stays unknown.
"""
from __future__ import annotations

import importlib
import math
import os
import platform
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _probe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"supported": True, **fn()}
    except Exception as exc:
        return {"supported": False, "reason": f"{type(exc).__name__}: {exc}"}


class SystemSampler:
    def __init__(self, *, ttl_s: float = 2.0) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value: dict[str, Any] | None = None
        self._volume: str | None = None
        self._previous: dict[str, tuple[float, tuple[float, ...]]] = {}

    def _delta(self, name: str, now: float, counters: tuple[float, ...]) -> tuple[tuple[float, ...] | None, str | None]:
        previous = self._previous.get(name)
        self._previous[name] = (now, counters)
        if previous is None:
            return None, "Collecting the first interval"
        elapsed = now - previous[0]
        differences = tuple(v - p for v, p in zip(counters, previous[1]))
        if elapsed <= 0 or len(counters) != len(previous[1]) or any(v < 0 for v in differences):
            return None, "Counters reset; collecting a new interval"
        return tuple(v / elapsed for v in differences), None

    def sample(self, data_dir: str | Path) -> dict[str, Any]:
        volume = str(data_dir)
        with self._lock:
            now = time.monotonic()
            if self._at is not None and now - self._at < self.ttl_s and self._volume == volume:
                return dict(self._value or {})
            try:
                ps = importlib.import_module("psutil")
            except ImportError:
                ps = None

            def need_psutil() -> Any:
                if ps is None:
                    raise RuntimeError("psutil is not installed on this host")
                return ps

            def cpu() -> dict[str, Any]:
                p = need_psutil()
                times = p.cpu_times()
                # Linux guest/guest_nice are already included in user/nice.
                total = sum(times) - getattr(times, "guest", 0) - getattr(times, "guest_nice", 0)
                idle = times.idle + getattr(times, "iowait", 0)
                rates, reason = self._delta("cpu", now, (total, idle))
                pct = None if rates is None or rates[0] <= 0 else max(0.0, min(100.0, (1 - rates[1] / rates[0]) * 100))
                load = None
                try:
                    load = list(os.getloadavg())
                except (AttributeError, OSError):
                    pass
                return {"utilization_pct": pct, "logical_cores": p.cpu_count(), "load_average": load,
                        **({"reason": reason or "CPU counter did not advance"} if pct is None else {})}

            def storage() -> dict[str, Any]:
                # Resolve an existing parent without creating the data directory.
                path = Path(data_dir).expanduser().absolute()
                while not path.exists() and path != path.parent:
                    path = path.parent
                disk = shutil.disk_usage(path)
                return {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free,
                        "scope": "gateway data volume"}

            def swap() -> dict[str, Any]:
                value = need_psutil().swap_memory()
                return {"used_bytes": value.used, "total_bytes": value.total}

            def transfers(kind: str) -> dict[str, Any]:
                p = need_psutil()
                value = p.disk_io_counters() if kind == "disk_io" else p.net_io_counters()
                if value is None:
                    raise RuntimeError("This platform does not report transfer counters")
                fields = ("read_bytes", "write_bytes") if kind == "disk_io" else ("bytes_recv", "bytes_sent")
                rates, reason = self._delta(kind, now, tuple(float(getattr(value, f)) for f in fields))
                if rates is not None and not all(math.isfinite(v) for v in rates):
                    rates, reason = None, "Invalid transfer interval"
                return {"read_bytes_per_s": None if rates is None else rates[0],
                        "write_bytes_per_s": None if rates is None else rates[1],
                        "scope": "all host disks" if kind == "disk_io" else "all host interfaces (includes loopback)",
                        **({"reason": reason} if reason else {})}

            result = {"ts": time.time(), "hardware": _probe(lambda: {"platform": sys.platform, "machine": platform.machine(), "host_name": socket.gethostname()}), "cpu": _probe(cpu), "storage": _probe(storage),
                      "swap": _probe(swap), "disk_io": _probe(lambda: transfers("disk_io")),
                      "network": _probe(lambda: transfers("network"))}
            self._at, self._value, self._volume = now, result, volume
            return dict(result)


_sampler = SystemSampler()


def get_host_system_metrics(data_dir: str | Path) -> dict[str, Any]:
    return _sampler.sample(data_dir)
