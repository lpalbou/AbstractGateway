"""The tray application: icon + menu + actions, on top of pystray.

Threading contract (pystray, verified 2026-09-05): menu callbacks run
synchronously on the GUI thread (macOS: the Cocoa main thread), so a callback
may show a native dialog but must hand every network call to a worker
thread. Icon/title updates are thread-safe; `update_menu()` is DESTRUCTIVE
while the menu is open on Windows/Linux (it closes under the cursor) and
freezes the open menu's labels on macOS — so the menu is rebuilt only on
state transitions, model-list changes and coarse value buckets, never on a
timer while the numbers wiggle. The tooltip carries the live numbers
instead (updating it is harmless everywhere).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..apps_manager import app_launch_config
from . import apps as tray_apps, dialogs, menu_model, platform as plat
from .client import GatewayClient, Result
from .icons import icon_signature, master_size_for_platform, render_icon
from .menu_model import (  # noqa: F401 - re-exported: the copy helpers live with the pure menu model
    APP_NAME,
    DOCS_URL,
    ISSUES_URL,
    MENU_RUN_ROWS,
    RUN_BADGE_UNKNOWN,
    RUN_BADGES,
    STATE_WORDS,
    AutostartView,
    MenuInputs,
    ModelsView,
    Node,
    fmt_duration,
    model_row_label,
    run_row_label,
    run_tally,
    state_lines,
)
from .menu_model import middle_ellipsis as _middle_ellipsis  # noqa: F401
from .menu_model import provider_label as _provider_label  # noqa: F401
from .menu_model import workflow_menu_name as _workflow_menu_name  # noqa: F401
from .sampler import ModelRow, RunRow, Sampler, Snapshot, fmt_bytes, fmt_pct  # noqa: F401 - RunRow re-exported

logger = logging.getLogger("abstractgateway.tray")

TWO_STEP_SECONDS = 8.0
MENU_VALUE_REBUILD_MIN_S = 15.0
MEMORY_WARN_PCT = 90.0
MEMORY_WARN_SUSTAIN_S = 30
MEMORY_WARN_COOLDOWN_S = 3600.0
# The Apps / Models / Start-at-login data (the "extras") refresh on their own
# slow clock — none of it drives the icon, and /models/installed walks the
# whole HF cache and asks LM Studio/Ollama.
EXTRAS_APPS_EVERY_S = 30.0
EXTRAS_MODELS_EVERY_S = 300.0
EXTRAS_AUTOSTART_EVERY_S = 60.0
EXTRAS_NETWORK_EVERY_S = 30.0
APP_READY_TIMEOUT_S = 30.0


def _ready_line(ready: bool, *, reason: Optional[str] = None, hint: Optional[str] = None) -> None:
    """The ONE stdout line the supervisor waits for (see tray_supervisor)."""
    payload: Dict[str, Any] = {"ready": bool(ready)}
    if reason:
        payload["reason"] = reason
    if hint:
        payload["hint"] = hint
    try:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def tooltip_text(snap: Snapshot) -> str:
    st = snap.gateway_state
    if st == "paused":
        return f"{APP_NAME} — Paused (still running)\nWorkflows wait until you resume"
    if st == "unreachable":
        return f"{APP_NAME} — Not responding\nRetrying on this computer"
    parts = []
    if snap.mem_supported and snap.mem_used is not None and snap.mem_total:
        parts.append(f"Memory {fmt_bytes(snap.mem_used)} of {fmt_bytes(snap.mem_total)}")
    elif snap.mem_pct is not None:
        parts.append(f"Memory {fmt_pct(snap.mem_pct)}")
    if snap.gpu_supported and snap.gpu_pct is not None:
        parts.append(f"GPU {fmt_pct(snap.gpu_pct)} busy")
    line2 = " · ".join(parts) if parts else "Collecting…"
    text = f"{APP_NAME} — {STATE_WORDS.get(st, st.title())}\n{line2}"
    return text[:120]


def menu_signature(snap: Snapshot, *, update_phase: str, pending: Optional[str], tk_available: bool, extras: tuple = ()) -> tuple:
    """When this changes, the menu is rebuilt (see the module docstring).
    `extras` = the Apps / Models / Start-at-login data (`TrayApp._extras_signature`)."""
    mem_bucket = None if snap.mem_pct is None else int(snap.mem_pct // 5)
    gpu_bucket = None if snap.gpu_pct is None else int(snap.gpu_pct // 10)
    mem_total_bucket = None if not snap.mem_total else int(snap.mem_total // (1 << 30))
    return (
        snap.gateway_state,
        snap.inflight_ticks > 0,
        min(snap.inflight_ticks, 99),
        tuple((r.key, r.locked, r.size_bytes) for r in snap.models),
        # The Workflows submenu is built from these; without them the menu
        # would keep showing the run list it was born with. Durations are
        # deliberately NOT here — they tick every second and would rebuild the
        # menu under the operator's cursor (see the module docstring).
        tuple((r.run_id, r.status, r.steps) for r in snap.runs),
        mem_bucket,
        gpu_bucket,
        mem_total_bucket,
        snap.gpu_supported,
        snap.mem_supported,
        snap.can_restart,
        update_phase,
        pending,
        tk_available,
        extras,
    )


def tkinter_available(python: Optional[str] = None) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("tkinter") is not None and importlib.util.find_spec("_tkinter") is not None
    except Exception:
        return False


def _host_port(base_url: str) -> tuple:
    """(host, port) of the gateway this tray belongs to — what start-at-login registers."""
    import urllib.parse

    u = urllib.parse.urlsplit(base_url)
    return (u.hostname or "127.0.0.1"), int(u.port or (443 if u.scheme == "https" else 80))


def _app_name(app_id: str) -> str:
    if app_id == tray_apps.ASSISTANT_ID:
        return tray_apps.ASSISTANT_NAME
    try:
        return tray_apps.web_app_spec(app_id)[1]
    except StopIteration:
        return app_id


def _load_failure_detail(r: Result) -> str:
    """The FULL reason a load failed: the in-band `{success: false, error}`
    envelope as well as an HTTP error (ADR-0026: failures carry the reason)."""
    d = r.data if isinstance(r.data, dict) else {}
    bits: List[str] = []
    err = d.get("error")
    if isinstance(err, dict):
        bits += [str(err.get(k)) for k in ("message", "detail", "code") if err.get(k)]
    elif isinstance(err, str) and err.strip():
        bits.append(err.strip())
    for k in ("message", "detail", "reason"):
        v = d.get(k)
        if isinstance(v, str) and v.strip() and v.strip() not in bits:
            bits.append(v.strip())
    if not bits:
        bits.append(r.detail)
    return " — ".join(bits)


def _wait_http(url: str, proc: Any, timeout_s: float) -> bool:
    """Bounded readiness poll of a process we started (any HTTP answer = up)."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2.0):  # noqa: S310 - loopback URL we just started
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.5)
    return False


class Prefs:
    """Tiny per-machine helper preferences (NOT gateway settings): whether the
    pause explanation was shown once, the memory warning switch, and
    `flat_menu` (no submenus — for Linux panels that drop them)."""

    def __init__(self, data_dir: Optional[Path]) -> None:
        self._path = (Path(data_dir) / "tray" / "prefs.json") if data_dir else None
        self.data: Dict[str, Any] = {"memory_warn": True, "pause_explained": False, "activity_geometry": None}
        try:
            if self._path and self._path.exists():
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
        except Exception:
            pass

    def save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:
            pass


class TrayApp:
    def __init__(self, handshake: Dict[str, Any]) -> None:
        self.base_url = str(handshake.get("base_url") or "http://127.0.0.1:8080").rstrip("/")
        self.token = str(handshake.get("token") or "")
        self.parent_pid = int(handshake.get("parent_pid") or 0)
        self.data_dir = Path(handshake["data_dir"]) if handshake.get("data_dir") else None
        self.version = str(handshake.get("version") or "")
        self.console_path = str(handshake.get("console_path") or "/console")
        self.client = GatewayClient(self.base_url, self.token)
        self.sampler = Sampler(self.client, version=self.version)
        self.prefs = Prefs(self.data_dir)
        self._icon: Any = None
        self._lock = threading.RLock()
        self._last_menu_sig: Optional[tuple] = None
        self._last_icon_sig: Optional[tuple] = None
        self._icon_cache: Dict[tuple, Any] = {}
        self._mode = "neutral"
        self._mode_checked_at = 0.0
        self._update_phase = "idle"  # idle | checking | available | updating | installed | failed | not_possible
        self._update_latest: Optional[str] = None
        self._update_frame = 0
        self._pending: Optional[Dict[str, Any]] = None  # two-step confirmation fallback
        self._activity_proc: Optional[subprocess.Popen] = None
        self._tk = tkinter_available()
        self._mem_high_since: Optional[float] = None
        self._mem_warned_at = 0.0
        self._stopping = False
        self._master = master_size_for_platform(sys.platform)
        self._snap: Optional[Snapshot] = None
        self._last_title: Optional[str] = None
        self._last_state: Optional[str] = None
        self._last_menu_rebuild_at = 0.0
        # Extras: Apps / Models / Start at login (see _extras_loop).
        self._extras_wake = threading.Event()
        self._autostart: Optional[AutostartView] = None
        self._autostart_busy = False
        self._apps: tuple = ()
        self._apps_fetched = False
        self._app_launches: Dict[str, Any] = {}  # argv per app id (global web apps, Assistant)
        self._global_procs: Dict[str, tuple] = {}  # app id -> (Popen, url) for global web apps started here
        self._models_view = ModelsView()
        self._loading: Dict[str, str] = {}  # provider/model -> label, while a load runs
        self._ejecting: set = set()
        self._menu_has_menu = True  # pystray backend draws a menu at all (xorg does not)
        self._network = menu_model.NetworkView()
        self._network_busy = False
        self._npm_root: Optional[str] = None
        self._npm_root_at = -1e9

    # ------------------------------------------------------------------ run

    def run(self) -> int:
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001
            logger.error("pystray unavailable: %s", exc)
            _ready_line(False, reason=f"the tray library could not start here ({type(exc).__name__}: {exc})", hint="Linux: install python3-gi and the AppIndicator bindings, or python-xlib")
            return 3
        dialogs.set_mac_app_identity(APP_NAME)
        self._pick_mode(force=True)
        first = render_icon(self._master, state="starting", mode=self._mode)
        icon_cls = self._darwin_icon_class(pystray) if sys.platform == "darwin" else pystray.Icon
        try:
            self._icon = icon_cls(
                "abstractgateway",
                first,
                title=f"{APP_NAME} — Starting…",
                menu=pystray.Menu(self._menu_items),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("tray icon could not be created: %s", exc)
            _ready_line(False, reason=f"no system tray available here ({type(exc).__name__}: {exc})")
            return 4
        self._menu_has_menu = bool(getattr(self._icon, "HAS_MENU", True))
        self.sampler.add_listener(self._on_snapshot)
        self.sampler.start()
        threading.Thread(target=self._watchdog, name="tray-watchdog", daemon=True).start()
        threading.Thread(target=self._stdin_watch, name="tray-stdin", daemon=True).start()
        threading.Thread(target=self._extras_loop, name="tray-extras", daemon=True).start()
        self._install_signals()

        def _setup(icon: Any) -> None:
            icon.visible = True
            _ready_line(True)
            if not self._menu_has_menu:
                # pystray's xorg backend has no menu at all: say where the controls are.
                self._notify(
                    f"{APP_NAME} is running",
                    "This desktop's tray shows no menu: click the icon to open the console. Start at login, apps and models: "
                    "`abstractgateway service enable`, `abstractgateway apps`, `abstractgateway models`.",
                )

        try:
            self._icon.run(setup=_setup)
        finally:
            self._stopping = True
            self._extras_wake.set()
            self.sampler.stop()
            self._close_activity_window()
            self._stop_global_apps()
        return 0

    def _stdin_watch(self) -> None:
        """The parent keeps our stdin open for life; EOF means it is gone
        (SIGKILL and exec included) — the strongest liveness signal on every OS."""
        try:
            while not self._stopping:
                chunk = sys.stdin.buffer.read(1) if hasattr(sys.stdin, "buffer") else sys.stdin.read(1)
                if not chunk:
                    break
        except Exception:
            pass
        if not self._stopping:
            logger.info("tray: parent closed the control pipe; exiting")
            self.stop()

    def _ui(self, fn: Callable[[], None]) -> None:
        """Run a pystray mutation on the GUI thread. AppKit is not
        thread-safe: on macOS every icon/title/menu update is marshalled to
        the main thread (non-blocking); elsewhere pystray's backends are
        safe to call from the sampler thread."""
        if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
            try:
                from PyObjCTools import AppHelper  # type: ignore[import-not-found]

                AppHelper.callAfter(self._act(fn))
                return
            except Exception:
                pass
        self._act(fn)()

    def _darwin_icon_class(self, pystray: Any) -> Any:
        """Retina-crisp icons on macOS: pystray force-resizes to a 22 px 1x
        square; we render a 44 px master and tell NSImage its POINT size."""
        import io

        base = pystray.Icon

        class RetinaIcon(base):  # type: ignore[misc,valid-type]
            def _assert_image(self):  # noqa: D401 - pystray internal
                if getattr(self, "_icon_image", None) is not None:
                    return
                try:
                    import AppKit
                    import Foundation

                    src = self._icon
                    thickness = float(self._status_bar.thickness()) or 22.0
                    scale = src.height / thickness if src.height else 2.0
                    b = io.BytesIO()
                    src.save(b, "png")
                    data = Foundation.NSData(b.getvalue())
                    img = AppKit.NSImage.alloc().initWithData_(data)
                    img.setSize_((src.width / scale, src.height / scale))
                    self._icon_image = img
                    self._status_item.button().setImage_(img)
                except Exception:
                    base._assert_image(self)

        return RetinaIcon

    def _install_signals(self) -> None:
        def _term(*_a: Any) -> None:
            self.stop()

        try:
            if sys.platform == "darwin":
                from PyObjCTools import MachSignals  # type: ignore[import-not-found]

                MachSignals.signal(signal.SIGTERM, _term)
            else:
                signal.signal(signal.SIGTERM, _term)
        except Exception:
            pass

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass

    def _watchdog(self) -> None:
        """Exit with the parent; never outlive the gateway that spawned us."""
        misses = 0
        while not self._stopping:
            time.sleep(2.0)
            if self.parent_pid and not plat.pid_alive(self.parent_pid):
                misses += 1
                if misses >= 2:
                    logger.info("tray: parent %s is gone; exiting", self.parent_pid)
                    self.stop()
                    return
            else:
                misses = 0
            self._reap_activity_window()

    # ------------------------------------------------------- snapshot → UI

    def _on_snapshot(self, snap: Snapshot) -> None:
        if self._icon is None or self._stopping:
            return
        self._snap = snap
        if snap.unauthorized:
            # Our token is dead: the gateway that spawned us is gone (or
            # another gateway owns the port). Nothing to show; leave quietly.
            logger.warning("tray: the gateway refused this helper's token; exiting")
            self.stop()
            return
        self._pick_mode()
        self._check_memory_pressure(snap)
        self._note_transitions(snap)
        if self._pending and time.monotonic() > float(self._pending.get("deadline") or 0):
            self._pending = None
        # Icon
        state = snap.gateway_state
        if state == "updating":
            self._update_frame = (self._update_frame + 1) % 8
        sig = icon_signature(size=self._master, state=state, mem_pct=snap.mem_pct, gpu_pct=snap.gpu_pct, active=snap.inflight_ticks > 0, frame=self._update_frame, mode=self._mode)
        image = None
        if sig != self._last_icon_sig:
            self._last_icon_sig = sig
            image = self._icon_cache.get(sig)
            if image is None:
                image = render_icon(self._master, state=state, mem_pct=snap.mem_pct, gpu_pct=snap.gpu_pct, active=snap.inflight_ticks > 0, frame=self._update_frame, mode=self._mode)
                if len(self._icon_cache) > 256:
                    self._icon_cache.clear()
                self._icon_cache[sig] = image
        title = tooltip_text(snap)
        # Menu (rebuilt only on coarse changes; value-only changes are
        # additionally rate-limited off macOS, where a rebuild closes an
        # open menu under the cursor)
        msig = menu_signature(snap, update_phase=self._update_phase, pending=(self._pending or {}).get("key"), tk_available=self._tk, extras=self._extras_signature())
        rebuild = False
        if msig != self._last_menu_sig:
            structural = (msig[:4] + msig[7:]) != ((self._last_menu_sig or ())[:4] + (self._last_menu_sig or ())[7:]) if self._last_menu_sig else True
            now = time.monotonic()
            if structural or sys.platform == "darwin" or (now - self._last_menu_rebuild_at) >= MENU_VALUE_REBUILD_MIN_S:
                self._last_menu_sig = msig
                self._last_menu_rebuild_at = now
                rebuild = True

        def _apply() -> None:
            if image is not None:
                self._icon.icon = image
            if title != self._last_title:
                self._last_title = title
                self._icon.title = title
            if rebuild:
                self._icon.update_menu()

        self._ui(_apply)

    def _note_transitions(self, snap: Snapshot) -> None:
        """One notification per meaningful transition (Windows users often
        have the icon hidden in the overflow tray; the icon alone is not enough)."""
        prev = self._last_state
        self._last_state = snap.gateway_state
        if prev is None:
            if snap.gateway_state == "paused":
                self._notify("Workflows are paused", f"{APP_NAME} is running but workflows wait until you choose Resume Workflows from this icon.")
            return
        if prev == snap.gateway_state:
            return
        if snap.gateway_state == "unreachable":
            self._notify(f"{APP_NAME} isn't responding", "Retrying on this computer. If it was restarting, this clears in a few seconds.")
        elif prev == "unreachable" and snap.gateway_state == "running":
            self._notify(f"{APP_NAME} is back", "The console and workflows are available again.")

    def _pick_mode(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._mode_checked_at) < 30.0:
            return
        self._mode_checked_at = now
        dark = plat.system_prefers_dark()
        self._mode = "neutral" if dark is None else ("dark" if dark else "light")

    def _check_memory_pressure(self, snap: Snapshot) -> None:
        if not self.prefs.data.get("memory_warn", True) or snap.mem_pct is None:
            self._mem_high_since = None
            return
        now = time.monotonic()
        if snap.mem_pct >= MEMORY_WARN_PCT:
            if self._mem_high_since is None:
                self._mem_high_since = now
            elif (now - self._mem_high_since) >= MEMORY_WARN_SUSTAIN_S and (now - self._mem_warned_at) >= MEMORY_WARN_COOLDOWN_S:
                self._mem_warned_at = now
                # Names a place that EXISTS on this machine: the Activity
                # window is only in the menu where tkinter can draw it, and
                # "Models" is always there.
                self._notify("Memory is almost full", "Ejecting a model or closing other apps will help. The menu's Models list shows what is using it.")
        else:
            self._mem_high_since = None

    def _notify(self, title: str, message: str) -> None:
        try:
            if self._icon is not None and getattr(self._icon, "HAS_NOTIFICATION", False):
                self._icon.notify(message, title)
        except Exception:
            logger.debug("tray: notify failed", exc_info=True)

    # ------------------------------------------------------------- the menu

    def menu_inputs(self) -> MenuInputs:
        """Everything the pure menu model reads, as one immutable value."""
        snap = self._snap or self.sampler.snapshot()
        mv = self._models_view
        return MenuInputs(
            snap=snap,
            update_phase=self._update_phase,
            update_latest=self._update_latest,
            pending_label=(self._pending or {}).get("label"),
            tk_available=self._tk,
            memory_warn=bool(self.prefs.data.get("memory_warn", True)),
            autostart=(dataclasses.replace(self._autostart, busy=True) if (self._autostart is not None and self._autostart_busy) else self._autostart),
            apps=tuple(self._apps),
            apps_fetched=self._apps_fetched,
            models=ModelsView(
                installed=mv.installed,
                installed_error=mv.installed_error,
                defaults=mv.defaults,
                fetched=mv.fetched,
                loading=tuple(sorted(self._loading)),
                ejecting=tuple(sorted(self._ejecting)),
            ),
            base_url=self.base_url,
            network=dataclasses.replace(self._network, busy=self._network_busy),
        )

    def _menu_items(self):
        """pystray asks for the items on every rebuild: build the pure model,
        render it. `flat_menu` (tray prefs) is the explicit degrade for panels
        that drop submenus."""
        nodes = menu_model.build_menu(self.menu_inputs())
        if self.prefs.data.get("flat_menu"):
            nodes = menu_model.flatten(nodes)
        for item in self._render_nodes(nodes):
            yield item

    def _render_nodes(self, nodes) -> List[Any]:
        import pystray

        out: List[Any] = []
        for n in nodes:
            if n.separator:
                out.append(pystray.Menu.SEPARATOR)
                continue
            kw: Dict[str, Any] = {"enabled": bool(n.enabled)}
            if n.default:
                kw["default"] = True
            if n.radio:
                kw["radio"] = True
            if n.checked is not None:
                kw["checked"] = (lambda value: (lambda _i: value))(bool(n.checked))
            if n.children is not None:
                out.append(pystray.MenuItem(n.label, pystray.Menu(*self._render_nodes(n.children)), **kw))
            elif n.action is not None:
                out.append(pystray.MenuItem(n.label, self._act((lambda a: (lambda: self.dispatch(a)))(n.action)), **kw))
            else:
                kw["enabled"] = False
                out.append(pystray.MenuItem(n.label, None, **kw))
        return out

    def dispatch(self, action: tuple) -> None:
        """Menu node action → method. An unknown action is a bug: loud."""
        name, args = action[0], tuple(action[1:])
        fn = self.dispatch_table().get(str(name))
        if fn is None:
            raise KeyError(f"tray: no handler for menu action {action!r}")
        fn(*args)

    def dispatch_table(self) -> Dict[str, Callable[..., Any]]:
        return {
            "open_console": self.open_console,
            "open_console_tab": self.open_console_tab,
            "show_activity": self.show_activity,
            "open_runs": self.open_runs,
            "pause": self.pause,
            "resume": self.resume,
            "confirm_pending": self._run_pending,
            "check_update": self.check_or_apply_update,
            "restart": self.restart,
            "toggle_autostart": self.toggle_autostart,
            "open_url": plat.open_url,
            "copy_console_link": self.copy_console_link,
            "toggle_memory_warn": self.toggle_memory_warn,
            "about": self.about,
            "quit": self.quit_gateway,
            "force_quit": self.force_quit,
            "eject": self.eject,
            "load": self.load_model,
            "app_open": self.app_open,
            "app_launch": self.app_launch,
            "app_install": self.app_install,
            "app_launch_global": self.app_launch_global,
            "app_open_url": self.app_open_url,
            "app_launch_tui": self.app_launch_tui,
            "assistant_launch": self.assistant_launch,
            "network_set": self.network_set,
            "network_restart": self.network_restart,
            "copy_address": self.copy_address,
        }

    # --------------------------------------------------------------- extras

    def _extras_signature(self) -> tuple:
        a = self._autostart
        return (
            (a.state, a.summary, a.problems) if a is not None else None,
            self._autostart_busy,
            self._apps_fetched,
            tuple((e.id, e.status, e.source, e.url, e.install_available, e.last_error, int(e.job_percent or 0) // 10, e.tui_installed, e.tui_launch_available) for e in self._apps),
            self._models_view.fetched,
            tuple((m.key, m.size_bytes) for m in self._models_view.installed),
            self._models_view.installed_error,
            tuple((d.key, d.task, d.status) for d in self._models_view.defaults),
            tuple(sorted(self._loading)),
            tuple(sorted(self._ejecting)),
            bool(self.prefs.data.get("flat_menu")),
            self._network,
            self._network_busy,
        )

    def _extras_loop(self) -> None:
        """Slow, independent refreshes of what the Apps / Models / Start-at-login
        rows show. A failed read keeps the last good data and records why."""
        last = {"apps": 0.0, "models": 0.0, "autostart": 0.0, "network": 0.0}
        while not self._stopping:
            now = time.monotonic()
            changed = False
            if now - last["autostart"] >= EXTRAS_AUTOSTART_EVERY_S:
                last["autostart"] = now
                changed |= self._refresh_autostart()
            if now - last["network"] >= EXTRAS_NETWORK_EVERY_S:
                last["network"] = now
                changed |= self._refresh_network()
            if now - last["apps"] >= EXTRAS_APPS_EVERY_S:
                last["apps"] = now
                changed |= self._refresh_apps()
            if now - last["models"] >= EXTRAS_MODELS_EVERY_S and (self._snap is None or self._snap.reachable):
                last["models"] = now
                changed |= self._refresh_models()
            if changed:
                self._force_menu_rebuild()
            self._extras_wake.wait(timeout=5.0)
            if self._extras_wake.is_set():
                self._extras_wake.clear()
                # poke_extras() asked for everything now
                last = {"apps": 0.0, "models": 0.0, "autostart": 0.0, "network": 0.0}

    def poke_extras(self) -> None:
        self._extras_wake.set()

    def _refresh_autostart(self) -> bool:
        from .. import autostart

        if self.data_dir is None:
            view = AutostartView("unknown", "this tray was started without a data folder")
        else:
            try:
                st = autostart.autostart_status(data_dir=self.data_dir)
                view = AutostartView(st["state"], st.get("summary") or "", tuple(st.get("problems") or ()), False, bool(st.get("experimental")))
            except Exception as exc:  # noqa: BLE001
                view = AutostartView("unknown", f"{type(exc).__name__}: {exc}")
        before = self._autostart
        self._autostart = view
        return before != view

    def _refresh_network(self) -> bool:
        r = self.client.network()
        if r.ok and isinstance(r.data, dict):
            view = menu_model.parse_network(r.data)
        elif r.status == 404:
            view = menu_model.NetworkView(available=False, error="Needs a newer gateway (no /api/gateway/network)")
        elif self._network.available:
            return False  # a blip keeps the last good answer
        else:
            view = menu_model.NetworkView(available=False, error=f"network settings unavailable: {r.detail}")
        before = self._network
        self._network = view
        return before != view

    def _refresh_apps(self) -> bool:
        probes = tray_apps.Probes()
        # `npm root -g` starts node: once per 10 minutes is plenty.
        if (time.monotonic() - self._npm_root_at) > 600.0:
            try:
                self._npm_root = probes.npm_root()
            except Exception:
                self._npm_root = None
            self._npm_root_at = time.monotonic()
        npm_root = self._npm_root
        globals_found = {spec[0]: tray_apps.detect_global_web_app(spec[0], probes, npm_root=npm_root) for spec in tray_apps.WEB_APPS}
        assistant = tray_apps.detect_assistant(probes)
        r = self.client.apps()
        payload = r.data if (r.ok and isinstance(r.data, dict)) else None
        # Global web apps this tray started: alive?
        running = {}
        for app_id, (proc, url) in list(self._global_procs.items()):
            if proc.poll() is None:
                running[app_id] = url
            else:
                self._global_procs.pop(app_id, None)
        entries = tray_apps.build_app_entries(payload, None if payload else (r.detail or "the apps list is unavailable"), globals_found=globals_found, assistant=assistant, local_running=running)
        self._app_launches = {k: v.get("launch") for k, v in globals_found.items()}
        self._app_launches["assistant"] = assistant.get("launch")
        before = (self._apps, self._apps_fetched)
        self._apps, self._apps_fetched = entries, True
        return before != (entries, True)

    def _refresh_models(self) -> bool:
        inst = self.client.models_installed()
        avail = self.client.model_availability()
        if inst.ok:
            installed, err = menu_model.parse_installed(inst.data)
        else:
            installed, err = self._models_view.installed, f"installed models unavailable: {inst.detail}"
        defaults = menu_model.parse_defaults(avail.data) if avail.ok else self._models_view.defaults
        view = ModelsView(installed=installed, installed_error=err, defaults=defaults, fetched=True)
        before = self._models_view
        self._models_view = view
        return before != view

    # -------------------------------------------------------- menu actions

    def toggle_autostart(self) -> None:
        """The checkbox: on → off, anything else → on (repair / replace)."""
        from .. import autostart

        view = self._autostart
        if self.data_dir is None or view is None:
            self._info("Start at login is unavailable", "This tray was started without its gateway's data folder, so it cannot register it.", style="warning")
            return
        turning_on = view.state != "on"
        if turning_on and view.state == "other":
            if dialogs.confirm(
                "Replace the other gateway?",
                f"{view.summary}. Starting THIS gateway at login replaces that registration.",
                ok_label="Replace",
                danger=True,
            ) is not True:
                return
        # No host/port: the registration runs plain `serve` and the Network
        # setting binds it (2026-09-24). Passing this tray's URL would write
        # 127.0.0.1 INTO the setting and undo a "Local network" choice.

        def _do() -> None:
            self._autostart_busy = True
            self._force_menu_rebuild()
            try:
                if turning_on:
                    out = autostart.enable_autostart(data_dir=self.data_dir, actor="tray")
                else:
                    out = autostart.disable_autostart(data_dir=self.data_dir)
            finally:
                self._autostart_busy = False
            self._refresh_autostart()
            self._force_menu_rebuild()
            after = out.get("after") or {}
            if out.get("ok"):
                if turning_on:
                    self._notify("Starts at login", f"{APP_NAME} will start when you log in ({after.get('summary')}).")
                else:
                    self._notify("No longer starts at login", f"{APP_NAME} keeps running now; it will not start at your next login.")
            else:
                self._info("Couldn't change Start at login", str(out.get("error") or after.get("summary") or "unknown error"), style="warning")

        self._bg(_do, "tray-autostart")

    def eject(self, key: str) -> None:
        snap = self._snap or self.sampler.snapshot()
        row = next((r for r in snap.models if r.key == key), None)
        if row is None:
            self._info("Already unloaded", "That model is no longer in memory.")
            return
        self.unload(row)

    def load_model(self, provider: str, model: str, task: str = "text_generation") -> None:
        key = f"{provider}/{model}"
        if key in self._loading:
            self._notify("Already loading", model)
            return
        snap = self._snap or self.sampler.snapshot()
        m = next((x for x in self._models_view.installed if x.key == key), None)
        size = m.size_bytes if m else None
        free = (snap.mem_total - snap.mem_used) if (snap.mem_total and snap.mem_used is not None) else None
        name = menu_model.short_model_name(model)

        def _go() -> None:
            self._loading[key] = name
            self._force_menu_rebuild()
            self._notify(f"Loading {name}", f"{menu_model.provider_label(provider)} · {fmt_bytes(size) if size is not None else 'size unknown'} — this can take a while for a big model.")
            t0 = time.monotonic()
            before_rss = (self._snap or snap).process_rss
            try:
                r = self.client.load_model(provider=provider, model=model, task=task)
            finally:
                self._loading.pop(key, None)
            self.sampler.poke()
            self._force_menu_rebuild()
            took = time.monotonic() - t0
            ok = r.ok and isinstance(r.data, dict) and r.data.get("success") is not False
            if ok:
                logger.info("tray: loaded %s in %.1fs (gateway rss before %s)", key, took, before_rss)
                self._notify(f"Loaded {name}", f"In memory after {took:.0f} s · {fmt_bytes(size) if size is not None else 'size unknown'} · {menu_model.provider_label(provider)}")
            else:
                self._info(f"Couldn't load {name}", _load_failure_detail(r), style="warning")

        if size is not None and free is not None and size > free:
            self._confirm(
                f"load:{key}",
                f"Load {menu_model.middle_ellipsis(name, 24)}",
                f"Load {name}?",
                f"It needs about {fmt_bytes(size)} and {fmt_bytes(free)} is free now. The system may slow down or swap. Eject another model first to make room.",
                ok_label="Load Anyway",
                danger=True,
                action=lambda: self._bg(_go, "tray-load"),
            )
            return
        self._bg(_go, "tray-load")

    # -- network (mission R's /api/gateway/network)

    def network_set(self, mode: str) -> None:
        words = menu_model.NETWORK_WORDS.get(mode, mode)
        nv = self._network

        def _go(ack: bool) -> None:
            self._network_busy = True
            self._force_menu_rebuild()
            try:
                r = self.client.set_network(mode, acknowledge_internet=ack)
            finally:
                self._network_busy = False
            self._refresh_network()
            self._force_menu_rebuild()
            data = r.data if isinstance(r.data, dict) else {}
            if r.status == 404:
                self._info("Network settings unavailable", "This gateway has no network settings yet (it needs the version with /api/gateway/network).", style="warning")
                return
            if not r.ok or data.get("ok") is False:
                auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
                fix = auth.get("fix") or self._network.auth_fix
                reason = str(data.get("refused_reason") or r.detail)
                self._info(f"Couldn't switch to {words}", reason + (f"\n\nTo fix: {fix}" if fix else ""), style="warning")
                return
            if data.get("restart_required"):
                self._notify(f"Network: {words}", "Restart to apply — the menu's Network item has \"Restart to apply\".")
            else:
                self._notify(f"Network: {words}", "Applied.")

        if mode == "internet":
            warnings = " ".join(f"• {w}" for w in nv.warnings) if nv.warnings else ""
            body = (
                "Anyone who can reach this computer from the internet will reach the gateway's sign-in page. "
                "Use it only behind your own firewall rules or a tunnel you control, with strong passwords."
                + (f"\n\n{warnings}" if warnings else "")
            )
            self._confirm("network:internet", "Expose to the Internet", "Open the gateway to the internet?", body, ok_label="I Understand — Expose It", danger=True, action=lambda: self._bg(lambda: _go(True), "tray-network"))
            return
        self._bg(lambda: _go(False), "tray-network")

    def network_restart(self) -> None:
        """Apply a pending network change: the gateway's own network restart
        when it has one, else the normal restart (same confirmation)."""

        def _do() -> None:
            self.sampler.set_override("restarting")

            def _go() -> None:
                r = self.client.network_restart()
                if r.status == 404:
                    r = self.client.restart(reason="network")
                if not r.ok:
                    self.sampler.set_override(None)
                    self._info("Couldn't restart", r.detail, style="warning")

            self._bg(_go, "tray-network-restart")

        self._confirm("restart", f"Restart {APP_NAME}", f"Restart {APP_NAME} to apply the network change?", "Running workflows pause at their next step and continue after the restart. The console is unavailable for a few seconds.", ok_label="Restart", danger=False, action=_do)

    def copy_address(self, url: str) -> None:
        if plat.copy_to_clipboard(url):
            self._notify(f"Copied {url}", "Paste it in a browser on a device that can reach this computer.")
        else:
            self._info("Address", url)

    # -- apps

    def _open_signed_in(self, rel_or_abs: str) -> None:
        url = rel_or_abs if rel_or_abs.startswith("http") else self.base_url + rel_or_abs
        if not plat.open_url(url):
            self._info("Couldn't open the browser", url, style="warning")

    def app_open(self, app_id: str) -> None:
        def _do() -> None:
            r = self.client.app_open(app_id)
            if r.ok and isinstance(r.data, dict) and r.data.get("open_url"):
                self._open_signed_in(str(r.data["open_url"]))
            else:
                self._info(f"Couldn't open {_app_name(app_id)}", r.detail, style="warning")
                self.poke_extras()

        self._bg(_do, "tray-app-open")

    def app_launch(self, app_id: str) -> None:
        name = _app_name(app_id)

        def _do() -> None:
            self._notify(f"Starting {name}", "It opens in your browser when it is ready.")
            r = self.client.app_launch(app_id)
            self.poke_extras()
            if not r.ok:
                self._info(f"Couldn't start {name}", r.detail, style="warning")
                return
            o = self.client.app_open(app_id)
            if o.ok and isinstance(o.data, dict) and o.data.get("open_url"):
                self._open_signed_in(str(o.data["open_url"]))
            else:
                self._info(f"{name} started, but couldn't open it", o.detail, style="warning")

        self._bg(_do, "tray-app-launch")

    def app_launch_tui(self, app_id: str) -> None:
        """"Open <app> in Terminal": the gateway opens the window (the same
        route as the console's button), signed in through a one-time code."""
        name = _app_name(app_id)

        def _do() -> None:
            r = self.client.app_launch_tui(app_id)
            if r.ok and isinstance(r.data, dict) and r.data.get("ok"):
                self._notify(f"{name} is opening in {r.data.get('terminal') or 'a terminal'}", "Signed in to this gateway.")
            else:
                self._info(f"Couldn't open {name} in a terminal", r.detail, style="warning")
                self.poke_extras()

        self._bg(_do, "tray-app-launch-tui")

    def app_install(self, app_id: str) -> None:
        """"Install X…": the same install as the console's Install button
        (mission LL): the browser app AND its terminal app when one exists for
        this computer (Code), one job; the Assistant into the gateway's Python.
        Nothing opens by itself afterwards: the menu then offers Open."""
        name = _app_name(app_id)
        if app_id == tray_apps.ASSISTANT_ID:
            body = f"{APP_NAME} installs AbstractAssistant (a desktop app for your menu bar) into its own Python. Progress shows here."
        elif app_id in tray_apps.TERMINAL_APP_IDS:
            body = f"{APP_NAME} downloads {name} for the browser and for the terminal (and Node.js the first time, about 56 MB). Progress shows here."
        else:
            body = f"{APP_NAME} downloads {name} from the npm registry (and Node.js the first time, about 56 MB). Progress shows here."
        self._confirm(f"install:{app_id}", f"Install {name}", f"Install {name}?", body, ok_label="Install", danger=False, action=lambda: self._bg(lambda: self._install_now(app_id), "tray-app-install"))

    def _install_now(self, app_id: str) -> None:
        name = _app_name(app_id)
        r = self.client.app_install(app_id)
        if not r.ok or not isinstance(r.data, dict) or not isinstance(r.data.get("job"), dict):
            self._info(f"Couldn't install {name}", r.detail, style="warning")
            return
        job_id = str(r.data["job"].get("id") or "")
        self._notify(f"Installing {name}", "Downloading… this takes a minute or two.")
        self.poke_extras()
        last_step = ""
        deadline = time.monotonic() + 1800.0
        while not self._stopping and time.monotonic() < deadline:
            time.sleep(1.0)
            j = self.client.apps_job(job_id)
            if not (j.ok and isinstance(j.data, dict)):
                continue
            job = j.data.get("job") if isinstance(j.data.get("job"), dict) else j.data
            state = str(job.get("state") or "")
            step = str(job.get("message") or "")
            if step and step != last_step and state == "running":
                last_step = step
                logger.info("tray: %s install: %s", app_id, step)
            if state in {"queued", "running"}:
                continue
            self.poke_extras()
            if state == "succeeded":
                where = "Launch it from the Apps menu." if app_id == tray_apps.ASSISTANT_ID else f"Open {name} is in the Apps menu."
                self._notify(f"{name} is installed", where)
            else:
                err = job.get("error") if isinstance(job.get("error"), dict) else {}
                reason = " ".join(str(x) for x in (err.get("message"), err.get("hint")) if x) or step or state
                self._info(f"{name} didn't install", f"{reason} (log: {job.get('log_path') or 'the gateway logs'})", style="warning")
            return
        self._info(f"{name} is still installing", "It keeps going in the gateway; the Apps menu shows it when it is done.")

    def app_launch_global(self, app_id: str) -> None:
        """A global npm/PATH install the gateway does not manage: start it here
        (scrubbed env, the gateway URL passed in), open it when it answers.
        It is stopped when this tray exits."""
        spec = tray_apps.web_app_spec(app_id)
        argv = self._app_launches.get(app_id)
        name = spec[1]
        if not argv:
            self._info(f"Couldn't start {name}", "The global install was not found any more.", style="warning")
            self.poke_extras()
            return

        def _do() -> None:
            port = tray_apps.free_port(spec[4])
            if port is None:
                self._info(f"Couldn't start {name}", f"No free port from {spec[4]}.", style="warning")
                return
            env = tray_apps.scrubbed_env(os.environ)
            flags, app_env = app_launch_config(app_id, port=port, host="127.0.0.1", gateway_url=self.base_url, gateway_url_env=spec[5])
            env.update(app_env)
            log = (self.data_dir / "logs" / "apps" / f"{app_id}-global.log") if self.data_dir else None
            try:
                proc = tray_apps.spawn_detached([*argv, *flags], env=env, log_path=log)
            except OSError as exc:
                self._info(f"Couldn't start {name}", f"{argv[0]}: {exc}", style="warning")
                return
            url = f"http://127.0.0.1:{port}/"
            if not _wait_http(url, proc, APP_READY_TIMEOUT_S):
                code = proc.poll()
                why = f"it exited with code {code}" if code is not None else f"it did not answer on {url} within {int(APP_READY_TIMEOUT_S)} s"
                self._info(f"{name} didn't start", f"{why}. Log: {log or 'none'}", style="warning")
                return
            self._global_procs[app_id] = (proc, url)
            self.poke_extras()
            self._notify(f"{name} is running", "Global install: sign in inside the app. Install it from this menu instead to have it open signed in.")
            plat.open_url(url)

        self._bg(_do, "tray-app-global")

    def app_open_url(self, app_id: str) -> None:
        entry = self._global_procs.get(app_id)
        if entry is None:
            self.poke_extras()
            return
        plat.open_url(entry[1])

    def _stop_global_apps(self) -> None:
        for _app_id, (proc, _url) in list(self._global_procs.items()):
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        self._global_procs.clear()

    def assistant_launch(self) -> None:
        argv = self._app_launches.get("assistant")
        if not argv:
            self._info("Couldn't launch the Assistant", "AbstractAssistant was not found any more.", style="warning")
            self.poke_extras()
            return

        def _do() -> None:
            # Never a second copy (the console's rule, apps_manager.launch_desktop):
            # a running app bundle is brought forward by `open -a`; one started
            # from its command is left alone.
            nonlocal argv
            now = tray_apps.detect_assistant(tray_apps.Probes())
            if now.get("running"):
                bundle, run = now.get("bundle"), [str(x) for x in (now.get("running_argv") or [])]
                if not (bundle and run and f"{bundle}/Contents/MacOS/" in run[0]):
                    self._notify("The Assistant is already running", "Its icon is in the menu bar.")
                    return
                argv = ["open", "-a", str(bundle)]
            log = (self.data_dir / "logs" / "assistant-launch.log") if self.data_dir else None
            try:
                proc = tray_apps.spawn_detached(argv, env=tray_apps.scrubbed_env(os.environ), log_path=log)
            except OSError as exc:
                self._info("Couldn't launch the Assistant", f"{argv[0]}: {exc}", style="warning")
                return
            time.sleep(2.0)
            code = proc.poll()
            if code not in (None, 0):
                self._info("The Assistant didn't start", f"`{' '.join(argv[:3])}` exited with code {code}. Log: {log or 'none'}", style="warning")
                return
            self._notify("Assistant launched", "AbstractAssistant is starting (it lives in your menu bar / tray).")

        self._bg(_do, "tray-assistant")

    def _act(self, fn: Callable[[], Any]) -> Callable[..., None]:
        """Wrap a menu action: swallow + log, never let a callback raise into the GUI loop."""

        def _inner(*_args: Any) -> None:
            try:
                fn()
            except Exception:
                logger.exception("tray: action failed")

        return _inner

    def _bg(self, fn: Callable[[], Any], name: str = "tray-action") -> None:
        threading.Thread(target=self._act(fn), name=name, daemon=True).start()

    # --------------------------------------------------------- confirmations

    def _confirm(self, key: str, label: str, title: str, body: str, *, ok_label: str, danger: bool, action: Callable[[], Any]) -> None:
        """Ask natively; when no dialog facility exists fall back to a
        two-step menu item ("Confirm: …") that expires after a few seconds."""
        answer = dialogs.confirm(title, body, ok_label=ok_label, danger=danger)
        if answer is True:
            action()
            return
        if answer is False:
            return
        self._pending = {"key": key, "label": label, "deadline": time.monotonic() + TWO_STEP_SECONDS, "action": action}
        self._notify(title, f"{body} Open the menu again and choose “Confirm: {label}”.")
        self._force_menu_rebuild()

    def _run_pending(self) -> None:
        pending = self._pending
        self._pending = None
        self._force_menu_rebuild()
        if pending and time.monotonic() <= float(pending.get("deadline") or 0):
            pending["action"]()

    def _force_menu_rebuild(self) -> None:
        self._last_menu_sig = None
        if self._snap is not None:
            self._on_snapshot(self._snap)

    def _info(self, title: str, body: str, *, style: str = "informational") -> None:
        if not dialogs.info(title, body, style=style):
            self._notify(title, body)

    # --------------------------------------------------------------- actions

    def open_console(self) -> None:
        self.open_console_tab(None)

    def open_console_tab(self, tab: Optional[str]) -> None:
        """Open the console SIGNED IN (a one-time claim link minted locally, as
        `abstractgateway claim` does), so an expired 8-hour session never
        greets the user with a token prompt. Minting touches a file, so it runs
        off the GUI thread; a plain URL is opened (and the reason shown) when a
        claim is impossible."""

        def _do() -> None:
            from .signin import console_link

            link = console_link(self.base_url, self.console_path, self.data_dir, tab=tab)
            if not plat.open_url(link.url):
                self._info("Couldn't open the browser", self.base_url + self.console_path, style="warning")
                return
            if not link.signed_in and link.note:
                logger.info("tray: console opened without a sign-in link: %s", link.note)
                self._notify("Console opened", f"Not signed in automatically: {link.note}.")

        self._bg(_do, "tray-open-console")

    def open_runs(self) -> None:
        """The console's Runtimes tab, where runs actually live."""
        self.open_console_tab("runtimes")

    def copy_console_link(self) -> None:
        # The PLAIN URL on purpose: a claim link is a credential and does not
        # belong on a clipboard that other apps can read.
        url = self.base_url + self.console_path
        if plat.copy_to_clipboard(url):
            self._notify("Console link copied", url)
        else:
            self._info("Console link", url)

    def toggle_memory_warn(self) -> None:
        self.prefs.data["memory_warn"] = not bool(self.prefs.data.get("memory_warn", True))
        self.prefs.save()

    def pause(self) -> None:
        self.sampler.set_override("pausing")

        def _do() -> None:
            r = self.client.pause()
            self.sampler.set_override(None)
            self.sampler.poke()
            if not r.ok:
                self._info("Couldn't pause", r.detail, style="warning")
                return
            if not self.prefs.data.get("pause_explained"):
                self.prefs.data["pause_explained"] = True
                self.prefs.save()
                self._notify("Workflows paused", f"{APP_NAME} keeps running, but no new workflow steps start until you choose Resume. Messages from Telegram, email and other connected apps still arrive and wait.")

        self._bg(_do, "tray-pause")

    def resume(self) -> None:
        self.sampler.set_override("resuming")

        def _do() -> None:
            r = self.client.resume()
            self.sampler.set_override(None)
            self.sampler.poke()
            if not r.ok:
                self._info("Couldn't resume", r.detail, style="warning")

        self._bg(_do, "tray-resume")

    def unload(self, row: ModelRow) -> None:
        size = fmt_bytes(row.size_bytes) if row.size_bytes is not None else "its memory"
        frees = f"This frees {size} of memory." if row.size_bytes is not None else "This frees the memory it uses."
        snap = self._snap or self.sampler.snapshot()
        # Eject semantics (mission I): calls running on the model are cancelled
        # first; the next request that needs it loads it again. Say so when
        # something IS running, because that is when it matters.
        busy = " Work running on it right now is stopped first." if snap.inflight_ticks > 0 else ""
        if row.locked:
            title = f"{row.name} is kept in memory"
            body = f"It was locked so it stays loaded. Eject it anyway? It frees {size} and loads again the next time it's needed.{busy}"
            ok = "Eject Anyway"
        else:
            title = f"Eject {row.name}?"
            body = f"{frees}{busy} The next time something needs this model it loads again from disk, which can take a while."
            ok = "Eject"

        def _do() -> None:
            self._bg(lambda: self._unload_now(row, force=row.locked), "tray-unload")

        self._confirm(f"unload:{row.key}", f"Eject {_middle_ellipsis(row.name, 24)}", title, body, ok_label=ok, danger=True, action=_do)

    def _unload_now(self, row: ModelRow, *, force: bool) -> None:
        self._ejecting.add(row.key)
        self._force_menu_rebuild()
        try:
            r = self.client.unload_model(row.target, force=force)
            if not r.ok and r.model_locked and not force:
                # The lock surfaced only now: ask once more, with the locked copy.
                body = f"It was locked so it stays loaded. Eject it anyway? It frees {fmt_bytes(row.size_bytes) if row.size_bytes is not None else 'its memory'}."
                if dialogs.confirm(f"{row.name} is kept in memory", body, ok_label="Eject Anyway", danger=True) is True:
                    r = self.client.unload_model(row.target, force=True)
                else:
                    return
        finally:
            self._ejecting.discard(row.key)
            self._force_menu_rebuild()
        self.sampler.poke()
        if r.ok and not (isinstance(r.data, dict) and r.data.get("success") is False):
            freed = f" · freed {fmt_bytes(row.size_bytes)}" if row.size_bytes is not None else ""
            self._notify("Model ejected", f"{row.name}{freed}")
        else:
            self._info(f"Couldn't eject {row.name}", _load_failure_detail(r), style="warning")

    def restart(self) -> None:
        def _do() -> None:
            self.sampler.set_override("restarting")

            def _go() -> None:
                r = self.client.restart(reason="tray")
                if not r.ok:
                    self.sampler.set_override(None)
                    self._info("Couldn't restart", r.detail, style="warning")

            self._bg(_go, "tray-restart")

        self._confirm("restart", f"Restart {APP_NAME}", f"Restart {APP_NAME}?", "Running workflows pause at their next step and continue after the restart. The console is unavailable for a few seconds.", ok_label="Restart", danger=False, action=_do)

    def quit_gateway(self) -> None:
        def _do() -> None:
            self.sampler.set_override("stopping")

            def _go() -> None:
                r = self.client.shutdown(reason="tray")
                if not r.ok:
                    self.sampler.set_override(None)
                    self._info("Couldn't quit", r.detail, style="warning")

            self._bg(_go, "tray-quit")

        # It used to end "...to keep it running but remove this icon, choose
        # Hide instead." There is no Hide any more: the icon is here for as
        # long as the gateway is. Quit is now the only thing that removes it,
        # and saying so plainly is the whole warning.
        self._confirm("quit", f"Quit {APP_NAME}", f"Quit {APP_NAME}?", f"Workflows stop, this icon disappears, and the console goes offline until you start {APP_NAME} again. To free the GPU without stopping anything, use Pause Workflows instead.", ok_label="Quit", danger=True, action=_do)

    def force_quit(self) -> None:
        def _do() -> None:
            def _go() -> None:
                pid = self.parent_pid
                if pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                    for _ in range(10):
                        time.sleep(0.5)
                        if not plat.pid_alive(pid):
                            break
                    else:
                        try:
                            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                        except Exception:
                            pass
                self.stop()

            self._bg(_go, "tray-force-quit")

        self._confirm("force-quit", f"Force Quit {APP_NAME}", f"Force quit {APP_NAME}?", f"{APP_NAME} isn't responding. Force quitting stops it immediately; anything a workflow was doing at that moment is lost.", ok_label="Force Quit", danger=True, action=_do)

    def about(self) -> None:
        import platform as _platform

        py = f"Python {sys.version_info.major}.{sys.version_info.minor}"
        os_name = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, _platform.system() or sys.platform)
        try:
            os_ver = _platform.mac_ver()[0] if sys.platform == "darwin" else _platform.release()
        except Exception:
            os_ver = ""
        data = str(self.data_dir) if self.data_dir else "—"
        home = str(Path.home())
        if data.startswith(home):
            data = "~" + data[len(home):]
        body = (
            "Runs AI workflows on this computer. Works fully offline.\n\n"
            f"Console\t{self.base_url}{self.console_path}\n"
            f"Data folder\t{data}\n"
            f"{py} · {os_name} {os_ver}".strip() + "\n\n"
            "© Laurent-Philippe Albou · MIT license"
        )
        self._info(f"{APP_NAME} {self.version}".strip(), body)

    # --------------------------------------------------------------- update

    def check_or_apply_update(self) -> None:
        phase = self._update_phase
        if phase == "installed":
            self.restart()
            return
        if phase == "available" and self._update_latest:
            self._offer_update(self._update_latest)
            return
        self._update_phase = "checking"
        self._force_menu_rebuild()

        def _do() -> None:
            r = self.client.check_update()
            self._handle_check_result(r)

        self._bg(_do, "tray-update-check")

    def _handle_check_result(self, r: Result) -> None:
        if not r.ok or not isinstance(r.data, dict):
            self._update_phase = "idle"
            self._force_menu_rebuild()
            self._info("Couldn't check for updates", r.detail, style="warning")
            return
        check = r.data.get("check") if isinstance(r.data.get("check"), dict) else {}
        install = r.data.get("install") if isinstance(r.data.get("install"), dict) else {}
        current = str(r.data.get("current") or self.version)
        if check.get("offline"):
            self._update_phase = "idle"
            self._force_menu_rebuild()
            self._info("Couldn't check for updates", f"{APP_NAME} couldn't reach the internet. It keeps working offline — try again when you're connected.")
            return
        if check.get("error") and check.get("latest") is None:
            self._update_phase = "idle"
            self._force_menu_rebuild()
            self._info("Couldn't check for updates", f"The update server didn't answer properly ({check.get('error')}). Try again later.", style="warning")
            return
        latest = str(check.get("latest") or "")
        if check.get("update_available") is not True:
            self._update_phase = "idle"
            self._force_menu_rebuild()
            self._info("You're up to date", f"{APP_NAME} {current} is the latest version.")
            return
        self._update_latest = latest
        if not install.get("upgradable"):
            self._update_phase = "not_possible"
            self._force_menu_rebuild()
            kind = str(install.get("kind") or "unknown")
            bodies = {
                "editable": "This copy runs from a source folder, so the menu can't update it. Update the folder with git.",
                "docker": "This copy runs in a container. Pull the new image to update.",
                "local-file": "This copy was installed from a local file. Install the newer file to update.",
            }
            body = bodies.get(kind, f"Couldn't tell how {APP_NAME} was installed. The documentation lists the update steps.")
            self._info(f"{latest} is available, but not from here", body)
            return
        self._update_phase = "available"
        self._force_menu_rebuild()
        self._offer_update(latest)

    def _offer_update(self, latest: str) -> None:
        current = self.version or "the current version"
        ok = dialogs.confirm("Update available", f"{APP_NAME} {latest} is available (you have {current}). Installing takes a minute or two; workflows keep running until you restart.", ok_label="Update Now", cancel_label="Later", danger=False)
        if ok is None:
            self._pending = {"key": "update", "label": f"Update to {latest}", "deadline": time.monotonic() + TWO_STEP_SECONDS, "action": self._start_update}
            self._force_menu_rebuild()
            return
        if ok:
            self._start_update()

    def _start_update(self) -> None:
        self._update_phase = "updating"
        self.sampler.set_override("updating")
        self._force_menu_rebuild()

        def _do() -> None:
            r = self.client.start_update()
            if not r.ok:
                self._update_phase = "available"
                self.sampler.set_override(None)
                self._force_menu_rebuild()
                self._info("The update didn't start", r.detail, style="warning")
                return
            # Poll the job until it settles.
            while not self._stopping:
                time.sleep(3.0)
                st = self.client.update_state()
                if not (st.ok and isinstance(st.data, dict)):
                    continue
                job = st.data.get("job") if isinstance(st.data.get("job"), dict) else {}
                state = str(job.get("state") or "")
                if state == "running":
                    continue
                self.sampler.set_override(None)
                if state == "succeeded":
                    self._update_phase = "installed"
                    self._force_menu_rebuild()
                    after = str(job.get("version_after") or self._update_latest or "")
                    go = dialogs.confirm("Update installed", f"{APP_NAME} {after} is ready. Restart to start using it — running workflows continue after the restart.", ok_label="Restart Now", cancel_label="Later", danger=False)
                    if go:
                        self.sampler.set_override("restarting")
                        r2 = self.client.restart(reason="update")
                        if not r2.ok:
                            self.sampler.set_override(None)
                            self._info("Couldn't restart", r2.detail, style="warning")
                else:
                    self._update_phase = "failed"
                    self._force_menu_rebuild()
                    self._info("The update didn't finish", f"{APP_NAME} {self.version} is still installed and working. Details are in the console under Resources → Gateway.", style="warning")
                return

        self._bg(_do, "tray-update")

    # ------------------------------------------------------ activity window

    def show_activity(self) -> None:
        self._reap_activity_window()
        if self._activity_proc is not None and self._activity_proc.poll() is None:
            self._notify("Activity window", "The Activity window is already open.")
            return
        handshake = {
            "base_url": self.base_url,
            "token": self.token,
            "parent_pid": os.getpid(),
            "gateway_pid": self.parent_pid,
            "data_dir": str(self.data_dir) if self.data_dir else None,
            "version": self.version,
            "console_path": self.console_path,
        }
        cmd = [sys.executable, "-m", "abstractgateway.tray", "monitor"]
        kwargs: Dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": None}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(cmd, **kwargs)
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(handshake) + "\n").encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.close()
            self._activity_proc = proc
        except Exception as exc:  # noqa: BLE001
            self._info("Couldn't open the Activity window", str(exc), style="warning")

    def _reap_activity_window(self) -> None:
        proc = self._activity_proc
        if proc is not None and proc.poll() is not None:
            self._activity_proc = None

    def _close_activity_window(self) -> None:
        proc = self._activity_proc
        self._activity_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
