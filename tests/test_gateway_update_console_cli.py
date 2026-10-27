"""`abstractgateway update` and `abstractgateway console` (and their `--update` / `--console`
spellings).

- `update --check`: an installer install behind the newest release prints the packages that
  would change (framework, gateway, Python packages, browser apps, terminal apps) and the root
  changelog sections between the two releases, newest first, and exits 10; up to date exits 0;
  an install the installer did not make is refused with the right instruction, and nothing is
  fetched.
- `update`: runs the sha-checked installer in the foreground as `/bin/sh <data>/update/install-….sh
  --yes --no-open --no-modify-path --data-dir <data>` (no --no-start: the installer restarts the
  service itself), its output on this terminal, and exits with the installer's exit code.
  Without a terminal and without --yes it refuses.
- `console`: finds abstractgateway-console next to the running `abstractgateway`, resolves the
  URL and token like every other verb and execs it with `--gateway-url URL --token TOKEN`;
  the token never appears in this command's output; a missing binary says who builds it.

No network: the release, the changelog and the installer are fakes."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway import cli, console_cli, self_update, update_cli

pytestmark = pytest.mark.basic

COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
SCRIPT = (
    b"#!/bin/sh\n# AbstractFramework bootstrap installer (test double)\n"
    b'AF_CRATE_CONSOLE="abstractgateway-console@0.16.0"\n'
    b'AF_CRATE_CODE_CLI="abstractcode@0.10.0"\n'
    b'echo "installer ran: $*"\nexit 3\n'
)
CHANGELOG = """# Changelog

## [Unreleased]

- not released yet

## [0.11.6] - 2026-10-18

Pins **AbstractGateway 0.15.4**: a cold first start no longer exits on the watchdog.

- Who is affected: fresh installs.

## [0.11.5] - 2026-10-18

Pins **AbstractGateway 0.15.2**: leftover apps are stopped and replaced.

## [0.11.4] - 2026-10-17

Installers keep the browser apps on the release.
"""
TOKEN = "admin-token-for-the-console-0123456789"


@pytest.fixture(autouse=True)
def _reset_update_state():
    self_update._reset_for_tests()
    yield
    self_update._reset_for_tests()


def _release(version: str = "0.11.6", gateway: str = "0.15.4") -> Dict[str, Any]:
    return {
        "ok": True,
        "offline": False,
        "commit": COMMIT,
        "version": version,
        "gateway_version": gateway,
        "python_packages": {"abstractgateway": gateway, "abstractcore": "2.26.3", "abstractruntime": "0.10.3", "abstractassistant": "0.14.0"},
        "npm_apps": {"flow": "0.10.0", "code": "0.11.2", "observer": "0.8.0"},
        "crates": {"abstractgateway-console": "0.16.0", "abstractcode": "0.10.0"},
        "manifest_url": f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.FRAMEWORK_MANIFEST_PATH}",
        "installer_url": f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.INSTALLER_SCRIPT_PATH}",
        "installer_bytes": SCRIPT,
    }


def _installer_info(data_dir: Path, framework: str | None = "0.11.4", *, gateway: str = "0.15.0") -> self_update.InstallInfo:
    return self_update.InstallInfo(
        kind="installer", upgradable=True, reason=None, command=None, display_command="x",
        python=sys.executable, prefix="/home/u/.local/share/uv/tools/abstractgateway", version=gateway,
        data_dir=str(data_dir), framework_version=framework,
    )


def _snapshot(gateway: str = "0.15.0") -> Dict[str, str]:
    return {"abstractgateway": gateway, "abstractcore": "2.26.2", "abstractruntime": "0.10.3", "packaging": "24.0"}


def _http_get_ok(url: str, accept: str, timeout_s: float) -> bytes:
    if url == f"{self_update.FRAMEWORK_RAW}/{COMMIT}/{self_update.ROOT_CHANGELOG_PATH}":
        return CHANGELOG.encode()
    raise AssertionError(f"unexpected fetch {url}")


def _installed_app(data_dir: Path, app_id: str, version: str, package: str) -> None:
    root = data_dir / "apps"
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"schema": 1, "apps": {}}
    state["apps"][app_id] = {"version": version, "enabled": False}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    pkg = root / app_id / version / "node_modules" / Path(*package.split("/"))
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": package, "version": version, "bin": "bin/cli.js"}), encoding="utf-8")


def _ns(**kw: Any) -> Any:
    import argparse

    base = {"check": False, "yes": False, "data_dir": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _run(args: Any, **kw: Any) -> int:
    return update_cli.run_update(args, **kw)


# ------------------------------------------------------------------ update --check


def test_update_check_lists_what_changes_and_the_release_notes_then_exits_10(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import apps_manager as am

    monkeypatch.setattr(am, "_is_script", lambda p: False)  # the fake console script stands in for a native binary
    data = tmp_path / "data"
    _installed_app(data, "flow", "0.9.0", "@abstractframework/flow")
    _installed_app(data, "observer", "0.8.0", "@abstractframework/observer")  # already at the release's version
    # The terminal console, an older build, in the gateway's terminal-apps folder (<data>/apps/bin
    # when the gateway is not a uv tool install, as under pytest).
    legacy_bin = data / "apps" / "bin"
    legacy_bin.mkdir(parents=True, exist_ok=True)
    console = legacy_bin / "abstractgateway-console"
    console.write_text(
        "#!/bin/sh\ncase \"$1\" in --help) echo 'abstractgateway-console — configure an AbstractGateway from the terminal';; --version) echo 'abstractgateway-console 0.15.0';; esac\n",
        encoding="utf-8",
    )
    console.chmod(0o755)

    code = _run(
        _ns(check=True, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=_release,
        http_get=_http_get_ok,
        snapshot=lambda python: _snapshot(),
    )
    out = capsys.readouterr().out
    assert code == update_cli.EXIT_UPDATE_AVAILABLE
    assert "Installed: AbstractFramework 0.11.4" in out
    assert "Available: AbstractFramework 0.11.6 (gateway 0.15.4)" in out
    # The table: every package whose installed version differs, framework and gateway first.
    rows = [line.strip() for line in out.splitlines() if "->" in line]
    assert rows[0].startswith("AbstractFramework") and "0.11.4 -> 0.11.6" in rows[0]
    assert rows[1].startswith("abstractgateway ") and "0.15.0 -> 0.15.4" in rows[1] and "gateway" in rows[1]
    assert any(r.startswith("abstractcore") and "2.26.2 -> 2.26.3" in r for r in rows)
    assert any(r.startswith("flow") and "0.9.0 -> 0.10.0" in r and "browser app" in r for r in rows)
    assert any(r.startswith("abstractgateway-console") and "0.15.0 -> 0.16.0" in r and "terminal app" in r for r in rows)
    # Not listed: a package at the release's version, one not installed (the Assistant, Code's apps).
    assert not any(r.startswith("abstractruntime") or r.startswith("observer") or r.startswith("abstractassistant") or r.startswith("code") or r.startswith("abstractcode") for r in rows)
    # The release notes: the sections between the installed release and the newest, verbatim, newest first.
    assert "Release notes:" in out
    assert out.index("## [0.11.6] - 2026-10-18") < out.index("## [0.11.5] - 2026-10-18")
    assert "a cold first start no longer exits on the watchdog" in out
    assert "## [0.11.4]" not in out and "[Unreleased]" not in out and "not released yet" not in out
    assert "--check: nothing was changed" in out
    assert not (data / "update").exists()  # --check writes no installer


def test_update_check_up_to_date_exits_0_and_prints_no_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    code = _run(
        _ns(check=True, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.6", gateway="0.15.4"),
        fetch_release=_release,
        http_get=lambda *a: (_ for _ in ()).throw(AssertionError("no changelog fetch when up to date")),
        snapshot=lambda python: (_ for _ in ()).throw(AssertionError("no snapshot when up to date")),
    )
    out = capsys.readouterr().out
    assert code == update_cli.EXIT_OK
    assert "Up to date: AbstractFramework 0.11.6" in out
    assert "->" not in out and "What changes" not in out


def test_update_check_without_release_notes_says_so_and_goes_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import urllib.error

    data = tmp_path / "data"

    def offline(url: str, accept: str, timeout_s: float) -> bytes:
        raise urllib.error.URLError("no route")

    code = _run(
        _ns(check=True, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=_release,
        http_get=offline,
        snapshot=lambda python: _snapshot(),
    )
    captured = capsys.readouterr()
    assert code == update_cli.EXIT_UPDATE_AVAILABLE
    assert "AbstractFramework  0.11.4 -> 0.11.6" in captured.out
    assert "could not fetch them" in captured.err and "CHANGELOG.md" in captured.err


def test_update_refuses_an_install_the_installer_did_not_make(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    checkout = tmp_path / "src" / "abstractgateway"
    monkeypatch.setattr(self_update, "_direct_url_info", lambda: {"url": checkout.as_uri(), "dir_info": {"editable": True}})
    editable = self_update.InstallInfo(kind="editable", upgradable=False, reason="installed from a source checkout", command=None, display_command=None, python=sys.executable, prefix=str(tmp_path / "venv"))

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a refused update fetches nothing")

    code = _run(_ns(check=True, data_dir=str(data)), detect=lambda **kw: editable, fetch_release=never, http_get=never)
    err = capsys.readouterr().err
    assert code == update_cli.EXIT_REFUSED
    assert err.strip() == f"This gateway runs from a checkout at {checkout}: pull and restart it."

    pip = self_update.InstallInfo(kind="pip", upgradable=True, reason=None, command=["py", "-m", "pip"], display_command="py -m pip install --upgrade abstractgateway", python=sys.executable, prefix=str(tmp_path / "venv"))
    code = _run(_ns(check=True, data_dir=str(data)), detect=lambda **kw: pip, fetch_release=never, http_get=never)
    err = capsys.readouterr().err
    assert code == update_cli.EXIT_REFUSED
    assert "not installed by the AbstractFramework installer (pip)" in err
    assert "py -m pip install --upgrade abstractgateway" in err and self_update.INSTALLER_ONE_LINER in err


# ------------------------------------------------------------------ update (the run)


def test_update_runs_the_checked_installer_in_the_foreground_and_returns_its_exit_code(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    code = _run(
        _ns(yes=True, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=_release,
        http_get=_http_get_ok,
        snapshot=lambda python: _snapshot(),
    )
    out, err = capfd.readouterr()
    assert code == 3, (out, err)  # the fake installer's exit code, as is
    scripts = list((data / "update").glob("install-*.sh"))
    assert len(scripts) == 1 and scripts[0].name.startswith(f"install-{hashlib.sha256(SCRIPT).hexdigest()[:12]}-")
    assert scripts[0].read_bytes() == SCRIPT
    # The installer's own output reached this terminal (fd level), with the CLI's flags: no --no-start.
    m = re.search(r"^installer ran: (.*)$", out, re.MULTILINE)
    assert m, out
    assert m.group(1) == f"--yes --no-open --no-modify-path --data-dir {data}"
    assert f"Running the AbstractFramework installer: /bin/sh {scripts[0]} --yes --no-open --no-modify-path --data-dir {data}" in out
    assert "exited with code 3" in err and str(data / "logs") in err
    assert "Done." not in out


def test_update_success_ends_with_the_console_url(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    ok_script = SCRIPT.replace(b"exit 3", b"exit 0")
    rel = dict(_release(), installer_bytes=ok_script)
    code = _run(
        _ns(yes=True, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=lambda: rel,
        http_get=_http_get_ok,
        snapshot=lambda python: _snapshot(),
    )
    out, _ = capfd.readouterr()
    assert code == 0
    assert "Done. Console: http://127.0.0.1:8080/console" in out


def test_update_without_a_terminal_needs_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"

    class NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NotATty())
    ran: List[List[str]] = []
    code = _run(
        _ns(yes=False, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=_release,
        http_get=_http_get_ok,
        snapshot=lambda python: _snapshot(),
        run=lambda command, **kw: ran.append(list(command)) or 0,
    )
    assert code == update_cli.EXIT_REFUSED
    assert "pass --yes" in capsys.readouterr().err
    assert ran == [] and not (data / "update").exists()


def test_update_asks_once_and_a_no_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    prompts: List[str] = []

    def answer_no(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer_no)
    ran: List[List[str]] = []
    code = _run(
        _ns(yes=False, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=_release,
        http_get=_http_get_ok,
        snapshot=lambda python: _snapshot(),
        run=lambda command, **kw: ran.append(list(command)) or 0,
    )
    assert code == update_cli.EXIT_OK and prompts == ["Upgrade now? [Y/n] "]
    assert "Nothing changed." in capsys.readouterr().out and ran == []


def test_update_on_windows_starts_the_powershell_installer_in_a_new_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    ps1 = b"# AbstractFramework bootstrap installer for Windows (test double)\r\n"

    def get(url: str, accept: str, timeout_s: float) -> bytes:
        if url.endswith("/scripts/install.ps1"):
            assert f"/{COMMIT}/" in url
            return ps1
        return _http_get_ok(url, accept, timeout_s)

    started: List[Any] = []

    def popen(command: List[str], **kw: Any) -> Any:
        started.append((list(command), kw))
        return object()

    code = _run(
        _ns(yes=True, data_dir=str(data)),
        detect=lambda **kw: _installer_info(data, "0.11.4"),
        fetch_release=_release,
        http_get=get,
        snapshot=lambda python: _snapshot(),
        run=popen,
        platform="win32",
    )
    assert code == 0 and len(started) == 1
    command, kw = started[0]
    assert command[:5] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    script = Path(command[5])
    assert script.parent == data / "update" and script.read_bytes() == ps1
    assert command[6:] == ["-NoOpen", "-NoModifyPath", "-DataDir", str(data)]
    assert "creationflags" in kw
    assert "new PowerShell window" in capsys.readouterr().out


# ------------------------------------------------------------------ pieces


def test_changelog_sections_between_installed_and_latest_newest_first() -> None:
    between = update_cli.changelog_sections(CHANGELOG, installed="0.11.4", latest="0.11.6")
    assert [s.splitlines()[0] for s in between] == ["## [0.11.6] - 2026-10-18", "## [0.11.5] - 2026-10-18"]
    assert between[0].rstrip().endswith("- Who is affected: fresh installs.")
    # Up to the latest only: a newer section in the file (say the file moved on) is not shown.
    assert [s.splitlines()[0] for s in update_cli.changelog_sections(CHANGELOG, installed="0.11.4", latest="0.11.5")] == ["## [0.11.5] - 2026-10-18"]
    # No release recorded (a --pin install): the latest section only.
    assert [s.splitlines()[0] for s in update_cli.changelog_sections(CHANGELOG, installed=None, latest="0.11.6")] == ["## [0.11.6] - 2026-10-18"]
    assert update_cli.changelog_sections(CHANGELOG, installed="0.11.6", latest="0.11.6") == []


def test_the_release_fetch_carries_the_browser_apps_and_the_crates() -> None:
    manifest = {
        "framework": {"version": "0.11.6"},
        "bootstrap": {"gateway_version": "0.15.4"},
        "python_packages": [{"distribution": "abstractcore", "version": "2.26.3"}],
        "npm_apps": [{"id": "flow", "package": "@abstractframework/flow", "version": "0.10.0"}, {"id": "code", "package": "@abstractframework/code", "version": "0.11.2"}],
    }

    def get(url: str, accept: str, timeout_s: float) -> bytes:
        if url == self_update.FRAMEWORK_COMMIT_API:
            return (COMMIT + "\n").encode()
        if url.endswith(self_update.FRAMEWORK_MANIFEST_PATH):
            return json.dumps(manifest).encode()
        if url.endswith(self_update.INSTALLER_SCRIPT_PATH):
            return SCRIPT
        raise AssertionError(url)

    rel = self_update._fetch_framework_release(http_get=get)
    assert rel["ok"]
    assert rel["npm_apps"] == {"flow": "0.10.0", "code": "0.11.2"}
    assert rel["crates"] == {"abstractgateway-console": "0.16.0", "abstractcode": "0.10.0"}


def test_the_cli_installer_command_has_no_no_start() -> None:
    cmd = self_update.installer_command(Path("/d/update/install-abc.sh"), "/d", flags=self_update.INSTALLER_CLI_FLAGS)
    assert cmd == ["/bin/sh", "/d/update/install-abc.sh", "--yes", "--no-open", "--no-modify-path", "--data-dir", "/d"]
    # The gateway's own job (the console's Update) keeps --no-start: it must never stop itself.
    assert "--no-start" in self_update.installer_command(Path("x.sh"), "/d")


# ------------------------------------------------------------------ console


def _bin_next_to_abstractgateway(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "abstractgateway").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "abstractgateway").chmod(0o755)
    console = bindir / "abstractgateway-console"
    console.write_text("#!/bin/sh\nexit 0\n")
    console.chmod(0o755)
    return console


def test_console_execs_the_binary_next_to_abstractgateway_with_the_resolved_url_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    console = _bin_next_to_abstractgateway(tmp_path)
    data = tmp_path / "data"
    (data / "auth").mkdir(parents=True)
    (data / "auth" / "bootstrap-admin-token").write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "bin" / "abstractgateway"), "console"])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # nothing on PATH: the folder next to the command decides
    monkeypatch.delenv("ABSTRACTGATEWAY_URL", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)
    execs: List[Any] = []

    import argparse

    code = console_cli.run_console(argparse.Namespace(url=None, token=None, data_dir=str(data)), execv=lambda path, argv: execs.append((path, list(argv))))
    assert code == 0
    assert execs == [(str(console), [str(console), "--gateway-url", "http://127.0.0.1:8080", "--token", TOKEN])]
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err

    # Explicit flags win; a token given by hand is passed as is and still never printed.
    execs.clear()
    code = console_cli.run_console(argparse.Namespace(url="http://10.0.0.5:9090/", token="user-token-xyz", data_dir=str(data)), execv=lambda path, argv: execs.append((path, list(argv))))
    assert code == 0 and execs[0][1] == [str(console), "--gateway-url", "http://10.0.0.5:9090", "--token", "user-token-xyz"]
    captured = capsys.readouterr()
    assert "user-token-xyz" not in captured.out + captured.err


def test_console_without_the_binary_says_who_builds_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "nowhere" / "abstractgateway"), "console"])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    import argparse

    code = console_cli.run_console(argparse.Namespace(url="http://127.0.0.1:1", token="t", data_dir=str(tmp_path / "data")), execv=lambda *a: pytest.fail("nothing to exec"))
    err = capsys.readouterr().err
    assert code == console_cli.EXIT_REFUSED
    assert "abstractgateway-console" in err and "installer builds it" in err and "cargo install abstractgateway-console" in err


def test_find_console_binary_prefers_the_folder_of_the_running_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "bin"
    here.mkdir()
    local = here / "abstractgateway-console"
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    elsewhere = tmp_path / "other" / "abstractgateway-console"
    elsewhere.parent.mkdir()
    elsewhere.write_text("#!/bin/sh\n")
    elsewhere.chmod(0o755)
    assert console_cli.find_console_binary(argv0=str(here / "abstractgateway"), which=lambda name: str(elsewhere)) == local
    # Not next to the command: PATH.
    assert console_cli.find_console_binary(argv0=str(tmp_path / "x" / "abstractgateway"), which=lambda name: str(elsewhere)) == elsewhere
    assert console_cli.find_console_binary(argv0=str(tmp_path / "x" / "abstractgateway"), which=lambda name: None) is None
    assert console_cli.exe_name("win32") == "abstractgateway-console.exe"


# ------------------------------------------------------------------ dispatch and the --flag spellings


@pytest.mark.parametrize("spelling", [["update", "--check"], ["--update", "--check"]])
def test_update_dispatches_from_both_spellings(spelling: List[str], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []
    monkeypatch.setattr(update_cli, "run_update", lambda args: seen.append(args) or 10)
    with pytest.raises(SystemExit) as exc:
        cli.main(list(spelling))
    assert exc.value.code == 10
    assert len(seen) == 1 and seen[0].cmd == "update" and seen[0].check is True and seen[0].yes is False


@pytest.mark.parametrize("spelling", [["console", "--url", "http://127.0.0.1:1"], ["--console", "--url", "http://127.0.0.1:1"]])
def test_console_dispatches_from_both_spellings(spelling: List[str], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []
    monkeypatch.setattr(console_cli, "run_console", lambda args: seen.append(args) or 0)
    with pytest.raises(SystemExit) as exc:
        cli.main(list(spelling))
    assert exc.value.code == 0
    assert len(seen) == 1 and seen[0].cmd == "console" and seen[0].url == "http://127.0.0.1:1" and seen[0].token is None


def test_help_lists_both_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert re.search(r"^\s+update\s+Upgrade AbstractFramework", out, re.MULTILINE), out
    assert re.search(r"^\s+console\s+Open the terminal console", out, re.MULTILINE), out
    assert "--update" in out and "--console" in out
