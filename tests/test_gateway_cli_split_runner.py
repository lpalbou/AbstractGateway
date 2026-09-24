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
    # A NON-loopback bind without any auth configuration still refuses
    # (first-run 2026-09-23 made only the loopback case automatic).
    from abstractgateway import cli as gateway_cli

    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        gateway_cli.main(["serve", "--host", "0.0.0.0", "--port", "9999", "--no-runner"])
    assert "Refusing to start: no sign-in would protect this gateway" in str(e.value)
    assert "abstractgateway network set lan" in str(e.value)
    assert "--host 127.0.0.1" in str(e.value)
    # Operator rule (mission Z): no "export ABSTRACTGATEWAY_…" instruction.
    assert "export " not in str(e.value) and "ABSTRACTGATEWAY_" not in str(e.value)


@pytest.mark.basic
def test_cli_serve_user_auth_bootstraps_admin_without_legacy_token(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from abstractgateway import cli as gateway_cli
    from abstractgateway.users import GatewayUserRegistry

    called: dict[str, object] = {}
    uvicorn = types.ModuleType("uvicorn")

    def _run(app: str, **kwargs: object) -> None:
        called["app"] = app
        called.update(kwargs)

    uvicorn.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    gateway_cli.main(["serve", "--host", "127.0.0.1", "--port", "9999", "--no-runner"])

    token_file = tmp_path / "runtime" / "auth" / "bootstrap-admin-token"
    token = token_file.read_text(encoding="utf-8").strip()
    principal = GatewayUserRegistry().authenticate(token)
    assert principal is not None
    assert principal.user_id == "admin"
    assert principal.tenant_id == "default"
    assert principal.runtime_id == "default"
    assert called["app"] == "abstractgateway.app:app"
    assert called["host"] == "127.0.0.1"
    err = capsys.readouterr().err
    assert "Gateway user auth: enabled." in err
    assert "Gateway admin user: default/admin" in err
    assert "Gateway admin token file:" in err
    # A loopback first launch shows both the admin token and the one-time
    # console link (`--no-print-token` hides the token, see below).
    assert f"Gateway admin token: {token}" in err
    assert "First run: open http://127.0.0.1:9999/console#claim=agclaim_" in err


def _serve_stderr(tmp_path, monkeypatch: pytest.MonkeyPatch, capsys, argv: list[str]) -> tuple[str, str]:
    """Run `serve` with a stub uvicorn; return (stderr, bootstrap token)."""
    from abstractgateway import cli as gateway_cli

    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda app, **kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN", raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    gateway_cli.main(["serve", *argv, "--no-runner"])

    token = (tmp_path / "runtime" / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    return capsys.readouterr().err, token


@pytest.mark.basic
def test_cli_serve_prints_admin_token_by_default_on_loopback(tmp_path, monkeypatch, capsys) -> None:
    err, token = _serve_stderr(tmp_path, monkeypatch, capsys, ["--host", "127.0.0.1", "--port", "9999"])
    assert f"Gateway admin token: {token}" in err
    # The one-time console link is printed as well; the two are complementary.
    assert "First run: open http://127.0.0.1:9999/console#claim=agclaim_" in err


@pytest.mark.basic
def test_cli_serve_no_print_token_hides_it(tmp_path, monkeypatch, capsys) -> None:
    err, token = _serve_stderr(tmp_path, monkeypatch, capsys, ["--host", "127.0.0.1", "--port", "9999", "--no-print-token"])
    assert token not in err
    assert "not printed; `serve --print-token` prints it" in err


@pytest.mark.basic
def test_cli_serve_public_bind_hides_token_unless_print_token(tmp_path, monkeypatch, capsys) -> None:
    err, token = _serve_stderr(tmp_path, monkeypatch, capsys, ["--host", "0.0.0.0", "--port", "9999"])
    assert token not in err
    err, token = _serve_stderr(tmp_path, monkeypatch, capsys, ["--host", "0.0.0.0", "--port", "9999", "--print-token"])
    assert f"Gateway admin token: {token}" in err


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
