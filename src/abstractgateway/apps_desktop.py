"""Desktop apps next to the browser apps: AbstractAssistant.

AbstractAssistant is the framework's desktop companion: a Python/Qt menu-bar
app (PyPI `abstractassistant`), not a browser app. The root `abstractframework`
package pins it, so a full install already has it in the same Python
environment as the gateway; a gateway-only install may not. The console lists
it as the sixth app card (`kind: "desktop"`, after the five browser apps) and
the tray's Apps submenu offers "Launch Assistant"; BOTH read the presence
detection in this module (`detect_assistant`), so they always agree.

Presence (read-only, nothing is imported or launched to find it):

- the `abstractassistant` console script next to the gateway's own Python
  (`sys.executable`'s folder, and this interpreter's scripts folder), or on
  PATH;
- the importable package: `importlib.util.find_spec("abstractassistant")`
  WITHOUT importing it. A spec without an origin is a namespace portion (a
  folder named `abstractassistant` in the working directory, as in the
  framework checkout) and does not count;
- the macOS app bundle `AbstractAssistant.app` in /Applications or
  ~/Applications.

ONE ARTIFACT (R10.5, 2026-10-04): the gateway reports, launches and watches
one Assistant — the installed PACKAGE (its console script, else this Python
running its entry point) when it is installed, else the macOS bundle. A
leftover bundle (a hand-built AbstractAssistant.app from weeks ago) is never
launched over an installed package: the card said 0.13.0 and Open started a
0.5.0 bundle.

Version: the chosen artifact's — the package metadata
(`importlib.metadata.version`) for the package, the bundle's `Info.plist`
(`CFBundleShortVersionString`) for the bundle.

Running: a process of the CHOSEN artifact whose command line names the
assistant (psutil, the style of the external-app probe in apps_manager): the
console script, `python -m abstractassistant[.cli|.macos_entry]`, the tray's
`python -c "from abstractassistant.cli import main"` (the package), or the
bundle's own executable (the bundle). A process of the OTHER artifact is not
"running" for the card: it is reported as `other_running` with one sentence
("Another Assistant is running: <where> <version> — quit it to use
<version>"), and Open still launches the chosen artifact.

Launching (same machine only: it opens on the gateway computer's screen) is a
detached process with the scrubbed environment every app gets (no token,
secret or key): the console script, else this Python running its entry
point, else (no package installed) `open -a <bundle>` (macOS brings a running
one forward instead of starting a second). A NEW Assistant started from the
gateway (console or tray) is signed in by a one-time hand-over:
`--gateway-url <url> --gateway-handover-file <file>` (after `--args` for
`open -a`), the file being a 0600 JSON under <data dir>/handover/ holding
{schema: "abstractgateway.desktop_handover.v1", code, base_url, expires_at}.
The Assistant reads and deletes it, then trades the code on loopback at
POST /api/gateway/apps/desktop-handover (single use, two minutes) for a
remembered gateway session (`assistant_argv_with_handover`,
apps_manager.mint_desktop_handover). A running Assistant receives no code.
A token or code is never put on a command line nor in the environment.

Installing = `uv pip install abstractassistant` (or `python -m pip install`)
into the gateway's own Python, as an apps job, with every `abstract*` package
the gateway runs pinned to its installed version as `name==version`
requirements in the same command (the way engines_install.py protects the
gateway when it installs an engine). The pins are never a constraints file:
uv 0.11 splits a `--constraint` path at whitespace even when it is one argv
element, and the macOS data folder (`Application Support`) always has a space.
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class DesktopAppSpec:
    id: str
    name: str
    package: str  # PyPI distribution (and import name)
    script: str  # console script
    bundle: str  # macOS app bundle name
    description: str


# One line, from abstractassistant/README.md ("a gateway-native desktop
# assistant ... a macOS menu-bar app with a compact palette, a hands-free voice
# conversation mode").
ASSISTANT = DesktopAppSpec(
    id="assistant",
    name="Assistant",
    package="abstractassistant",
    script="abstractassistant",
    bundle="AbstractAssistant.app",
    description="A menu-bar assistant: chat or talk hands-free.",
)
DESKTOP_APPS: Tuple[DesktopAppSpec, ...] = (ASSISTANT,)
DESKTOP_BY_ID: Dict[str, DesktopAppSpec] = {a.id: a for a in DESKTOP_APPS}

LAUNCH_CHECK_S = 2.0  # a launch that exits non-zero within this is a failure
RUNNING_CACHE_TTL_S = 3.0


def is_desktop_app(app_id: Any) -> bool:
    return str(app_id or "").strip().lower() in DESKTOP_BY_ID


# ---------------------------------------------------------------------------
# Probes: every OS touch point, injectable (tests pass fakes)
# ---------------------------------------------------------------------------


def _dist_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import version

        return str(version(name))
    except Exception:
        return None


def _entry_point(script: str) -> Optional[str]:
    """`module:attr` of a console script, read from package metadata (no import)."""
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="console_scripts"):
            if ep.name == script:
                return str(ep.value)
    except Exception:
        return None
    return None


def _process_argvs() -> List[Tuple[int, List[str]]]:
    """(pid, argv) of every process this user may read (psutil; [] without it)."""
    try:
        import psutil  # optional (AbstractCore's local extras)
    except Exception:
        return []
    out: List[Tuple[int, List[str]]] = []
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            info = p.info
            argv = info.get("cmdline") or []
            if argv and int(info["pid"]) != me:
                out.append((int(info["pid"]), [str(a) for a in argv]))
        except Exception:  # noqa: BLE001 - AccessDenied, ZombieProcess, gone
            continue
    return out


def _read_plist_version(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    v = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
    return str(v) if v else None


def _script_dirs() -> List[str]:
    dirs: List[str] = []
    for d in (os.path.dirname(sys.executable or ""), sysconfig.get_path("scripts")):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


@dataclass
class DesktopProbes:
    which: Callable[[str], Optional[str]] = shutil.which
    exists: Callable[[str], bool] = os.path.exists
    find_spec: Callable[[str], Any] = importlib.util.find_spec
    platform: str = sys.platform
    home: Path = field(default_factory=Path.home)
    script_dirs: Sequence[str] = field(default_factory=_script_dirs)
    python: str = sys.executable
    dist_version: Callable[[str], Optional[str]] = _dist_version
    plist_version: Callable[[str], Optional[str]] = _read_plist_version
    entry_point: Callable[[str], Optional[str]] = _entry_point
    processes: Callable[[], List[Tuple[int, List[str]]]] = _process_argvs


def system_probes() -> DesktopProbes:
    """The real machine (tests replace this module function: conftest)."""
    return DesktopProbes()


# ---------------------------------------------------------------------------
# Detection (pure over DesktopProbes)
# ---------------------------------------------------------------------------


def _is_real_spec(spec: Any) -> bool:
    """A regular or single-module install; a namespace portion (no origin) is not."""
    if spec is None:
        return False
    origin = getattr(spec, "origin", None)
    return bool(origin) and origin not in {"namespace", "built-in", "frozen"}


def _base(arg: str) -> str:
    return arg.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def is_assistant_argv(argv: Sequence[str], spec: DesktopAppSpec = ASSISTANT) -> bool:
    """This command line is the Assistant (and not, say, a test run or an
    editor inside a folder named abstractassistant)."""
    if not argv:
        return False
    a0 = str(argv[0])
    if f"{spec.bundle}/Contents/MacOS/" in a0:
        return True
    names = {spec.script, spec.script + ".exe"}
    if _base(a0) in names:
        return True
    if _base(a0).startswith("python") or _base(a0).startswith("pythonw"):
        rest = [str(a) for a in argv[1:4]]
        if rest and _base(rest[0]) in names:
            return True
        for i, a in enumerate(rest[:-1]):
            nxt = rest[i + 1]
            if a == "-m" and (nxt == spec.package or nxt.startswith(spec.package + ".")):
                return True
            if a == "-c" and f"from {spec.package}." in nxt:
                return True
    return False


def _argv_artifact(argv: Sequence[str], spec: DesktopAppSpec = ASSISTANT) -> Tuple[str, Optional[str]]:
    """("bundle", <bundle path>) for the bundle's own executable, else ("package", None)."""
    a0 = str(argv[0]) if argv else ""
    marker = f"{spec.bundle}/Contents/MacOS/"
    if marker in a0:
        return "bundle", a0[: a0.index(marker) + len(spec.bundle)]
    return "package", None


def other_running_sentence(other: Dict[str, Any], *, chosen_version: Optional[str], spec: DesktopAppSpec = ASSISTANT) -> str:
    """"Another Assistant is running: /Applications/AbstractAssistant.app 0.5.0 — quit it to use 0.13.0"."""
    where = str(other.get("location") or ("the app bundle" if other.get("artifact") == "bundle" else "another install"))
    ver = f" {other['version']}" if other.get("version") else ""
    use = f" to use {chosen_version}" if chosen_version else f" to use the installed {spec.name}"
    return f"Another {spec.name} is running: {where}{ver} — quit it{use}"


def detect_assistant(probes: Optional[DesktopProbes] = None, *, spec: DesktopAppSpec = ASSISTANT, with_running: bool = True) -> Dict[str, Any]:
    """Where the Assistant is on this machine, how to launch it, and whether it
    runs. ONE function for the console (apps_manager) and the tray (tray/apps).

    ONE artifact: the installed package (script, else python entry point) when
    installed, else the bundle — `launch`, `version` and `running` are all
    that artifact's. A running process of the other artifact is
    `other_running` {artifact, location, version, pid, sentence}, never
    `running`.

    {installed, found_by: [...], source: script|python|bundle|"", launch: argv|None,
     launches: [(source, argv)], bundle, script, package_origin, version,
     location, running, pid, running_argv, other_running}"""
    p = probes or system_probes()
    found: List[str] = []
    launches: List[Tuple[str, List[str]]] = []
    bundle_launches: List[Tuple[str, List[str]]] = []
    bundle_path: Optional[str] = None
    if p.platform == "darwin":
        for base in (Path("/Applications"), Path(p.home) / "Applications"):
            b = base / spec.bundle
            if p.exists(str(b)):
                found.append(f"bundle:{b}")
                bundle_launches.append(("bundle", ["open", "-a", str(b)]))
                bundle_path = bundle_path or str(b)
    exe = spec.script + (".exe" if p.platform.startswith("win") else "")
    script: Optional[str] = None
    # Next to the gateway's own Python first (the same environment), then PATH.
    for d in p.script_dirs or ():
        cand = str(Path(d) / exe)
        if p.exists(cand):
            script = cand
            break
    if script is None:
        script = p.which(spec.script)
    if script:
        found.append(f"script:{script}")
        launches.append(("script", [script]))
    try:
        mod_spec = p.find_spec(spec.package)
    except (ImportError, ValueError):
        mod_spec = None
    origin: Optional[str] = None
    if _is_real_spec(mod_spec):
        origin = str(mod_spec.origin)
        found.append(f"python:{origin}")
        ep = p.entry_point(spec.script)
        if ep and ":" in ep:
            mod, attr = ep.split(":", 1)
            launches.append(("python", [p.python, "-c", f"import sys; from {mod} import {attr} as _m; sys.exit(_m())"]))
    elif mod_spec is not None:
        found.append(f"namespace-only (ignored: a folder named {spec.package}, not an install)")
    # The package (script, then python entry point) first; the bundle only
    # launches when no package is installed.
    launches.extend(bundle_launches)
    installed = bool(launches)
    source, launch = (launches[0][0], list(launches[0][1])) if launches else ("", None)
    artifact = "bundle" if source == "bundle" else ("package" if source else "")
    version: Optional[str] = None
    if artifact == "package":
        version = p.dist_version(spec.package)
    elif artifact == "bundle" and bundle_path:
        version = p.plist_version(str(Path(bundle_path) / "Contents" / "Info.plist"))
    location = bundle_path if source == "bundle" else (script if source == "script" else (origin if source == "python" else None))
    pid: Optional[int] = None
    running_argv: Optional[List[str]] = None
    other: Optional[Dict[str, Any]] = None
    if with_running and artifact:
        try:
            for proc_pid, argv in p.processes():
                if not is_assistant_argv(argv, spec):
                    continue
                kind, where = _argv_artifact(argv, spec)
                if kind == artifact:
                    pid, running_argv = proc_pid, list(argv)
                    break
                if other is None:
                    if kind == "bundle":
                        other_ver = p.plist_version(str(Path(where) / "Contents" / "Info.plist")) if where else None
                    else:
                        other_ver = None  # another Python's package: its version is not ours to read
                        where = str(argv[0])
                    other = {"artifact": kind, "location": where, "version": other_ver, "pid": proc_pid, "argv": list(argv)}
        except Exception:  # noqa: BLE001 - a probe failure is "not running"
            pid = None
    if other is not None:
        other["sentence"] = other_running_sentence(other, chosen_version=version, spec=spec)
    return {
        "installed": installed,
        "found_by": found,
        "source": source,
        "launch": launch,
        "launches": launches,
        "bundle": bundle_path,
        "script": script,
        "package_origin": origin,
        "version": version,
        "location": location,
        "running": pid is not None,
        "pid": pid,
        "running_argv": running_argv,
        "other_running": other,
    }


def assistant_argv_with_handover(argv: Sequence[str], *, gateway_url: str, handover_file: str) -> List[str]:
    """The Assistant's launch argv plus the hand-over flags (CONTRACTS A-3):
    `--gateway-url <url> --gateway-handover-file <path>`. For a macOS app
    bundle (`open -a <bundle>`) they go after `--args`. The code itself is
    never on the command line nor in the environment: it is in the file."""
    out = [str(a) for a in argv]
    flags = ["--gateway-url", str(gateway_url), "--gateway-handover-file", str(handover_file)]
    if len(out) >= 3 and Path(out[0]).name == "open" and out[1] == "-a":
        return out + ["--args", *flags]
    return out + flags


def launch_command_text(argv: Optional[Sequence[str]]) -> Optional[str]:
    if not argv:
        return None
    return " ".join(shlex.quote(str(a)) for a in argv)


# ---------------------------------------------------------------------------
# Installing into the gateway's own Python
# ---------------------------------------------------------------------------


def find_uv(which: Callable[[str], Optional[str]] = shutil.which, home: Optional[Path] = None) -> Optional[str]:
    found = which("uv")
    if found:
        return found
    h = home or Path.home()
    for cand in (h / ".local" / "bin" / "uv", h / ".cargo" / "bin" / "uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if cand.exists():
            return str(cand)
    return None


def pip_install_argv(package: str, *, python: str, uv: Optional[str], has_pip: bool) -> Optional[List[str]]:
    """The install command for the gateway's Python, or None without uv or pip."""
    if uv:
        return [uv, "pip", "install", "--python", python, package]
    if has_pip:
        return [python, "-m", "pip", "install", "--disable-pip-version-check", package]
    return None


# `-I` (isolated): the probe sees the environment's own site-packages only,
# never a checkout's `*.egg-info` in the gateway's working directory or a
# PYTHONPATH entry - pins become requirements, so a stray one would be installed.
ABSTRACT_PINS_PROBE = "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions() if (d.metadata['Name'] or '').lower().startswith('abstract')}))"


def abstract_pins_argv(python: str) -> List[str]:
    return [python, "-I", "-c", ABSTRACT_PINS_PROBE]


def pin_requirements(pins: Dict[str, str]) -> List[str]:
    """`name==version` for every pin, as requirements for the SAME install
    command. Never a `--constraint <file>`: uv splits that path at whitespace
    even when it arrives as one argv element, so any data folder with a space
    in its path (macOS `Application Support`, a Windows user name) breaks the
    install. Pinning an installed package to its own version changes nothing
    and makes the installer report a plain conflict when the new package
    needs other versions."""
    return [f"{name}=={version}" for name, version in sorted(pins.items())]


def abstract_pins(python: str, *, run: Optional[Callable[..., Any]] = None) -> Dict[str, str]:
    """{name: version} of every installed `abstract*` distribution in `python`."""
    runner = run or subprocess.run
    try:
        cp = runner(abstract_pins_argv(python), capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    if getattr(cp, "returncode", 1) != 0:
        return {}
    try:
        pins = json.loads((cp.stdout or "").strip().splitlines()[-1])
    except Exception:
        return {}
    return {str(k): str(v) for k, v in pins.items()} if isinstance(pins, dict) else {}


def stream_command(argv: Sequence[str], *, on_line: Callable[[str], None], cancelled: Callable[[], bool], env: Optional[Dict[str, str]] = None) -> int:
    """Run argv, hand each output line to on_line, terminate it on cancel.
    Returns the exit code (-1 when cancelled)."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(0.3):
            if cancelled():
                try:
                    proc.terminate()
                except Exception:
                    pass
                return

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        code = proc.wait()
    finally:
        stop.set()
    return -1 if cancelled() else int(code)


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


def spawn_detached(argv: Sequence[str], *, env: Dict[str, str], log_path: Optional[Path]) -> subprocess.Popen:
    """argv in its own session, output to log_path. Raises OSError when it
    cannot start at all."""
    out: Any = subprocess.DEVNULL
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "ab")  # noqa: SIM115 - handed to the child
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT if log_path is not None else subprocess.DEVNULL, "env": dict(env), "close_fds": True}
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(list(argv), **kwargs)
    finally:
        if log_path is not None:
            try:
                out.close()
            except Exception:
                pass


def quit_process(pid: int, *, timeout: float = 10.0) -> bool:
    """Ask a process to quit (SIGTERM; the Qt app closes cleanly), wait for
    it, force it after `timeout`. True when it is gone."""
    try:
        import psutil

        proc = psutil.Process(int(pid))
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return True
    except Exception:  # noqa: BLE001 - already gone, or not ours to stop
        try:
            import psutil

            return not psutil.pid_exists(int(pid))
        except Exception:  # noqa: BLE001
            return False


def wait_launch(proc: Any, *, seconds: float = LAUNCH_CHECK_S, sleep: Callable[[float], None] = time.sleep) -> Optional[int]:
    """None while it keeps running (or exited 0: `open -a` returns at once),
    else its non-zero exit code."""
    deadline = seconds
    step = 0.2
    waited = 0.0
    while waited < deadline:
        code = proc.poll()
        if code is not None:
            return None if code == 0 else int(code)
        sleep(step)
        waited += step
    return None


def tail_text(path: Optional[Path], lines: int = 40) -> str:
    if path is None:
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception:
        return ""


__all__: Iterable[str] = (
    "ASSISTANT",
    "DESKTOP_APPS",
    "DESKTOP_BY_ID",
    "DesktopAppSpec",
    "DesktopProbes",
    "detect_assistant",
    "is_assistant_argv",
    "is_desktop_app",
    "launch_command_text",
    "system_probes",
)
