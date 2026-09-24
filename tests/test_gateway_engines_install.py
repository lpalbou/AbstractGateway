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
        return f"{self.sha_published}  ./Ollama-darwin.zip\nffff  ./ollama-darwin.tgz\n"

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


def test_linux_ollama_asks_pkexec_on_a_desktop_and_a_terminal_when_headless(tmp_path) -> None:
    linux = ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc", gui_session=True)
    sysd = FakeSystem(tmp_path)
    sysd.whiches["pkexec"] = "/usr/bin/pkexec"
    reg = _registry(_installer(tmp_path, sysd, host=linux), tmp_path)
    snap, _ = reg.start("ollama", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "needs_admin" and job["admin_prompt"]["method"] == "pkexec"
    assert sysd.elevated() == []
    sysd.http["http://127.0.0.1:11434/api/version"] = {"version": "0.34.3"}
    reg.continue_job(job["job_id"], run_inline=True)
    assert reg.get(job["job_id"]).snapshot()["state"] == "done"
    assert sysd.elevated() == [["pkexec", "/bin/sh", "-c", ei.OLLAMA_LINUX_SCRIPT]]

    headless = ei.HostFacts(os_id="linux", arch="x86_64", libc="glibc", gui_session=False)
    sysd2 = FakeSystem(tmp_path)
    reg2 = _registry(_installer(tmp_path, sysd2, host=headless), tmp_path)
    snap, _ = reg2.start("ollama", run_inline=True)
    job = reg2.get(snap["job_id"]).snapshot()
    assert job["admin_prompt"]["method"] == "manual" and job["admin_prompt"]["command"].startswith("sudo sh -c ")
    assert job["continue_actions"] == ["recheck"]
    reg2.continue_job(job["job_id"], run_inline=True)  # nothing installed yet -> still waiting, nothing elevated
    assert reg2.get(job["job_id"]).snapshot()["state"] == "needs_admin"
    assert sysd2.elevated() == []


# ---------------------------------------------------------------------------
# llama.cpp: wheel first, tools visibly, failures readable
# ---------------------------------------------------------------------------


def test_llamacpp_installs_the_metal_wheel_without_a_compiler(tmp_path) -> None:
    sysd = FakeSystem(tmp_path)
    sysd.on(lambda a: a[:1] == ["/gw/bin/python"] and "gpu_offload" in a[-1], lambda a: (0, '__AG_VERIFY__{"version": "0.3.28", "gpu_offload": true}'))
    reg = _registry(_installer(tmp_path, sysd), tmp_path)
    snap, _ = reg.start("llamacpp", run_inline=True)
    job = reg.get(snap["job_id"]).snapshot()
    assert job["state"] == "done" and job["result"] == {"installed": True, "version": "0.3.28", "gpu_offload": True, "location": "/gw/bin/python", "method": "metal wheel"}
    pip = [c for c in sysd.calls if c[:3] == ["/fake/uv", "pip", "install"]]
    assert pip == [["/fake/uv", "pip", "install", "--python", "/gw/bin/python", "llama-cpp-python==0.3.28", "--find-links",
                    "https://abetlen.github.io/llama-cpp-python/whl/metal/llama-cpp-python/", "--only-binary", "llama-cpp-python"]]
    assert ["xcode-select", "-p"] not in sysd.calls  # no compiler question when a wheel exists


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
    assert plan.command_preview == ["/fake/uv", "pip", "install", "--python", "/gw/bin/python", "vllm", "--torch-backend=auto"]


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
