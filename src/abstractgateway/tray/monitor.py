"""The Activity window (tkinter) — two live graphs, the loaded models, pause.

Runs as its OWN process (`python -m abstractgateway.tray monitor`) so Tk owns
its main thread on every platform (on macOS both AppKit and Tk insist on the
main thread; the tray process already gave it to AppKit). Talks to the
gateway through the same loopback client + sampler the tray uses.

Layout and palette follow the creative review (2026-09-05): 440×600, memory
first (the console's rule: RAM is the primary meter), fixed axes, gridlines
at 25/50/75 %, a dashed 90 % line, the gateway's own footprint as a dashed
line that is never stacked, and labelled degraded states instead of blanks.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import platform as plat
from .client import GatewayClient
from .sampler import ModelRow, Sampler, Snapshot, fmt_bytes, fmt_pct

APP_NAME = "AbstractGateway"

DARK = {
    "bg": "#0b0f14",
    "card": "#10161e",
    "grid": "#1c2632",
    "text": "#d7dee8",
    "text2": "#8593a5",
    "muted": "#748096",
    "mem_line": "#6ea8d8",
    "mem_fill": "#253647",
    "rss_line": "#3d6f9c",
    "gpu_line": "#e8a54a",
    "gpu_fill": "#403528",
    "ok": "#7bc98c",
    "warn": "#e0af68",
    "err": "#d9705a",
    "button_bg": "#161e28",
    "button_fg": "#d7dee8",
}
LIGHT = {
    "bg": "#f7f7fb",
    "card": "#ffffff",
    "grid": "#e6e8f0",
    "text": "#0f172a",
    "text2": "#334155",
    "muted": "#64748b",
    "mem_line": "#2a6396",
    "mem_fill": "#d4e5f3",
    "rss_line": "#5a8fc0",
    "gpu_line": "#8f5a0e",
    "gpu_fill": "#f8e4c9",
    "ok": "#12883e",
    "warn": "#b16105",
    "err": "#dc2626",
    "button_bg": "#eef0f5",
    "button_fg": "#0f172a",
}

STATE_WORDS = {
    "running": "Running",
    "pausing": "Pausing…",
    "paused": "Paused — still running",
    "starting": "Starting…",
    "restarting": "Restarting…",
    "updating": "Updating…",
    "stopping": "Quitting…",
    "unreachable": "Not responding",
}


class _Prefs:
    def __init__(self, data_dir: Optional[str]) -> None:
        self._path = (Path(data_dir) / "tray" / "monitor.json") if data_dir else None
        self.data: Dict[str, Any] = {"geometry": None, "keep_on_top": False}
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


class Graph:
    """One fixed-axis area+line canvas (0–100 % of `total`)."""

    def __init__(self, parent: Any, pal: Dict[str, str], *, line: str, fill: str, height: int = 110) -> None:
        import tkinter as tk

        self.pal = pal
        self.line_key = line
        self.fill_key = fill
        self.canvas = tk.Canvas(parent, height=height, bg=pal["card"], highlightthickness=0, bd=0)
        self.height = height

    def widget(self) -> Any:
        return self.canvas

    def draw(self, values: tuple, *, secondary: Optional[tuple] = None, unsupported: Optional[str] = None, stale: bool = False) -> None:
        c = self.canvas
        c.delete("all")
        w = max(50, int(c.winfo_width() or 408))
        h = self.height
        pal = self.pal
        if unsupported:
            c.create_text(w / 2, h / 2, text=unsupported, fill=pal["muted"], font=("TkDefaultFont", 11), width=w - 24, justify="center")
            return
        top, bottom, left, right = 8, h - 6, 2, w - 2
        for frac in (0.25, 0.5, 0.75):
            y = bottom - (bottom - top) * frac
            c.create_line(left, y, right, y, fill=pal["grid"], width=1)
        y90 = bottom - (bottom - top) * 0.9
        c.create_line(left, y90, right, y90, fill=pal["err"], width=1, dash=(3, 4))
        c.create_line(left, bottom, right, bottom, fill=pal["grid"], width=1)
        vals = list(values)
        n = len(vals)
        if n == 0 or all(v is None for v in vals):
            c.create_text(w / 2, h / 2, text="collecting…", fill=pal["muted"], font=("TkDefaultFont", 11))
            return

        def xy(i: int, v: float) -> tuple[float, float]:
            x = left + (right - left) * i / max(1, n - 1)
            y = bottom - (bottom - top) * max(0.0, min(100.0, float(v))) / 100.0
            return x, y

        def runs(series: List[Optional[float]]):
            cur: List[tuple[float, float]] = []
            for i, v in enumerate(series):
                if v is None:
                    if len(cur) > 1:
                        yield cur
                    cur = []
                else:
                    cur.append(xy(i, v))
            if len(cur) > 1:
                yield cur

        for run in runs(vals):
            poly = [(run[0][0], bottom)] + run + [(run[-1][0], bottom)]
            flat = [coord for pt in poly for coord in pt]
            c.create_polygon(*flat, fill=pal[self.fill_key], outline="")
            flat_line = [coord for pt in run for coord in pt]
            c.create_line(*flat_line, fill=pal[self.line_key], width=1.5, joinstyle="round", smooth=False)
        if secondary is not None:
            for run in runs(list(secondary)):
                flat_line = [coord for pt in run for coord in pt]
                c.create_line(*flat_line, fill=pal["rss_line"], width=1, dash=(4, 3))
        if stale:
            c.create_line(right - 1, top, right - 1, bottom, fill=pal["err"], width=2)


class Monitor:
    def __init__(self, handshake: Dict[str, Any]) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.base_url = str(handshake.get("base_url") or "http://127.0.0.1:8080").rstrip("/")
        self.console_path = str(handshake.get("console_path") or "/console")
        self.version = str(handshake.get("version") or "")
        self.gateway_pid = int(handshake.get("gateway_pid") or 0)
        self.tray_pid = int(handshake.get("parent_pid") or 0)
        self.client = GatewayClient(self.base_url, str(handshake.get("token") or ""))
        self.sampler = Sampler(self.client, version=self.version)
        self.prefs = _Prefs(handshake.get("data_dir"))
        dark = plat.system_prefers_dark()
        self.pal = DARK if (dark is None or dark) else LIGHT
        self._last_models_key: Optional[tuple] = None
        self._last_seen: Optional[float] = None
        self._busy = False
        self._model_rows: List[Any] = []

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} Activity")
        self.root.configure(bg=self.pal["bg"])
        self.root.minsize(400, 520)
        geometry = self.prefs.data.get("geometry")
        self.root.geometry(str(geometry) if geometry else "440x600")
        if self.prefs.data.get("keep_on_top"):
            self.root.attributes("-topmost", True)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ building

    def _build(self) -> None:
        tk, ttk, pal = self.tk, self.ttk, self.pal
        style = ttk.Style(self.root)
        try:
            if sys.platform.startswith("linux"):
                style.theme_use("clam")
        except Exception:
            pass
        style.configure("Tray.TButton", padding=(10, 4))
        style.configure("Tray.TCheckbutton", background=pal["bg"], foreground=pal["muted"])

        outer = tk.Frame(self.root, bg=pal["bg"], padx=16, pady=16)
        outer.pack(fill="both", expand=True)
        self.outer = outer

        # Header: state pill + buttons
        head = tk.Frame(outer, bg=pal["bg"])
        head.pack(fill="x")
        self.state_dot = tk.Canvas(head, width=10, height=10, bg=pal["bg"], highlightthickness=0)
        self.state_dot.pack(side="left", padx=(0, 6))
        self.state_lbl = tk.Label(head, text="Starting…", bg=pal["bg"], fg=pal["text"], font=("TkDefaultFont", 13, "bold"))
        self.state_lbl.pack(side="left")
        self.console_btn = ttk.Button(head, text="Open Console", style="Tray.TButton", command=lambda: plat.open_url(self.base_url + self.console_path))
        self.console_btn.pack(side="right")
        self.pause_btn = ttk.Button(head, text="Pause Workflows", style="Tray.TButton", command=self._toggle_pause)
        self.pause_btn.pack(side="right", padx=(0, 8))

        self.banner = tk.Label(outer, text="", bg=pal["bg"], fg=pal["err"], font=("TkDefaultFont", 11), anchor="w", justify="left", wraplength=400)
        self.banner.pack(fill="x", pady=(6, 0))

        # Memory block
        self.mem_title, self.mem_value = self._block_title(outer, "Memory", top=16)
        self.mem_sub = tk.Label(outer, text="", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 11), anchor="w")
        self.mem_sub.pack(fill="x")
        self.mem_graph = Graph(outer, pal, line="mem_line", fill="mem_fill")
        self.mem_graph.widget().pack(fill="x", pady=(4, 0))
        self._axis(outer)

        # GPU block
        self.gpu_title, self.gpu_value = self._block_title(outer, "GPU", top=16)
        self.gpu_sub = tk.Label(outer, text="", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 11), anchor="w")
        self.gpu_sub.pack(fill="x")
        self.gpu_graph = Graph(outer, pal, line="gpu_line", fill="gpu_fill")
        self.gpu_graph.widget().pack(fill="x", pady=(4, 0))
        self._axis(outer)

        # Models block
        self.models_title, _ = self._block_title(outer, "Loaded models (0)", top=16)
        self.models_frame = tk.Frame(outer, bg=pal["bg"])
        self.models_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.models_note = tk.Label(self.models_frame, text="Collecting…", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 11), anchor="w", justify="left", wraplength=400)
        self.models_note.pack(fill="x")

        # Footer
        foot = tk.Frame(outer, bg=pal["bg"])
        foot.pack(fill="x", pady=(12, 0))
        self.updated_lbl = tk.Label(foot, text="", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 11), anchor="w")
        self.updated_lbl.pack(side="left")
        self.keep_var = tk.BooleanVar(value=bool(self.prefs.data.get("keep_on_top")))
        keep = ttk.Checkbutton(foot, text="Keep on top", variable=self.keep_var, style="Tray.TCheckbutton", command=self._toggle_keep_on_top)
        keep.pack(side="right")

    def _block_title(self, parent: Any, title: str, *, top: int) -> tuple[Any, Any]:
        tk, pal = self.tk, self.pal
        row = tk.Frame(parent, bg=pal["bg"])
        row.pack(fill="x", pady=(top, 0))
        lbl = tk.Label(row, text=title, bg=pal["bg"], fg=pal["text"], font=("TkDefaultFont", 13, "bold"), anchor="w")
        lbl.pack(side="left")
        val = tk.Label(row, text="", bg=pal["bg"], fg=pal["text"], font=("TkDefaultFont", 13), anchor="e")
        val.pack(side="right")
        return lbl, val

    def _axis(self, parent: Any) -> None:
        tk, pal = self.tk, self.pal
        row = tk.Frame(parent, bg=pal["bg"])
        row.pack(fill="x")
        tk.Label(row, text="2 min ago", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 10)).pack(side="left")
        tk.Label(row, text="now", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 10)).pack(side="right")
        tk.Label(row, text="1 min ago", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 10)).pack()

    # ------------------------------------------------------------- actions

    def _toggle_pause(self) -> None:
        if self._busy:
            return
        snap = self.sampler.snapshot()
        self._busy = True
        self.pause_btn.state(["disabled"])

        def _do() -> None:
            try:
                if snap.paused or snap.gateway_state == "pausing":
                    self.client.resume()
                else:
                    self.client.pause()
                self.sampler.poke()
            finally:
                self._busy = False

        threading.Thread(target=_do, daemon=True).start()

    def _toggle_keep_on_top(self) -> None:
        on = bool(self.keep_var.get())
        try:
            self.root.attributes("-topmost", on)
        except Exception:
            pass
        self.prefs.data["keep_on_top"] = on
        self.prefs.save()

    def _unload(self, row: ModelRow) -> None:
        from tkinter import messagebox

        size = fmt_bytes(row.size_bytes) if row.size_bytes is not None else "its memory"
        if row.locked:
            ok = messagebox.askyesno(f"{row.name} is kept in memory", f"It was locked so it stays loaded. Unload it anyway? It frees {size} and loads again the next time it's needed.", icon="warning", default="no", parent=self.root)
        else:
            ok = messagebox.askyesno(f"Unload {row.name}?", f"This frees {size} of memory. The next time something needs this model it loads again from disk, which can take a while.", icon="warning", default="no", parent=self.root)
        if not ok:
            return

        def _do() -> None:
            r = self.client.unload_model(row.target, force=row.locked)
            if not r.ok and r.model_locked and not row.locked:
                r = self.client.unload_model(row.target, force=True)
            self.sampler.poke()
            if not r.ok:
                self.root.after(0, lambda: messagebox.showwarning(f"Couldn't unload {row.name}", r.detail, parent=self.root))

        threading.Thread(target=_do, daemon=True).start()

    def _on_close(self) -> None:
        try:
            self.prefs.data["geometry"] = self.root.geometry()
            self.prefs.save()
        except Exception:
            pass
        self.sampler.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------- refresh

    def _refresh(self) -> None:
        try:
            snap = self.sampler.snapshot()
            self._paint(snap)
        except Exception:
            pass
        # Exit with the gateway (the token dies with it) — the tray restarts us on demand.
        if self.gateway_pid and not plat.pid_alive(self.gateway_pid):
            self._on_close()
            return
        self.root.after(1000, self._refresh)

    def _paint(self, snap: Snapshot) -> None:
        pal = self.pal
        st = snap.gateway_state
        color = {"running": pal["ok"], "paused": pal["warn"], "pausing": pal["warn"], "unreachable": pal["err"]}.get(st, pal["muted"])
        self.state_dot.delete("all")
        self.state_dot.create_oval(1, 1, 9, 9, fill=color, outline="")
        label = STATE_WORDS.get(st, st.title())
        if st == "running" and snap.inflight_ticks > 0:
            label = f"Running · working on {snap.inflight_ticks} step{'s' if snap.inflight_ticks != 1 else ''}"
        if snap.models and st == "running" and snap.inflight_ticks == 0:
            n = len(snap.models)
            label = f"Running · {n} model{'s' if n != 1 else ''}" + (f" · {fmt_bytes(snap.models_total_bytes)}" if snap.models_total_bytes else "")
        self.state_lbl.configure(text=label, fg=pal["text"])
        reachable = st not in {"unreachable", "starting", "restarting", "stopping"}
        self.pause_btn.configure(text="Resume Workflows" if (snap.paused or st == "pausing") else "Pause Workflows")
        if reachable and not self._busy:
            self.pause_btn.state(["!disabled"])
        else:
            self.pause_btn.state(["disabled"])
        if st == "unreachable":
            self.banner.configure(text=f"Can't reach {APP_NAME}. If it's restarting, this clears in a few seconds.")
        elif snap.paused:
            self.banner.configure(text="Workflows wait until you resume. Entities with their own schedule keep going.", fg=pal["warn"])
        else:
            self.banner.configure(text="")
        if st == "unreachable":
            self.banner.configure(fg=pal["err"])

        if snap.reachable:
            self._last_seen = time.time()
        stale = not snap.reachable and snap.consecutive_failures >= 3

        # Memory
        if snap.mem_supported and snap.mem_pct is not None:
            if snap.mem_used is not None and snap.mem_total:
                self.mem_value.configure(text=f"{fmt_bytes(snap.mem_used)} of {fmt_bytes(snap.mem_total)} · {fmt_pct(snap.mem_pct)}")
            else:
                self.mem_value.configure(text=fmt_pct(snap.mem_pct))
            self.mem_value.configure(fg=pal["err"] if snap.mem_pct >= 90 else pal["text"])
            rss = f"this gateway {fmt_bytes(snap.process_rss)}" if snap.process_rss is not None else ""
            self.mem_sub.configure(text=rss)
            self.mem_graph.draw(snap.mem_history, secondary=snap.process_history, stale=stale)
        else:
            self.mem_value.configure(text="")
            self.mem_sub.configure(text="")
            self.mem_graph.draw((), unsupported="Memory figures aren't available on this computer.")

        # GPU
        if snap.gpu_supported and snap.gpu_pct is not None:
            self.gpu_value.configure(text=f"{fmt_pct(snap.gpu_pct)} busy", fg=pal["err"] if snap.gpu_pct >= 90 else pal["text"])
            self.gpu_sub.configure(text=snap.gpu_name or "")
            self.gpu_graph.draw(snap.gpu_history, stale=stale)
        elif snap.gpu_supported:
            self.gpu_value.configure(text="")
            self.gpu_graph.draw(snap.gpu_history, stale=stale)
        else:
            self.gpu_value.configure(text="")
            self.gpu_sub.configure(text="")
            self.gpu_graph.draw((), unsupported="GPU activity isn't available on this computer.")

        # Models
        key = tuple((r.key, r.locked, r.size_bytes) for r in snap.models) + (st == "unreachable", snap.models_error)
        if key != self._last_models_key:
            self._last_models_key = key
            self._rebuild_models(snap)

        # Footer
        age = (time.time() - self._last_seen) if self._last_seen else None
        if age is None:
            text = "Waiting for the first answer…"
        else:
            text = f"Updated {int(age)} s ago"
        if self.version:
            text += f" · {APP_NAME} {self.version}"
        self.updated_lbl.configure(text=text, fg=pal["err"] if (age is not None and age > 10) else pal["muted"])

    def _rebuild_models(self, snap: Snapshot) -> None:
        tk, ttk, pal = self.tk, self.ttk, self.pal
        for w in self._model_rows:
            try:
                w.destroy()
            except Exception:
                pass
        self._model_rows = []
        n = len(snap.models)
        self.models_title.configure(text=f"Loaded models ({n})")
        if snap.gateway_state == "unreachable":
            self.models_note.configure(text=f"Unavailable while {APP_NAME} isn't responding.")
            self.models_note.pack(fill="x")
            return
        if snap.models_error and not snap.models:
            self.models_note.configure(text="The model list isn't available right now.")
            self.models_note.pack(fill="x")
            return
        if not snap.models:
            self.models_note.configure(text="No models loaded. Models appear here while they are in memory.")
            self.models_note.pack(fill="x")
            return
        self.models_note.pack_forget()
        for row in snap.models:
            line = tk.Frame(self.models_frame, bg=pal["bg"])
            line.pack(fill="x", pady=2)
            tk.Label(line, text=row.name, bg=pal["bg"], fg=pal["text"], font=("TkDefaultFont", 12), anchor="w").pack(side="left")
            btn = ttk.Button(line, text="Unload", style="Tray.TButton", command=lambda r=row: self._unload(r))
            btn.pack(side="right")
            size = fmt_bytes(row.size_bytes) if row.size_bytes is not None else "size unknown"
            if row.size_source == "estimated":
                size = f"~{size}"
            tk.Label(line, text=size, bg=pal["bg"], fg=pal["text2"], font=("TkDefaultFont", 12), anchor="e", width=10).pack(side="right", padx=(0, 10))
            if row.locked:
                tk.Label(line, text="kept in memory", bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 11)).pack(side="right", padx=(0, 8))
            tk.Label(line, text=row.provider, bg=pal["bg"], fg=pal["muted"], font=("TkDefaultFont", 11)).pack(side="left", padx=(8, 0))
            self._model_rows.append(line)

    # ---------------------------------------------------------------- run

    def run(self) -> int:
        self.sampler.start()
        self.root.after(300, self._refresh)
        self.root.mainloop()
        self.sampler.stop()
        return 0


def run_monitor(handshake: Dict[str, Any]) -> int:
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"[tray] the Activity window needs tkinter, which this Python lacks ({exc}). Use the console's Resources tab instead.", file=sys.stderr)
        return 2
    return Monitor(handshake).run()
