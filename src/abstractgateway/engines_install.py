"""Local engine installs a non-technical user can finish without a terminal.

`GET /api/gateway/engines` rows and `POST /api/gateway/engines/{id}/install`
jobs (contract `gateway_engines_v2` / `engine_install_job_v1`, documented in
docs/engines.md). The ORDER every installer follows:

1. USER-LEVEL FIRST. Prebuilt wheels into the gateway's own Python
   environment; vendor-signed apps into a folder this user can already write
   (`/Applications` for an admin account -- the plain drag-to-Applications
   act, no password -- else `~/Applications`). No compiler, no sudo.
2. TOOLS, VISIBLY. A step that needs a compiler (a llama.cpp source build when
   no prebuilt wheel exists) stops in `needs_tools` with the reason BEFORE it
   starts. On macOS the one action is `xcode-select --install`: Apple's own
   installer dialog, no admin rights involved; the job resumes by itself once
   the tools are there.
3. ADMIN ONLY THROUGH THE OS PROMPT, AND ONLY AFTER SAYING SO. A step that
   genuinely needs administrator rights (an app into a `/Applications` this
   user cannot write, Ollama's Linux installer) stops in `needs_admin` with
   the exact reason and command. Nothing elevated runs until a person presses
   Continue; then the gateway asks the OS (macOS: the standard password dialog
   through `osascript ... with administrator privileges`; Linux desktop:
   `pkexec`). There is no `sudo` in a hidden shell anywhere in this module, and
   `_JobContext.run_admin` is the one door every elevated command goes through.

ADR-0026: every job keeps its FULL log (`details`, and a log file on disk),
never a tail; `message` is plain language written for the person pressing the
button, and the log is behind it. Downloads report bytes/total; long steps
heartbeat at least every `HEARTBEAT_S` seconds.

This module does its own subprocess/network work on purpose: AbstractCore's
`engine_install` runs a fixed vendor command and can only report its exit
code, which is what put a 43-line CMake log and "uv exited 1" in front of a
user. Detection (installed? version? running?) is still AbstractCore's, read
through `core_config`; this module adds only what AbstractCore cannot see yet
(apps placed in `~/Applications`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ENGINES_SCHEMA = "gateway_engines_v2"
JOB_SCHEMA = "engine_install_job_v1"

STATES = ("queued", "downloading", "installing", "needs_admin", "needs_tools", "done", "failed", "cancelled")
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
PAUSED_STATES = frozenset({"needs_admin", "needs_tools"})

#: Seconds of silence after which a running step says it is still working.
HEARTBEAT_S = 3.0

# --- llama.cpp: upstream's prebuilt wheels (abetlen/llama-cpp-python) -----------
# PyPI carries only the sdist, so a plain `pip install llama-cpp-python` is a
# CMake build. Upstream publishes wheels per backend under one flat
# `--find-links` page per backend. The pins match scripts/install.sh (the
# root installer measured them): Metal 0.3.32-0.3.35 wheels fail zip CRC
# checks and uv refuses them, 0.3.28 is the newest that installs.
LLAMA_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl"
LLAMA_METAL_PIN = "0.3.28"
LLAMA_CPU_PIN = "0.3.35"

# --- Ollama -----------------------------------------------------------------------
OLLAMA_RELEASES = "https://github.com/ollama/ollama/releases"
OLLAMA_MAC_ASSET = "Ollama-darwin.zip"
#: Developer ID team that signs Ollama.app ("Infra Technologies, Inc").
OLLAMA_TEAM_ID = "3MU9H2V9Y9"
OLLAMA_BUNDLE_ID = "com.electron.ollama"
OLLAMA_LINUX_SCRIPT = "curl -fsSL https://ollama.com/install.sh | sh"
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"

# --- LM Studio ------------------------------------------------------------------------
#: Redirects to the current signed dmg on installers.lmstudio.ai.
LMSTUDIO_MAC_LATEST = "https://lmstudio.ai/download/latest/darwin/arm64"
#: Developer ID team that signs LM Studio.app ("Element Labs Inc").
LMSTUDIO_TEAM_ID = "D65G88RHWN"
LMSTUDIO_LINUX_SCRIPT = "curl -fsSL https://lmstudio.ai/install.sh | bash"
LMSTUDIO_DEFAULT_URL = "http://127.0.0.1:1234"

_USER_AGENT = "abstractgateway-engine-installer"

ENGINE_TEXT: Dict[str, Dict[str, str]] = {
    "ollama": {
        "name": "Ollama",
        "description": "A local model server with its own model library. Runs in the menu bar and starts at login.",
        "download_url": "https://ollama.com/download",
        "docs_url": "https://docs.ollama.com",
    },
    "lmstudio": {
        "name": "LM Studio",
        "description": "A desktop app to find, download and serve local models, with a local server for other apps.",
        "download_url": "https://lmstudio.ai/download",
        "docs_url": "https://lmstudio.ai/docs",
    },
    "mlx": {
        "name": "MLX (mlx-lm)",
        "description": "Apple's machine-learning framework: the fastest way to run models on an Apple Silicon Mac.",
        "download_url": "https://pypi.org/project/mlx-lm/",
        "docs_url": "https://github.com/ml-explore/mlx-lm",
    },
    "llamacpp": {
        "name": "llama.cpp",
        "description": "Runs GGUF model files inside the gateway (Metal on Apple Silicon, CPU elsewhere).",
        "download_url": "https://github.com/abetlen/llama-cpp-python",
        "docs_url": "https://llama-cpp-python.readthedocs.io",
    },
    "vllm": {
        "name": "vLLM",
        "description": "A high-throughput model server for NVIDIA GPUs on Linux.",
        "download_url": "https://docs.vllm.ai/en/latest/getting_started/installation/",
        "docs_url": "https://docs.vllm.ai",
    },
    "huggingface": {
        "name": "Hugging Face (transformers)",
        "description": "PyTorch + transformers inside the gateway, for models no other engine runs. Several GB.",
        "download_url": "https://pypi.org/project/transformers/",
        "docs_url": "https://huggingface.co/docs/transformers",
    },
}
ENGINE_IDS: Tuple[str, ...] = ("ollama", "lmstudio", "mlx", "llamacpp", "vllm", "huggingface")
SERVER_ENGINES = frozenset({"ollama", "lmstudio"})


# ---------------------------------------------------------------------------
# Typed outcomes of a step
# ---------------------------------------------------------------------------


@dataclass
class AdminPrompt:
    """One elevated act, surfaced to a person before it may run."""

    key: str
    reason: str
    command: str  # the exact shell command that runs elevated
    method: str  # osascript | pkexec | manual
    prompt_text: str = ""

    def public(self) -> Dict[str, Any]:
        label = {
            "osascript": "Continue with administrator password",
            "pkexec": "Continue with administrator password",
            "manual": "I ran it -- re-check",
        }[self.method]
        return {
            "key": self.key,
            "reason": self.reason,
            "command": self.command,
            "method": self.method,
            "button": label,
            "prompt_text": self.prompt_text or self.reason,
            "where": "the gateway host's screen" if self.method != "manual" else "a terminal on the gateway host",
        }


@dataclass
class ToolsPrompt:
    key: str
    reason: str
    tools: str
    action_kind: str  # xcode_select_install | manual
    command: str
    started: bool = False

    def public(self) -> Dict[str, Any]:
        automatic = self.action_kind == "xcode_select_install"
        return {
            "key": self.key,
            "reason": self.reason,
            "tools": self.tools,
            "action": {
                "kind": self.action_kind,
                "command": self.command,
                "available": automatic,
                "button": "Install tools" if automatic else "I installed them -- re-check",
            },
            "started": self.started,
        }


class NeedsAdmin(Exception):
    def __init__(self, prompt: AdminPrompt, message: Optional[str] = None):
        super().__init__(prompt.reason)
        self.prompt = prompt
        self.message = message


class NeedsTools(Exception):
    def __init__(self, prompt: ToolsPrompt):
        super().__init__(prompt.reason)
        self.prompt = prompt


class InstallFailed(Exception):
    def __init__(self, message: str, *, code: str = "failed"):
        super().__init__(message)
        self.message = message
        self.code = code


class Cancelled(Exception):
    pass


class PrivilegeViolation(RuntimeError):
    """An elevated command was about to run without a surfaced, approved prompt."""


class JobStateError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409, reason: str = "bad_state"):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


# ---------------------------------------------------------------------------
# Host facts
# ---------------------------------------------------------------------------


@dataclass
class HostFacts:
    os_id: str
    arch: str
    accelerator: Optional[str] = None
    macos_version: Optional[Tuple[int, int]] = None
    translated: bool = False  # an x86_64 Python under Rosetta on an Apple Silicon Mac
    libc: Optional[str] = None  # glibc | musl
    is_root: bool = False
    gui_session: bool = True
    python: str = sys.executable

    def public(self) -> Dict[str, Any]:
        return {
            "os": self.os_id,
            "arch": self.arch,
            "accelerator": self.accelerator,
            "macos_version": ".".join(str(p) for p in self.macos_version) if self.macos_version else None,
            "rosetta": self.translated,
            "libc": self.libc,
            "python": self.python,
        }


def _norm_arch(raw: str) -> str:
    raw = (raw or "").lower()
    if raw in {"arm64", "aarch64", "armv8", "arm64e"}:
        return "arm64"
    if raw in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    return raw


def detect_host(accelerator: Optional[str] = None) -> HostFacts:
    system = platform.system().lower()
    os_id = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system, system)
    facts = HostFacts(os_id=os_id, arch=_norm_arch(platform.machine()), accelerator=accelerator)
    if os_id == "darwin":
        try:
            parts = [int(p) for p in (platform.mac_ver()[0] or "0.0").split(".")[:2]]
            facts.macos_version = (parts[0], parts[1] if len(parts) > 1 else 0)
        except ValueError:
            facts.macos_version = None
        try:
            out = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=3)
            facts.translated = out.stdout.strip() == "1"
        except Exception:
            facts.translated = False
    if os_id == "linux":
        libc = platform.libc_ver()[0] or ""
        facts.libc = "glibc" if libc == "glibc" else ("musl" if not libc else libc)
        facts.gui_session = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if hasattr(os, "geteuid"):
        facts.is_root = os.geteuid() == 0
    return facts


def _current_login_name() -> str:
    """The account that owns this process, from the password database (the
    uid is the truth; an inherited USER variable can name someone else, e.g.
    under sudo). Empty when it cannot be resolved (the caller then skips the
    chown)."""
    try:
        import pwd

        return str(pwd.getpwuid(os.getuid()).pw_name or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Side effects, in one replaceable object (tests substitute a recorder)
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class System:
    """Every process launch, download and filesystem probe the installers make."""

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def run(self, argv: List[str], *, env: Optional[Dict[str, str]] = None, timeout: float = 60.0, input_text: Optional[str] = None) -> Tuple[int, str]:
        try:
            proc = subprocess.run(  # noqa: S603 - argv built from constants and paths this module chose
                argv, capture_output=True, text=True, timeout=timeout, env=env, input=input_text,
                stdin=None if input_text is not None else subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            return 127, f"{argv[0]}: not found ({exc})"
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return 124, out + f"\n{argv[0]}: timed out after {timeout:.0f} s"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def stream(
        self,
        argv: List[str],
        *,
        env: Optional[Dict[str, str]] = None,
        on_line: Callable[[str], None],
        on_quiet: Callable[[float], None],
        cancel: threading.Event,
        on_proc: Callable[[Optional[subprocess.Popen]], None] = lambda p: None,
    ) -> int:
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, bufsize=1, env=env, start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            on_line(f"{argv[0]}: not found ({exc})")
            return 127
        on_proc(proc)
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                lines.put(raw.rstrip("\n"))
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        started = last = time.monotonic()
        try:
            while True:
                if cancel.is_set():
                    _terminate(proc)
                    raise Cancelled()
                try:
                    item = lines.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last >= HEARTBEAT_S:
                        on_quiet(time.monotonic() - started)
                        last = time.monotonic()
                    continue
                if item is None:
                    break
                on_line(item)
                last = time.monotonic()
            return proc.wait()
        finally:
            on_proc(None)

    def resolve_redirect(self, url: str, timeout: float = 20.0) -> Optional[str]:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return exc.headers.get("Location")
            raise

    def head(self, url: str, timeout: float = 20.0) -> Dict[str, str]:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https constants
            return {k.lower(): v for k, v in resp.headers.items()}

    def fetch_text(self, url: str, timeout: float = 30.0) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")

    def http_json(self, url: str, timeout: float = 2.0) -> Optional[Any]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def download(
        self,
        url: str,
        dest: Path,
        *,
        on_progress: Callable[[int, Optional[int]], None],
        cancel: threading.Event,
    ) -> Dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        sha, md5 = hashlib.sha256(), hashlib.md5()  # noqa: S324 - md5 only compares with a vendor ETag
        done = 0
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or 0) or None
            final_url = resp.geturl()
            on_progress(0, total)
            while True:
                if cancel.is_set():
                    fh.close()
                    part.unlink(missing_ok=True)
                    raise Cancelled()
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                sha.update(chunk)
                md5.update(chunk)
                done += len(chunk)
                on_progress(done, total)
        if total is not None and done != total:
            part.unlink(missing_ok=True)
            raise InstallFailed(f"The download stopped early ({done} of {total} bytes). Check the connection and try again.", code="download_incomplete")
        os.replace(part, dest)
        return {"path": str(dest), "bytes": done, "sha256": sha.hexdigest(), "md5": md5.hexdigest(), "final_url": final_url}

    def spawn_detached(self, argv: List[str], *, env: Dict[str, str], log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(  # noqa: S603
                argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                start_new_session=(os.name != "nt"),
            )
        return proc.pid

    def writable_dir(self, path: Path) -> bool:
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def applescript_admin_argv(command: str, prompt_text: str) -> List[str]:
    """`osascript` argv that shows macOS's standard administrator password dialog."""

    def q(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return ["osascript", "-e", f"do shell script {q(command)} with prompt {q(prompt_text)} with administrator privileges"]


def pkexec_argv(command: str) -> List[str]:
    return ["pkexec", "/bin/sh", "-c", command]


XCODE_SELECT_INSTALL = ["xcode-select", "--install"]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class EngineJob:
    def __init__(self, engine: str, *, force: bool, location: str, log_dir: Optional[Path]):
        self.id = "eng_" + uuid.uuid4().hex[:12]
        self.engine = engine
        self.force = force
        self.location = location
        self.state = "queued"
        self.phase = "queued"
        self.percent: Optional[float] = 0.0
        self.bytes_done: Optional[int] = None
        self.bytes_total: Optional[int] = None
        self.message = "Waiting to start"
        self._base_message = self.message
        self.log: List[str] = []
        self.events: List[Dict[str, Any]] = []
        self.admin_prompt: Optional[AdminPrompt] = None
        self.tools_prompt: Optional[ToolsPrompt] = None
        self.approved_admin: set = set()
        self.tools_requested: set = set()
        self.tools_launched: set = set()
        self.cancel_event = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.started_at = _now_iso()
        self.updated_at = self.started_at
        self.finished_at: Optional[str] = None
        self._t0 = time.monotonic()
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self.command: List[str] = []
        self.attempt = 0
        self.lock = threading.RLock()
        self.log_path: Optional[Path] = (log_dir / f"{self.id}.log") if log_dir else None
        self.thread: Optional[threading.Thread] = None
        self._event("queued", "Waiting to start")

    # -- recording ---------------------------------------------------------
    def _write_log(self, line: str) -> None:
        self.log.append(line)
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def _event(self, state: str, message: str) -> None:
        stamp = _now_iso()
        self.events.append({"at": stamp, "state": state, "message": message, "percent": self.percent})
        self._write_log(f"[{stamp}] {state}: {message}")

    def logline(self, line: str) -> None:
        with self.lock:
            self._write_log(line)
            self.updated_at = _now_iso()

    def set_state(self, state: str, message: str, *, percent: Optional[float] = None) -> None:
        assert state in STATES, state
        with self.lock:
            changed = state != self.state or message != self._base_message
            self.state = state
            self._base_message = message
            self.message = message
            if percent is not None and (self.percent is None or percent >= self.percent or state in {"queued", "done"}):
                self.percent = round(float(percent), 1)  # never goes backwards within a run
            self.updated_at = _now_iso()
            if changed:
                self._event(state, message)

    def heartbeat(self, elapsed: float) -> None:
        with self.lock:
            self.message = f"{self._base_message} (still working, {int(elapsed)} s)"
            self.updated_at = _now_iso()

    def set_bytes(self, done: int, total: Optional[int], lo: float, hi: float) -> None:
        with self.lock:
            self.bytes_done, self.bytes_total = done, total
            if total:
                self.percent = max(self.percent or 0.0, round(lo + (hi - lo) * min(1.0, done / total), 1))
                self.message = f"{self._base_message} ({done / 1e6:.0f} of {total / 1e6:.0f} MB)"
            else:
                self.message = f"{self._base_message} ({done / 1e6:.0f} MB)"
            self.updated_at = _now_iso()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            legacy = {
                "queued": "queued", "downloading": "running", "installing": "running", "needs_admin": "running",
                "needs_tools": "running", "done": "completed", "failed": "failed", "cancelled": "cancelled",
            }[self.state]
            continue_actions: List[str] = []
            if self.state == "needs_admin":
                continue_actions = ["approve_admin"] if self.admin_prompt and self.admin_prompt.method != "manual" else ["recheck"]
            elif self.state == "needs_tools":
                if self.tools_prompt and self.tools_prompt.action_kind == "xcode_select_install" and not self.tools_prompt.started:
                    continue_actions = ["install_tools", "recheck"]
                else:
                    continue_actions = ["recheck"]
            return {
                "schema": JOB_SCHEMA,
                "job_id": self.id,
                "kind": "engine_install",
                "engine": self.engine,
                "engine_name": ENGINE_TEXT.get(self.engine, {}).get("name", self.engine),
                "state": self.state,
                "status": legacy,
                "host_status": legacy,
                "percent": self.percent,
                "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total,
                "message": self.message,
                "details": "\n".join(self.log),
                "log_path": str(self.log_path) if self.log_path else None,
                "admin_prompt": self.admin_prompt.public() if self.state == "needs_admin" and self.admin_prompt else None,
                "tools_prompt": self.tools_prompt.public() if self.state == "needs_tools" and self.tools_prompt else None,
                "continue_actions": continue_actions,
                "can_cancel": self.state not in TERMINAL_STATES,
                "events": [dict(e) for e in self.events],
                "command": list(self.command),
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "finished_at": self.finished_at,
                "elapsed_s": round(time.monotonic() - self._t0, 1),
                "force": self.force,
                "location": self.location,
                "dry_run": False,
                "result": dict(self.result) if self.result else None,
                "error": dict(self.error) if self.error else None,
                "cli_equivalent": f"abstractgateway engines install {self.engine} --yes",
            }


class _JobContext:
    """What an installer may do. `run_admin` is the ONLY door to elevation."""

    def __init__(self, job: EngineJob, installer: "EngineInstaller"):
        self.job = job
        self.inst = installer
        self.sys = installer.system

    # progress
    def state(self, state: str, message: str, percent: Optional[float] = None) -> None:
        self.check_cancel()
        self.job.set_state(state, message, percent=percent)

    def log(self, line: str) -> None:
        self.job.logline(line)

    def check_cancel(self) -> None:
        if self.job.cancel_event.is_set():
            raise Cancelled()

    def run(self, argv: List[str], *, timeout: float = 120.0, input_text: Optional[str] = None) -> Tuple[int, str]:
        self.check_cancel()
        self.log("$ " + " ".join(shlex.quote(a) for a in argv))
        # The command runs on a worker thread so the job keeps saying it is
        # alive (codesign / ditto / hdiutil on a 500 MB app take 10-30 s).
        box: Dict[str, Any] = {}

        def work() -> None:
            try:
                box["res"] = self.sys.run(argv, timeout=timeout, input_text=input_text)
            except BaseException as exc:  # re-raised below, on the job's thread
                box["exc"] = exc

        worker = threading.Thread(target=work, daemon=True)
        t0 = time.monotonic()
        worker.start()
        while True:
            worker.join(HEARTBEAT_S)
            if not worker.is_alive():
                break
            self.job.heartbeat(time.monotonic() - t0)
        if "exc" in box:
            raise box["exc"]
        rc, out = box["res"]
        for line in (out or "").splitlines():
            self.log(line)
        self.log(f"(exit {rc})")
        return rc, out

    def stream(self, argv: List[str], *, env: Optional[Dict[str, str]] = None, on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, str]:
        self.check_cancel()
        self.job.command = list(argv)
        self.log("$ " + " ".join(shlex.quote(a) for a in argv))
        captured: List[str] = []

        def line(text: str) -> None:
            captured.append(text)
            self.log(text)
            if on_line is not None:
                on_line(text)

        def set_proc(p: Optional[subprocess.Popen]) -> None:
            self.job.proc = p

        rc = self.sys.stream(argv, env=env, on_line=line, on_quiet=self.job.heartbeat, cancel=self.job.cancel_event, on_proc=set_proc)
        self.log(f"(exit {rc})")
        return rc, "\n".join(captured)

    def download(self, url: str, dest: Path, *, lo: float, hi: float) -> Dict[str, Any]:
        self.log(f"download {url} -> {dest}")
        return self.sys.download(url, dest, on_progress=lambda d, t: self.job.set_bytes(d, t, lo, hi), cancel=self.job.cancel_event)

    # tools & admin
    def require_tools(self, present: Callable[[], bool], prompt: ToolsPrompt) -> None:
        if present():
            return
        job = self.job
        if prompt.action_kind == "xcode_select_install" and prompt.key in job.tools_requested:
            if prompt.key not in job.tools_launched:
                job.tools_launched.add(prompt.key)
                prompt.started = True
                job.tools_prompt = prompt
                self.state("needs_tools", "Apple's installer for the command-line tools is open on the gateway host's screen. "
                           "Click Install there; this continues by itself when it finishes.")
                self.run(list(XCODE_SELECT_INSTALL), timeout=60)
            prompt.started = True
            job.tools_prompt = prompt
            deadline = time.monotonic() + self.inst.tools_wait_s
            waited = 0.0
            while time.monotonic() < deadline:
                self.check_cancel()
                if present():
                    self.log("command-line tools are now installed")
                    return
                time.sleep(self.inst.tools_poll_s)
                waited += self.inst.tools_poll_s
                job.heartbeat(waited)
            prompt.started = True
            raise NeedsTools(prompt)
        raise NeedsTools(prompt)

    def run_admin(self, prompt: AdminPrompt) -> None:
        """Run `prompt.command` with administrator rights -- only after a person approved THIS prompt."""

        job = self.job
        if prompt.key not in job.approved_admin:
            raise NeedsAdmin(prompt)
        # The approval exists only because `continue` saw this very prompt in
        # the `needs_admin` state (see EngineJobRegistry.continue_job).
        if job.admin_prompt is None or job.admin_prompt.key != prompt.key or job.admin_prompt.command != prompt.command:
            raise PrivilegeViolation(f"refusing an elevated command that was never shown: {prompt.command}")
        if prompt.method == "osascript":
            argv = applescript_admin_argv(prompt.command, prompt.prompt_text or prompt.reason)
        elif prompt.method == "pkexec":
            argv = pkexec_argv(prompt.command)
        else:
            # manual: a person ran it in a terminal; re-checking is the caller's job.
            job.approved_admin.discard(prompt.key)
            return
        self.state("installing", "Waiting for the administrator password on the gateway host's screen")
        rc, out = self.run(argv, timeout=15 * 60)
        job.approved_admin.discard(prompt.key)  # one approval, one run
        if rc != 0:
            text = out or ""
            if "-128" in text or "User canceled" in text or "cancelled" in text.lower() or rc == 126:
                raise NeedsAdmin(prompt, message="The administrator password prompt was cancelled. Press Continue to try again.")
            if "-1713" in text or "No user interaction allowed" in text or "not authorized" in text.lower():
                manual = AdminPrompt(prompt.key + ":manual", prompt.reason + " No password dialog can be shown on this host (no desktop session).",
                                     f"sudo sh -c {shlex.quote(prompt.command)}", "manual")
                raise NeedsAdmin(manual, message="The gateway host cannot show a password dialog. Run the command in a terminal on the host, then re-check.")
            raise InstallFailed(f"The administrator step failed ({prompt.reason})", code="admin_step_failed")


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


@dataclass
class InstallPlan:
    engine: str
    available: bool
    method: str  # wheel | app | script | unsupported
    target: Optional[str] = None
    needs_admin: bool = False
    admin_reason: Optional[str] = None
    needs_tools: bool = False
    tools_action: Optional[Dict[str, Any]] = None
    notes: str = ""
    command_preview: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    download_bytes: Optional[int] = None

    def public(self, url: Optional[str]) -> Dict[str, Any]:
        return {
            "available": self.available,
            "method": self.method,
            "target": self.target,
            "needs_admin": self.needs_admin,
            "admin_reason": self.admin_reason,
            "needs_tools": self.needs_tools,
            "tools_action": self.tools_action,
            "notes": self.notes,
            "steps": list(self.steps),
            "command_preview": list(self.command_preview),
            # contract-B names the older console and CLI read
            "argv": list(self.command_preview),
            "url": url,
            "requires_admin": self.needs_admin,
            "download_bytes": self.download_bytes,
        }


class EngineInstaller:
    """Plans and runs one engine install. Every path and URL is a parameter so a
    scratch prefix can be tested for real without touching the host's own."""

    def __init__(
        self,
        *,
        system: Optional[System] = None,
        host: Optional[HostFacts] = None,
        python: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        home: Optional[Path] = None,
        system_apps_dir: Path = Path("/Applications"),
        ollama_url: str = OLLAMA_DEFAULT_URL,
        lmstudio_url: str = LMSTUDIO_DEFAULT_URL,
        start_mode: str = "app",  # app | serve (serve: run the bundled `ollama serve`, no GUI)
        llama_index: str = LLAMA_WHEEL_INDEX,
        start_timeout_s: float = 60.0,
        tools_wait_s: float = 45 * 60.0,
        tools_poll_s: float = 5.0,
        core_rows: Optional[Dict[str, Dict[str, Any]]] = None,
        legacy_argv: Optional[Callable[[str], List[str]]] = None,
    ):
        self.system = system or System()
        self.host = host or detect_host()
        self.python = python or self.host.python
        self.cache_dir = Path(cache_dir) if cache_dir else _default_engines_cache_dir()
        self.home = Path(home) if home else Path(os.path.expanduser("~"))
        self.system_apps_dir = Path(system_apps_dir)
        self.ollama_url = ollama_url.rstrip("/")
        self.lmstudio_url = lmstudio_url.rstrip("/")
        self.start_mode = start_mode
        self.llama_index = llama_index.rstrip("/")
        self.start_timeout_s = start_timeout_s
        self.tools_wait_s = tools_wait_s
        self.tools_poll_s = tools_poll_s
        self.core_rows = core_rows or {}
        self.legacy_argv = legacy_argv
        self.last_spawned_pid: Optional[int] = None

    # -- support ------------------------------------------------------------
    def support(self, eid: str) -> Tuple[bool, Optional[str]]:
        h = self.host
        mac_arm = h.os_id == "darwin" and h.arch == "arm64"
        if eid == "mlx":
            if h.os_id != "darwin":
                return False, "MLX runs only on Apple Silicon Macs."
            if h.translated:
                return False, "This gateway's Python runs under Rosetta (x86_64); MLX needs a native Apple Silicon Python."
            if h.arch != "arm64":
                return False, "MLX runs only on Apple Silicon Macs; this is an Intel Mac."
            if h.macos_version and h.macos_version < (14, 0):
                return False, "MLX needs macOS 14 (Sonoma) or later."
            return True, None
        if eid == "lmstudio":
            if h.os_id == "darwin" and not mac_arm:
                return False, "LM Studio for Mac needs Apple Silicon (M1 or later); this is an Intel Mac."
            if h.os_id == "darwin" and h.macos_version and h.macos_version < (14, 0):
                return False, "LM Studio needs macOS 14 (Sonoma) or later."
            if h.os_id in {"darwin", "linux", "windows"}:
                return True, None
            return False, f"LM Studio has no build for {h.os_id}."
        if eid == "vllm":
            if h.os_id == "linux" and h.accelerator == "cuda":
                return True, None
            if h.os_id == "darwin":
                return False, "vLLM does not run on macOS. Use MLX, llama.cpp, Ollama or LM Studio here, or point the gateway at a vLLM server on a Linux machine with an NVIDIA GPU."
            return False, "vLLM needs Linux with an NVIDIA GPU (CUDA). Point the gateway at a remote vLLM server instead."
        if eid == "ollama":
            if h.os_id == "darwin" and h.macos_version and h.macos_version < (14, 0):
                return False, "Ollama for Mac needs macOS 14 (Sonoma) or later."
            if h.os_id in {"darwin", "linux", "windows"}:
                return True, None
            return False, f"Ollama has no build for {h.os_id}."
        if eid in {"llamacpp", "huggingface"}:
            if h.os_id in {"darwin", "linux", "windows"}:
                return True, None
            return False, f"no supported build for {h.os_id}"
        return False, f"unknown engine {eid!r}"

    # -- app locations ---------------------------------------------------------
    def user_apps_dir(self) -> Path:
        return self.home / "Applications"

    def app_candidates(self, app_name: str) -> List[Path]:
        return [self.system_apps_dir / app_name, self.user_apps_dir() / app_name]

    def find_app(self, app_name: str) -> Optional[Path]:
        for path in self.app_candidates(app_name):
            if (path / "Contents" / "Info.plist").exists():
                return path
        return None

    def app_target(self, location: str) -> Tuple[Path, bool]:
        """(apps dir, needs admin). `auto` = /Applications when this user can write it, else ~/Applications."""

        if location == "user":
            return self.user_apps_dir(), False
        writable = self.system.writable_dir(self.system_apps_dir)
        if location == "system":
            return self.system_apps_dir, not writable
        return (self.system_apps_dir, False) if writable else (self.user_apps_dir(), False)

    # -- python env helpers ------------------------------------------------------
    def uv(self) -> Optional[str]:
        found = self.system.which("uv")
        if found:
            return found
        for cand in (self.home / ".local" / "bin" / "uv", self.home / ".cargo" / "bin" / "uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
            if cand.exists():
                return str(cand)
        return None

    def pip_prefix(self) -> List[str]:
        uv = self.uv()
        if uv:
            return [uv, "pip", "install", "--python", self.python]
        return [self.python, "-m", "pip", "install", "--disable-pip-version-check"]

    def _llama_wheel(self) -> Optional[Tuple[str, str, str]]:
        """(pin, backend, find-links URL) of the prebuilt wheel for this host, or None."""

        h = self.host
        if h.os_id == "darwin" and h.arch == "arm64" and not h.translated:
            return LLAMA_METAL_PIN, "metal", f"{self.llama_index}/metal/llama-cpp-python/"
        if h.os_id == "linux" and h.arch in {"x86_64", "arm64"}:
            return LLAMA_CPU_PIN, "cpu", f"{self.llama_index}/cpu/llama-cpp-python/"
        if h.os_id == "windows" and h.arch == "x86_64":
            return LLAMA_CPU_PIN, "cpu", f"{self.llama_index}/cpu/llama-cpp-python/"
        return None

    def _llama_wheel_argv(self, pin: str, links: str) -> List[str]:
        # `--only-binary` (uv pip and pip alike): the PyPI sdist is never built on this path.
        return [*self.pip_prefix(), f"llama-cpp-python=={pin}", "--find-links", links, "--only-binary", "llama-cpp-python"]

    def _compiler_present(self) -> bool:
        if self.host.os_id == "darwin":
            # `xcode-select -p` never pops Apple's dialog (unlike /usr/bin/clang on a machine without the tools).
            rc, out = self.system.run(["xcode-select", "-p"], timeout=10)
            return rc == 0 and bool(out.strip()) and Path(out.strip().splitlines()[0]).exists()
        if self.host.os_id == "linux":
            return bool((self.system.which("cc") or self.system.which("gcc")) and (self.system.which("c++") or self.system.which("g++")))
        return True  # windows: not planned here (see legacy path)

    def _tools_prompt(self, reason: str) -> ToolsPrompt:
        if self.host.os_id == "darwin":
            return ToolsPrompt("xcode-clt", reason, "Apple command-line developer tools", "xcode_select_install", "xcode-select --install")
        return ToolsPrompt("c-compiler", reason, "a C/C++ compiler", "manual",
                           "sudo apt-get install -y build-essential   # Fedora: sudo dnf install gcc gcc-c++ make")

    # -- plans ----------------------------------------------------------------------------
    def plan(self, eid: str, *, location: str = "auto") -> InstallPlan:
        supported, reason = self.support(eid)
        if not supported:
            return InstallPlan(eid, False, "unsupported", notes=reason or "not supported on this machine")
        h = self.host
        if h.os_id == "windows":
            argv = self.legacy_argv(eid) if self.legacy_argv else []
            return InstallPlan(eid, bool(argv), "script" if eid in SERVER_ENGINES else "wheel", notes="Windows: runs the vendor command AbstractCore plans (unchanged).", command_preview=argv, steps=["run the vendor command"])
        if eid == "llamacpp":
            wheel = self._llama_wheel()
            if wheel:
                pin, backend, links = wheel
                return InstallPlan(
                    eid, True, "wheel", target=self.python,
                    notes=f"Installs the prebuilt llama.cpp {backend} wheel {pin} into the gateway's Python; no compiler, no admin.",
                    command_preview=self._llama_wheel_argv(pin, links),
                    steps=["download the prebuilt wheel", "install into the gateway's Python", "check that it loads"],
                )
            present = self._compiler_present()
            reason_text = (f"No prebuilt llama.cpp wheel exists for {h.os_id} {h.arch}; building from source needs "
                           + ("the Apple command-line tools." if h.os_id == "darwin" else "a C/C++ compiler."))
            prompt = self._tools_prompt(reason_text)
            return InstallPlan(
                eid, True, "wheel", target=self.python, needs_tools=not present,
                tools_action=prompt.public()["action"] if not present else None,
                notes="Builds llama.cpp from source (5-15 minutes)." + ("" if present else " " + reason_text),
                command_preview=[*self.pip_prefix(), "llama-cpp-python"],
                steps=["check the compiler", "build llama.cpp from source", "check that it loads"],
            )
        if eid == "mlx":
            return InstallPlan(eid, True, "wheel", target=self.python, notes="Installs mlx and mlx-lm (prebuilt wheels) into the gateway's Python; no admin.",
                               command_preview=self._mlx_argv(), steps=["install mlx-lm", "check that MLX sees the GPU"])
        if eid == "huggingface":
            return InstallPlan(eid, True, "wheel", target=self.python, notes="Installs transformers, torch and huggingface_hub into the gateway's Python; several GB, no admin.",
                               command_preview=[*self.pip_prefix(), "abstractcore[huggingface]==<installed>"], steps=["install", "check that it loads"])
        if eid == "vllm":
            base = self.pip_prefix()
            extra = ["--torch-backend=auto"] if base[1:3] == ["pip", "install"] else []
            return InstallPlan(eid, True, "wheel", target=self.python, notes="Installs vLLM (PyTorch + CUDA wheels, several GB) into the gateway's Python; no admin.",
                               command_preview=[*base, "vllm", *extra], steps=["install", "check that it loads"])
        if eid == "ollama":
            if h.os_id == "darwin":
                apps, admin = self.app_target(location)
                return InstallPlan(
                    eid, True, "app", target=str(apps / "Ollama.app"), needs_admin=admin,
                    admin_reason=(f"{apps} is not writable by this account; placing Ollama.app there needs an administrator." if admin else None),
                    notes=f"Downloads Ollama's signed macOS app from its official GitHub release, checks its SHA-256 and signature, places it in {apps} and starts it.",
                    command_preview=[f"download {OLLAMA_RELEASES}/latest/download/{OLLAMA_MAC_ASSET}", f"place {apps}/Ollama.app", "open -j -a Ollama.app --args hidden"],
                    steps=["download (about 200 MB)", "verify checksum and signature", f"place in {apps}", "start the server"],
                    download_bytes=None,
                )
            if h.is_root:
                return InstallPlan(eid, True, "script", notes="Runs Ollama's official Linux installer (installs to /usr/local and a systemd service).",
                                   command_preview=["sh", "-c", OLLAMA_LINUX_SCRIPT], steps=["run the vendor installer", "check the server"])
            return InstallPlan(
                eid, True, "script", needs_admin=True,
                admin_reason="Ollama's Linux installer writes /usr/local and creates the `ollama` system service; it needs root.",
                notes="Runs Ollama's official Linux installer after an administrator approves it.",
                command_preview=["sh", "-c", OLLAMA_LINUX_SCRIPT], steps=["administrator approval", "run the vendor installer", "check the server"],
            )
        if eid == "lmstudio":
            if h.os_id == "darwin":
                apps, admin = self.app_target(location)
                return InstallPlan(
                    eid, True, "app", target=str(apps / "LM Studio.app"), needs_admin=admin,
                    admin_reason=(f"{apps} is not writable by this account; placing LM Studio.app there needs an administrator." if admin else None),
                    notes=f"Downloads LM Studio's signed disk image from lmstudio.ai, checks its signature, copies the app to {apps} and starts its local server.",
                    command_preview=[f"download {LMSTUDIO_MAC_LATEST}", "hdiutil attach -readonly -nobrowse", f"ditto 'LM Studio.app' {apps}", "hdiutil detach", "lms server start"],
                    steps=["download (about 570 MB)", "verify signature", f"copy to {apps}", "start the local server"],
                )
            return InstallPlan(eid, True, "script", notes="Runs LM Studio's official installer: the headless daemon (llmster) and the `lms` CLI under ~/.lmstudio; no admin.",
                               command_preview=["bash", "-c", LMSTUDIO_LINUX_SCRIPT], steps=["run the vendor installer", "start the local server"])
        return InstallPlan(eid, False, "unsupported", notes=f"unknown engine {eid}")

    def _mlx_argv(self) -> List[str]:
        return [*self.pip_prefix(), "mlx-lm", "--only-binary", "mlx"]

    # -- detection this module adds --------------------------------------------------
    def app_version(self, app: Path) -> Optional[str]:
        try:
            with open(app / "Contents" / "Info.plist", "rb") as fh:
                v = plistlib.load(fh).get("CFBundleShortVersionString")
            return str(v) if v else None
        except Exception:
            return None

    def lms_cli(self) -> Optional[str]:
        found = self.system.which("lms")
        if found:
            return found
        cand = self.home / ".lmstudio" / "bin" / "lms"
        return str(cand) if cand.exists() else None

    # -- run ------------------------------------------------------------------------------
    def install(self, eid: str, ctx: _JobContext) -> Dict[str, Any]:
        supported, reason = self.support(eid)
        if not supported:
            raise InstallFailed(reason or "not supported on this machine", code="unsupported")
        if self.host.os_id == "windows":
            return self._install_legacy(eid, ctx)
        probe = _ALREADY_PROBES.get(eid)
        if probe and not ctx.job.force:
            rc, out = self.system.run([self.python, "-c", probe], timeout=120)
            for line in (out or "").splitlines():
                if rc == 0 and line.startswith("__AG_VERIFY__"):
                    info = json.loads(line[len("__AG_VERIFY__"):])
                    ctx.log(f"already importable in {self.python}: {info}")
                    return {"installed": True, "already_installed": True, "location": self.python, **info}
        fn = {
            "llamacpp": self._install_llamacpp,
            "mlx": self._install_mlx,
            "huggingface": self._install_huggingface,
            "vllm": self._install_vllm,
            "ollama": self._install_ollama,
            "lmstudio": self._install_lmstudio,
        }[eid]
        return fn(ctx)

    # wheels -------------------------------------------------------------------------------
    def _abstract_constraints(self, ctx: _JobContext) -> List[str]:
        """Keep every `abstract*` package the gateway runs exactly as it is."""

        rc, out = self.system.run([self.python, "-c", "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions() if (d.metadata['Name'] or '').lower().startswith('abstract')}))"], timeout=30)
        if rc != 0:
            return []
        try:
            pins = json.loads(out.strip().splitlines()[-1])
        except Exception:
            return []
        if not pins:
            return []
        path = self.cache_dir / f"constraints-{ctx.job.id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{name}=={ver}\n" for name, ver in sorted(pins.items())), encoding="utf-8")
        ctx.log(f"keeping the gateway's own packages as they are: {', '.join(f'{k}=={v}' for k, v in sorted(pins.items()))}")
        return ["--constraint", str(path)]

    def _pip_phases(self, ctx: _JobContext, lo: float, hi: float) -> Callable[[str], None]:
        def on_line(text: str) -> None:
            t = text.strip()
            span = hi - lo
            if t.startswith("Resolved"):
                ctx.job.set_state("downloading", "Resolving packages", percent=lo + 0.15 * span)
            elif t.startswith("Downloading") or t.startswith("Collecting"):
                ctx.job.set_state("downloading", "Downloading packages", percent=lo + 0.35 * span)
            elif t.startswith("Building") or "Building wheel" in t:
                ctx.job.set_state("installing", "Building from source (this takes several minutes)", percent=lo + 0.5 * span)
            elif t.startswith("Prepared") or t.startswith("Installing collected"):
                ctx.job.set_state("installing", "Installing packages", percent=lo + 0.8 * span)
            elif t.startswith("Installed") or t.startswith("Successfully installed"):
                ctx.job.set_state("installing", "Packages installed", percent=lo + 0.95 * span)
        return on_line

    def _pip(self, ctx: _JobContext, argv: List[str], *, lo: float, hi: float, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        full_env = dict(os.environ)
        full_env.update(env or {})
        full_env.setdefault("UV_NO_PROGRESS", "1")
        return ctx.stream(argv, env=full_env, on_line=self._pip_phases(ctx, lo, hi))

    def _verify_import(self, ctx: _JobContext, code: str, what: str) -> Dict[str, Any]:
        ctx.state("installing", f"Checking that {what} loads", percent=96)
        rc, out = ctx.run([self.python, "-c", code.replace("print(json.dumps(", "print('__AG_VERIFY__' + json.dumps(")], timeout=180)
        if rc != 0:
            raise InstallFailed(f"{what} installed, but it does not load in the gateway's Python. The log shows why.", code="verify_failed")
        for line in (out or "").splitlines():
            if line.startswith("__AG_VERIFY__"):
                try:
                    return json.loads(line[len("__AG_VERIFY__"):])
                except Exception:
                    break
        return {"output": (out or "").strip()}

    def _install_llamacpp(self, ctx: _JobContext) -> Dict[str, Any]:
        h = self.host
        wheel = self._llama_wheel()
        constraints = self._abstract_constraints(ctx)
        wheel_error = ""
        if wheel:
            pin, backend, links = wheel
            ctx.state("downloading", f"Installing the prebuilt llama.cpp {backend} wheel {pin}", percent=5)
            rc, out = self._pip(ctx, [*self._llama_wheel_argv(pin, links), *constraints], lo=5, hi=90)
            if rc == 0:
                info = self._verify_import(ctx, "import json, llama_cpp; print(json.dumps({'version': llama_cpp.__version__, 'gpu_offload': bool(llama_cpp.llama_supports_gpu_offload())}))", "llama.cpp")
                if h.os_id == "darwin" and h.arch == "arm64" and not info.get("gpu_offload"):
                    ctx.log("warning: llama.cpp loaded but reports no GPU offload (Metal)")
                importlib.invalidate_caches()
                return {"installed": True, "version": info.get("version"), "gpu_offload": info.get("gpu_offload"), "location": self.python, "method": f"{backend} wheel"}
            wheel_error = _last_error_line(out)
            ctx.log(f"the prebuilt wheel did not install: {wheel_error}")
            why = f"The prebuilt llama.cpp wheel did not install ({wheel_error})"
        else:
            why = f"No prebuilt llama.cpp wheel exists for {h.os_id} {h.arch}" + (" under Rosetta" if h.translated else "")
        # Source build: say so, and check the compiler BEFORE starting it.
        tools_reason = why + "; building from source needs " + ("the Apple command-line tools." if h.os_id == "darwin" else "a C/C++ compiler.")
        ctx.require_tools(self._compiler_present, self._tools_prompt(tools_reason))
        ctx.state("installing", why + ". Building llama.cpp from source instead (5-15 minutes).", percent=20)
        env = {"CMAKE_ARGS": "-DGGML_METAL=on"} if h.os_id == "darwin" and h.arch == "arm64" else {}
        rc, out = self._pip(ctx, [*self.pip_prefix(), "llama-cpp-python", *constraints], lo=20, hi=92, env=env)
        if rc != 0:
            raise InstallFailed(
                why + ", and building it from source failed: " + (_last_error_line(out) or "see the log") + ". The full build log is in the details.",
                code="build_failed",
            )
        info = self._verify_import(ctx, "import json, llama_cpp; print(json.dumps({'version': llama_cpp.__version__, 'gpu_offload': bool(llama_cpp.llama_supports_gpu_offload())}))", "llama.cpp")
        importlib.invalidate_caches()
        return {"installed": True, "version": info.get("version"), "gpu_offload": info.get("gpu_offload"), "location": self.python, "method": "source build"}

    def _install_mlx(self, ctx: _JobContext) -> Dict[str, Any]:
        ctx.state("downloading", "Installing MLX (prebuilt wheels)", percent=5)
        rc, out = self._pip(ctx, [*self._mlx_argv(), *self._abstract_constraints(ctx)], lo=5, hi=90)
        if rc != 0:
            raise InstallFailed("MLX could not be installed: " + (_last_error_line(out) or "see the log"), code="pip_failed")
        info = self._verify_import(ctx, "import json, mlx.core as mx, importlib.metadata as m; print(json.dumps({'version': m.version('mlx'), 'mlx_lm': m.version('mlx-lm'), 'device': str(mx.default_device())}))", "MLX")
        importlib.invalidate_caches()
        return {"installed": True, "version": info.get("version"), "mlx_lm_version": info.get("mlx_lm"), "device": info.get("device"), "location": self.python, "method": "wheel"}

    def _install_huggingface(self, ctx: _JobContext) -> Dict[str, Any]:
        rc, out = self.system.run([self.python, "-c", "import importlib.metadata as m; print(m.version('abstractcore'))"], timeout=30)
        spec = f"abstractcore[huggingface]=={out.strip().splitlines()[-1]}" if rc == 0 and out.strip() else "abstractcore[huggingface]"
        ctx.state("downloading", "Installing transformers and PyTorch (several GB)", percent=5)
        rc, out = self._pip(ctx, [*self.pip_prefix(), spec, *self._abstract_constraints(ctx)], lo=5, hi=90)
        if rc != 0:
            raise InstallFailed("The Hugging Face stack could not be installed: " + (_last_error_line(out) or "see the log"), code="pip_failed")
        info = self._verify_import(ctx, "import json, importlib.metadata as m; import transformers; print(json.dumps({'version': m.version('transformers')}))", "transformers")
        importlib.invalidate_caches()
        return {"installed": True, "version": info.get("version"), "location": self.python, "method": "wheel"}

    def _install_vllm(self, ctx: _JobContext) -> Dict[str, Any]:
        ctx.state("downloading", "Installing vLLM (PyTorch + CUDA wheels, several GB)", percent=5)
        rc, out = self._pip(ctx, [*self.plan("vllm").command_preview, *self._abstract_constraints(ctx)], lo=5, hi=90)
        if rc != 0:
            raise InstallFailed("vLLM could not be installed: " + (_last_error_line(out) or "see the log"), code="pip_failed")
        info = self._verify_import(ctx, "import json, importlib.metadata as m; import vllm; print(json.dumps({'version': m.version('vllm')}))", "vLLM")
        importlib.invalidate_caches()
        return {"installed": True, "version": info.get("version"), "location": self.python, "method": "wheel"}

    def _install_legacy(self, eid: str, ctx: _JobContext) -> Dict[str, Any]:
        argv = self.legacy_argv(eid) if self.legacy_argv else []
        if not argv:
            raise InstallFailed("No install command for this engine on this host.", code="no_plan")
        ctx.state("installing", f"Running the vendor installer for {ENGINE_TEXT[eid]['name']}", percent=10)
        rc, out = ctx.stream(argv)
        if rc != 0:
            raise InstallFailed(f"The {ENGINE_TEXT[eid]['name']} installer failed: " + (_last_error_line(out) or "see the log"), code="installer_failed")
        return {"installed": True, "location": None, "method": "vendor command"}

    # apps: shared -------------------------------------------------------------------------
    def _verify_signature(self, ctx: _JobContext, app: Path, team_id: str, vendor: str) -> Dict[str, Any]:
        ctx.state("installing", f"Checking {vendor}'s signature", percent=74)
        rc, out = ctx.run(["codesign", "--verify", "--deep", "--strict", str(app)], timeout=300)
        if rc != 0:
            raise InstallFailed(f"The downloaded {app.name} failed Apple's code-signature check; it was not installed.", code="signature_invalid")
        rc, out = ctx.run(["codesign", "-dv", "--verbose=2", str(app)], timeout=60)
        m = re.search(r"TeamIdentifier=(\S+)", out or "")
        if not m or m.group(1) != team_id:
            raise InstallFailed(
                f"The downloaded {app.name} is signed by team {m.group(1) if m else 'unknown'}, not {vendor} ({team_id}); it was not installed.",
                code="signature_team_mismatch",
            )
        rc_gk, out_gk = ctx.run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)], timeout=120)
        return {"team_id": team_id, "gatekeeper": "accepted" if rc_gk == 0 else f"not assessed ({_last_error_line(out_gk)})"}

    def _place_app(self, ctx: _JobContext, staged: Path, apps_dir: Path, needs_admin: bool, vendor: str) -> Path:
        dest = apps_dir / staged.name
        if needs_admin:
            user = _current_login_name()
            cmd = f"/bin/mkdir -p {shlex.quote(str(apps_dir))} && /bin/rm -rf {shlex.quote(str(dest))} && /usr/bin/ditto {shlex.quote(str(staged))} {shlex.quote(str(dest))}"
            if user:
                cmd += f" && /usr/sbin/chown -R {shlex.quote(user)}:admin {shlex.quote(str(dest))}"
            prompt = AdminPrompt(
                key=f"place:{dest}",
                reason=f"Copying {staged.name} into {apps_dir}, which this account cannot write, needs an administrator.",
                command=cmd,
                method="osascript",
                prompt_text=f"AbstractGateway wants to install {vendor} in {apps_dir}.",
            )
            ctx.state("installing", f"Placing {staged.name} in {apps_dir} (administrator)", percent=85)
            ctx.run_admin(prompt)
        else:
            apps_dir.mkdir(parents=True, exist_ok=True)
            ctx.state("installing", f"Placing {staged.name} in {apps_dir}", percent=85)
            aside = None
            if dest.exists():
                aside = dest.with_name(dest.name + f".replaced-{int(time.time())}")
                os.rename(dest, aside)
            rc, _ = ctx.run(["ditto", str(staged), str(dest)], timeout=900)
            if rc != 0:
                if aside is not None and not dest.exists():
                    os.rename(aside, dest)
                raise InstallFailed(f"Copying {staged.name} to {apps_dir} failed; the log shows why.", code="place_failed")
            if aside is not None:
                shutil.rmtree(aside, ignore_errors=True)
        if not (dest / "Contents" / "Info.plist").exists():
            raise InstallFailed(f"{dest} is not there after the copy.", code="place_failed")
        return dest

    def _wait_http(self, ctx: Optional[_JobContext], url: str, what: str) -> Optional[Any]:
        deadline = time.monotonic() + self.start_timeout_s
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            if ctx is not None:
                ctx.check_cancel()
            got = self.system.http_json(url, timeout=2.0)
            if got is not None:
                return got
            if ctx is not None:
                ctx.job.heartbeat(time.monotonic() - t0)
            time.sleep(1.0)
        return None

    # Ollama -------------------------------------------------------------------------------
    def _ollama_release(self, ctx: _JobContext) -> Tuple[str, str, Optional[str]]:
        """(tag, zip url, expected sha256) from Ollama's official GitHub release."""

        loc = ""
        try:
            loc = self.system.resolve_redirect(f"{OLLAMA_RELEASES}/latest") or ""
        except Exception as exc:
            ctx.log(f"could not resolve the latest Ollama release: {exc}")
        m = re.search(r"/tag/(v[\w.\-]+)", loc)
        if not m:
            raise InstallFailed("Could not find the current Ollama release on GitHub. Check the internet connection and try again.", code="release_lookup_failed")
        tag = m.group(1)
        base = f"{OLLAMA_RELEASES}/download/{tag}"
        expected: Optional[str] = None
        try:
            sums = self.system.fetch_text(f"{base}/sha256sum.txt")
            for line in sums.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("./") == OLLAMA_MAC_ASSET:
                    expected = parts[0].lower()
        except Exception as exc:
            ctx.log(f"sha256sum.txt unavailable: {exc}")
        if not expected:
            raise InstallFailed(f"Ollama {tag} publishes no checksum for {OLLAMA_MAC_ASSET}; not installing an unverified download.", code="checksum_unavailable")
        return tag, f"{base}/{OLLAMA_MAC_ASSET}", expected

    def _install_ollama(self, ctx: _JobContext) -> Dict[str, Any]:
        if self.host.os_id == "linux":
            return self._install_ollama_linux(ctx)
        job = ctx.job
        existing = self.find_app("Ollama.app")
        if existing and not job.force:
            ctx.log(f"Ollama.app already at {existing}")
            status = self.start_ollama(ctx)
            return {"installed": True, "already_installed": True, "location": str(existing), "version": self.app_version(existing), **status}
        apps_dir, needs_admin = self.app_target(job.location)
        ctx.state("downloading", "Finding the current Ollama release", percent=1)
        tag, url, expected = self._ollama_release(ctx)
        dest = self.cache_dir / "downloads" / f"Ollama-darwin-{tag}.zip"
        if dest.exists() and sha256_file(dest) == expected:
            ctx.log(f"reusing the verified download {dest}")
            job.set_bytes(dest.stat().st_size, dest.stat().st_size, 2, 70)
        else:
            ctx.state("downloading", f"Downloading Ollama {tag}", percent=2)
            got = ctx.download(url, dest, lo=2, hi=70)
            if got["sha256"] != expected:
                dest.unlink(missing_ok=True)
                raise InstallFailed(f"The Ollama download does not match its published SHA-256; it was deleted and not installed.", code="checksum_mismatch")
            ctx.log(f"sha256 verified: {expected}")
        ctx.state("installing", "Unpacking Ollama.app", percent=72)
        staging = self.cache_dir / "staging" / job.id
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        rc, _ = ctx.run(["ditto", "-x", "-k", str(dest), str(staging)], timeout=600)
        app = staging / "Ollama.app"
        if rc != 0 or not app.exists():
            raise InstallFailed("The Ollama download could not be unpacked; the log shows why.", code="unpack_failed")
        signature = self._verify_signature(ctx, app, OLLAMA_TEAM_ID, "Ollama")
        placed = self._place_app(ctx, app, apps_dir, needs_admin, "Ollama")
        shutil.rmtree(staging, ignore_errors=True)
        ctx.state("installing", "Starting Ollama", percent=92)
        status = self.start_ollama(ctx)
        return {"installed": True, "location": str(placed), "version": self.app_version(placed), "release": tag, "sha256": expected, "signature": signature, **status}

    def _install_ollama_linux(self, ctx: _JobContext) -> Dict[str, Any]:
        if self.system.which("ollama") and not ctx.job.force:
            return {"installed": True, "already_installed": True, **self.start_ollama(ctx)}
        if self.host.is_root:
            ctx.state("installing", "Running Ollama's official Linux installer", percent=10)
            rc, out = ctx.stream(["sh", "-c", OLLAMA_LINUX_SCRIPT])
            if rc != 0:
                raise InstallFailed("Ollama's installer failed: " + (_last_error_line(out) or "see the log"), code="installer_failed")
        else:
            prompt = AdminPrompt(
                key="ollama-linux-script",
                reason="Ollama's Linux installer writes /usr/local and creates the `ollama` system service (systemd); it needs root.",
                command=OLLAMA_LINUX_SCRIPT,
                method="pkexec" if (self.system.which("pkexec") and self.host.gui_session) else "manual",
                prompt_text="AbstractGateway wants to run Ollama's official installer.",
            )
            if prompt.method == "manual":
                prompt.command = f"sudo sh -c {shlex.quote(OLLAMA_LINUX_SCRIPT)}"
                if prompt.key in ctx.job.approved_admin:
                    ctx.job.approved_admin.discard(prompt.key)
                    if not self.system.which("ollama") and self.system.http_json(self.ollama_url + "/api/version") is None:
                        raise NeedsAdmin(prompt, message="Ollama is not installed yet. Run the command in a terminal on the gateway host, then re-check.")
                else:
                    raise NeedsAdmin(prompt)
            else:
                ctx.state("installing", "Running Ollama's official Linux installer (administrator)", percent=10)
                ctx.run_admin(prompt)
        got = self._wait_http(ctx, self.ollama_url + "/api/version", "Ollama")
        return {"installed": True, "running": got is not None, "server_version": (got or {}).get("version"), "base_url": self.ollama_url}

    def start_ollama(self, ctx: Optional[_JobContext] = None) -> Dict[str, Any]:
        got = self.system.http_json(self.ollama_url + "/api/version")
        if got is not None:
            return {"running": True, "server_version": got.get("version"), "base_url": self.ollama_url, "started": False}
        app = self.find_app("Ollama.app") if self.host.os_id == "darwin" else None
        log = (lambda s: ctx.log(s)) if ctx else (lambda s: None)
        if app is not None and self.start_mode == "app" and self.ollama_url == OLLAMA_DEFAULT_URL:
            argv = ["open", "-j", "-g", "-a", str(app), "--args", "hidden"]
            if not str(app).startswith(str(self.system_apps_dir) + "/"):
                # Outside /Applications the app would ask to move itself there; `--fast-startup` skips that prompt.
                argv.append("--fast-startup")
            rc, out = (ctx.run(argv, timeout=30) if ctx else self.system.run(argv, timeout=30))
            if rc != 0:
                log("`open` could not start the app (no desktop session?); starting the bundled server instead")
                self._spawn_ollama_serve(app, log)
        elif app is not None:
            self._spawn_ollama_serve(app, log)
        elif self.system.which("ollama"):
            self._spawn_ollama_serve(None, log)
        else:
            return {"running": False, "base_url": self.ollama_url, "started": False, "message": "Ollama is not installed"}
        got = self._wait_http(ctx, self.ollama_url + "/api/version", "Ollama")
        if got is None:
            raise InstallFailed(f"Ollama was installed but its server did not answer at {self.ollama_url} within {int(self.start_timeout_s)} s.", code="start_failed")
        return {"running": True, "server_version": got.get("version"), "base_url": self.ollama_url, "started": True}

    def _spawn_ollama_serve(self, app: Optional[Path], log: Callable[[str], None]) -> None:
        binary = str(app / "Contents" / "Resources" / "ollama") if app else (self.system.which("ollama") or "ollama")
        env = dict(os.environ)
        env["OLLAMA_HOST"] = urllib.parse.urlparse(self.ollama_url).netloc
        env["HOME"] = str(self.home)
        log_path = self.cache_dir / "logs" / "ollama-serve.log"
        pid = self.system.spawn_detached([binary, "serve"], env=env, log_path=log_path)
        log(f"started `{binary} serve` (pid {pid}, OLLAMA_HOST={env['OLLAMA_HOST']}, log {log_path})")
        self.last_spawned_pid = pid

    def stop_ollama(self) -> Dict[str, Any]:
        if self.last_spawned_pid:
            try:
                os.kill(self.last_spawned_pid, signal.SIGTERM)
                out = f"stopped pid {self.last_spawned_pid}"
            except OSError as exc:
                out = f"pid {self.last_spawned_pid}: {exc}"
            self.last_spawned_pid = None
        elif self.host.os_id == "darwin" and self.start_mode == "app":
            # Not elevated: asks the app to quit, as its menu's Quit does.
            rc, out = self.system.run(["osascript", "-e", f'tell application id "{OLLAMA_BUNDLE_ID}" to quit'], timeout=30)
        else:
            return {"running": self.system.http_json(self.ollama_url + "/api/version") is not None, "base_url": self.ollama_url,
                    "message": "This Ollama runs as a system service; stop it with the host's service manager (systemctl stop ollama)."}
        t0 = time.monotonic()
        while time.monotonic() - t0 < 15:
            if self.system.http_json(self.ollama_url + "/api/version") is None:
                return {"running": False, "base_url": self.ollama_url, "output": out}
            time.sleep(0.5)
        return {"running": True, "base_url": self.ollama_url, "output": out, "message": "Ollama is still answering"}

    # LM Studio ------------------------------------------------------------------------
    def _install_lmstudio(self, ctx: _JobContext) -> Dict[str, Any]:
        if self.host.os_id == "linux":
            if self.lms_cli() and not ctx.job.force:
                return {"installed": True, "already_installed": True, "cli": self.lms_cli(), **self.start_lmstudio(ctx)}
            ctx.state("installing", "Running LM Studio's official installer (headless daemon and lms CLI)", percent=10)
            rc, out = ctx.stream(["bash", "-c", LMSTUDIO_LINUX_SCRIPT])
            if rc != 0:
                raise InstallFailed("LM Studio's installer failed: " + (_last_error_line(out) or "see the log"), code="installer_failed")
            return {"installed": True, "cli": self.lms_cli(), **self.start_lmstudio(ctx)}
        job = ctx.job
        existing = self.find_app("LM Studio.app")
        if existing and not job.force:
            return {"installed": True, "already_installed": True, "location": str(existing), "version": self.app_version(existing), **self.start_lmstudio(ctx)}
        apps_dir, needs_admin = self.app_target(job.location)
        ctx.state("downloading", "Finding the current LM Studio release", percent=1)
        try:
            url = self.system.resolve_redirect(LMSTUDIO_MAC_LATEST) or ""
        except Exception as exc:
            ctx.log(f"could not resolve the LM Studio download: {exc}")
            url = ""
        if not url.startswith("https://") or not url.endswith(".dmg"):
            raise InstallFailed("Could not find the current LM Studio download on lmstudio.ai. Check the internet connection and try again.", code="release_lookup_failed")
        name = url.rsplit("/", 1)[-1]
        version = (re.search(r"LM-Studio-([\w.\-]+)-arm64\.dmg", name) or [None, None])[1]
        etag = ""
        try:
            etag = (self.system.head(url).get("etag") or "").strip('"')
        except Exception as exc:
            ctx.log(f"HEAD {url} failed: {exc}")
        dest = self.cache_dir / "downloads" / name
        if dest.exists() and re.fullmatch(r"[0-9a-f]{32}", etag) and _md5_file(dest) == etag:
            ctx.log(f"reusing the verified download {dest}")
            job.set_bytes(dest.stat().st_size, dest.stat().st_size, 2, 70)
        else:
            ctx.state("downloading", f"Downloading LM Studio {version or ''}".strip(), percent=2)
            got = ctx.download(url, dest, lo=2, hi=70)
            if re.fullmatch(r"[0-9a-f]{32}", etag):
                if got["md5"] != etag:
                    dest.unlink(missing_ok=True)
                    raise InstallFailed("The LM Studio download is corrupted (it does not match the server's checksum); it was deleted.", code="checksum_mismatch")
                ctx.log(f"md5 matches the server ETag {etag} (transfer integrity; authenticity is the code signature below)")
            else:
                ctx.log("the server published no single-part ETag; authenticity rests on the code signature check")
        mount = self.cache_dir / "mnt" / job.id
        mount.mkdir(parents=True, exist_ok=True)
        ctx.state("installing", "Opening the LM Studio disk image", percent=71)
        rc, out = ctx.run(["hdiutil", "attach", "-nobrowse", "-noautoopen", "-readonly", "-mountpoint", str(mount), str(dest)], timeout=600, input_text="Y\n")
        if rc != 0:
            raise InstallFailed("The LM Studio disk image could not be opened; the log shows why.", code="mount_failed")
        try:
            apps = sorted(mount.glob("*.app"))
            if not apps:
                raise InstallFailed("The LM Studio disk image holds no app.", code="unpack_failed")
            staged = apps[0]
            signature = self._verify_signature(ctx, staged, LMSTUDIO_TEAM_ID, "LM Studio")
            placed = self._place_app(ctx, staged, apps_dir, needs_admin, "LM Studio")
        finally:
            rc_d, _ = ctx.run(["hdiutil", "detach", str(mount)], timeout=120)
            if rc_d != 0:
                ctx.run(["hdiutil", "detach", "-force", str(mount)], timeout=120)
        result = {"installed": True, "location": str(placed), "version": self.app_version(placed), "release": version, "signature": signature}
        if self.autostart_lmstudio:
            ctx.state("installing", "Starting LM Studio and its local server", percent=92)
            result.update(self.start_lmstudio(ctx, launch_app=True))
        return result

    autostart_lmstudio = True

    def start_lmstudio(self, ctx: Optional[_JobContext] = None, *, launch_app: bool = False) -> Dict[str, Any]:
        url = self.lmstudio_url + "/v1/models"
        got = self.system.http_json(url)
        if got is not None:
            return {"running": True, "base_url": self.lmstudio_url, "started": False}
        run = (lambda argv, t: ctx.run(argv, timeout=t)) if ctx else (lambda argv, t: self.system.run(argv, timeout=t))
        cli = self.lms_cli()
        app = self.find_app("LM Studio.app") if self.host.os_id == "darwin" else None
        if cli is None and app is not None:
            # First launch of the app installs its `lms` CLI under ~/.lmstudio/bin.
            run(["open", "-j", "-g", "-a", str(app)], 30)
            t0 = time.monotonic()
            while cli is None and time.monotonic() - t0 < self.start_timeout_s:
                if ctx:
                    ctx.check_cancel()
                    ctx.job.heartbeat(time.monotonic() - t0)
                time.sleep(1.0)
                cli = self.lms_cli()
        if cli is None:
            return {"running": False, "base_url": self.lmstudio_url, "started": False,
                    "message": "LM Studio is installed. Open it once on the gateway host to finish its first-run setup, then press Start."}
        rc, out = run([cli, "server", "start"], 120)
        got = self._wait_http(ctx, url, "LM Studio")
        return {"running": got is not None, "base_url": self.lmstudio_url, "started": True,
                **({} if got is not None else {"message": "LM Studio's server did not answer: " + (_last_error_line(out) or "see the log")})}

    def stop_lmstudio(self) -> Dict[str, Any]:
        cli = self.lms_cli()
        if cli is None:
            return {"running": None, "message": "the `lms` CLI is not installed"}
        rc, out = self.system.run([cli, "server", "stop"], timeout=60)
        return {"running": self.system.http_json(self.lmstudio_url + "/v1/models") is not None, "output": out}


#: "Is it already there?" for the in-process engines (skipped with `force`).
_ALREADY_PROBES: Dict[str, str] = {
    "llamacpp": "import json, importlib.metadata as m, llama_cpp; print('__AG_VERIFY__' + json.dumps({'version': m.version('llama-cpp-python')}))",
    "mlx": "import json, importlib.metadata as m, mlx.core, mlx_lm; print('__AG_VERIFY__' + json.dumps({'version': m.version('mlx'), 'mlx_lm_version': m.version('mlx-lm')}))",
    "vllm": "import json, importlib.metadata as m; import vllm; print('__AG_VERIFY__' + json.dumps({'version': m.version('vllm')}))",
    "huggingface": "import json, importlib.metadata as m; import transformers, torch; print('__AG_VERIFY__' + json.dumps({'version': m.version('transformers')}))",
}


def _md5_file(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_ERROR_HINTS = (
    (re.compile(r"xcrun: error|xcode-select|CommandLineTools|No developer tools", re.I), "the Apple command-line tools are missing"),
    (re.compile(r"CMAKE_C_COMPILER|No CMAKE_C_COMPILER|C compiler .* not found|cc: not found|gcc: not found", re.I), "no C compiler was found"),
    (re.compile(r"CRC|BadZipFile|corrupt", re.I), "the wheel file is corrupted"),
    (re.compile(r"No solution found|Could not find a version|no matching distribution|No matching distribution", re.I), "no matching package for this Python and machine"),
    (re.compile(r"Temporary failure in name resolution|Name or service not known|nodename nor servname|Connection refused|timed out", re.I), "the network is unreachable"),
    (re.compile(r"No space left on device", re.I), "the disk is full"),
)


def _last_error_line(output: str) -> str:
    text = output or ""
    for pattern, hint in _ERROR_HINTS:
        if pattern.search(text):
            return hint
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("(exit")]
    for ln in reversed(lines):
        if re.search(r"error|failed|cannot|not found", ln, re.I):
            return ln
    return lines[-1] if lines else ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class EngineJobRegistry:
    """Install jobs in this process. One install at a time."""

    def __init__(self, installer_factory: Callable[[], EngineInstaller], *, log_dir: Optional[Path] = None):
        self._factory = installer_factory
        self._jobs: Dict[str, EngineJob] = {}
        self._installers: Dict[str, EngineInstaller] = {}
        self._lock = threading.Lock()
        self.log_dir = log_dir

    def service_installer(self) -> EngineInstaller:
        """One long-lived installer for start/stop (it remembers a server it spawned)."""

        with self._lock:
            if getattr(self, "_service", None) is None:
                self._service = self._factory()
            return self._service

    def active(self) -> Optional[EngineJob]:
        with self._lock:
            for job in self._jobs.values():
                if job.state not in TERMINAL_STATES:
                    return job
        return None

    def active_for(self, eid: str) -> Optional[EngineJob]:
        job = self.active()
        return job if job is not None and job.engine == eid else None

    def get(self, job_id: str) -> Optional[EngineJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.snapshot() for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)]

    def start(self, eid: str, *, force: bool = False, location: str = "auto", run_inline: bool = False) -> Tuple[Dict[str, Any], bool]:
        """(snapshot, joined). Joins the engine's running job; 409 while another engine installs."""

        if eid not in ENGINE_TEXT:
            raise JobStateError(f"unknown engine {eid!r}; known: {', '.join(ENGINE_IDS)}", status_code=404, reason="unknown_engine")
        if location not in {"auto", "user", "system"}:
            raise JobStateError("location must be auto, user or system", status_code=400, reason="bad_location")
        installer = self._factory()
        supported, reason = installer.support(eid)
        if not supported:
            raise JobStateError(reason or "not supported on this machine", status_code=409, reason="unsupported_on_this_machine")
        with self._lock:
            for job in self._jobs.values():
                if job.state not in TERMINAL_STATES:
                    if job.engine == eid:
                        return job.snapshot(), True
                    raise JobStateError(f"{ENGINE_TEXT[job.engine]['name']} is being installed; wait for it to finish (job {job.id}).", reason="busy")
            job = EngineJob(eid, force=force, location=location, log_dir=self.log_dir)
            self._jobs[job.id] = job
            self._installers[job.id] = installer
        job.logline(f"install {eid} on {installer.host.os_id} {installer.host.arch} (python {installer.python}, location {location}, force {force})")
        self._launch(job, run_inline=run_inline)
        return job.snapshot(), False

    def _launch(self, job: EngineJob, *, run_inline: bool) -> None:
        if run_inline:
            self._run(job)
            return
        t = threading.Thread(target=self._run, args=(job,), name=f"engine-install-{job.id}", daemon=True)
        job.thread = t
        t.start()

    def _run(self, job: EngineJob) -> None:
        installer = self._installers[job.id]
        ctx = _JobContext(job, installer)
        job.attempt += 1
        try:
            result = installer.install(job.engine, ctx)
            with job.lock:
                job.result = result
                job.admin_prompt = None
                job.tools_prompt = None
                job.finished_at = _now_iso()
            name = ENGINE_TEXT[job.engine]["name"]
            if result.get("already_installed"):
                msg = f"{name} is already installed" + (f" ({result['version']})" if result.get("version") else "")
            else:
                msg = f"{name} is installed" + (f" ({result['version']})" if result.get("version") else "")
            if result.get("running") is True:
                msg += " and running"
            elif result.get("message"):
                msg += ". " + str(result["message"])
            job.set_state("done", msg, percent=100)
        except NeedsAdmin as exc:
            with job.lock:
                job.admin_prompt = exc.prompt
            job.set_state("needs_admin", exc.message or f"{exc.prompt.reason} Press \"{exc.prompt.public()['button']}\" to continue.")
            job.logline(f"needs_admin: {exc.prompt.reason} | command: {exc.prompt.command} | method: {exc.prompt.method}")
        except NeedsTools as exc:
            with job.lock:
                job.tools_prompt = exc.prompt
            tail = " Apple's installer is open on the gateway host; press Continue when it finishes." if exc.prompt.started else ""
            job.set_state("needs_tools", exc.prompt.reason + tail)
            job.logline(f"needs_tools: {exc.prompt.reason} | action: {exc.prompt.command}")
        except Cancelled:
            with job.lock:
                job.finished_at = _now_iso()
            job.set_state("cancelled", "Cancelled; nothing further was changed")
        except InstallFailed as exc:
            with job.lock:
                job.error = {"code": exc.code, "message": exc.message}
                job.finished_at = _now_iso()
            job.set_state("failed", exc.message)
        except PrivilegeViolation as exc:
            with job.lock:
                job.error = {"code": "privilege_violation", "message": str(exc)}
                job.finished_at = _now_iso()
            job.set_state("failed", "Refused to run an administrator command that was never shown to you.")
        except Exception as exc:  # the full traceback goes to the log; the message stays readable
            job.logline(traceback.format_exc())
            with job.lock:
                job.error = {"code": "unexpected", "message": f"{type(exc).__name__}: {exc}"}
                job.finished_at = _now_iso()
            job.set_state("failed", f"The install stopped on an unexpected error ({type(exc).__name__}: {exc}). The log has the details.")

    def continue_job(self, job_id: str, action: Optional[str] = None, *, run_inline: bool = False) -> Dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise JobStateError(f"no job {job_id}", status_code=404, reason="not_found")
        with job.lock:
            if job.state not in PAUSED_STATES:
                raise JobStateError(f"job {job_id} is {job.state}; only a job waiting for tools or an administrator can continue", reason="not_paused")
            if job.state == "needs_admin":
                prompt = job.admin_prompt
                act = action or ("approve_admin" if prompt and prompt.method != "manual" else "recheck")
                if act not in {"approve_admin", "recheck"}:
                    raise JobStateError(f"action {act!r} does not apply to needs_admin", status_code=400, reason="bad_action")
                if prompt is not None:
                    # THE approval: only a prompt that was surfaced (it is the job's
                    # current admin_prompt, state needs_admin) can be approved.
                    job.approved_admin.add(prompt.key)
                job.logline(f"continue ({act}) after needs_admin: {prompt.command if prompt else ''}")
            else:
                prompt_t = job.tools_prompt
                act = action or ("install_tools" if prompt_t and prompt_t.action_kind == "xcode_select_install" and not prompt_t.started else "recheck")
                if act not in {"install_tools", "recheck"}:
                    raise JobStateError(f"action {act!r} does not apply to needs_tools", status_code=400, reason="bad_action")
                if act == "install_tools":
                    if not prompt_t or prompt_t.action_kind != "xcode_select_install":
                        raise JobStateError("these tools cannot be installed from here; install them on the host, then re-check", status_code=400, reason="manual_tools")
                    job.tools_requested.add(prompt_t.key)
                job.logline(f"continue ({act}) after needs_tools")
            job.state = "queued"
            job._base_message = job.message = "Continuing"
            job.events.append({"at": _now_iso(), "state": "queued", "message": f"Continuing ({act})", "percent": job.percent})
        self._launch(job, run_inline=run_inline)
        return job.snapshot()

    def cancel(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.get(job_id)
        if job is None:
            return None
        with job.lock:
            if job.state in TERMINAL_STATES:
                return job.snapshot()
            job.cancel_event.set()
            proc = job.proc
            paused = job.state in PAUSED_STATES or job.state == "queued" and job.thread is None
        if proc is not None:
            _terminate(proc)
        if paused:
            with job.lock:
                job.finished_at = _now_iso()
            job.set_state("cancelled", "Cancelled; nothing further was changed")
        return job.snapshot()


# ---------------------------------------------------------------------------
# Rows (GET /engines)
# ---------------------------------------------------------------------------


def engine_row(installer: EngineInstaller, core_row: Dict[str, Any], *, active_job: Optional[EngineJob], install_allowed: bool, is_admin: bool = True) -> Dict[str, Any]:
    eid = str(core_row.get("id"))
    text = ENGINE_TEXT.get(eid, {"name": core_row.get("name") or eid, "description": "", "download_url": core_row.get("docs_url"), "docs_url": core_row.get("docs_url")})
    supported, reason = installer.support(eid)
    installed = bool(core_row.get("installed"))
    version = core_row.get("version")
    location = core_row.get("install_location")
    if installer.host.os_id == "darwin" and eid in {"ollama", "lmstudio"}:
        app = installer.find_app("Ollama.app" if eid == "ollama" else "LM Studio.app")
        if app is not None:
            installed = True
            location = location if location and str(location).endswith(".app") else str(app)
            version = version or installer.app_version(app)
    if eid == "mlx" and installed and not supported:
        installed = False  # a stray wheel on an unsupported host is not a working engine
    plan = installer.plan(eid)
    install = plan.public(text.get("download_url"))
    install["available"] = bool(plan.available and supported)
    install["allowed"] = bool(install_allowed)
    install["fallback"] = {"kind": "open_page", "url": text.get("download_url"), "recheck": True}
    running = core_row.get("running")
    reachable = core_row.get("reachable")
    base_url = core_row.get("base_url") or ({"ollama": installer.ollama_url, "lmstudio": installer.lmstudio_url}.get(eid))
    actions: List[Dict[str, Any]] = []

    def act(aid: str, label: str, *, method: str = "POST", path: Optional[str] = None, enabled: bool = True, why: Optional[str] = None, url: Optional[str] = None) -> None:
        a: Dict[str, Any] = {"id": aid, "label": label, "enabled": enabled}
        if path:
            a.update(method=method, path=path)
        if url:
            a["url"] = url
        if why:
            a["reason"] = why
        actions.append(a)

    busy = active_job is not None and active_job.state not in TERMINAL_STATES
    if supported and not installed and install["available"]:
        why = None if install_allowed else "Engine installs are turned off on this gateway (allow_engine_install)."
        if not is_admin:
            why = "Only an administrator can install engines."
        label = "Install"
        if plan.needs_admin:
            label = "Install (administrator)"
        act("install", label, path=f"/api/gateway/engines/{eid}/install", enabled=bool(install_allowed and is_admin and not busy), why=why or ("An install is running." if busy else None))
    if supported and not installed and text.get("download_url"):
        act("open_page", "Open download page", method="GET", url=text["download_url"], enabled=True)
        act("recheck", "I have installed it -- re-check", method="GET", path="/api/gateway/engines?probe=1")
    if eid in SERVER_ENGINES and installed and supported:
        # `running` is known only on a probed read (?probe=1); unknown -> neither button.
        if running is True:
            act("stop", "Stop", path=f"/api/gateway/engines/{eid}/stop", enabled=is_admin)
        elif running is False:
            act("start", "Start", path=f"/api/gateway/engines/{eid}/start", enabled=is_admin)
    act("docs", "Docs", method="GET", url=text.get("docs_url"), enabled=True)
    row = {
        # the v2 contract
        "id": eid,
        "name": text.get("name"),
        "description": text.get("description"),
        "supported": supported,
        "support_reason": reason,
        "installed": installed,
        "version": version,
        "install_location": location,
        "running": running,
        "reachable": reachable,
        "base_url": base_url,
        "models_count": core_row.get("models_count"),
        "install": install,
        "actions": actions,
        "active_job": ({"job_id": active_job.id, "state": active_job.state, "percent": active_job.percent, "message": active_job.message} if active_job else None),
        # contract-B names kept for the older console and the CLI
        "kind": core_row.get("kind"),
        "provider": core_row.get("provider"),
        "supported_on_host": supported,
        "unsupported_reason": reason,
        "reachability": core_row.get("reachability"),
        "docs_url": text.get("docs_url"),
    }
    for key in ("cli", "cli_version", "mlx_lm_version", "llama_server", "huggingface_hub_version"):
        if key in core_row:
            row[key] = core_row[key]
    return row


def engines_payload(installer: EngineInstaller, core_payload: Dict[str, Any], registry: EngineJobRegistry, *, install_allowed: bool, is_admin: bool = True) -> Dict[str, Any]:
    rows_in = {str(r.get("id")): r for r in (core_payload.get("engines") or []) if isinstance(r, dict)}
    active = registry.active()
    engines = []
    for eid in ENGINE_IDS:
        core_row = rows_in.get(eid) or {"id": eid}
        engines.append(engine_row(installer, core_row, active_job=active if active and active.engine == eid else None, install_allowed=install_allowed, is_admin=is_admin))
    for eid, core_row in rows_in.items():  # an engine AbstractCore knows and this module does not: pass it through
        if eid not in ENGINE_IDS:
            engines.append(dict(core_row))
    out = dict(core_payload)
    out.update(
        schema=ENGINES_SCHEMA,
        core_schema=core_payload.get("schema"),
        engines=engines,
        host=dict(core_payload.get("host") or {}, **installer.host.public()),
        active_job=active.snapshot() if active else None,
    )
    return out


def _core_rows_by_id(payload: Any) -> Dict[str, Dict[str, Any]]:
    return {str(r.get("id")): r for r in ((payload or {}).get("engines") or []) if isinstance(r, dict)}


# ---------------------------------------------------------------------------
# Process-wide wiring for the gateway
# ---------------------------------------------------------------------------

_REGISTRY: Optional[EngineJobRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def gateway_engines_dir() -> Path:
    try:
        from .users import gateway_data_dir_from_env

        return Path(gateway_data_dir_from_env()) / "engines"
    except Exception:
        return _default_engines_cache_dir()


def _default_engines_cache_dir() -> Path:
    """The per-OS user cache (honours XDG_CACHE_HOME on Linux, ~/Library/Caches
    on macOS, %LOCALAPPDATA% on Windows) - never a hard-coded ~/.cache."""
    from .host_paths import user_cache_dir

    return user_cache_dir() / "engines"


def _legacy_argv(eid: str) -> List[str]:
    try:
        from .core_config import core_engine_install_plan

        return [str(a) for a in (core_engine_install_plan(eid).get("argv") or [])]
    except Exception:
        return []


def default_installer(*, accelerator: Optional[str] = None) -> EngineInstaller:
    return EngineInstaller(host=detect_host(accelerator), cache_dir=gateway_engines_dir(), legacy_argv=_legacy_argv)


def default_registry() -> EngineJobRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = EngineJobRegistry(default_installer, log_dir=gateway_engines_dir() / "jobs")
        return _REGISTRY


def reset_default_registry_for_tests(registry: Optional[EngineJobRegistry] = None) -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = registry


def dry_run_payload(installer: EngineInstaller, eid: str, *, location: str = "auto") -> Dict[str, Any]:
    if eid not in ENGINE_TEXT:
        raise JobStateError(f"unknown engine {eid!r}; known: {', '.join(ENGINE_IDS)}", status_code=404, reason="unknown_engine")
    plan = installer.plan(eid, location=location)
    public = plan.public(ENGINE_TEXT[eid].get("download_url"))
    msg = ("would: " + "; ".join(plan.steps)) if plan.available else (plan.notes or "no install path on this machine")
    return {
        "schema": JOB_SCHEMA,
        "job_id": None,
        "kind": "engine_install",
        "engine": eid,
        "dry_run": True,
        "state": "done" if plan.available else "failed",
        "status": "completed" if plan.available else "failed",
        "message": msg,
        "plan": public,
        "command": list(plan.command_preview),
        "result": {"status": "planned" if plan.available else "unsupported", "message": msg},
        "cli_equivalent": f"abstractgateway engines install {eid} --yes",
    }
