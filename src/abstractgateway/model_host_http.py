"""Optional HTTP listeners in the existing execution owner.

Native RPC readiness never waits for these services. The legacy listener keeps
Gateway admission; the direct listener uses Core's own configuration and auth.
Neither listener owns or tears down the owner's provider resources.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Dict

from .endpoint_settings import read_settings
from .model_host import BACKOFF_S, MAX_RESTARTS, RESTART_WINDOW_S

logger = logging.getLogger("abstractgateway.model_host.http")


STATUS_FILE = Path("split") / "model_host_http.json"


def read_http_status(data_dir: Path) -> dict:
    """Non-secret service state from a live execution owner, for split API readers."""
    from .first_run import pid_alive

    try:
        settings = read_settings(data_dir)
        doc = json.loads((Path(data_dir) / STATUS_FILE).read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or pid_alive(doc.get("pid")) is not True:
            return {}
        services = doc.get("services")
        if not isinstance(services, dict):
            return {}
        if not (settings.enabled or settings.direct_enabled):
            return {name: state for name, state in services.items()
                    if isinstance(state, dict) and state.get("state") == "stopping"}
        return services
    except (OSError, ValueError):
        return {}


class OptionalHTTP:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._handles: Dict[str, Any] = {}
        self._state = {name: {"state": "disabled", "url": None, "error": None}
                       for name in ("legacy", "direct")}
        self._requested = {"legacy": False, "direct": False}
        self._attempts = {"legacy": [], "direct": []}
        self._next_attempt = {"legacy": 0.0, "direct": 0.0}
        self._gave_up = {"legacy": False, "direct": False}
        self._published = None
        self._thread = threading.Thread(target=self._run, name="model-host-optional-http", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            return {name: dict(state) for name, state in self._state.items()}

    def _record(self, name: str, state: str, *, url=None, error=None) -> None:
        with self._lock:
            self._state[name] = {"state": state, "url": url, "error": error}
        try:
            self._publish()
        except OSError:
            logger.exception("Could not publish optional HTTP status")

    def _publish(self) -> None:
        path = self.data_dir / STATUS_FILE
        if not any(self._requested.values()) and not self._handles:
            path.unlink(missing_ok=True)
            self._published = None
            return
        payload = json.dumps({"pid": os.getpid(), "services": self.snapshot()})
        if payload == self._published:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".model-host-http-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(tmp, path)
            self._published = payload
        finally:
            Path(tmp).unlink(missing_ok=True)

    def reconcile(self) -> None:
        settings = read_settings(self.data_dir)
        with self._lock:
            self._state.pop("configuration", None)
        requested = {"legacy": settings.enabled, "direct": settings.direct_enabled}
        for name, enabled in requested.items():
            now = time.monotonic()
            if enabled != self._requested[name]:
                self._requested[name] = enabled
                self._attempts[name].clear()
                self._gave_up[name] = False
                self._next_attempt[name] = 0.0
            handle = self._handles.get(name)
            if now < self._next_attempt[name]:
                continue
            if handle is not None and (not enabled or not handle.is_running):
                try:
                    handle.stop()
                except Exception as exc:
                    # Keep the handle and retry cleanup. Never claim disabled
                    # while the old listener may still admit requests.
                    self._record(name, "stopping", url=handle.url, error=f"{type(exc).__name__}: {exc}")
                    self._next_attempt[name] = now + BACKOFF_S[0]
                    continue
                del self._handles[name]
                handle = None
                if enabled:
                    self._record(name, "failed", error="The optional HTTP listener stopped")
                    self._next_attempt[name] = now + BACKOFF_S[min(len(self._attempts[name]), len(BACKOFF_S) - 1)]
                    continue
            if not enabled:
                self._record(name, "disabled")
                continue
            if handle is not None:
                continue
            attempts = self._attempts[name]
            attempts[:] = [started for started in attempts if now - started < RESTART_WINDOW_S]
            if self._gave_up[name] or len(attempts) >= MAX_RESTARTS:
                self._gave_up[name] = True
                self._record(name, "failed", error="Optional HTTP restart limit reached; toggle its setting to retry")
                continue
            attempts.append(now)
            self._record(name, "starting")
            try:
                if name == "legacy":
                    from .split_mode import ModelHost

                    handle = ModelHost(self.data_dir).start()
                else:
                    from abstractruntime.integrations.abstractcore.server_facade import start_core_server

                    handle = start_core_server()
                self._handles[name] = handle
                self._record(name, "up", url=handle.url)
            except Exception as exc:
                self._record(name, "failed", error=f"{type(exc).__name__}: {exc}")
                self._next_attempt[name] = now + BACKOFF_S[min(len(attempts) - 1, len(BACKOFF_S) - 1)]
                logger.exception("Optional %s Core HTTP listener failed; native RPC remains available", name)
        self._publish()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.reconcile()
                except Exception as exc:
                    self._record("configuration", "failed", error=f"{type(exc).__name__}: {exc}")
                    logger.exception("Could not reconcile optional Core HTTP settings")
                self._stop.wait(0.5)
        finally:
            for name, handle in list(self._handles.items()):
                try:
                    handle.stop()
                except Exception as exc:
                    self._record(name, "stopping", url=getattr(handle, "url", None),
                                 error=f"{type(exc).__name__}: {exc}")
                    logger.exception("Optional %s HTTP listener cleanup failed", name)
                else:
                    del self._handles[name]
                    self._record(name, "disabled")
            if not self._handles:
                (self.data_dir / STATUS_FILE).unlink(missing_ok=True)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=20.0)
