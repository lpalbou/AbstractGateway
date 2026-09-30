"""A stop that lands while the gateway is still starting.

The lifespan starts the heavy boot on a background thread ("gateway-boot") and
the enabled browser apps on another ("apps-autostart"), then yields. Its
shutdown (Ctrl-C, SIGTERM through uvicorn, a TestClient that leaves at once)
used to return without waiting for either: the boot then built the service,
cached it after the stop's clear and started the runner and the email worker
of a gateway that had shut down; autostart started an app after the apps were
stopped (an orphan child). These tests hold the boot / the app start with an
event, stop during it and check that nothing is left running.

Deterministic: the order of events is forced with events, never with sleeps
that the assertions depend on.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, List

import pytest

from abstractgateway import service as service_mod


def _wait_until(pred: Callable[[], bool], timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class _Worker:
    def __init__(self, name: str, log: List[str]) -> None:
        self.name = name
        self.log = log
        self.alive = False

    def start(self) -> None:
        self.alive = True
        self.log.append(f"{self.name} start")

    def stop(self) -> None:
        self.alive = False
        self.log.append(f"{self.name} stop")


class _FakeService:
    def __init__(self, base_dir: Path, log: List[str]) -> None:
        self.runner = _Worker("runner", log)
        self.email_worker = _Worker("email", log)
        self.telegram_bridge = None
        self.agora_bridge = None
        self.entity_chat_host = None
        self.entity_registry = None
        self.stores = type("S", (), {"base_dir": str(base_dir)})()


@pytest.fixture()
def slow_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The service build blocks until the test releases it."""
    entered = threading.Event()
    release = threading.Event()
    log: List[str] = []
    built: List[_FakeService] = []

    def _build(*_a: Any, **_k: Any) -> _FakeService:
        entered.set()
        assert release.wait(30), "the test never released the build"
        svc = _FakeService(tmp_path / "data", log)
        built.append(svc)
        log.append("built")
        return svc

    monkeypatch.setattr(service_mod, "create_default_gateway_service", _build)
    monkeypatch.setattr(service_mod, "gateway_multi_user_enabled", lambda: False)
    return entered, release, log, built


def _boot_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "gateway-boot" and t.is_alive()]


def _cancel_requested() -> bool:
    ev = getattr(service_mod, "_boot_cancel", None)
    return bool(ev is not None and ev.is_set())


def test_stop_during_boot_waits_and_leaves_nothing_running(slow_boot) -> None:
    entered, release, log, built = slow_boot
    service_mod.begin_gateway_boot()
    assert entered.wait(10), "the boot never reached the service build"

    stop_returned = threading.Event()
    errors: List[BaseException] = []

    def _stop() -> None:
        try:
            service_mod.stop_gateway_runner()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop_returned.set()

    stopper = threading.Thread(target=_stop)
    stopper.start()
    # Let the stop reach its wait (it asks the boot to stop first). The old
    # code instead blocked on the service lock the build holds and, once the
    # build ended, stopped a service whose runner the boot started right
    # after. Nothing asserted below depends on how long this takes.
    _wait_until(lambda: stop_returned.is_set() or _cancel_requested(), 1.0)
    release.set()
    stopper.join(30)
    for t in _boot_threads():
        t.join(30)

    assert not errors, errors
    assert not stopper.is_alive() and not _boot_threads()
    assert built, "the build ran"
    svc = built[0]
    assert not svc.runner.alive and not svc.email_worker.alive, log
    assert "runner start" not in log and "email start" not in log, log  # cancelled before starting anything
    assert service_mod._service is None  # nothing cached after the stop
    assert service_mod.gateway_boot_state()["state"] != "ready"


def test_stop_after_boot_finished_is_unchanged(slow_boot) -> None:
    entered, release, log, built = slow_boot
    release.set()
    service_mod.begin_gateway_boot()
    assert service_mod.wait_for_gateway_boot(30) == "ready"
    svc = built[0]
    assert svc.runner.alive and svc.email_worker.alive
    service_mod.stop_gateway_runner()
    assert not svc.runner.alive and not svc.email_worker.alive, log
    assert service_mod._service is None


def test_boot_that_outlives_the_stop_wait_stops_itself(slow_boot, monkeypatch: pytest.MonkeyPatch) -> None:
    """A build longer than the stop's wait: the stop gives up waiting, and the
    boot, when its build ends, starts nothing and stops what it built."""
    entered, release, log, built = slow_boot
    monkeypatch.setattr(service_mod, "BOOT_STOP_WAIT_S", 0.0)
    service_mod.begin_gateway_boot()
    assert entered.wait(10)
    stopper = threading.Thread(target=service_mod.stop_gateway_runner)  # gives up waiting at once
    stopper.start()
    assert _wait_until(_cancel_requested)
    release.set()
    stopper.join(30)
    for t in _boot_threads():
        t.join(30)
    assert not stopper.is_alive() and not _boot_threads()
    assert "runner start" not in log and "email start" not in log, log
    assert service_mod._service is None


def test_lifespan_exit_during_boot_leaves_no_boot_thread_or_worker(slow_boot) -> None:
    """The serve path: uvicorn runs the lifespan shutdown on Ctrl-C/SIGTERM,
    which TestClient's exit reproduces."""
    from fastapi.testclient import TestClient

    from abstractgateway.app import app

    entered, release, log, built = slow_boot
    # Release the build only once the shutdown asked the boot to stop (or, the
    # old race, once the lifespan exit has returned).
    exited = threading.Event()
    releaser = threading.Thread(target=lambda: (_wait_until(lambda: exited.is_set() or _cancel_requested(), 1.0), release.set()))
    with TestClient(app):
        assert entered.wait(10)
        releaser.start()
    exited.set()
    releaser.join(30)
    for t in _boot_threads():
        t.join(30)
    assert not _boot_threads()
    assert all(not s.runner.alive and not s.email_worker.alive for s in built), log
    assert service_mod._service is None


def test_apps_shutdown_waits_for_autostart_and_stops_the_app_it_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import apps_manager as am

    m = am.AppsManager(tmp_path / "apps-data", urlopen=lambda *a, **k: None, install_allowed=lambda: True)
    in_launch = threading.Event()
    release = threading.Event()
    launched: List[str] = []

    class _Proc:
        def __init__(self, app_id: str) -> None:
            self.app_id = app_id
            self.running = True
            self.spec = am.spec_for(app_id)

        def stop(self) -> None:
            self.running = False

    def _launch(app_id: str, **_k: Any) -> dict:
        launched.append(app_id)
        in_launch.set()
        assert release.wait(30)
        m._procs[app_id] = _Proc(app_id)
        return {"url": f"http://127.0.0.1:1/{app_id}"}

    monkeypatch.setattr(m, "reap_orphans", lambda: None)
    monkeypatch.setattr(m, "app_state", lambda app_id: {"enabled": True})
    monkeypatch.setattr(m, "launch", _launch)
    monkeypatch.setattr(am, "get_apps_manager", lambda *a, **k: m)
    with am._MANAGERS_LOCK:
        saved = dict(am._MANAGERS)
        am._MANAGERS.clear()
        am._MANAGERS["test"] = m
    try:
        am.start_apps_on_boot()
        assert in_launch.wait(10)
        pending = getattr(am, "_AUTOSTART", None)
        stop_returned = threading.Event()
        stopper = threading.Thread(target=lambda: (am.stop_apps_on_shutdown(), stop_returned.set()))
        stopper.start()
        assert _wait_until(lambda: stop_returned.is_set() or (pending is not None and pending[1].is_set()))
        release.set()
        stopper.join(30)
        for t in threading.enumerate():
            if t.name == "apps-autostart":
                t.join(30)
        assert not stopper.is_alive()
        assert launched == [am.APPS[0].id], launched  # the next enabled app was never started
        assert all(not p.running for p in m._procs.values()), {k: p.running for k, p in m._procs.items()}
    finally:
        with am._MANAGERS_LOCK:
            am._MANAGERS.clear()
            am._MANAGERS.update(saved)
