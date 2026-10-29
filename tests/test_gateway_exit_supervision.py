"""OVH 2026-10-28/29: the systemd unit's gateway failed to bind (a port held by an
unsupervised gateway), shut down, and its exit belt exited 0 — `Restart=on-failure`
never retried; and the admin token was printed into the journal. Each case red
without its fix."""

from __future__ import annotations

import sys
import threading

import pytest

from abstractgateway import cli, work_pools

# The real belt (tests/conftest.py replaces it for every test so a serve never kills pytest).
REAL_BELT = cli._exit_now_if_nondaemon_stragglers


def test_a_failed_start_keeps_its_exit_code_through_the_belt(monkeypatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(cli.os, "_exit", lambda code: exits.append(code))
    straggler = threading.Event()
    t = threading.Thread(target=straggler.wait, name="straggler", daemon=False)
    t.start()
    try:
        try:
            raise SystemExit(3)  # uvicorn's STARTUP_FAILURE (port in use)
        except SystemExit:
            REAL_BELT(cli._pending_exit_code())
    finally:
        straggler.set()
        t.join()
    assert exits == [3], "a failed start must not exit 0 (systemd would not restart it)"


@pytest.mark.parametrize("exc,code", [(None, 0), (SystemExit(3), 3), (SystemExit(None), 0), (RuntimeError("x"), 1), (KeyboardInterrupt(), 0)])
def test_the_pending_exit_code(exc, code) -> None:
    if exc is None:
        assert cli._pending_exit_code() == 0
        return
    try:
        raise exc
    except BaseException:
        assert cli._pending_exit_code() == code


def test_idle_pool_workers_are_stopped_before_the_belt() -> None:
    import asyncio

    async def use() -> int:
        return await work_pools.off_loop("light", lambda: 41 + 1)

    assert asyncio.run(use()) == 42
    assert any(t.name.startswith("gw-light") for t in threading.enumerate())
    work_pools.shutdown_all(timeout_s=2.0)
    assert not any(t.name.startswith("gw-light") and t.is_alive() for t in threading.enumerate())


def test_the_admin_token_is_never_printed_into_a_service_log(tmp_path, monkeypatch, capsys) -> None:
    """Under systemd (JOURNAL_STREAM set) a loopback gateway names the token file, never the token."""
    for var in ("JOURNAL_STREAM", "INVOCATION_ID", "XPC_SERVICE_NAME"):
        monkeypatch.delenv(var, raising=False)
    assert cli._stderr_is_a_service_log() is False
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    assert cli._stderr_is_a_service_log() is True
    monkeypatch.delenv("JOURNAL_STREAM")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    assert cli._stderr_is_a_service_log() is False, "a Terminal session"
    monkeypatch.setenv("XPC_SERVICE_NAME", "dev.abstractframework.gateway")
    assert cli._stderr_is_a_service_log() is True


@pytest.mark.parametrize("service,shown", [(False, True), (True, False)])
def test_a_loopback_gateway_under_systemd_names_the_file_not_the_token(tmp_path, monkeypatch, capsys, service, shown) -> None:
    for var in ("JOURNAL_STREAM", "INVOCATION_ID", "XPC_SERVICE_NAME", "ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    if service:
        monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    cli._maybe_bootstrap_user_auth_admin(host="127.0.0.1", port=9999)
    token = (tmp_path / "runtime" / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    err = capsys.readouterr().err
    assert (token in err) is shown
    assert "Gateway admin token" in err
