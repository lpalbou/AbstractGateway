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

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import dialogs, platform as plat
from .client import GatewayClient, Result
from .icons import icon_signature, master_size_for_platform, render_icon
from .sampler import ModelRow, RunRow, Sampler, Snapshot, fmt_bytes, fmt_pct

logger = logging.getLogger("abstractgateway.tray")

APP_NAME = "AbstractGateway"
DOCS_URL = "https://www.lpalbou.info/AbstractGateway/"
ISSUES_URL = "https://github.com/lpalbou/abstractgateway/issues"
TWO_STEP_SECONDS = 8.0
MENU_VALUE_REBUILD_MIN_S = 15.0
MENU_RUN_ROWS = 8  # a menu is a glance; the console is the list
MEMORY_WARN_PCT = 90.0
MEMORY_WARN_SUSTAIN_S = 30
MEMORY_WARN_COOLDOWN_S = 3600.0

STATE_WORDS = {
    "running": "Running",
    "pausing": "Pausing…",
    "paused": "Paused",
    "starting": "Starting…",
    "restarting": "Restarting…",
    "updating": "Updating…",
    "stopping": "Quitting…",
    "unreachable": "Not responding",
}


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


def _middle_ellipsis(text: str, limit: int = 32) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return s[:head] + "…" + s[-tail:]


def _provider_label(provider: str) -> str:
    p = str(provider or "").strip()
    names = {"lmstudio": "LM Studio", "ollama": "Ollama", "mlx": "MLX", "huggingface": "Hugging Face", "openai": "OpenAI", "anthropic": "Anthropic", "vllm": "vLLM", "llamacpp": "llama.cpp"}
    return names.get(p.lower(), p)


def model_row_label(row: ModelRow) -> str:
    bits = [_middle_ellipsis(row.name), fmt_bytes(row.size_bytes) if row.size_bytes is not None else "size unknown", _provider_label(row.provider)]
    if row.locked:
        bits.append("kept in memory")
    return " · ".join(bits)


# A tray menu item is PLAIN TEXT on every platform pystray supports — there is
# no per-item colour to set. An emoji is the only badge that actually renders,
# and these five read at a glance without needing the word next to them.
RUN_BADGES = {
    "running": "🟢",
    "waiting": "🟡",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⚪️",
}
RUN_BADGE_UNKNOWN = "◦"


def fmt_duration(seconds: Optional[float]) -> str:
    """Coarse on purpose: a glance wants "2m 13s", never "133.42 s"."""
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _workflow_menu_name(workflow_id: str) -> str:
    """What a person calls this workflow.

    The gateway already decoded the catalog id, so what arrives is
    `bundle:flow`. A flow id that is a bare hex hash (`c53b1579`) is generated,
    not named — it identifies nothing to a reader and costs nine characters
    the bundle name needs, so it goes; a NAMED flow (`react-coding:coder`)
    stays, because there it is the half that says what ran.

    The trim keeps the HEAD: for a workflow the identity is the front of the
    name, unlike a model id where the `@4bit` tail is what tells builds apart.
    """
    name = str(workflow_id or "").strip()
    bundle, sep, flow = name.partition(":")
    if sep and flow and len(flow) >= 6 and all(c in "0123456789abcdef" for c in flow.lower()):
        name = bundle
    return name if len(name) <= 34 else name[:33] + "…"


def run_row_label(row: RunRow) -> str:
    """`✅ coding-agent:coder · 12 steps · 2m 13s`.

    The workflow name is the elastic part: it is what tells two runs apart, but
    the badge, the step count and the duration are what the operator came for,
    so they are never squeezed out by a long bundle name.
    """
    badge = RUN_BADGES.get(row.status, RUN_BADGE_UNKNOWN)
    bits = [f"{badge} {_workflow_menu_name(row.workflow_id)}"]
    if row.steps is not None:
        bits.append(f"{row.steps} step" + ("" if row.steps == 1 else "s"))
    duration = fmt_duration(row.duration_s)
    if duration:
        bits.append(duration if row.status != "running" else f"{duration} so far")
    return " · ".join(bits)


def run_tally(rows: Sequence[RunRow]) -> str:
    """The submenu's first line: what the last day amounted to."""
    if not rows:
        return "No runs in the last 24 hours"
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    order = ("running", "waiting", "completed", "failed", "cancelled")
    words = {"running": "running", "waiting": "waiting", "completed": "done", "failed": "failed", "cancelled": "cancelled"}
    parts = [f"{counts[st]} {words[st]}" for st in order if counts.get(st)]
    parts += [f"{n} {st}" for st, n in sorted(counts.items()) if st not in order]
    return f"Last 24 hours — {' · '.join(parts)}"


def state_lines(snap: Snapshot, *, update_phase: str = "idle", update_latest: Optional[str] = None) -> tuple[str, str]:
    """(header, detail) — the two disabled lines at the top of the menu."""
    st = snap.gateway_state
    header = f"{APP_NAME} — {STATE_WORDS.get(st, st.title())}"
    if st == "running":
        n = len(snap.models)
        if n == 0:
            detail = "Ready · no models loaded"
        else:
            total = f" · {fmt_bytes(snap.models_total_bytes)}" if snap.models_total_bytes else ""
            detail = f"Ready · {n} model{'s' if n != 1 else ''} loaded{total}"
        if snap.inflight_ticks > 0:
            detail = f"Working on {snap.inflight_ticks} step{'s' if snap.inflight_ticks != 1 else ''}" + (f" · {n} model{'s' if n != 1 else ''} loaded" if n else "")
    elif st == "pausing":
        n = snap.inflight_ticks
        if snap.step_gate_supported is False:
            detail = f"Finishing {n} run{'s' if n != 1 else ''} (older runtime: up to 100 steps each), then pausing"
        else:
            detail = f"Finishing {n} run{'s' if n != 1 else ''} at the next step, then pausing"
    elif st == "paused":
        detail = "Still running — workflows wait until you resume"
    elif st in {"starting", "restarting"}:
        detail = "Usually a few seconds"
    elif st == "updating":
        detail = f"Installing {update_latest or 'the update'} · workflows keep running"
    elif st == "stopping":
        detail = "Stopping workflows and the console"
    else:
        detail = "Can't reach it on this computer · retrying"
    return header, detail


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


def menu_signature(snap: Snapshot, *, update_phase: str, pending: Optional[str], tk_available: bool) -> tuple:
    """When this changes, the menu is rebuilt (see the module docstring)."""
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
    )


def tkinter_available(python: Optional[str] = None) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("tkinter") is not None and importlib.util.find_spec("_tkinter") is not None
    except Exception:
        return False


class Prefs:
    """Tiny per-machine helper preferences (NOT gateway settings): whether the
    pause explanation was shown once, and the memory warning switch."""

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
        self.sampler.add_listener(self._on_snapshot)
        self.sampler.start()
        threading.Thread(target=self._watchdog, name="tray-watchdog", daemon=True).start()
        threading.Thread(target=self._stdin_watch, name="tray-stdin", daemon=True).start()
        self._install_signals()

        def _setup(icon: Any) -> None:
            icon.visible = True
            _ready_line(True)

        try:
            self._icon.run(setup=_setup)
        finally:
            self._stopping = True
            self.sampler.stop()
            self._close_activity_window()
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
        msig = menu_signature(snap, update_phase=self._update_phase, pending=(self._pending or {}).get("key"), tk_available=self._tk)
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
                # "Loaded Models" is always there.
                self._notify("Memory is almost full", "Unloading a model or closing other apps will help. The menu's Loaded Models list shows what is using it.")
        else:
            self._mem_high_since = None

    def _notify(self, title: str, message: str) -> None:
        try:
            if self._icon is not None and getattr(self._icon, "HAS_NOTIFICATION", False):
                self._icon.notify(message, title)
        except Exception:
            logger.debug("tray: notify failed", exc_info=True)

    # ------------------------------------------------------------- the menu

    def _menu_items(self):
        import pystray

        MI, SEP = pystray.MenuItem, pystray.Menu.SEPARATOR
        snap = self._snap or self.sampler.snapshot()
        st = snap.gateway_state
        reachable = st not in {"unreachable", "starting", "restarting", "stopping"}
        header, detail = state_lines(snap, update_phase=self._update_phase, update_latest=self._update_latest)

        yield MI(header, None, enabled=False)
        yield MI(detail, None, enabled=False)
        if self._pending:
            yield SEP
            yield MI(f"Confirm: {self._pending['label']}", self._act(self._run_pending))
        yield SEP
        # ONE door to the console (operator ruling 2026-09-06). "Show Activity
        # in Console" was a second item that opened the same browser at a
        # different anchor — two entries for one destination, and the first
        # thing a new user has to choose between. The Activity WINDOW is a
        # different thing (a native window, no browser) and stays where tkinter
        # can render it.
        yield MI("Open Console", self._act(self.open_console), default=True, enabled=reachable or st == "unreachable")
        if self._tk:
            yield MI("Show Activity Window…", self._act(self.show_activity))
        yield SEP
        # WORKFLOWS: what this machine has been doing, then the one control
        # over it. Pause/Resume is deliberately the LAST item of this group —
        # it is the high-level "stop everything and let me look" lever, and it
        # reads as one only next to the list of what would stop.
        yield MI("Workflows", pystray.Menu(self._workflow_items), enabled=reachable)
        if snap.paused or st == "pausing":
            yield MI("Resume Workflows", self._act(self.resume), enabled=reachable)
        else:
            yield MI("Pause Workflows", self._act(self.pause), enabled=reachable)
        yield SEP
        if snap.mem_supported and (snap.mem_used is not None and snap.mem_total):
            yield MI(f"Memory   {fmt_bytes(snap.mem_used)} of {fmt_bytes(snap.mem_total)} ({fmt_pct(snap.mem_pct)})", None, enabled=False)
        elif snap.mem_pct is not None:
            yield MI(f"Memory   {fmt_pct(snap.mem_pct)}", None, enabled=False)
        if snap.gpu_supported and snap.gpu_pct is not None:
            yield MI(f"GPU   {fmt_pct(snap.gpu_pct)} busy", None, enabled=False)
        yield MI(f"Loaded Models ({len(snap.models)})", pystray.Menu(self._model_items), enabled=reachable)
        yield SEP
        yield MI(self._update_label(), self._act(self.check_or_apply_update), enabled=reachable and self._update_phase not in {"checking", "updating"} and not snap.update_job_running)
        yield MI(f"Restart {APP_NAME}…", self._act(self.restart), enabled=reachable and snap.can_restart)
        yield MI(
            "Help",
            pystray.Menu(
                # NO "(needs internet)" / "(on this computer)" suffixes
                # (operator ruling 2026-09-06). A menu item names the thing it
                # opens; annotating where it lives is noise the reader has to
                # step over every time, and the browser says so anyway the one
                # time it matters.
                MI("Documentation", self._act(lambda: plat.open_url(DOCS_URL))),
                MI("Report a Problem…", self._act(lambda: plat.open_url(ISSUES_URL))),
                MI("Developer API Reference", self._act(lambda: plat.open_url(self.base_url + "/docs"))),
                MI("Copy Console Link", self._act(self.copy_console_link)),
                MI("Warn when memory is almost full", self._act(self.toggle_memory_warn), checked=lambda _i: bool(self.prefs.data.get("memory_warn", True))),
                SEP,
                MI(f"About {APP_NAME}…", self._act(self.about)),
            ),
        )
        yield SEP
        # NO "Hide the icon" ITEM (operator ruling 2026-09-06). The icon IS
        # the gateway's presence on the desktop: while it runs, it is there.
        # An icon a user can make disappear is an icon a user loses — and the
        # only way back was a console setting they had no reason to look for.
        if st == "unreachable":
            yield MI(f"Force Quit {APP_NAME}…", self._act(self.force_quit))
        else:
            yield MI(f"Quit {APP_NAME}…", self._act(self.quit_gateway), enabled=snap.can_shutdown or not reachable)

    def _workflow_items(self):
        """The last day of runs: a tally, then the runs themselves.

        Every row is INFORMATION, not an action. There is no per-run page to
        deep-link to, and a list where each row opens the same console tab
        would be five buttons pretending to be five destinations — so the one
        real action sits at the bottom, once.
        """
        import pystray

        MI, SEP = pystray.MenuItem, pystray.Menu.SEPARATOR
        snap = self._snap or self.sampler.snapshot()
        rows = snap.runs
        if snap.runs_error and not rows:
            yield MI("Run list unavailable", None, enabled=False)
            yield MI(_middle_ellipsis(str(snap.runs_error), 44), None, enabled=False)
            return
        yield MI(run_tally(rows), None, enabled=False)
        if rows:
            yield SEP
            for row in rows[:MENU_RUN_ROWS]:
                yield MI(run_row_label(row), None, enabled=False)
            if len(rows) > MENU_RUN_ROWS:
                yield MI(f"…and {len(rows) - MENU_RUN_ROWS} more", None, enabled=False)
        yield SEP
        yield MI("Open Runs in Console", self._act(self.open_runs), enabled=snap.reachable)

    def _model_items(self):
        import pystray

        MI = pystray.MenuItem
        snap = self._snap or self.sampler.snapshot()
        if snap.models_error and not snap.models:
            yield MI("Model list unavailable", None, enabled=False)
            return
        if not snap.models:
            yield MI("No models loaded", None, enabled=False)
            return
        for row in snap.models:
            yield MI(model_row_label(row), self._act(lambda r=row: self.unload(r)))

    def _update_label(self) -> str:
        p = self._update_phase
        if p == "checking":
            return "Checking for Updates…"
        if p == "available" and self._update_latest:
            return f"Update to {self._update_latest}…"
        if p == "updating":
            return "Updating…"
        if p == "installed":
            return "Restart to Finish Update…"
        return "Check for Updates…"

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
        plat.open_url(self.base_url + self.console_path)

    def open_runs(self) -> None:
        """The console's Runtimes tab, where runs actually live."""
        plat.open_url(self.base_url + self.console_path + "#runtimes")

    def copy_console_link(self) -> None:
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
        if row.locked:
            title = f"{row.name} is kept in memory"
            body = f"It was locked so it stays loaded. Unload it anyway? It frees {size} and loads again the next time it's needed."
            ok = "Unload Anyway"
        else:
            title = f"Unload {row.name}?"
            body = f"{frees} The next time something needs this model it loads again from disk, which can take a while."
            ok = "Unload"

        def _do() -> None:
            self._bg(lambda: self._unload_now(row, force=row.locked), "tray-unload")

        self._confirm(f"unload:{row.key}", f"Unload {_middle_ellipsis(row.name, 24)}", title, body, ok_label=ok, danger=True, action=_do)

    def _unload_now(self, row: ModelRow, *, force: bool) -> None:
        r = self.client.unload_model(row.target, force=force)
        if not r.ok and r.model_locked and not force:
            # The lock surfaced only now: ask once more, with the locked copy.
            body = f"It was locked so it stays loaded. Unload it anyway? It frees {fmt_bytes(row.size_bytes) if row.size_bytes is not None else 'its memory'}."
            if dialogs.confirm(f"{row.name} is kept in memory", body, ok_label="Unload Anyway", danger=True) is True:
                r = self.client.unload_model(row.target, force=True)
            else:
                return
        self.sampler.poke()
        if r.ok:
            freed = f" · freed {fmt_bytes(row.size_bytes)}" if row.size_bytes is not None else ""
            self._notify("Model unloaded", f"{row.name}{freed}")
        else:
            self._info(f"Couldn't unload {row.name}", r.detail, style="warning")

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
