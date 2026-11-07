"""HTTPS on the Tailscale name while the gateway runs (tailscale_https.py).

Operator rulings 2026-10-10: on by default; the https address exists only while the
gateway is up; it is configured at launch AND picked up when Tailscale comes up later.

Every test drives a FAKE `tailscale` CLI (never the machine's), shaped like the real one
on macOS: the `tailscale` on PATH is a shell script that runs the CLI WITHOUT `exec`;
`status --json` answers `status.json`; a foreground `serve <target>` publishes its
configuration and removes it when it ends (what tailscaled does), and `serve status
--json` lists it under `Foreground` (keyed by session), a `--bg` one (`serve.json`
written by a test) at the top level.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict

import pytest

from abstractgateway import tailscale_https as th

pytestmark = pytest.mark.basic

NAME = "scratch-mac.tail1234.ts.net"
RUNNING = {"BackendState": "Running", "Self": {"DNSName": NAME + "."}, "CertDomains": [NAME]}

_FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, os, signal, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    def read(name, default):
        try:
            with open(os.path.join(here, name)) as f:
                return f.read()
        except FileNotFoundError:
            return default
    args = sys.argv[1:]
    if args == ["status", "--json"]:
        text = read("status.json", None)
        if text is None:
            sys.exit(1)
        print(text)
    elif args == ["serve", "status", "--json"]:
        config = json.loads(read("serve.json", "{{}}"))
        fg = read("foreground.json", None)
        if fg is not None:
            config["Foreground"] = {{"947f733cc60aef79": json.loads(fg)}}
        print(json.dumps(config))
    elif len(args) == 2 and args[0] == "serve":
        if read("serve.json", None) is not None or read("foreground.json", None) is not None:
            print("port 443 is already in use", file=sys.stderr)
            sys.exit(1)
        name = json.loads(read("status.json", "{{}}"))["Self"]["DNSName"].rstrip(".")
        served = {{"TCP": {{"443": {{"HTTPS": True}}}}, "Web": {{name + ":443": {{"Handlers": {{"/": {{"Proxy": args[1]}}}}}}}}}}
        with open(os.path.join(here, "foreground.json"), "w") as f:
            json.dump(served, f)
        with open(os.path.join(here, "serve.pid"), "w") as f:
            f.write(str(os.getpid()))
        def end(*_):
            try:
                os.remove(os.path.join(here, "foreground.json"))
            except FileNotFoundError:
                pass
            sys.exit(0)
        signal.signal(signal.SIGTERM, end)
        signal.signal(signal.SIGINT, end)
        while True:
            time.sleep(0.1)
    else:
        sys.exit(2)
    """
)


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    d = tmp_path / "ts"
    d.mkdir()
    cli = d / "tailscale-cli"
    cli.write_text(_FAKE.format(python=sys.executable))
    cli.chmod(0o755)
    # The PATH entry is a wrapper script WITHOUT exec, as /usr/local/bin/tailscale on macOS.
    exe = d / "tailscale"
    exe.write_text(f'#!/bin/sh\n"{cli}" "$@"\n')
    exe.chmod(0o755)
    (d / "status.json").write_text(json.dumps(RUNNING))
    return d


def _served(fake: Path) -> Dict[str, Any]:
    """The running foreground command's configuration ({} when none)."""
    p = fake / "foreground.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _wait(cond, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # A zombie still answers kill(0); it no longer runs.
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return False
    return out == "" or out.startswith("Z")


# ---------------------------------------------------------------- plan (read-only)


def test_plan_says_why_https_cannot_be_served():
    target = "http://127.0.0.1:8080"
    assert th.plan(target, binary=None)["state"] == "unavailable"
    assert "not installed" in th.plan(target, binary=None)["reason"]
    assert th.plan(target, binary=None)["installed"] is False  # the consoles show one optional offer, not a state line
    stopped = th.plan(target, binary="ts", run_json=lambda cmd: {"BackendState": "Stopped"})
    assert stopped["state"] == "unavailable" and "not running" in stopped["reason"]
    no_cert = th.plan(target, binary="ts", run_json=lambda cmd: {**RUNNING, "CertDomains": []} if cmd[1] == "status" else {})
    assert no_cert["state"] == "unavailable" and "HTTPS Certificates" in no_cert["reason"]
    free = th.plan(target, binary="ts", run_json=lambda cmd: RUNNING if cmd[1] == "status" else {})
    assert free == {"state": "starting", "url": f"https://{NAME}", "target": target, "reason": ""}


def test_an_existing_serve_on_443_is_never_replaced():
    target = "http://127.0.0.1:8080"

    def config(proxy):
        return {"TCP": {"443": {"HTTPS": True}}, "Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": proxy}}}}}

    other = th.plan(target, binary="ts", run_json=lambda cmd: RUNNING if cmd[1] == "status" else config("http://127.0.0.1:3000"))
    assert other["state"] == "taken" and "http://127.0.0.1:3000" in other["reason"]
    # The operator's own `tailscale serve --bg` to this gateway: left alone, with how to hand it over.
    own = th.plan(target, binary="ts", run_json=lambda cmd: RUNNING if cmd[1] == "status" else config(target))
    assert own["state"] == "taken" and "tailscale serve reset" in own["reason"]
    # The person's own `tailscale funnel --bg` to this gateway (public): left alone, and
    # never the advice to reset it, which would end the public address.
    funnel = {**config(target), "AllowFunnel": {f"{NAME}:443": True}}
    public = th.plan(target, binary="ts", run_json=lambda cmd: RUNNING if cmd[1] == "status" else funnel)
    assert public["state"] == "taken" and "Tailscale Funnel" in public["reason"]
    assert "reset" not in public["reason"]
    # A running foreground command's configuration counts too (listed under Foreground).
    fg = {"Foreground": {"947f733cc60aef79": config("http://127.0.0.1:3000")}}
    assert th.plan(target, binary="ts", run_json=lambda cmd: RUNNING if cmd[1] == "status" else fg)["state"] == "taken"


def test_the_proxy_target_is_where_the_gateway_listens():
    assert th.proxy_target("0.0.0.0", 8080) == "http://127.0.0.1:8080"
    assert th.proxy_target("127.0.0.1", 18080) == "http://127.0.0.1:18080"
    assert th.proxy_target("::", 8080) == "http://127.0.0.1:8080"
    assert th.proxy_target("192.168.1.20", 8080) == "http://192.168.1.20:8080"


# ---------------------------------------------------------------- the command's lifetime


def test_start_serves_the_name_and_stop_removes_it(fake: Path, tmp_path: Path):
    ts = th.TailscaleHttps()
    st = ts.start(bind_host="0.0.0.0", port=8080, log_dir=tmp_path / "logs", binary=str(fake / "tailscale"))
    assert st == {"state": "active", "url": f"https://{NAME}", "target": "http://127.0.0.1:8080", "reason": ""}
    assert _served(fake)["Web"][f"{NAME}:443"]["Handlers"]["/"]["Proxy"] == "http://127.0.0.1:8080"
    cli_pid = int((fake / "serve.pid").read_text())
    ts.stop()
    assert _wait(lambda: not (fake / "foreground.json").exists()), "the address ends with the command"
    assert _wait(lambda: _pid_gone(cli_pid)), "the CLI itself ends, not only the wrapper script"
    assert ts.status()["state"] == "off"


def test_an_existing_configuration_is_left_untouched(fake: Path, tmp_path: Path):
    mine = {"TCP": {"443": {"HTTPS": True}}, "Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
    (fake / "serve.json").write_text(json.dumps(mine))
    ts = th.TailscaleHttps()
    st = ts.start(bind_host="0.0.0.0", port=8080, log_dir=tmp_path / "logs", binary=str(fake / "tailscale"))
    assert st["state"] == "taken"
    assert json.loads((fake / "serve.json").read_text()) == mine
    ts.stop()
    assert json.loads((fake / "serve.json").read_text()) == mine, "stop never removes a configuration the gateway did not make"


def test_tailscale_coming_up_later_is_picked_up_without_a_restart(fake: Path, tmp_path: Path):
    (fake / "status.json").write_text(json.dumps({"BackendState": "Stopped"}))
    ts = th.TailscaleHttps()
    stop = threading.Event()
    loop = threading.Thread(
        target=ts.keep_serving,
        kwargs=dict(bind_host="127.0.0.1", port=18080, log_dir=tmp_path / "logs", stop_event=stop,
                    recheck_s=0.2, binary=str(fake / "tailscale")),
        daemon=True,
    )
    loop.start()
    try:
        assert _wait(lambda: ts.status()["state"] == "unavailable")
        assert not (fake / "foreground.json").exists()
        (fake / "status.json").write_text(json.dumps(RUNNING))  # Tailscale comes up
        assert _wait(lambda: ts.status()["state"] == "active"), ts.status()
        assert _served(fake)["Web"][f"{NAME}:443"]["Handlers"]["/"]["Proxy"] == "http://127.0.0.1:18080"
        # The command ends on its own: the loop starts it again.
        pid = int((fake / "serve.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        assert _wait(lambda: (fake / "serve.pid").exists() and int((fake / "serve.pid").read_text()) != pid
                     and ts.status()["state"] == "active"), ts.status()
    finally:
        stop.set()
        loop.join(timeout=5)
        ts.stop()
    assert _wait(lambda: not (fake / "foreground.json").exists())


_GATEWAY = textwrap.dedent(
    """\
    import sys, time
    from pathlib import Path
    from abstractgateway import tailscale_https as th
    st = th.get_tailscale_https().start(bind_host="0.0.0.0", port=8080, log_dir=Path(sys.argv[2]), binary=sys.argv[1])
    print(st["state"], flush=True)
    time.sleep(120)
    """
)


def test_the_address_ends_when_the_gateway_is_killed(fake: Path, tmp_path: Path):
    """A crash or the event-loop watchdog's os._exit runs no cleanup: the watchdog
    process notices its parent is gone and ends the command."""
    gw = subprocess.Popen([sys.executable, "-c", _GATEWAY, str(fake / "tailscale"), str(tmp_path / "logs")],
                          stdout=subprocess.PIPE, text=True)
    try:
        assert gw.stdout.readline().strip() == "active"
        serve_pid = int((fake / "serve.pid").read_text())
        gw.send_signal(signal.SIGKILL)
        gw.wait(timeout=5)
        assert _wait(lambda: not (fake / "foreground.json").exists(), timeout=10), "the https address outlived the gateway"
        assert _wait(lambda: _pid_gone(serve_pid), timeout=5)
    finally:
        if gw.poll() is None:
            gw.kill()


# ---------------------------------------------------------------- `serve`


def _fake_uvicorn(monkeypatch: pytest.MonkeyPatch, seen: Dict[str, Any], expect: str) -> None:
    uvicorn = types.ModuleType("uvicorn")

    class _Config:
        def __init__(self, app: str, **kwargs: Any) -> None:
            self.timeout_graceful_shutdown = kwargs.get("timeout_graceful_shutdown")

    class _Server:
        def __init__(self, config: Any) -> None:
            self.config, self.should_exit, self.started = config, False, False

        def run(self) -> None:
            self.started = True
            # One status per process: an earlier serve in this test process may have left another state.
            _wait(lambda: th.get_tailscale_https().status()["state"] == expect, timeout=10)
            seen["while_serving"] = th.get_tailscale_https().status()

    uvicorn.Config, uvicorn.Server = _Config, _Server  # type: ignore[attr-defined]
    uvicorn.run = lambda *a, **k: None  # type: ignore[attr-defined]
    main_mod = types.ModuleType("uvicorn.main")
    main_mod.STARTUP_FAILURE = 3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "uvicorn.main", main_mod)


def test_serve_serves_https_while_it_runs_and_not_after(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import cli, host_control

    host_control._reset_for_tests()
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "operator-token-for-tests")
    monkeypatch.setattr(th, "_tailscale_binary", lambda: str(fake / "tailscale"))
    seen: Dict[str, Any] = {}
    _fake_uvicorn(monkeypatch, seen, expect="active")
    cli.main(["serve", "--no-tray", "--no-runner", "--port", "18897", "--data-dir", str(tmp_path / "data")])
    assert seen["while_serving"]["state"] == "active", seen
    assert seen["while_serving"]["url"] == f"https://{NAME}"
    assert _wait(lambda: not (fake / "foreground.json").exists()), "the address ends with serve"
    assert th.get_tailscale_https().status()["state"] == "off"


def test_serve_tailscale_https_off(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import cli, host_control

    host_control._reset_for_tests()
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "operator-token-for-tests")
    monkeypatch.setattr(th, "_tailscale_binary", lambda: str(fake / "tailscale"))
    seen: Dict[str, Any] = {}
    _fake_uvicorn(monkeypatch, seen, expect="off")
    cli.main(["serve", "--no-tray", "--no-runner", "--tailscale-https", "off", "--port", "18898", "--data-dir", str(tmp_path / "data")])
    assert seen["while_serving"]["state"] == "off"
    assert "--tailscale-https off" in seen["while_serving"]["reason"]
    assert not (fake / "foreground.json").exists()
