"""Keep this Mac awake while remote access is on (contract `gateway_keep_awake_v1`).

Operator ruling 2026-11-02: a phone or a watch reaches the gateway from away
(Tailscale Funnel, Tailscale Serve, an `internet` exposure behind a proxy) only
while the Mac is awake, and a Mac set to idle-sleep after a minute dropped them
for hours. So while remote access is on, the gateway keeps the Mac from going
to sleep ON ITS OWN, "unless a user specifically put the mac on sleep mode":

* Idle sleep only. The gateway holds `PreventUserIdleSystemSleep`, the
  assertion `caffeinate -i` holds. Apple menu > Sleep, closing the lid without
  an external display and the power button still sleep the Mac; the display
  still turns off. On battery and on the charger alike.
* Held by THIS process, through IOKit (`IOPMAssertionCreateWithName` /
  `IOPMAssertionRelease`, via ctypes), named "AbstractGateway: remote access"
  (`pmset -g assertions` lists it under the gateway's pid). The system drops
  it when the process exits, crashes or is killed. Nothing is installed, no
  power setting is changed (no pmset, no launchd).
* Remote access is on when this gateway was started with the `internet`
  network exposure, or when Tailscale Serve or Funnel forwards to its port on
  this machine (`tailscale serve status --json`). The check runs at start and
  every 60 seconds, so turning Funnel on or off is followed within a minute.
* The setting `keep_awake`, on by default: `serve --keep-awake on|off` for one
  run > the saved setting (console Resources > Gateway > Manage gateway > Stay awake, or
  `abstractgateway config set keep_awake on|off`) > on. A saved change applies
  at once in the serving process (the settings door calls `refresh`) and
  within a minute when another process wrote the store.

Why IOKit rather than a `caffeinate -i -w <gateway pid>` child: the assertion
belongs to the gateway itself, so there is no second process to start, watch
or leave behind; it carries a name that says who holds it and why; and
releasing it when remote access goes off is one call, not a child to kill.

Other systems: not implemented. The equivalents are `systemd-inhibit
--what=idle` (Linux) and `SetThreadExecutionState(ES_CONTINUOUS |
ES_SYSTEM_REQUIRED)` (Windows), tracked by root backlog 1101. There the status
says "Not keeping awake: not available on this system", and `serve` logs it
once.
"""

from __future__ import annotations

import ctypes
import datetime
import json
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

SCHEMA = "gateway_keep_awake_v1"
SETTING_KEY = "keep_awake"
ASSERTION_TYPE = "PreventUserIdleSystemSleep"
ASSERTION_NAME = "AbstractGateway: remote access"
CHECK_INTERVAL_S = 60.0

LABEL = "Keep this Mac awake while remote access is on"
HELP = (
    "While remote access is on (internet exposure, or Tailscale Serve or Funnel to this gateway), this Mac does "
    "not go to sleep on its own, so your phone and watch can reach it. Sleeping it yourself still works, and "
    "the display still turns off. On by default."
)
FLAG = "--keep-awake on|off"
CLI = "abstractgateway config set keep_awake on|off"

LINE_AWAKE = "Staying awake for remote access"
LINE_NOT = "Not keeping awake"

VIA_LABELS = {
    "internet": "internet exposure",
    "tailscale_funnel": "Tailscale Funnel",
    "tailscale_serve": "Tailscale Serve",
}

_log = logging.getLogger("abstractgateway.keep_awake")


def _say(level: str, message: str) -> None:
    """One line on the gateway's stderr, like its other boot lines ([INFO] gc, [INFO] commands)."""
    print(f"[{level}] keep awake: {message}", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# The setting: flag > stored > on
# ---------------------------------------------------------------------------


def resolve_setting(
    data_dir: Path, *, stored: Optional[Dict[str, Any]] = None, launch: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """{value, source: flag | stored | default, stored_value?} plus the
    labels every door shows (console, `config get`, the tray)."""
    from .runtime_config import _bool, _read_store, read_launch_settings

    if stored is None:
        stored = _read_store(Path(data_dir))
    if launch is None:
        launch = read_launch_settings(Path(data_dir))
    saved = stored.get(SETTING_KEY)
    if launch.get(SETTING_KEY) is not None:
        out: Dict[str, Any] = {"value": _bool(launch[SETTING_KEY], True), "source": "flag"}
        if saved is not None:
            out["stored_value"] = _bool(saved, True)  # saved, applies once the flag is gone
    elif saved is not None:
        out = {"value": _bool(saved, True), "source": "stored"}
    else:
        out = {"value": True, "source": "default"}
    out.update({"key": SETTING_KEY, "label": LABEL, "help": HELP, "flag": FLAG, "cli": CLI})
    return out


# ---------------------------------------------------------------------------
# Remote access: internet exposure, or Tailscale Serve/Funnel to our port
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"})


def _targets_port(target: Any, port: int) -> bool:
    """True when a Serve handler's target (`http://127.0.0.1:8080`,
    `localhost:8080`, `127.0.0.1:8080`) is this machine's `port`."""
    raw = str(target or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "tcp://" + raw
    try:
        parts = urlsplit(raw)
        found = parts.port
    except ValueError:
        return False
    if found is None:
        found = {"http": 80, "https": 443, "https+insecure": 443}.get(parts.scheme)
    return found == int(port) and str(parts.hostname or "").lower() in _LOOPBACK_HOSTS


def parse_tailscale_forwarding(text: str, port: int) -> Optional[str]:
    """`tailscale serve status --json` -> "tailscale_funnel" when Funnel
    reaches this machine's `port`, "tailscale_serve" when only the tailnet
    does, None otherwise (or on unreadable output). A foreground
    `tailscale serve` session sits under `Foreground: {session: config}`."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    configs = [data] + [c for c in (data.get("Foreground") or {}).values() if isinstance(c, dict)]
    found: Optional[str] = None
    for cfg in configs:
        funnel = {str(k) for k, v in (cfg.get("AllowFunnel") or {}).items() if v}
        for hostport, web in (cfg.get("Web") or {}).items():
            handlers = (web or {}).get("Handlers") if isinstance(web, dict) else None
            for handler in (handlers or {}).values():
                if isinstance(handler, dict) and _targets_port(handler.get("Proxy"), port):
                    if str(hostport) in funnel:
                        return "tailscale_funnel"
                    found = "tailscale_serve"
        for listen, tcp in (cfg.get("TCP") or {}).items():
            if isinstance(tcp, dict) and _targets_port(tcp.get("TCPForward"), port):
                if any(k.rsplit(":", 1)[-1] == str(listen) for k in funnel):
                    return "tailscale_funnel"
                found = "tailscale_serve"
    return found


def tailscale_forwarding(port: int) -> Optional[str]:
    """This machine's Tailscale Serve/Funnel forwarding to `port`, or None
    (no CLI, Tailscale not running, nothing forwarded). Never raises."""
    from .network_exposure import _run, tailscale_binary

    exe = tailscale_binary()
    if not exe:
        return None
    return parse_tailscale_forwarding(_run([exe, "serve", "status", "--json"], timeout=3.0) or "", port)


# ---------------------------------------------------------------------------
# The macOS assertion (IOKit through ctypes)
# ---------------------------------------------------------------------------


class PowerAssertionError(RuntimeError):
    pass


class MacIdleSleepAssertion:
    """One `PreventUserIdleSystemSleep` assertion owned by this process."""

    _IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
    _CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    _UTF8 = 0x08000100  # kCFStringEncodingUTF8
    _LEVEL_ON = 255  # kIOPMAssertionLevelOn

    def __init__(self) -> None:
        iokit = ctypes.cdll.LoadLibrary(self._IOKIT)
        cf = ctypes.cdll.LoadLibrary(self._CF)
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
        iokit.IOPMAssertionCreateWithName.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        iokit.IOPMAssertionRelease.restype = ctypes.c_int32
        iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        self._iokit, self._cf = iokit, cf

    def _cfstr(self, text: str) -> int:
        ref = self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), self._UTF8)
        if not ref:
            raise PowerAssertionError("could not build a CoreFoundation string")
        return ref

    def acquire(self, name: str) -> int:
        kind, label = self._cfstr(ASSERTION_TYPE), self._cfstr(name)
        try:
            out = ctypes.c_uint32(0)
            rc = self._iokit.IOPMAssertionCreateWithName(kind, self._LEVEL_ON, label, ctypes.byref(out))
        finally:
            self._cf.CFRelease(kind)
            self._cf.CFRelease(label)
        if rc != 0:
            raise PowerAssertionError(f"macOS refused the assertion (IOReturn {rc & 0xFFFFFFFF:#010x})")
        return int(out.value)

    def release(self, assertion_id: int) -> None:
        rc = self._iokit.IOPMAssertionRelease(ctypes.c_uint32(int(assertion_id)))
        if rc != 0:
            raise PowerAssertionError(f"macOS refused the release (IOReturn {rc & 0xFFFFFFFF:#010x})")


def default_backend(platform: str = sys.platform) -> Optional[Any]:
    """The assertion backend for this system, or None where there is none yet."""
    return MacIdleSleepAssertion() if platform == "darwin" else None


_UNSUPPORTED = {
    "linux": "the Linux equivalent would be `systemd-inhibit --what=idle`",
    "win32": "the Windows equivalent would be SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)",
}


# ---------------------------------------------------------------------------
# The controller: one per serving process
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Serving:
    data_dir: Path
    port: int
    exposure: str  # the effective exposure this process was started with


class KeepAwake:
    """Holds the assertion exactly while the setting is on AND remote access is on.

    `configure` (by `serve`) then `start` (the app's lifespan) start a daemon
    thread that checks at once and every `interval_s`; `refresh` checks now
    (after a settings write); `stop` (lifespan shutdown) releases. A process that `serve` did not configure reports
    `state: inactive` and never holds anything."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[], Optional[Any]] = default_backend,
        tailscale_fn: Callable[[int], Optional[str]] = tailscale_forwarding,
        platform: str = sys.platform,
        interval_s: float = CHECK_INTERVAL_S,
    ) -> None:
        self._backend_factory = backend_factory
        self._tailscale_fn = tailscale_fn
        self._platform = platform
        self._interval_s = float(interval_s)
        self._lock = threading.RLock()
        self._serving: Optional[_Serving] = None
        self._backend: Optional[Any] = None
        self._backend_error: Optional[str] = None
        self._assertion_id: Optional[int] = None
        self._since: Optional[str] = None
        self._error: Optional[str] = None
        self._last: Dict[str, Any] = {}
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._backend_tried = False

    # -- lifecycle ---------------------------------------------------------

    def configure(self, *, data_dir: Path, port: int, exposure: str) -> None:
        with self._lock:
            self._serving = _Serving(Path(data_dir), int(port), str(exposure or "unknown"))

    def start(self) -> None:
        """Never blocks (the app's lifespan calls it): the first check, like
        every later one, runs on the controller's own thread."""
        with self._lock:
            if self._serving is None or self._thread is not None:
                return
            self._stopped = False
            if not self._backend_tried:
                self._backend_tried = True
                try:
                    self._backend = self._backend_factory()
                except Exception as exc:  # noqa: BLE001 - a missing framework must never stop the gateway
                    self._backend_error = f"{type(exc).__name__}: {exc}"
            if self._backend is None:
                why = self._backend_error or _UNSUPPORTED.get(self._platform, "no equivalent is implemented")
                _say("INFO", f"not available on {self._platform} ({why}); this machine may sleep while remote access is on")
            else:
                self._wake.clear()
                self._thread = threading.Thread(target=self._loop, name="keep-awake", daemon=True)
                self._thread.start()
        if self._backend is None:
            self.refresh()  # no probe runs without a backend: cheap

    def stop(self) -> None:
        """Release, and never hold again until the next `start`."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._stopped = True
            self._wake.set()
            self._release("the gateway is stopping")
        if thread is not None:
            thread.join(timeout=5.0)

    def refresh(self) -> Dict[str, Any]:
        """Check now and hold or release. The probes (the settings store, the
        Tailscale CLI) run outside the lock, so `status` never waits on them."""
        with self._lock:
            serving, backend = self._serving, self._backend
        if serving is None:
            return self.status()
        setting = resolve_setting(serving.data_dir)
        remote: Dict[str, Any] = {"on": None, "via": None, "label": None}
        if backend is not None and setting["value"]:
            remote = self._remote_access(serving)
        with self._lock:
            want = bool(self._backend is not None and setting["value"] and remote["on"] and not self._stopped)
            if want and self._assertion_id is None:
                self._acquire(remote)
            elif not want and self._assertion_id is not None:
                self._release("the setting is off" if not setting["value"] else "remote access is off")
            if not want:
                self._error = None
            self._last = {"setting": setting, "remote_access": remote, "checked_at": _now_iso()}
            return self.status()

    def _loop(self) -> None:
        while not self._wake.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad check
                _log.exception("Keep awake: the periodic check failed")
            self._wake.wait(self._interval_s)

    # -- holding -----------------------------------------------------------

    def _remote_access(self, serving: _Serving) -> Dict[str, Any]:
        via: Optional[str] = "internet" if serving.exposure == "internet" else None
        if via is None:
            try:
                via = self._tailscale_fn(serving.port)
            except Exception:  # noqa: BLE001 - a Tailscale probe never breaks the check
                via = None
        return {"on": via is not None, "via": via, "label": VIA_LABELS.get(via or "")}

    def _acquire(self, remote: Dict[str, Any]) -> None:
        assert self._backend is not None
        try:
            self._assertion_id = int(self._backend.acquire(ASSERTION_NAME))
        except Exception as exc:  # noqa: BLE001 - reported in the status, retried at the next check
            msg = str(exc) or type(exc).__name__
            if msg != self._error:
                _say("WARN", f"could not hold '{ASSERTION_NAME}': {msg}")
            self._error = msg
            return
        self._since, self._error = _now_iso(), None
        _say("INFO", f"holding '{ASSERTION_NAME}' (remote access: {remote['label']}): this Mac does not idle-sleep "
                     "while remote access is on")

    def _release(self, why: str) -> None:
        if self._assertion_id is None or self._backend is None:
            self._assertion_id = None
            return
        aid, self._assertion_id, self._since = self._assertion_id, None, None
        try:
            self._backend.release(aid)
            _say("INFO", f"released '{ASSERTION_NAME}' ({why})")
        except Exception as exc:  # noqa: BLE001 - the process exit drops it anyway
            _say("WARN", f"release failed ({exc}); macOS drops it when the gateway exits")

    # -- the payload -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            serving, last = self._serving, dict(self._last)
            holding = self._assertion_id is not None
            setting = last.get("setting")
            remote = last.get("remote_access") or {"on": None, "via": None, "label": None}
            if serving is None or not self._backend_tried:
                state, detail = "inactive", "This process was not started by `abstractgateway serve`."
            elif self._backend is None:
                state, detail = "unsupported", "Not available on this system yet."
            elif not last:
                state, detail = "checking", "Checking whether remote access is on."
            elif holding:
                state = "awake"
                detail = (f"Remote access is on ({remote.get('label')}): this Mac does not go to sleep on its own. "
                          "Sleeping it yourself still works.")
            elif self._error:
                state, detail = "error", f"macOS did not allow it: {self._error}"
            elif setting and not setting.get("value"):
                state, detail = "off", "Turned off: this Mac may sleep, and remote access stops while it sleeps."
            else:
                state, detail = "no_remote_access", "Remote access is off."
            return {
                "schema": SCHEMA,
                "state": state,
                "holding": holding,
                "line": LINE_AWAKE if holding else LINE_NOT,
                "detail": detail,
                "setting": setting,
                "remote_access": remote,
                "supported": self._backend is not None,
                "platform": self._platform,
                "assertion": {"type": ASSERTION_TYPE, "name": ASSERTION_NAME},
                "since": self._since,
                "error": self._error,
                "checked_at": last.get("checked_at"),
                "interval_s": self._interval_s,
            }


_controller = KeepAwake()


def controller() -> KeepAwake:
    return _controller


def configure(*, data_dir: Path, port: int, exposure: str) -> None:
    _controller.configure(data_dir=data_dir, port=port, exposure=exposure)


def start_configured() -> None:
    _controller.start()


def stop_active() -> None:
    _controller.stop()
