from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
import getpass

import pytest


@pytest.mark.basic
def test_cli_serve_no_runner_sets_env_and_invokes_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli as gateway_cli

    called: dict[str, object] = {}

    uvicorn = types.ModuleType("uvicorn")

    def _run(app: str, **kwargs: object) -> None:
        called["app"] = app
        called.update(kwargs)
        assert os.environ.get("ABSTRACTGATEWAY_RUNNER") == "0"

    uvicorn.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)

    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    monkeypatch.delenv("ABSTRACTGATEWAY_RUNNER", raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    gateway_cli.main(["serve", "--no-runner", "--host", "127.0.0.1", "--port", "9999"])

    assert called["app"] == "abstractgateway.app:app"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9999
    assert called["reload"] is False
    assert called["log_level"] == "error"


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


@pytest.mark.basic
def test_cli_serve_requires_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli as gateway_cli

    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        gateway_cli.main(["serve", "--host", "127.0.0.1", "--port", "9999", "--no-runner"])
    assert "Missing gateway auth token" in str(e.value)


@pytest.mark.basic
def test_cli_serve_refuses_weak_token_on_public_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli as gateway_cli

    # Weak token is allowed on loopback, but should be rejected when binding publicly.
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    with pytest.raises(SystemExit) as e:
        gateway_cli.main(["serve", "--host", "0.0.0.0", "--port", "9999", "--no-runner"])
    assert "Refusing to start" in str(e.value)


@pytest.mark.basic
def test_cli_telegram_auth_uses_runtime_bootstrap(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway import cli as gateway_cli
    from abstractruntime.integrations import abstractcore as runtime_abstractcore

    called: dict[str, object] = {}

    def _bootstrap(*, login_code: str | None = None, two_factor_password: str | None = None, timeout_s: float = 30.0) -> dict[str, object]:
        called["login_code"] = login_code
        called["two_factor_password"] = two_factor_password
        called["timeout_s"] = timeout_s
        return {"success": True, "ready": True}

    monkeypatch.setattr(runtime_abstractcore, "bootstrap_telegram_auth_from_env", _bootstrap)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "12345")
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": "secret")

    gateway_cli.main(["telegram-auth", "--timeout-s", "9"])

    out = capsys.readouterr().out
    assert "TDLib authorization: OK" in out
    assert called == {"login_code": "12345", "two_factor_password": "secret", "timeout_s": 9.0}
