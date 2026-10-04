"""Round 12, R12.1 (DESIGN.md): the gateway side of the command sandbox.

- HOST policy, set ONCE per process at boot (``command_sandbox.configure_at_boot``, called by
  ``service.start_gateway_runner``): the base environment of every command = the gateway's SCRUBBED
  environment (the very function used for the apps, ``apps_manager._scrubbed_child_env``) and
  ``unsandboxed_commands_allowed`` from the NEW ``serve --unsandboxed-commands`` flag (default off,
  also on the split ``runner``; never an environment variable), audited as
  ``command_sandbox_configured``.
- State for the console and the TUI: ``GET /workspace/policy`` → ``command_sandbox`` {state, kind,
  line, sentence, …}; the console shows ``line`` under the Accounts head with ``sentence`` as the
  kit tooltip.
- Tools inventory: ``GET /discovery/tools`` marks every process-spawning tool (the runtime's own
  ``SANDBOXED_TOOL_NAMES``) ``sandboxed: true|false`` + ``sandbox: <state sentence>``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "r12w2-command-sandbox-admin"


@pytest.fixture(autouse=True)
def _fresh_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from abstractgateway import command_sandbox

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    command_sandbox._reset_for_tests()
    yield
    command_sandbox._reset_for_tests()


def _audit(tmp_path: Path) -> list:
    p = tmp_path / "runtime" / "audit_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_the_flag_exists_on_serve_and_runner(capsys: pytest.CaptureFixture) -> None:
    from abstractgateway.cli import main

    for cmd in ("serve", "runner"):
        with pytest.raises(SystemExit):
            main([cmd, "--help"])
        out = capsys.readouterr().out
        assert "--unsandboxed-commands" in out, (cmd, out)
    assert "There is no environment variable" in " ".join(out.split()) or cmd == "runner"


def test_serve_records_the_flag_before_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` hands the parsed flag to command_sandbox (default off) before anything boots."""
    from abstractgateway import cli, command_sandbox

    seen = []
    monkeypatch.setattr(command_sandbox, "set_unsandboxed_commands", lambda allowed, *, source: seen.append((allowed, source)))
    monkeypatch.setattr(cli, "_migrate_legacy_core_config_store", lambda: (_ for _ in ()).throw(SystemExit(0)))
    for argv, want in ((["serve"], []), (["serve", "--unsandboxed-commands"], [])):
        seen.clear()
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert seen == want  # the store migration runs first and stopped us: nothing recorded yet
    # Past the store migration: the flag is recorded with its source.
    monkeypatch.setattr(cli, "_migrate_legacy_core_config_store", lambda: None)
    import abstractgateway.first_run as fr

    monkeypatch.setattr(fr, "apply_loopback_auth_default", lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(0)))
    for argv, want in ((["serve", "--port", "1"], [(False, "serve")]), (["serve", "--port", "1", "--unsandboxed-commands"], [(True, "serve")])):
        seen.clear()
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert seen == want, (argv, seen)


def test_never_an_environment_variable() -> None:
    """The module reads no environment variable to decide the flag (only the env it scrubs)."""
    src = (Path(__import__("abstractgateway.command_sandbox").command_sandbox.__file__)).read_text(encoding="utf-8")
    assert "getenv" not in src and "environ.get" not in src and "environ[" not in src


def test_boot_configures_the_core_with_the_apps_scrub_and_audits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractruntime.integrations.abstractcore.command_sandbox_host import host_policy

    from abstractgateway import command_sandbox
    from abstractgateway.apps_manager import _scrubbed_child_env

    monkeypatch.setenv("R12W2_PROBE_TOKEN", "t0ken")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("SOME_SECRET", "s")
    monkeypatch.setenv("R12W2_PLAIN", "visible")
    rec = command_sandbox.configure_at_boot()
    assert rec["first"] is True and rec["unsandboxed_commands_allowed"] is False
    pol = host_policy()
    assert pol["configured"] is True and pol["unsandboxed_commands_allowed"] is False
    keys = set(pol["env_keys"])
    # The SAME scrub as the apps: byte-identical key set, no secret, no gateway variable.
    assert keys == set(_scrubbed_child_env(dict(os.environ)))
    for gone in ("R12W2_PROBE_TOKEN", "OPENAI_API_KEY", "SOME_SECRET", "ABSTRACTGATEWAY_AUTH_TOKEN", "ABSTRACTGATEWAY_DATA_DIR"):
        assert gone not in keys, gone
    assert "R12W2_PLAIN" in keys
    # Audited once; a second boot in the same process changes nothing.
    again = command_sandbox.configure_at_boot()
    assert again["first"] is False
    lines = [e for e in _audit(tmp_path) if e.get("event") == "command_sandbox_configured"]
    assert len(lines) == 1
    assert lines[0]["unsandboxed_commands_allowed"] is False and lines[0]["kind"] == rec["kind"] and lines[0]["line"] == rec["line"]


def test_the_flag_reaches_the_core_and_the_audit(tmp_path: Path) -> None:
    from abstractruntime.integrations.abstractcore.command_sandbox_host import host_policy

    from abstractgateway import command_sandbox

    command_sandbox.set_unsandboxed_commands(True, source="serve")
    rec = command_sandbox.configure_at_boot()
    assert host_policy()["unsandboxed_commands_allowed"] is True
    line = [e for e in _audit(tmp_path) if e.get("event") == "command_sandbox_configured"][0]
    assert line["unsandboxed_commands_allowed"] is True and line["source"] == "serve" and line["actor"] == "system:serve"
    assert rec["unsandboxed_commands_allowed"] is True


@pytest.mark.parametrize(
    "kinds,flag,state,line",
    [
        ({"allowed_only": "macos-sandbox-exec", "any_except_denied": "macos-sandbox-exec"}, False, "sandboxed", "Commands sandboxed: macOS sandbox-exec"),
        ({"allowed_only": "linux-bwrap", "any_except_denied": "linux-bwrap"}, True, "sandboxed", "Commands sandboxed: Linux bubblewrap"),
        ({"allowed_only": "none", "any_except_denied": "none"}, False, "refused", "Commands refused: no sandbox on this host"),
        ({"allowed_only": "none", "any_except_denied": "none"}, True, "unsandboxed", "Unsandboxed commands allowed (flag)"),
        ({"allowed_only": "linux-landlock", "any_except_denied": "none"}, False, "partial",
         "Commands sandboxed: Linux Landlock (Deny everything, allow listed workspaces); otherwise commands refused"),
    ],
)
def test_the_state_line(monkeypatch: pytest.MonkeyPatch, kinds: dict, flag: bool, state: str, line: str) -> None:
    import abstractruntime.integrations.abstractcore.command_sandbox_host as sb

    from abstractgateway import command_sandbox

    monkeypatch.setattr(sb, "host_sandbox_kind", lambda posture="allowed_only": kinds[posture])
    command_sandbox.set_unsandboxed_commands(flag, source="serve")
    command_sandbox.configure_at_boot()
    st = command_sandbox.state()
    assert st["state"] == state and st["line"] == line and st["sentence"]
    assert st["unsandboxed_commands_allowed"] is flag
    fields = command_sandbox.tool_sandbox_fields("execute_command")
    assert fields["sandboxed"] is (state in ("sandboxed", "partial"))
    if state == "sandboxed":
        assert fields["sandbox"] == "Sandboxed to this run's workspaces"
    assert command_sandbox.tool_sandbox_fields("read_file") is None


def test_the_process_tools_are_the_runtimes_own_list() -> None:
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import SANDBOXED_TOOL_NAMES

    from abstractgateway import command_sandbox

    assert command_sandbox.process_tools() == frozenset(SANDBOXED_TOOL_NAMES)
    assert "execute_command" in command_sandbox.process_tools()


def test_routes_carry_the_state_and_the_tool_marks(tmp_path: Path) -> None:
    from abstractgateway import command_sandbox
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as c:
        command_sandbox.configure_at_boot()  # the boot thread may not have reached the runner start yet
        pol = c.get("/api/gateway/workspace/policy").json()
        st = command_sandbox.state()
        assert pol["command_sandbox"]["line"] == st["line"] and pol["command_sandbox"]["state"] == st["state"]
        tools = c.get("/api/gateway/discovery/tools").json()
        assert tools["command_sandbox"]["line"] == st["line"]
        by_name = {row["name"]: row for row in tools["items"] if isinstance(row, dict) and row.get("name")}
        ex = by_name["execute_command"]
        assert isinstance(ex["sandboxed"], bool) and ex["sandbox"]
        if st["state"] == "sandboxed":
            assert ex["sandboxed"] is True and ex["sandbox"] == "Sandboxed to this run's workspaces"
        assert "sandboxed" not in by_name["read_file"]


def test_the_console_shows_the_line_with_the_tooltip() -> None:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert 'id="accounts-command-sandbox"' in html and 'data-af-tip=""' in html
    assert "async function loadCommandSandboxState()" in html
    body = html.split("async function loadCommandSandboxState()", 1)[1].split("async function loadAccounts()", 1)[0]
    assert "cs.line" in body and 'setAttribute("data-af-tip", cs.sentence' in body
    assert "loadCommandSandboxState();" in html.split("async function loadAccounts()", 1)[1].split("}", 3)[0] + html.split("async function loadAccounts()", 1)[1][:400]


# ------------------------------------------------------------------ real boot


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.parametrize("flag", [False, True])
def test_a_real_serve_audits_the_flag_at_boot(tmp_path: Path, flag: bool) -> None:
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    port = _free_port()
    env = {
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]
            + [str(Path(x).resolve()) for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x]
        ),
        "LANG": "en_US.UTF-8",
        "HF_HOME": str(home / "hf"),
        "HF_HUB_OFFLINE": "1",
        "ABSTRACTGATEWAY_DATA_DIR": str(data),
        "ABSTRACTGATEWAY_RUNNER": "0",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/",
        "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "NO_COLOR": "1",
        "R12W2_CANARY_TOKEN": "must-not-reach-commands",
    }
    argv = [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray"]
    if flag:
        argv.append("--unsandboxed-commands")
    log_path = tmp_path / "serve.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(argv, cwd=str(tmp_path), env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            entry = None
            deadline = time.time() + 90
            while time.time() < deadline and entry is None:
                time.sleep(0.5)
                audit = data / "audit_log.jsonl"
                if audit.exists():
                    for ln in audit.read_text().splitlines():
                        if '"command_sandbox_configured"' in ln:
                            entry = json.loads(ln)
                assert proc.poll() is None, log_path.read_text()[-3000:]
            assert entry is not None, log_path.read_text()[-3000:]
            assert entry["unsandboxed_commands_allowed"] is flag and entry["source"] == "serve"
            token = (data / "auth" / "bootstrap-admin-token").read_text().strip()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/gateway/workspace/policy", headers={"Authorization": f"Bearer {token}"})
            cs = json.loads(urllib.request.urlopen(req, timeout=20).read())["command_sandbox"]
            assert cs["unsandboxed_commands_allowed"] is flag and cs["configured"] is True
            assert cs["line"] in log_path.read_text()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
