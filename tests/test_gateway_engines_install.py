"""Engine installs a non-technical user can finish: user-level first, tools/admin only visibly.

`abstractgateway.engines_install` with a recording `System` double: every
process launch, download and probe is recorded, file effects (unzip, copy)
happen in tmp_path. Pinned here:

  - NOTHING runs with administrator rights unless the job first stopped in
    `needs_admin` with the reason and the exact command, and `continue` was
    called; the elevated argv is exactly the osascript / pkexec one;
  - `xcode-select --install` runs only from `needs_tools` + `install_tools`,
    and the job resumes by itself once the tools are there;
  - a failed build puts a plain-language `message` first and the FULL log in
    `details` (every line, never a tail); missing CLT is `needs_tools`;
  - unsupported engines are refusals with a reason and have no Install action;
  - the routes: continue/cancel/start/stop are admin-only and need the knob.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from abstractgateway import engines_install as ei

pytestmark = pytest.mark.basic

ZIP_BYTES = b"PK-fake-ollama-zip" * 1000
ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()
MAC = ei.HostFacts(os_id="darwin", arch="arm64", accelerator="metal", macos_version=(15, 0))


def _make_app(path: Path, version: str = "0.34.3") -> None:
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    (path / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleShortVersionString</key><string>'
        + version.encode()
        + b"</string></dict></plist>"
    )


class FakeSystem(ei.System):
    """Records every side effect; performs file effects inside tmp_path only."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.calls: List[List[str]] = []
        self.whiches: Dict[str, str] = {"uv": "/fake/uv"}
        self.writable: Dict[str, bool] = {}
        self.http: Dict[str, Any] = {}
        self.handlers: List[Tuple[Callable[[List[str]], bool], Callable[[List[str]], Tuple[int, str]]]] = []
        self.sha_published = ZIP_SHA
        self.team = ei.OLLAMA_TEAM_ID
        self.spawned: List[List[str]] = []
        # What `xcode-select -p` answers: a real folder under tmp, because the
        # installer checks that the answered path exists (on a Linux CI host
        # /Library/Developer/CommandLineTools does not).
        self.clt = tmp / "CommandLineTools"
        self.clt.mkdir(exist_ok=True)

    def on(self, match: Callable[[List[str]], bool], result: Callable[[List[str]], Tuple[int, str]]) -> None:
        self.handlers.insert(0, (match, result))

    def which(self, name: str) -> Optional[str]:
        return self.whiches.get(name)

    def _handle(self, argv: List[str]) -> Tuple[int, str]:
        self.calls.append(list(argv))
        for match, result in self.handlers:
            if match(argv):
                return result(argv)
        if argv[:1] == ["ditto"] and argv[1:3] == ["-x", "-k"]:
            _make_app(Path(argv[4]) / "Ollama.app")
            return 0, ""
        if argv[:1] == ["ditto"]:
            shutil.copytree(argv[1], argv[2], symlinks=True)
            return 0, ""
        if argv[:2] == ["codesign", "-dv"]:
            return 0, f"Identifier=x\nTeamIdentifier={self.team}\n"
        if argv[:2] == ["xcode-select", "-p"]:
            return 0, f"{self.clt}\n"
        return 0, ""

    def run(self, argv, *, env=None, timeout=60.0, input_text=None):
        return self._handle(list(argv))

    def stream(self, argv, *, env=None, on_line, on_quiet, cancel, on_proc=lambda p: None):
        rc, out = self._handle(list(argv))
        for line in out.splitlines():
            on_line(line)
        return rc

    def resolve_redirect(self, url, timeout=20.0):
        self.calls.append(["RESOLVE", url])
        if url.endswith("/releases/latest"):
            return "https://github.com/ollama/ollama/releases/tag/v0.34.3"
        return "https://installers.lmstudio.ai/darwin/arm64/0.4.25-1/LM-Studio-0.4.25-1-arm64.dmg"

    def head(self, url, timeout=20.0):
        return {"etag": '"' + hashlib.md5(ZIP_BYTES).hexdigest() + '"'}  # noqa: S324

    def fetch_text(self, url, timeout=30.0):
        self.calls.append(["FETCH", url])
        return f"{self.sha_published}  ./Ollama-darwin.zip\nffff  ./ollama-darwin.tgz\n{self.sha_published}  ./ollama-linux-amd64.tar.zst\n"

    def download(self, url, dest, *, on_progress, cancel):
        self.calls.append(["DOWNLOAD", url])
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = len(ZIP_BYTES)
        for done in (0, total // 2, total):
            on_progress(done, total)
        dest.write_bytes(ZIP_BYTES)
        return {"path": str(dest), "bytes": total, "sha256": ZIP_SHA, "md5": hashlib.md5(ZIP_BYTES).hexdigest(), "final_url": url}  # noqa: S324

    def http_json(self, url, timeout=2.0):
        return self.http.get(url)

    def writable_dir(self, path):
        return self.writable.get(str(path), True)

    def spawn_detached(self, argv, *, env, log_path):
        self.spawned.append(list(argv))
        self.http["http://127.0.0.1:11434/api/version"] = {"version": "0.34.3"}
        return 4242

    # helpers for assertions
    def elevated(self) -> List[List[str]]:
        return [c for c in self.calls if (c[:1] == ["osascript"] and "with administrator privileges" in c[-1]) or c[:1] == ["pkexec"] or c[:1] == ["sudo"]]

    def tools_installs(self) -> List[List[str]]:
        return [c for c in self.calls if c == ["xcode-select", "--install"]]


def _installer(tmp: Path, system: FakeSystem, host: ei.HostFacts = MAC, **kw: Any) -> ei.EngineInstaller:
    kw.setdefault("start_timeout_s", 2.0)
    return ei.EngineInstaller(
        system=system, host=host, python="/gw/bin/python", cache_dir=tmp / "cache", home=tmp / "home",
        system_apps_dir=tmp / "Applications", tools_poll_s=0.01, tools_wait_s=2.0, **kw,
    )


def _registry(inst: ei.EngineInstaller, tmp: Path) -> ei.EngineJobRegistry:
    return ei.EngineJobRegistry(lambda: inst, log_dir=tmp / "jobs")


# ---------------------------------------------------------------------------
# Administrator rights: never without a surfaced needs_admin + continue
# ---------------------------------------------------------------------------


def test_admin_command_runs_only_after_needs_admin_was_surfaced_and_continued(tmp_path, monkeypatch) -> None:
    # The chown names the account that owns the process (password database),
    # never an inherited USER variable (mission FF: that env read is gone).
    monkeypatch.setenv("USER", "someone-else")
    import pwd, os as _os

    owner = pwd.getpwuid(_os.getuid()).pw_name
    sysd = FakeSystem(tmp_path)
    (tmp_path / "Applications").mkdir()
    sysd.writable[str(tmp_path / "Applications")] = False  # a standard (non-admin) account
    sysd.http["http://127.0.0.1:11434/api/version"] = {"version": "0.34.3"}
    reg = _registry(_installer(tmp_path, sysd), tmp_path)

    snap, _ = reg.start("ollama", location="system", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "needs_admin", job["message"]
    assert sysd.elevated() == [], "an elevated command ran before the person was asked"
    prompt = job["admin_prompt"]
    staged = tmp_path / "cache" / "staging" / job["job_id"] / "Ollama.app"
    dest = tmp_path / "Applications" / "Ollama.app"
    assert prompt["method"] == "osascript" and prompt["button"] == "Continue with administrator password"
    assert "cannot write" in prompt["reason"]
    assert prompt["command"] == (
        f"/bin/mkdir -p {tmp_path / 'Applications'} && /bin/rm -rf {dest} && /usr/bin/ditto {staged} {dest}"
        + f" && /usr/sbin/chown -R {owner}:admin {dest}"
    )
    assert job["continue_actions"] == ["approve_admin"]
    # Plain language first, the reason and the button in it.
    assert job["message"].startswith("Copying Ollama.app into") and "Continue with administrator password" in job["message"]

    def admin_copies(argv):  # the OS prompt succeeds: the double performs the copy the command names
        _make_app(dest)
        return 0, ""

    sysd.on(lambda a: a[:1] == ["osascript"], admin_copies)
    reg.continue_job(job["job_id"], run_inline=True)
    done = reg.get(job["job_id"]).snapshot()
    assert done["state"] == "done", done["message"]
    assert sysd.elevated() == [ei.applescript_admin_argv(prompt["command"], prompt["prompt_text"])]
    assert sysd.elevated()[0][-1].endswith("with administrator privileges")
    assert 'with prompt "AbstractGateway wants to install Ollama in' in sysd.elevated()[0][-1]


def test_run_admin_refuses_a_prompt_that_was_never_surfaced(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    inst = _installer(tmp_path, sysd)
    job = ei.EngineJob("ollama", force=False, location="auto", log_dir=None)
    ctx = ei._JobContext(job, inst)
    prompt = ei.AdminPrompt("k", "because", "/usr/bin/true", "osascript")
    with pytest.raises(ei.NeedsAdmin):
        ctx.run_admin(prompt)
    # An approval for a key whose command was never shown (job.admin_prompt differs) is refused too.
    job.approved_admin.add("k")
    job.admin_prompt = ei.AdminPrompt("k", "because", "/bin/echo shown", "osascript")
    with pytest.raises(ei.PrivilegeViolation):
        ctx.run_admin(prompt)
    assert sysd.elevated() == []


def test_continue_approves_only_a_paused_job(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    sysd.http["http://127.0.0.1:11434/api/version"] = {"version": "0.34.3"}
    snap, _ = reg.start("ollama", run_inline=True)  # writable -> user-level, no admin
    assert reg.get(snap["job_id"]).snapshot()["state"] == "done"
    with pytest.raises(ei.JobStateError) as info:
        reg.continue_job(snap["job_id"])
    assert info.value.reason == "not_paused"
    assert sysd.elevated() == []


def test_cancelled_password_dialog_returns_to_needs_admin(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    (tmp_path / "Applications").mkdir()
    sysd.writable[str(tmp_path / "Applications")] = False
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("ollama", location="system", run_inline=True)
    sysd.on(lambda a: a[:1] == ["osascript"], lambda a: (1, "execution error: User canceled. (-128)"))
    reg.continue_job(snap["job_id"], run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "needs_admin"
    assert "cancelled" in job["message"]
    assert len(sysd.elevated()) == 1


def test_user_level_ollama_install_never_elevates_and_reports_bytes(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.writable[str(tmp_path / "Applications")] = False  # not an admin account -> ~/Applications

    def app_starts(argv):
        sysd.http["http://127.0.0.1:11434/api/version"] = {"version": "0.34.3"}
        return 0, ""

    sysd.on(lambda a: a[:1] == ["open"], app_starts)
    inst = _installer(tmp_path, sysd)
    reg = _registry(inst, tmp_path)
    snap, _ = reg.start("ollama", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "done", job["details"]
    assert sysd.elevated() == []
    assert job["result"]["location"] == str(tmp_path / "home" / "Applications" / "Ollama.app")
    assert job["bytes_done"] == job["bytes_total"] == len(ZIP_BYTES)
    assert ["DOWNLOAD", "https://github.com/ollama/ollama/releases/download/v0.34.3/Ollama-darwin.zip"] in sysd.calls
    states = [e["state"] for e in job["events"]]
    assert states[0] == "queued" and "downloading" in states and "installing" in states and states[-1] == "done"
    # Outside /Applications the app is started with --fast-startup (no "Move to Applications?" dialog).
    opens = [c for c in sysd.calls if c[:1] == ["open"]]
    assert opens and opens[0][-3:] == ["--args", "hidden", "--fast-startup"]


def test_checksum_mismatch_installs_nothing(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.sha_published = "0" * 64
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("ollama", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed" and job["error"]["code"] == "checksum_mismatch"
    assert not (tmp_path / "Applications" / "Ollama.app").exists()
    assert not list((tmp_path / "cache" / "downloads").glob("*.zip"))


def test_wrong_signing_team_installs_nothing(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.team = "EVIL123456"
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("ollama", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed" and job["error"]["code"] == "signature_team_mismatch"
    assert not (tmp_path / "Applications" / "Ollama.app").exists()


def test_linux_ollama_installs_for_the_user_without_an_administrator(tmp_path) -> None:
    """Operator 2026-10-29: "the gateway never requires admin rights for an engine". On Linux (not
    root) Ollama's official archive is unpacked into ~/.local and served as a systemd user service:
    no pkexec, no sudo, no needs_admin step, desktop or headless."""
    for gui in (True, False):
        root = tmp_path / ("gui" if gui else "headless")
        root.mkdir()
        linux = ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc", gui_session=gui)
        sysd = FakeSystem(root)
        sysd.whiches.update({"zstd": "/usr/bin/zstd", "systemctl": "/usr/bin/systemctl", "pkexec": "/usr/bin/pkexec"})
        home = root / "home"

        def unpack(argv):
            (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
            (home / ".local" / "bin" / "ollama").write_text("#!/bin/sh\n")
            return 0, ""

        sysd.on(lambda a: a[:2] == ["sh", "-c"] and "zstd -d -c" in a[2], unpack)
        sysd.on(lambda a: a[:4] == ["systemctl", "--user", "enable", "--now"],
                lambda a: (sysd.http.__setitem__("http://127.0.0.1:11434/api/version", {"version": "0.34.3"}) or (0, "")))
        inst = _installer(root, sysd, host=linux)
        plan = inst.plan("ollama")
        assert plan.available and not plan.needs_admin and plan.method == "user_archive"
        assert "no administrator" in plan.notes and "Apple" not in plan.notes
        reg = _registry(inst, root)
        snap, _ = reg.start("ollama", run_inline=True)
        job = reg.get(snap["job_id"]).snapshot()
        assert job["state"] == "done", job["message"]
        assert sysd.elevated() == [] and not any(c[:1] == ["pkexec"] for c in sysd.calls)
        assert ["DOWNLOAD", "https://github.com/ollama/ollama/releases/download/v0.34.3/ollama-linux-amd64.tar.zst"] in sysd.calls
        unit = home / ".config" / "systemd" / "user" / "ollama.service"
        ollama = home / ".local" / "bin" / "ollama"  # nested quotes in an f-string need Python 3.12 (PEP 701)
        assert f'ExecStart="{ollama}" serve' in unit.read_text()
        assert ["systemctl", "--user", "enable", "--now", "ollama.service"] in sysd.calls
        assert job["result"]["service"] == "systemd-user"
        # The row sees it although ~/.local/bin is not on the gateway's PATH.
        row = ei.engine_row(inst, {"id": "ollama", "installed": False}, active_job=None, install_allowed=True)
        assert row["installed"] is True


def test_linux_ollama_without_zstd_says_why_without_an_administrator_step(tmp_path) -> None:
    linux = ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc", gui_session=False)
    plan = _installer(tmp_path, FakeSystem(tmp_path), host=linux).plan("ollama")
    assert not plan.available and not plan.needs_admin
    assert "zstd" in plan.notes and "administrator" not in plan.notes.lower()


# ---------------------------------------------------------------------------
# llama.cpp: wheel first, tools visibly, failures readable
# ---------------------------------------------------------------------------


def test_llamacpp_installs_the_metal_wheel_without_a_compiler(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "gpu_offload" in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.3.28", "gpu_offload": true}'))
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "done" and job["result"] == {"installed": True, "version": "0.3.28", "gpu_offload": True, "location": "/gw/bin/python", "method": "metal wheel",
                                                        "message": "The next model load uses the new llama.cpp"}
    pip = [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]
    # AbstractCore's apple setting (ruling 2026-09-29: never a bare engine package), its
    # llama-cpp-python taken from upstream's prebuilt Metal wheel.
    assert pip == [["/fake/uv", "pip", "install", "--python", "/gw/bin/python", "abstractcore[apple]", "llama-cpp-python==0.3.28", "--find-links",
                    "https://abetlen.github.io/llama-cpp-python/whl/metal/llama-cpp-python/", "--only-binary", "llama-cpp-python"]]
    assert ["xcode-select", "-p"] not in sysd.calls  # no compiler question when a wheel exists


def test_engine_installs_pin_the_gateway_packages_when_the_cache_path_has_a_space(tmp_path) -> None:
    """uv splits a `--constraint` path at whitespace (a Windows cache under a
    user name with a space); the pins are requirements of the same command."""
    sysd = FakeSystem(tmp_path)
    pins = {"abstractgateway": "0.4.2", "AbstractRuntime": "0.4.34", "abstractcore": "2.15.1"}
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "startswith('abstract')" in a[-1], lambda a: (0, json.dumps(pins)) if "-I" in a else (0, json.dumps({**pins, "abstractstray": "9.9.9"})))
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "mlx.core" in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.30.0", "mlx_lm": "0.28.0", "device": "Device(gpu, 0)"}'))

    def fake_uv(a):
        for i, x in enumerate(a):
            if x == "--constraint" and not Path(a[i + 1].split()[0]).exists():
                return 2, f"error: File not found: `{a[i + 1].split()[0]}`"
        return 0, "Installed 2 packages in 1s"

    sysd.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"], fake_uv)
    inst = ei.EngineInstaller(
        system=sysd, host=MAC, python="/gw/bin/python", cache_dir=tmp_path / "Users" / "Jane Doe" / "AppData" / "Local" / "AbstractGateway",
        home=tmp_path / "home", system_apps_dir=tmp_path / "Applications", tools_poll_s=0.01, tools_wait_s=2.0, start_timeout_s=2.0,
    )
    reg = _registry(inst, tmp_path)
    snap, _ = reg.start("mlx", force=True, run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "done", job
    pip = [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]
    assert pip == [["/fake/uv", "pip", "install", "--python", "/gw/bin/python", "abstractcore[apple]", "--only-binary", "mlx",
                    "AbstractRuntime==0.4.34", "abstractcore==2.15.1", "abstractgateway==0.4.2"]]


def test_an_importable_engine_is_already_installed_unless_forced(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "llama_cpp" in a[-1] and "gpu_offload" not in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.3.35"}'))
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "gpu_offload" in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.3.28", "gpu_offload": true}'))
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "done" and job["result"]["already_installed"] is True
    assert job["message"] == "llama.cpp is already installed (0.3.35)"
    assert not [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]
    snap, _ = reg.start("llamacpp", force=True, run_inline=True)
    assert reg.get(snap["job_id"]).snapshot()["result"]["method"] == "metal wheel"
    assert len([c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]) == 1


BUILD_LOG = [f"      cmake line {i}: -- Detecting C compiler ABI info" for i in range(500)] + [
    "      CMake Error: CMAKE_C_COMPILER not set, after EnableLanguage",
    "      *** CMake configuration failed",
]


def test_no_wheel_and_no_command_line_tools_is_needs_tools_then_resumes(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    clt = {"present": False}
    sysd.on(lambda a: a[:2] == ["xcode-select", "-p"], lambda a: (0, f"{sysd.clt}\n") if clt["present"] else (2, "xcode-select: error: unable to get active developer directory"))
    sysd.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"] and "--only-binary" in a, lambda a: (1, "  x No solution found when resolving dependencies:\n  Because llama-cpp-python==0.3.28 has no wheels with a matching platform tag"))

    def install_tools(argv):
        clt["present"] = True  # Apple's installer finishes a moment later
        return 0, "xcode-select: note: install requested for command line developer tools"

    sysd.on(lambda a: a == ["xcode-select", "--install"], install_tools)
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "gpu_offload" in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.3.35", "gpu_offload": true}'))
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "needs_tools"
    assert job["message"] == ("The prebuilt llama.cpp wheel did not install (no matching package for this Python and machine); "
                              "building from source needs the Apple command-line tools.")
    assert job["tools_prompt"]["action"] == {"kind": "xcode_select_install", "command": "xcode-select --install", "available": True, "button": "Install tools"}
    assert job["continue_actions"] == ["install_tools", "recheck"]
    source_builds = [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"] and "--only-binary" not in c]
    assert sysd.tools_installs() == [] and source_builds == []  # no build started before the tools are there

    reg.continue_job(job["job_id"], "install_tools", run_inline=True)
    done = reg.get(job["job_id"]).snapshot()
    assert done["state"] == "done", done["message"]
    assert sysd.tools_installs() == [["xcode-select", "--install"]]
    assert done["result"]["method"] == "source build"
    assert sysd.elevated() == []  # xcode-select --install is Apple's own dialog, not an elevation


def test_a_failed_build_puts_the_message_first_and_the_whole_log_in_details(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"] and "--only-binary" in a, lambda a: (1, "error: Failed to read `llama-cpp-python==0.3.28`\n  Caused by: invalid CRC in zip entry"))
    sysd.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"] and "--only-binary" not in a, lambda a: (1, "Building llama-cpp-python==0.3.35\n" + "\n".join(BUILD_LOG)))
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed" and job["error"]["code"] == "build_failed"
    assert job["message"] == (
        "The prebuilt llama.cpp wheel did not install (the wheel file is corrupted), and building it from source failed: "
        "no C compiler was found. The full build log is in the details."
    )
    assert "\n" not in job["message"] and "cmake line" not in job["message"]
    details = job["details"].splitlines()
    for line in BUILD_LOG:  # every line, never a tail
        assert line in details
    assert Path(job["log_path"]).read_text().count("cmake line") == 500


def test_linux_without_a_compiler_names_the_package_to_install(tmp_path) -> None:
    host = ei.HostFacts(os_id="linux", arch="riscv64", libc="glibc")
    sysd = FakeSystem(tmp_path)
    reg = _registry(_installer(tmp_path, sysd, host=host), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "needs_tools"
    assert job["tools_prompt"]["action"]["available"] is False and "build-essential" in job["tools_prompt"]["action"]["command"]
    with pytest.raises(ei.JobStateError):
        reg.continue_job(job["job_id"], "install_tools")


# ---------------------------------------------------------------------------
# Support and rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, engine, why",
    [
        (MAC, "vllm", "does not run on macOS"),
        (ei.HostFacts(os_id="darwin", arch="x86_64", macos_version=(14, 5)), "mlx", "Intel Mac"),
        (ei.HostFacts(os_id="darwin", arch="x86_64", macos_version=(14, 5), translated=True), "mlx", "Rosetta"),
        (ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc"), "mlx", "only on Apple Silicon"),
        (ei.HostFacts(os_id="darwin", arch="x86_64", macos_version=(14, 5)), "lmstudio", "Intel Mac"),
        (ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cpu"), "vllm", "NVIDIA"),
    ],
)
def test_unsupported_engines_have_a_reason_and_no_install_action(tmp_path, host, engine, why) -> None:
    inst = _installer(tmp_path, FakeSystem(tmp_path), host=host)
    reg = _registry(inst, tmp_path)
    row = ei.engine_row(inst, {"id": engine, "installed": engine == "mlx"}, active_job=None, install_allowed=True)
    assert row["supported"] is False and why in row["support_reason"]
    assert row["install"]["available"] is False and row["install"]["method"] == "unsupported"
    assert "install" not in [a["id"] for a in row["actions"]]
    if engine == "mlx":
        assert row["installed"] is False  # a stray wheel on a host MLX cannot use is not a working engine
    with pytest.raises(ei.JobStateError) as info:
        reg.start(engine)
    assert info.value.reason == "unsupported_on_this_machine"


def test_vllm_on_linux_with_cuda_is_a_user_level_wheel(tmp_path) -> None:
    host = ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc")
    plan = _installer(tmp_path, FakeSystem(tmp_path), host=host).plan("vllm")
    assert plan.available and plan.method == "wheel" and not plan.needs_admin
    assert plan.command_preview == ["/fake/uv", "pip", "install", "--python", "/gw/bin/python", "abstractcore[gpu]", "--torch-backend=auto"]


@pytest.mark.parametrize("engine", ["llamacpp", "huggingface"])
@pytest.mark.parametrize(
    "host",
    [ei.HostFacts(os_id="darwin", arch="x86_64", macos_version=(14, 5)),
     ei.HostFacts(os_id="darwin", arch="arm64", macos_version=(14, 5), translated=True),
     ei.HostFacts(os_id="windows", arch="arm64")],
)
def test_hosts_without_an_install_setting_get_no_bare_engine_install(tmp_path, host, engine) -> None:
    """Ruling 2026-09-29: only `abstractcore`, `abstractcore[apple]`, `abstractcore[gpu]`.
    An Intel Mac (or a Rosetta Python) has no setting that carries the engine: the row says so
    plainly and offers no pip command (never `llama-cpp-python` / the transformers stack alone)."""
    sysd = FakeSystem(tmp_path)
    inst = _installer(tmp_path, sysd, host=host)
    plan = inst.plan(engine)
    assert plan.available is False and plan.method == "unsupported"
    assert "not available on this machine" in plan.notes and "pip install" not in plan.notes
    with pytest.raises(ei.JobStateError):
        _registry(inst, tmp_path).start(engine)
    assert not [c for c in sysd.calls if "install" in c]


@pytest.mark.parametrize(
    "host, engine, setting",
    [(MAC, "llamacpp", "apple"), (MAC, "mlx", "apple"), (MAC, "huggingface", "apple"),
     (ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc"), "llamacpp", "gpu"),
     (ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc"), "huggingface", "gpu"),
     (ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc"), "vllm", "gpu")],
)
def test_python_engine_plans_install_only_an_abstractcore_setting(tmp_path, host, engine, setting) -> None:
    plan = _installer(tmp_path, FakeSystem(tmp_path), host=host).plan(engine)
    packages, argv = [], plan.command_preview[5:]
    i = 0
    while i < len(argv):
        if argv[i] in {"--only-binary", "--find-links"}:
            i += 2  # option values, not requirements
            continue
        if not argv[i].startswith("-"):
            packages.append(argv[i])
        i += 1
    assert packages and packages[0].startswith(f"abstractcore[{setting}]")
    # The only other requirement allowed is the prebuilt-wheel pin of the setting's own llama.cpp.
    assert all(p.startswith("abstractcore[") or p.startswith("llama-cpp-python==") for p in packages), packages


CONTRACT_ROW_KEYS = {"id", "name", "description", "supported", "support_reason", "installed", "version", "running", "reachable",
                     "base_url", "models_count", "install", "actions"}
CONTRACT_INSTALL_KEYS = {"available", "method", "needs_admin", "admin_reason", "needs_tools", "tools_action"}


def test_rows_carry_the_contract_and_the_right_actions(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    inst = _installer(tmp_path, sysd)
    _make_app(tmp_path / "home" / "Applications" / "LM Studio.app", "0.4.25+1")
    reg = _registry(inst, tmp_path)
    core = {"schema": "engines_status_v1", "engines": [
        {"id": "ollama", "installed": False, "running": None},
        {"id": "lmstudio", "installed": False, "running": False, "reachable": False},
        {"id": "mlx", "installed": True, "version": "0.32.2"},
    ], "host": {"os": "darwin"}}
    payload = ei.engines_payload(inst, core, reg, install_allowed=True)
    assert payload["schema"] == "gateway_engines_v2"
    rows = {r["id"]: r for r in payload["engines"]}
    assert list(rows) == list(ei.ENGINE_IDS)
    for row in rows.values():
        assert CONTRACT_ROW_KEYS <= set(row), row["id"]
        assert CONTRACT_INSTALL_KEYS <= set(row["install"]), row["id"]
    assert [a["id"] for a in rows["ollama"]["actions"]] == ["install", "open_page", "recheck", "docs"]
    assert rows["ollama"]["install"]["method"] == "app" and rows["ollama"]["install"]["needs_admin"] is False
    # An app placed in ~/Applications is detected (AbstractCore looks only in /Applications).
    assert rows["lmstudio"]["installed"] is True and rows["lmstudio"]["version"] == "0.4.25+1"
    assert [a["id"] for a in rows["lmstudio"]["actions"]] == ["start", "docs"]
    assert rows["vllm"]["install"]["method"] == "unsupported"
    off = ei.engines_payload(inst, core, reg, install_allowed=False)
    install = [a for a in off["engines"][0]["actions"] if a["id"] == "install"][0]
    assert install["enabled"] is False and "allow_engine_install" in install["reason"]


def test_a_quiet_step_keeps_saying_it_is_alive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ei, "HEARTBEAT_S", 0.2)
    job = ei.EngineJob("mlx", force=False, location="auto", log_dir=None)
    job.set_state("installing", "Installing packages")
    seen: List[str] = []
    ctx = ei._JobContext(job, _installer(tmp_path, FakeSystem(tmp_path)))
    ctx.sys = ei.System()  # the real process runner
    t = threading.Thread(target=lambda: ctx.stream(["sleep", "1"]))
    t.start()
    while t.is_alive():
        seen.append(job.snapshot()["message"])
        time.sleep(0.1)
    assert any("still working" in m for m in seen), seen


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ei.EngineJobRegistry, inst: ei.EngineInstaller, *, bind: str = "127.0.0.1"):
    from fastapi.testclient import TestClient

    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "tok-engines")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", bind)
    monkeypatch.setattr(ei, "default_installer", lambda **kw: inst)
    ei.reset_default_registry_for_tests(registry)
    from abstractgateway import core_config

    monkeypatch.setattr(core_config.config_facade, "engine_inventory", lambda probe=False: {"schema": "engines_status_v1", "engines": [{"id": "ollama"}], "host": {"os": "darwin"}})
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": "Bearer tok-engines"}


def test_route_flow_install_needs_admin_continue_done(tmp_path, monkeypatch) -> None:
    sysd = FakeSystem(tmp_path)
    (tmp_path / "Applications").mkdir()
    sysd.writable[str(tmp_path / "Applications")] = False
    sysd.http["http://127.0.0.1:11434/api/version"] = {"version": "0.34.3"}
    inst = _installer(tmp_path, sysd)
    reg = _registry(inst, tmp_path)
    client, h = _client(tmp_path, monkeypatch, reg, inst)
    try:
        with client:
            rows = client.get("/api/gateway/engines", headers=h).json()
            assert rows["schema"] == "gateway_engines_v2" and rows["install_allowed"] is True
            started = client.post("/api/gateway/engines/ollama/install", headers=h, json={"location": "system"})
            assert started.status_code == 200, started.text
            jid = started.json()["job_id"]
            reg.get(jid).thread.join(5)
            job = client.get(f"/api/gateway/engines/jobs/{jid}", headers=h).json()
            assert job["state"] == "needs_admin" and sysd.elevated() == []
            assert client.post("/api/gateway/engines/jobs/nope/continue", headers=h, json={}).status_code == 404
            assert client.post(f"/api/gateway/engines/jobs/{jid}/continue", headers=h, json={"action": "install_tools"}).status_code == 400

            sysd.on(lambda a: a[:1] == ["osascript"], lambda a: (_make_app(tmp_path / "Applications" / "Ollama.app"), (0, ""))[1])
            resumed = client.post(f"/api/gateway/engines/jobs/{jid}/continue", headers=h, json={})
            assert resumed.status_code == 200, resumed.text
            reg.get(jid).thread.join(5)
            job = client.get(f"/api/gateway/engines/jobs/{jid}", headers=h).json()
            assert job["state"] == "done" and len(sysd.elevated()) == 1
            assert client.post(f"/api/gateway/engines/jobs/{jid}/continue", headers=h, json={}).status_code == 409
            listed = client.get("/api/gateway/engines/jobs", headers=h).json()
            assert listed["jobs"][0]["job_id"] == jid
            assert client.post("/api/gateway/engines/mlx/start", headers=h).status_code == 409
    finally:
        ei.reset_default_registry_for_tests(None)


def test_route_continue_and_server_actions_are_admin_and_knob_gated(tmp_path, monkeypatch) -> None:
    sysd = FakeSystem(tmp_path)
    inst = _installer(tmp_path, sysd)
    reg = _registry(inst, tmp_path)
    client, h = _client(tmp_path, monkeypatch, reg, inst, bind="0.0.0.0")
    try:
        with client:
            assert client.post("/api/gateway/engines/ollama/install", headers=h, json={}).status_code == 403
            assert client.post("/api/gateway/engines/jobs/eng_x/continue", headers=h, json={}).status_code == 403
            assert client.post("/api/gateway/engines/jobs/eng_x/continue", json={}).status_code == 401
            assert client.post("/api/gateway/engines/ollama/start").status_code == 401
            assert client.post("/api/gateway/engines/jobs/eng_x/cancel").status_code == 401
            dry = client.post("/api/gateway/engines/llamacpp/install", headers=h, json={"dry_run": True}).json()
            assert dry["dry_run"] is True and dry["plan"]["method"] == "wheel" and "--only-binary" in dry["command"]
            assert sysd.calls == [] or all(c[:1] != ["/fake/uv"] for c in sysd.calls)
    finally:
        ei.reset_default_registry_for_tests(None)


# ---------------------------------------------------------------------------
# Default download cache: the per-OS user cache, never a hard-coded ~/.cache
# ---------------------------------------------------------------------------


def test_default_engine_cache_honours_xdg_cache_home_on_linux(tmp_path, monkeypatch) -> None:
    from abstractgateway import host_paths

    xdg = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    monkeypatch.setattr(host_paths.sys, "platform", "linux")
    inst = ei.EngineInstaller(system=FakeSystem(tmp_path), host=MAC, python="/gw/bin/python", home=tmp_path / "home")
    assert inst.cache_dir == xdg / "abstractgateway" / "engines"
    # A relative XDG_CACHE_HOME is invalid per the spec and ignored.
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert host_paths.user_cache_dir(home=tmp_path) == tmp_path / ".cache" / "abstractgateway"


def test_user_cache_dir_is_the_platform_cache_on_macos_and_windows(tmp_path) -> None:
    from abstractgateway.host_paths import user_cache_dir

    assert user_cache_dir(system="darwin", env={"XDG_CACHE_HOME": "/x"}, home=tmp_path) == (
        tmp_path / "Library" / "Caches" / "AbstractGateway"
    )
    assert user_cache_dir(system="win32", env={"LOCALAPPDATA": str(tmp_path / "L")}, home=tmp_path) == (
        tmp_path / "L" / "AbstractGateway" / "Cache"
    )


WIN = ei.HostFacts(os_id="windows", arch="x86_64", accelerator="cuda")


@pytest.mark.parametrize("engine", ["llamacpp", "huggingface"])
def test_windows_x86_64_is_a_gpu_host_like_abstractcore_says(tmp_path, engine) -> None:
    """Windows x86_64 carries AbstractCore's gpu setting (abstractcore.utils.install_settings
    .host_setting): llama.cpp and Hugging Face are supported there, never "Not for this computer"."""
    from abstractcore.utils.install_settings import host_setting

    inst = _installer(tmp_path, FakeSystem(tmp_path), host=WIN)
    assert inst.local_setting() == host_setting("windows", "x86_64") == "gpu"
    assert inst.support(engine) == (True, None)


def test_windows_huggingface_installs_the_gpu_setting_with_pytorchs_cuda_build(tmp_path) -> None:
    # PyPI's Windows PyTorch is the CPU build: uv's --torch-backend=auto takes the CUDA build.
    plan = _installer(tmp_path, FakeSystem(tmp_path), host=WIN).plan("huggingface")
    assert plan.available and plan.method == "wheel"
    assert plan.command_preview == ["/fake/uv", "pip", "install", "--python", "/gw/bin/python",
                                    "abstractcore[gpu]==<installed>", "--torch-backend=auto"]


def test_windows_llamacpp_points_to_the_installer_never_a_bare_package(tmp_path) -> None:
    plan = _installer(tmp_path, FakeSystem(tmp_path), host=WIN).plan("llamacpp")
    assert plan.available is False and plan.method == "wheel"
    assert "AbstractFramework installer" in plan.notes
    assert plan.command_preview == [ei.WINDOWS_INSTALLER_COMMAND]
    assert not any("llama-cpp-python" in part for part in plan.command_preview)


def test_vllm_on_windows_says_wsl2_or_a_linux_server(tmp_path) -> None:
    ok, reason = _installer(tmp_path, FakeSystem(tmp_path), host=WIN).support("vllm")
    assert ok is False and "WSL2" in reason and "no native Windows build" in reason


@pytest.mark.parametrize(
    "os_id, arch",
    [(o, a) for o in ("darwin", "linux", "windows", "win32", "freebsd", "") for a in ("arm64", "aarch64", "x86_64", "amd64", "x64", "")],
)
def test_the_gateways_host_table_is_abstractcores(os_id, arch) -> None:
    """The gateway keeps its own copy (no direct AbstractCore import); it must say what
    AbstractCore says for every host, or the console calls a supported engine unavailable."""
    install_settings = pytest.importorskip("abstractcore.utils.install_settings")
    assert ei.host_setting(os_id, arch) == install_settings.host_setting(os_id, arch)


def test_the_gateways_unavailable_sentence_is_abstractcores() -> None:
    install_settings = pytest.importorskip("abstractcore.utils.install_settings")
    assert ei._not_available_here("X") == install_settings.not_available_here("X")


def test_linux_lmstudio_without_libatomic_stops_before_the_installer_and_resumes(tmp_path) -> None:
    # Operator QA (OVH test server, 2026-10-28): LM Studio's Linux installer died half-way on a
    # missing libatomic1. The plan says so up front; the job pauses in needs_tools with the
    # exact apt line BEFORE the vendor script runs, and a re-check resumes it.
    # A gateway running as root can install the library itself (the server installer's case).
    host = ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc", gui_session=False, is_root=True)
    sysd = FakeSystem(tmp_path)
    lib = {"present": False}
    sysd.on(lambda a: a[:2] == ["ldconfig", "-p"],
            lambda a: (0, "\tlibc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6\n"
                          + ("\tlibatomic.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libatomic.so.1\n" if lib["present"] else "")))
    inst = _installer(tmp_path, sysd, host=host)
    plan = inst.plan("lmstudio").public(None)
    assert plan["needs_tools"] is True and "libatomic1" in plan["notes"]
    assert plan["tools_action"]["command"].startswith("sudo apt-get install -y libatomic1")
    reg = _registry(inst, tmp_path)
    snap, _ = reg.start("lmstudio", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "needs_tools" and "libatomic1" in job["message"]
    assert job["tools_prompt"]["action"]["available"] is False
    assert not any(c[:2] == ["bash", "-c"] for c in sysd.calls)  # the vendor installer never ran
    lib["present"] = True  # the admin ran the apt line
    sysd.whiches["lms"] = str(tmp_path / "home" / ".lmstudio" / "bin" / "lms")
    reg.continue_job(job["job_id"], "recheck", run_inline=True)
    after = reg.get(job["job_id"]).snapshot()
    assert after["state"] != "needs_tools", after["message"]
    assert inst.plan("lmstudio").public(None)["needs_tools"] is False



def test_linux_lmstudio_without_libatomic_and_without_root_says_what_installs_here(tmp_path) -> None:
    """Operator 2026-10-29: a gateway without root never shows an administrator step; it says plainly
    that LM Studio cannot install on this machine (no libatomic1) and which engines install for this
    account, and never mentions Apple tools on Linux."""
    host = ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc", gui_session=False)
    sysd = FakeSystem(tmp_path)
    sysd.whiches["zstd"] = "/usr/bin/zstd"  # Ollama's archive can be unpacked here
    sysd.on(lambda a: a[:2] == ["ldconfig", "-p"], lambda a: (0, "\tlibc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6\n"))
    inst = _installer(tmp_path, sysd, host=host)
    plan = inst.plan("lmstudio")
    assert not plan.available and not plan.needs_admin and not plan.needs_tools
    assert "libatomic1" in plan.notes and "Apple" not in plan.notes and "sudo" not in plan.notes
    assert "Installs on this computer without an administrator:" in plan.notes and "llama.cpp" in plan.notes
    row = ei.engine_row(inst, {"id": "lmstudio", "installed": False}, active_job=None, install_allowed=True)
    assert not any(a.get("id") == "install" for a in row.get("actions") or [])


def test_the_console_names_apple_tools_only_for_apples_installer() -> None:
    from abstractgateway import console_ui

    html = console_ui.gateway_console_html() if hasattr(console_ui, "gateway_console_html") else ""
    src = html or open(console_ui.__file__, encoding="utf-8").read()
    assert 'job.tools_prompt.action.kind === "xcode_select_install") ? "Needs Apple tools" : "Needs a prerequisite"' in src


def test_ollama_user_unit_quotes_its_program_path():
    # A home directory with a space (or a quote, a backslash, a literal %) stays ONE argument.
    assert ei._systemd_exec_path("/home/a b/.local/bin/ollama") == '"/home/a b/.local/bin/ollama"'
    assert ei._systemd_exec_path('/home/x"y\\z/50%/ollama') == '"/home/x\\"y\\\\z/50%%/ollama"'


# -- llama.cpp update on an NVIDIA host keeps the CUDA build (operator 2026-10-29) ---------------
def test_llama_update_on_a_cuda_host_takes_the_matching_cuda_build(tmp_path):
    """The engine path used to install the CPU wheel on every Linux host: an "Update llama.cpp"
    on the OVH NVIDIA server would have dropped GPU offload. MUTANT: cpu index -> RED."""
    system = FakeSystem(tmp_path)
    system.on(lambda a: a[1:3] == ["-c", "import torch; print(torch.version.cuda or '')"], lambda a: (0, "12.4\n"))
    host = ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc")
    inst = _installer(tmp_path, system, host=host)
    pin, backend, links = inst._llama_wheel()
    assert (pin, backend) == (ei.LLAMA_CPU_PIN, "cu125") and links.endswith("/cu125/llama-cpp-python/")
    argv = inst._llama_wheel_argv(pin, links)
    assert "--reinstall-package" in argv and argv[argv.index("--reinstall-package") + 1] == "llama-cpp-python", (
        "uv would keep the installed CPU build of the same version")


def test_llama_update_without_a_cuda_torch_stays_on_the_cpu_build(tmp_path):
    system = FakeSystem(tmp_path)
    system.on(lambda a: a[1:3] == ["-c", "import torch; print(torch.version.cuda or '')"], lambda a: (0, "\n"))
    inst = _installer(tmp_path, system, host=ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc"))
    assert inst._llama_wheel()[1] == "cpu"


def test_llama_pin_is_the_release_that_knows_spark2_5():
    assert ei.LLAMA_CPU_PIN == "0.3.36"   # llama.cpp 0c1e5709 (vendored by 0.3.36) names spark2_5



_TORCH_PROBE = "import torch; print(torch.version.cuda or '')"


def test_nvidia_host_without_any_cuda_build_refuses_never_the_cpu_wheel(tmp_path):
    """Merge desk review: a CPU PyTorch on an NVIDIA box must not lead to the CPU wheel
    replacing a GPU llama.cpp. MUTANT: no refusal -> cpu index installed -> RED."""
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[1:3] == ["-c", _TORCH_PROBE], lambda a: (0, "\n"))
    sysd.on(lambda a: a[1:2] == ["-c"] and "libggml-cuda" in a[2], lambda a: (0, "\n"))
    host = ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc")
    reg = _registry(_installer(tmp_path, sysd, host=host), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "failed" and "NVIDIA GPU" in str(job.get("message") or job.get("details") or job), job
    assert not [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]], "nothing may be installed"


def test_nvidia_host_with_a_cpu_torch_keeps_the_installed_cuda_llama_build(tmp_path):
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[1:3] == ["-c", _TORCH_PROBE], lambda a: (0, "\n"))
    sysd.on(lambda a: a[1:2] == ["-c"] and "libggml-cuda" in a[2], lambda a: (0, "12\n"))
    inst = _installer(tmp_path, sysd, host=ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc"))
    assert inst._llama_wheel()[1] == "cu125"


def test_a_new_llamacpp_restarts_the_model_host_child(tmp_path, monkeypatch):
    """After the update the model host child (gateway #28) still holds the OLD llama.cpp:
    it is restarted and the result says so. MUTANT: no restart -> RED."""
    import abstractgateway.model_host as mh

    killed = []
    monkeypatch.setattr(mh, "supervisor", lambda: type("S", (), {"kill": lambda self, reason: killed.append(reason)})())
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "gpu_offload" in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.3.28", "gpu_offload": true}'))
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "done" and killed and "llama.cpp updated" in killed[0]
    assert job["result"]["message"] == "The model host restarts with the new llama.cpp; the next model load uses it"


def test_linux_host_with_nvidia_smi_is_cuda(monkeypatch):
    """OVH 8080 (2026-10-29): the installer was built with accelerator=None, so the CUDA branch never
    ran and "Update llama.cpp" chose the CPU wheel. MUTANT: no detection -> None -> RED."""
    import subprocess as sp
    monkeypatch.setattr(ei.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ei.shutil, "which", lambda n: "/usr/bin/nvidia-smi" if n == "nvidia-smi" else None)
    monkeypatch.setattr(ei.subprocess, "run", lambda argv, **k: sp.CompletedProcess(argv, 0, "GPU 0: Quadro RTX 5000 (UUID: x)\\n", ""))
    assert ei.detect_host().accelerator == "cuda"
    monkeypatch.setattr(ei.shutil, "which", lambda n: None)
    assert ei.detect_host().accelerator is None


class _LlamaHost:
    """A fake gateway Python with llama.cpp `installed` (None: absent): pip calls with `--target`
    stage a copy, the others replace the installed one; the load check answers for either."""

    def __init__(self, tmp_path, *, installed="0.3.35", staged=None, after_install=None,
                 stage_rc=0, stage_out="", install_rc=0):
        import re as _re
        self.sysd = FakeSystem(tmp_path)
        self.installed = installed
        self.good = {"gpu_offload": True, "cuda_lib": True}
        self.staged = dict(self.good, **(staged or {}))
        self.after_install = dict(self.good, **(after_install or {}))
        self.installed_props = dict(self.good)
        s = self.sysd

        def version(a):
            return (0, self.installed + "\n") if self.installed else (1, "PackageNotFoundError")

        def pip(a):
            v = next(x.split("==")[1] for x in a if x.startswith("llama-cpp-python=="))
            if "--target" in a:
                return stage_rc, stage_out
            if install_rc:
                return install_rc, "error: install failed"
            self.installed = v
            self.installed_props = dict(self.after_install) if v != "0.3.35" else dict(self.good)
            return 0, ""

        def check(a):
            m = _re.search(r"sys.path.insert\(0, '([^']+)'\)", a[2])
            if m:
                props, ver, f = self.staged, "0.3.36", m.group(1) + "/llama_cpp/__init__.py"
            else:
                props, ver, f = self.installed_props, self.installed, "/gw/site-packages/llama_cpp/__init__.py"
            return 0, "__AG_LLAMA_CHECK__" + json.dumps(dict(props, version=ver, file=f)) + "\n"

        s.on(lambda a: a[1:2] == ["-c"] and "m.version('llama-cpp-python')" in a[2], version)
        s.on(lambda a: a[1:3] == ["-c", "import torch; print(torch.version.cuda or '')"], lambda a: (0, "12.4\n"))
        s.on(lambda a: a[1:2] == ["-c"] and "libggml-cuda" in a[2] and "__AG_LLAMA_CHECK__" not in a[2], lambda a: (0, "12\n"))
        s.on(lambda a: a[1:2] == ["-c"] and "__AG_LLAMA_CHECK__" in a[2], check)
        s.on(lambda a: a[:3] == ["/fake/uv", "pip", "install"] and any(x.startswith("llama-cpp-python==") for x in a), pip)
        s.on(lambda a: a[:1] == ["/gw/bin/python"] and "gpu_offload" in a[-1] and "__AG_LLAMA_CHECK__" not in a[-1],
             lambda a: (0, '__AG_VERIFY__{"version": "0.3.36", "gpu_offload": true}'))

    def run(self, tmp_path):
        host = ei.HostFacts(os_id="linux", arch="x86_64", accelerator="cuda", libc="glibc")
        reg = _registry(_installer(tmp_path, self.sysd, host=host), tmp_path)
        snap, _ = reg.start("llamacpp", run_inline=True)
        job = reg.get(snap["job_id"]).snapshot()
        pips = [c for c in self.sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]
        return job, pips


def _job_text(job):
    return json.dumps(job)


def test_an_update_stages_and_checks_the_wheel_then_installs_llama_cpp_alone(tmp_path):
    """Re-resolving abstractcore[gpu] built stable-diffusion-cpp-python from source (no compiler on
    the server). The update stages the new wheel, checks it loads with GPU offload, then installs
    llama-cpp-python alone. MUTANT: the core spec on an update, or no staging -> RED."""
    h = _LlamaHost(tmp_path)
    job, pips = h.run(tmp_path)
    assert job["state"] == "done", job
    assert len(pips) == 2 and "--target" in pips[0] and "--target" not in pips[1], pips
    for c in pips:
        assert not any(x.startswith("abstractcore") for x in c), c
        assert "--no-deps" in c and "llama-cpp-python==0.3.36" in c and any("/cu125/" in x for x in c)
    assert h.installed == "0.3.36"


def test_a_staged_cuda_wheel_without_gpu_offload_never_replaces_the_working_one(tmp_path):
    """8080: the update left llama.cpp broken. MUTANT: install without the staged check -> RED."""
    h = _LlamaHost(tmp_path, staged={"gpu_offload": False})
    job, pips = h.run(tmp_path)
    assert job["state"] == "failed", job
    assert "llama.cpp 0.3.35 stays installed and unchanged" in _job_text(job) and "reports no GPU offload" in _job_text(job)
    assert [c for c in pips if "--target" not in c] == [] and h.installed == "0.3.35"


def test_a_staged_cuda_wheel_without_libggml_cuda_never_replaces_the_working_one(tmp_path):
    h = _LlamaHost(tmp_path, staged={"cuda_lib": False})
    job, pips = h.run(tmp_path)
    assert job["state"] == "failed", job
    assert "stays installed and unchanged" in _job_text(job) and "carries no libggml-cuda library" in _job_text(job)
    assert h.installed == "0.3.35"


def test_an_update_whose_wheel_does_not_download_never_asks_for_a_compiler(tmp_path):
    """8080: "building from source needs a C/C++ compiler" after a failed update.
    MUTANT: fall through to the source build -> needs_tools -> RED."""
    h = _LlamaHost(tmp_path, stage_rc=1, stage_out="error: Failed to download llama-cpp-python")
    job, pips = h.run(tmp_path)
    assert job["state"] == "failed", job
    t = _job_text(job)
    assert "stays installed and unchanged" in t and "compiler" not in t and "from source" not in t
    assert h.installed == "0.3.35" and len(pips) == 1


def test_an_installed_wheel_that_does_not_load_puts_the_old_one_back(tmp_path):
    """MUTANT: no rollback -> the job leaves 0.3.36 broken -> RED."""
    h = _LlamaHost(tmp_path, after_install={"gpu_offload": False})
    job, pips = h.run(tmp_path)
    assert job["state"] == "failed", job
    assert "0.3.35 is installed again and serves as before" in _job_text(job), job
    assert "llama-cpp-python==0.3.35" in pips[-1] and h.installed == "0.3.35"


def test_a_first_cuda_install_that_does_not_offload_fails_readably(tmp_path):
    """The first install (no llama.cpp yet): a cu* wheel without GPU offload is a failure, never a
    warning in the log. MUTANT: warn instead of fail -> RED."""
    h = _LlamaHost(tmp_path, installed=None, after_install={"gpu_offload": False})
    job, _ = h.run(tmp_path)
    assert job["state"] == "failed", job
    assert "reports no GPU offload: the CUDA build did not load" in _job_text(job)


def test_a_failed_update_keeps_the_card_ready_and_says_why():
    """The card of an installed engine whose update failed stays Ready (engineState never reads a
    failed job) and shows the job's sentence. MUTANT: drop the branch -> RED."""
    from abstractgateway import console_ui
    src = console_ui.__dict__.get("CONSOLE_UI_JS") or open(console_ui.__file__, encoding="utf-8").read()
    assert 'if (job && job.state === "failed" && e.installed === true)' in src
    assert "data-engine-update-failed" in src


def test_engine_install_texts_never_say_for_this_account():
    """The gateway's service account serves every user: an engine installs for this computer.
    MUTANT: restore "for this account" in the Ollama notes -> RED."""
    from pathlib import Path
    from abstractgateway import console_ui
    root = Path(ei.__file__).parent
    for f in (root / "engines_install.py", root / "routes" / "engines.py"):
        text = f.read_text(encoding="utf-8")
        assert "this account" not in text and "your account" not in text, f
    ui = Path(console_ui.__file__).read_text(encoding="utf-8")
    cards = ui[ui.index("function engineLocationChoice"):ui.index("function engineCardMarkup")]
    assert "your account" not in cards and "only your account" not in cards
