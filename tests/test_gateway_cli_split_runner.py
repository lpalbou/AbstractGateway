from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest


@pytest.mark.basic
def test_cli_serve_no_runner_sets_env_and_invokes_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli as gateway_cli

    called: dict[str, object] = {}

    uvicorn = types.ModuleType("uvicorn")

    def _run(app: str, *, host: str, port: int, reload: bool) -> None:
        called["app"] = app
        called["host"] = host
        called["port"] = port
        called["reload"] = reload
        assert os.environ.get("ABSTRACTGATEWAY_RUNNER") == "0"

    uvicorn.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)

    monkeypatch.delenv("ABSTRACTGATEWAY_RUNNER", raising=False)
    gateway_cli.main(["serve", "--no-runner", "--host", "127.0.0.1", "--port", "9999"])

    assert called["app"] == "abstractgateway.app:app"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9999
    assert called["reload"] is False


@pytest.mark.basic
def test_cli_runner_forces_runner_env_and_calls_start_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli as gateway_cli
    from abstractgateway import service as gateway_service

    calls: list[str] = []

    def _start() -> None:
        assert os.environ.get("ABSTRACTGATEWAY_RUNNER") == "1"
        calls.append("start")

    def _stop() -> None:
        assert os.environ.get("ABSTRACTGATEWAY_RUNNER") == "1"
        calls.append("stop")

    monkeypatch.setattr(gateway_service, "start_gateway_runner", _start)
    monkeypatch.setattr(gateway_service, "stop_gateway_runner", _stop)

    class _ImmediateEvent:
        def __init__(self) -> None:
            self._set = True

        def is_set(self) -> bool:
            return self._set

        def set(self) -> None:
            self._set = True

        def wait(self, timeout: float | None = None) -> bool:  # noqa: ARG002
            self._set = True
            return True

    monkeypatch.setattr(gateway_cli.threading, "Event", _ImmediateEvent)

    monkeypatch.delenv("ABSTRACTGATEWAY_RUNNER", raising=False)
    gateway_cli.main(["runner"])

    # CLI sets the env var for the process while running, but restores it on exit.
    assert os.environ.get("ABSTRACTGATEWAY_RUNNER") is None
    assert calls == ["start", "stop"]


@pytest.mark.basic
def test_runner_module_does_not_import_fastapi() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; import abstractgateway.runner; print('fastapi' in sys.modules)"],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=15,
    )
    assert proc.stdout.strip() == "False"
