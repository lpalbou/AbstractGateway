"""The redesigned tray menu (2026-11-01) as DATA: `build_menu(inputs) -> (Node, ...)`.

Same contract as `menu_model.build_menu` (pure: no pystray, no network, no
clock; the same `Node`, the same action tuples, so `app.py`'s dispatch table
and handlers serve both menus unchanged) with a different shape. The classic
menu stays in `menu_model.py`, byte for byte: `serve --tray-menu classic` or
`"menu_style": "classic"` in `<data_dir>/tray/prefs.json` brings it back.

What changed and why (the operator's complaints of 2026-11-01: not user
friendly, not organised, repetitive):

1. **A header, not a status line.** On macOS the first row is a native view
   (`header_mac.py`): the AbstractFramework logo, the product name, one line
   saying what the gateway is doing (`2 conversations running · 1 model
   loaded`, `Paused — conversations wait until you resume`) and a switch —
   ON while conversations run, OFF while the gateway is paused. Windows and
   Linux get the name (Linux with the logo beside it), the subtitle and a
   `☑ Running` check row that is the same switch. Pause / Resume is nowhere
   else: the switch is the control. The product name appears here and on
   Quit (the platform convention), not on six rows. The meters follow as one
   line of percentages.
2. **Two doors, then what needs you.** *Open Console* (the default item) and
   *Open Assistant* (the desktop app, promoted from Apps ▸) sit alone at the
   top. Anything waiting for a person — an approval, a question — follows as
   its own top-level rows (at most three, then one line naming the rest):
   the most actionable thing on the machine is never a submenu away.
3. **Sections in the order a person thinks.** *Act* (Console, Assistant,
   Needs you) · *Look* (Conversations, Automations, Models, Apps) · *Set up*
   (Connect, Settings, Help) · *Lifecycle* (Update when one is known, Pause,
   Restart, Quit). Automations are their own section: they are standing
   orders, not conversations. Each submenu opens with an information line.
4. **Models ▸ is about memory first.** The full meters lead, then what is in
   memory with one *Eject* row per model and *Eject All* when there are
   several: the tray exists to free the machine. *Load a Model ▸* stays as
   the second thing, the Activity window and the console's Resources tab at
   the foot.
5. **Less duplication.** *Addresses ▸* and *Network ▸* were two views of one
   thing and are now *Connect ▸*. *Check for Updates…* lives in Help ▸ until
   an update is known, then the update's state is a top-level row. The
   memory-warning switch moved from Help to Settings ▸, next to *Start at
   login*.
6. **One label grammar.** Verb first (*Open X*, *Install X…*, *Eject X…*),
   `…` only when a dialog or confirmation follows, a check mark only on a
   setting, radio marks only on *who can reach it*, badges only the five
   that render on every platform.

Everything the classic menu could do is still reachable (ADR-0026: nothing
silently dropped): `test_gateway_tray_menu_v2.py` checks that every classic
action name is produced by the new menu too.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

from . import menu_model as mm
from .menu_model import SEP, MenuInputs, Node, info
from .sampler import AutomationRow, Snapshot, fmt_bytes, fmt_pct

STYLE = "v2"
CLASSIC = "classic"
STYLES = (STYLE, CLASSIC)
#: Top-level "Needs you" rows before the rest is named in one line.
ATTENTION_ROWS = 3


def menu_style(*, flag: Optional[str], prefs: Optional[str]) -> str:
    """Which menu a tray shows: the launch flag (`serve --tray-menu`) for this
    run, else the machine's preference, else the redesign. An unknown word is
    ignored, never a crash: a typo in prefs.json still shows a menu."""
    for word in (flag, prefs):
        w = str(word or "").strip().lower()
        if w in STYLES:
            return w
    return STYLE


# ---------------------------------------------------------------------------
# The header
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderView:
    """What the native header shows (macOS) — and what the text fallback says."""

    title: str  # "AbstractGateway"
    subtitle: str  # what the gateway is doing, one line
    state: str  # the snapshot's gateway_state
    switch_on: Optional[bool]  # True: conversations run; False: paused; None: no switch
    switch_enabled: bool
    switch_tooltip: str


@dataclass(frozen=True)
class HeaderNode(Node):
    """The first row: `label` is the text fallback (an information line on
    Windows / Linux); `view` is what the macOS native header draws."""

    view: Optional[HeaderView] = None


def header_line(snap: Snapshot) -> str:
    """`AbstractGateway 0.18.0 — Running`: the header row's text — the product
    name ONCE, its version, its state. On macOS it is what VoiceOver reads for
    the native row; on Windows and Linux it is the row itself."""
    word = mm.STATE_WORDS.get(snap.gateway_state, snap.gateway_state.title())
    version = str(snap.version or "").strip()
    return f"{mm.APP_NAME} {version} — {word}" if version else f"{mm.APP_NAME} — {word}"


def header_title(snap: Snapshot) -> str:
    """`AbstractGateway 0.18.0`: the text header's first line on Windows and
    Linux, where the state is on the subtitle and the *Running* toggle."""
    version = str(snap.version or "").strip()
    return f"{mm.APP_NAME} {version}" if version else mm.APP_NAME


def _counts(snap: Snapshot) -> Dict[str, int]:
    out = {"running": 0, "attention": 0, "paused": 0, "done": 0}
    for row in snap.runs:
        out[mm.run_group(row)] += 1
    return out


def activity_line(snap: Snapshot, *, update_latest: Optional[str] = None) -> str:
    """What the machine is doing, in words — the header's subtitle. A running
    gateway: `2 conversations running · 1 needs you · 1 model loaded` / `Idle
    · no model loaded`; other states keep the classic detail (pausing,
    paused, starting, updating, stopping, unreachable)."""
    st = snap.gateway_state
    if st == "running":
        c = _counts(snap)
        active = c["running"] + c["paused"]
        parts: List[str] = []
        if snap.slow:
            parts.append("Slow to answer")
        parts.append(f"{active} conversation{'s' if active != 1 else ''} running" if active else "Idle")
        if c["attention"]:
            parts.append(f"{c['attention']} need{'s' if c['attention'] == 1 else ''} you")
        n = len(snap.models)
        parts.append(f"{n} model{'s' if n != 1 else ''} loaded" if n else "no model loaded")
        return " · ".join(parts)
    _, detail = mm.state_lines(snap, update_phase="idle", update_latest=update_latest)
    return detail or ""


def header_view(snap: Snapshot, *, update_latest: Optional[str] = None) -> HeaderView:
    """The native header: the name, the activity line (or the state's words
    while it is not plainly running), and the switch — ON while
    conversations run, OFF while paused; greyed while it cannot be told."""
    st = snap.gateway_state
    reachable = st not in {"unreachable", "starting", "restarting", "stopping"}
    word = mm.STATE_WORDS.get(st, st.title())
    activity = activity_line(snap, update_latest=update_latest)
    if st == "running" or activity.startswith(word):
        subtitle = activity  # the classic detail already leads with the state word ("Paused — …")
    else:
        subtitle = f"{word} · {activity}" if activity else word
    if st in {"paused", "pausing"} or snap.paused:
        on: Optional[bool] = False
    elif st in {"running", "updating"}:
        on = True
    else:
        on = None if st == "unreachable" else True
    enabled = reachable and st not in {"pausing", "updating"}
    tip = "Switch off to pause: no conversation takes a new step until you switch it back on. The console keeps answering." if on is not False else "Switch on to resume the conversations."
    return HeaderView(title=mm.APP_NAME, subtitle=subtitle, state=st, switch_on=on, switch_enabled=enabled, switch_tooltip=tip)


def meters_line(snap: Snapshot) -> str:
    """`Memory 32% · this gateway 10.9 GB · GPU 20%` — percentages at a
    glance; the full figures (of 128 GB, the GPU's name) are in Models ▸."""
    parts: List[str] = []
    if snap.mem_supported and snap.mem_pct is not None:
        parts.append(f"Memory {fmt_pct(snap.mem_pct)}")
    share = mm.gateway_share_words(snap)
    if share and parts:
        parts.append(share)
    if snap.gpu_supported and snap.gpu_pct is not None:
        parts.append(f"GPU {fmt_pct(snap.gpu_pct)}")
    return " · ".join(parts)


def toggle_row(view: HeaderView) -> Node:
    """The switch as a check item, for menus that cannot hold a control
    (Windows, Linux): `☑ Running` — unticking pauses the gateway, ticking
    resumes it; greyed while it cannot be told."""
    if view.switch_on is None:
        return Node("Running", None, enabled=False, checked=False)
    action = ("pause",) if view.switch_on else ("resume",)
    return Node("Running", action, checked=bool(view.switch_on), enabled=bool(view.switch_enabled))


def header_nodes(snap: Snapshot, *, native: bool, update_latest: Optional[str] = None) -> Tuple[Node, ...]:
    """The first rows. Native (macOS): ONE `HeaderNode` — logo, name,
    subtitle and the switch in one row. Text (Windows, Linux): a `HeaderNode`
    whose label is the name and version (Linux draws the logo next to it),
    the subtitle as an information line, and the `☑ Running` toggle."""
    view = header_view(snap, update_latest=update_latest)
    if native:
        return (HeaderNode(header_line(snap), None, enabled=False, view=view),)
    out: List[Node] = [HeaderNode(header_title(snap), None, enabled=False, view=view)]
    if view.subtitle:
        out.append(info(view.subtitle))
    out.append(toggle_row(view))
    return tuple(out)


# ---------------------------------------------------------------------------
# Act
# ---------------------------------------------------------------------------


def assistant_door(inputs: MenuInputs, *, reachable: bool) -> Tuple[Node, ...]:
    """*Open Assistant* at the top when the desktop app can be opened,
    *Install Assistant…* when the gateway can install it, a progress line
    while it installs; nothing when it is neither (Apps ▸ then lists it,
    greyed)."""
    if not inputs.apps_fetched:
        return ()
    for a in inputs.apps:
        if a.id != "assistant":
            continue
        if a.status in {"available", "running"}:
            out = [Node(f"Open {a.name}", ("assistant_launch",))]
            if a.notice:
                out.append(info(a.notice))
            return tuple(out)
        if a.status == "installing":
            pct = f" {int(a.job_percent)}%" if isinstance(a.job_percent, (int, float)) else ""
            return (info(f"{a.name} — installing{pct}…"),)
        if a.status == "not_installed" and a.install_available:
            return (Node(f"Install {a.name}…", ("app_install", a.id), enabled=reachable),)
    return ()


def _assistant_shown_at_top(inputs: MenuInputs, *, reachable: bool) -> bool:
    return bool(assistant_door(inputs, reachable=reachable))


def attention_rows(snap: Snapshot, *, observer: bool) -> Tuple[Node, ...]:
    """**Needs you (N)** at the top level: the approvals and questions, each
    with the classic row submenu (what is asked, Show and approve…, Deny,
    Open in Observer); at most `ATTENTION_ROWS`, then one line for the rest."""
    rows = [r for r in snap.runs if mm.run_group(r) == "attention"]
    if not rows:
        return ()
    out: List[Node] = [info(f"Needs you ({len(rows)})")]
    for row in rows[:ATTENTION_ROWS]:
        out.append(Node(mm.run_row_label(row), children=mm.run_submenu(row, observer=observer, reachable=snap.reachable)))
    if len(rows) > ATTENTION_ROWS:
        out.append(info(f"…and {len(rows) - ATTENTION_ROWS} more under Conversations"))
    return tuple(out)


# ---------------------------------------------------------------------------
# Look
# ---------------------------------------------------------------------------


def conversations_items(snap: Snapshot, *, observer: bool) -> Tuple[Node, ...]:
    """**Conversations ▸** — the complete inventory of conversations (the
    top-level *Needs you* rows are shortcuts into it): Running, Need
    attention, Recent (the last 24 hours, `MENU_RECENT_ROWS` then one line
    naming the rest), and the console door. Automations have their own
    section."""
    reachable = snap.reachable
    groups: Dict[str, List[mm.RunRow]] = {"running": [], "attention": [], "paused": [], "done": []}
    for row in snap.runs:
        groups[mm.run_group(row)].append(row)
    out: List[Node] = []
    if snap.runs_error and not snap.runs:
        out += [info("Conversation list unavailable"), info(mm.middle_ellipsis(str(snap.runs_error), 60))]
    else:
        running = groups["running"] + groups["paused"]
        out.append(info(f"Running ({len(running)})" if running else "Running — none"))
        for row in running:
            out.append(Node(mm.run_row_label(row), children=mm.run_submenu(row, observer=observer, reachable=reachable)))
        if groups["attention"]:
            out.append(SEP)
            out.append(info(f"Need attention ({len(groups['attention'])})"))
            for row in groups["attention"]:
                out.append(Node(mm.run_row_label(row), children=mm.run_submenu(row, observer=observer, reachable=reachable)))
        done = groups["done"]
        out.append(SEP)
        out.append(info(mm.recent_tally(done)))
        for row in done[: mm.MENU_RECENT_ROWS]:
            out.append(Node(mm.run_row_label(row), children=mm.run_submenu(row, observer=observer, reachable=reachable)))
        if len(done) > mm.MENU_RECENT_ROWS:
            out.append(info(f"…and {len(done) - mm.MENU_RECENT_ROWS} more"))
    out.append(SEP)
    out.append(Node("Open Conversations in Console", ("open_runs",), enabled=reachable))
    return tuple(out)


def automations_items(snap: Snapshot, *, observer: bool) -> Tuple[Node, ...]:
    """**Automations ▸** — the standing orders (`GET /automations`): Active
    (`⏱ <title> · Every day at 09:00 · next …`), then **Paused (N) ▸**, a
    folded submenu (ADR 0048), each row with the classic submenu (Open in
    Observer, Run now, Pause / Resume), then the Automations page in Observer.
    Nothing is computed by the tray: the order, the sections, the trigger words
    and the next run are the gateway's."""
    reachable = snap.reachable
    # The standing ones (active or paused), split by the served section (ADR 0048).
    standing = [a for a in snap.automations if a.status in {"active", "paused"}]
    active = [a for a in standing if a.section == "active"]
    paused = [a for a in standing if a.section == "paused"]
    out: List[Node] = []
    if snap.automations_error and not (active or paused):
        out += [info("Automation list unavailable"), info(mm.middle_ellipsis(str(snap.automations_error), 60))]
    elif not (active or paused):
        out.append(info("No automations yet"))

    def row(a: AutomationRow) -> Node:
        return Node(mm.automation_row_label(a), children=mm.automation_submenu(a, observer=observer, reachable=reachable, observer_path=snap.observer_automations_path))

    if active:
        out.append(info(f"Active ({len(active)})"))
        out.extend(row(a) for a in active)
    if paused:
        if active:
            out.append(SEP)
        out.append(Node(f"Paused ({len(paused)})", children=tuple(row(a) for a in paused)))
    out.append(SEP)
    out.append(Node("Open Automations in Observer", ("open_automations_observer",), enabled=bool(observer and reachable and snap.observer_automations_path)))
    return tuple(out)


def _eject_label(row: mm.ModelRow) -> str:
    size = fmt_bytes(row.size_bytes) if row.size_bytes is not None else "size unknown"
    bits = [size, mm.provider_label(row.provider)]
    if row.locked:
        bits.append("kept in memory")
    return f"Eject {mm.short_model_name(row.name)} ({', '.join(bits)})…"


def models_items(inputs: MenuInputs, *, reachable: bool) -> Tuple[Node, ...]:
    """**Models ▸** — memory first. The full meters; what is in memory, one
    *Eject X (size, engine)…* row per loaded model (the eject asks first, so
    a direct row is safe) and *Eject All — frees N…* with several; the
    gateway's own held-memory and pending-eject lines (classic); then *Load
    a Model ▸* (classic: your defaults, then every installed model by
    engine), the Activity window and the console's Resources tab."""
    snap, mv = inputs.snap, inputs.models
    out: List[Node] = []
    # The meters, in full.
    if snap.mem_supported and snap.mem_used is not None and snap.mem_total:
        share = mm.gateway_share_words(snap)
        out.append(info(f"Memory {fmt_bytes(snap.mem_used)} of {fmt_bytes(snap.mem_total)} ({fmt_pct(snap.mem_pct)})" + (f" · {share}" if share else "")))
    if snap.gpu_supported and snap.gpu_pct is not None:
        out.append(info(f"GPU {fmt_pct(snap.gpu_pct)} busy" + (f" · {snap.gpu_name}" if snap.gpu_name else "")))
    if out:
        out.append(SEP)
    # What is in memory.
    loaded = list(snap.models)
    total = snap.models_total_bytes
    held = mm._held_bytes(snap)
    if loaded:
        out.append(info(f"In memory ({len(loaded)})" + (f" · {fmt_bytes(total)}" if total else "")))
    elif snap.models_error:
        out.append(info(f"Loaded models unavailable: {mm.middle_ellipsis(snap.models_error, 60)}"))
    elif held:
        out.append(info(f"Gateway still holds {fmt_bytes(held)} (no model listed)"))
        for text in mm.held_lines(snap):
            out.append(info(mm.middle_ellipsis(text, 90)))
        out.append(info("Eject the held model in the Console, or restart the gateway to free it"))
    else:
        out.append(info("Nothing in memory"))
    if held and loaded:
        out.append(info(mm.middle_ellipsis(f"Gateway memory measured by {snap.device_held_basis or 'basis not reported'}", 90)))
    for text in mm.eject_lines(snap):
        out.append(info(mm.middle_ellipsis(text, 90)))
    for key in mv.loading:
        out.append(info(f"Loading {mm.short_model_name(key.split('/', 1)[-1])}…"))
    for row in loaded:
        busy = row.key in mv.ejecting
        label = f"Ejecting {mm.short_model_name(row.name)}…" if busy else _eject_label(row)
        out.append(Node(label, ("eject", row.key), enabled=reachable and not busy))
    if len(loaded) > 1:
        frees = f" — frees {fmt_bytes(total)}" if total else ""
        out.append(Node(f"Eject All{frees}…", ("eject_all",), enabled=reachable and not mv.ejecting))
    out.append(SEP)
    out.append(Node("Load a Model", children=mm.load_submenu(inputs, reachable=reachable), enabled=reachable))
    out.append(SEP)
    if inputs.tk_available:
        out.append(Node("Show Activity Window…", ("show_activity",)))
    out.append(Node("Open Resources in Console…", ("open_console_tab", "models"), enabled=reachable))
    return tuple(out)


def apps_section(inputs: MenuInputs, *, reachable: bool) -> Node:
    """**Apps ▸** — the classic rows. The Assistant's rows are left out while
    the top-level door shows them (one place, not two); when the Assistant is
    neither openable nor installable it stays here, greyed, as before."""
    classic = mm.apps_section(inputs, reachable=reachable)
    if not _assistant_shown_at_top(inputs, reachable=reachable):
        return classic
    assistant = next((a for a in inputs.apps if a.id == "assistant"), None)

    def _is_assistant_row(n: Node) -> bool:
        if assistant is None or n.separator:
            return False
        name = assistant.name
        return (
            n.label in {f"Launch {name}", f"Install {name}…", name}
            or n.label.startswith(f"{name} — installing")
            or (bool(assistant.notice) and n.label == assistant.notice)
        )

    cleaned: List[Node] = []
    for n in classic.children or ():
        if _is_assistant_row(n):
            continue
        if n.separator and (not cleaned or cleaned[-1].separator):
            continue  # a doubled separator the removal would leave
        cleaned.append(n)
    while cleaned and cleaned[-1].separator:
        cleaned.pop()
    return replace(classic, children=tuple(cleaned))


# ---------------------------------------------------------------------------
# Set up
# ---------------------------------------------------------------------------


def connect_section(inputs: MenuInputs, *, reachable: bool) -> Node:
    """**Connect ▸** — Addresses ▸ and Network ▸ as ONE place: where the
    gateway answers now, every address (a click copies it), who may reach it
    (the three radio choices), the restart that applies a change, the
    gateway's warnings, and the console's Network tab."""
    nv = inputs.network
    base = inputs.base_url.rstrip("/")
    label = "Connect" + (" — restart required" if nv.restart_required else "")
    items: List[Node] = []
    if nv.available:
        eff = mm.NETWORK_WORDS.get(str(nv.effective_mode or ""), str(nv.effective_mode or "unknown"))
        where = f"{nv.bind_host}:{nv.port}" if nv.bind_host and nv.port else (f"port {nv.port}" if nv.port else "")
        items.append(info(f"Now: {eff}" + (f" · {where}" if where else "")))
        if nv.overridden_by_cli:
            items.append(info("Started with an explicit --host/--port: the choice below applies when started without them"))
    else:
        items.append(info(mm.middle_ellipsis(nv.error or "Reading the network settings…", 80)))
    items.append(SEP)
    items.append(info("Addresses — click one to copy it"))
    if nv.available and nv.addresses:
        items.extend(Node(mm.address_label(a), ("copy_address", a.url)) for a in nv.addresses)
    else:
        items.append(Node(f"{base} · {mm.ADDRESS_WORDS['loopback']}", ("copy_address", base)))
    if nv.available:
        items.append(SEP)
        items.append(info("Who can reach it"))
        busy = nv.busy
        for mode, words in mm.NETWORK_MODES:
            text = words + ("…" if mode == "internet" else "")
            items.append(Node(text, ("network_set", mode), checked=(nv.configured_mode == mode), radio=True, enabled=reachable and not busy))
        if nv.restart_required:
            items.append(Node(f"Restart to apply ({mm.NETWORK_WORDS.get(str(nv.configured_mode), nv.configured_mode)})", ("network_restart",), enabled=reachable and not busy))
        if nv.auth_ok_for_mode is False:
            items.append(info("⚠ Sign-in is not set up for this mode" + (f": {mm.middle_ellipsis(nv.auth_fix, 70)}" if nv.auth_fix else "")))
        for w in nv.warnings:
            items.append(info("⚠ " + mm.middle_ellipsis(w, 80)))
    items.append(SEP)
    items.append(Node("Open Network in Console…", ("open_console_tab", "network"), enabled=reachable))
    return Node(label, children=tuple(items))


AUTOSTART_SHORT = "Start at login"


def settings_section(inputs: MenuInputs, *, reachable: bool) -> Node:
    """**Settings ▸** — the per-machine switches: *Start at login* (the
    classic item, its state logic unchanged, the product name dropped from
    the label), *Warn when memory is almost full*, *Classic menu* (back to the
    previous layout at the next menu open; the preference is remembered)."""
    items: List[Node] = []
    for n in mm.autostart_nodes(inputs.autostart):
        items.append(replace(n, label=n.label.replace(mm.AUTOSTART_LABEL, AUTOSTART_SHORT, 1)))
    items.append(Node("Warn when memory is almost full", ("toggle_memory_warn",), checked=bool(inputs.memory_warn)))
    items.append(SEP)
    items.append(Node("Classic menu", ("set_menu_style", CLASSIC), checked=False))
    return Node("Settings", children=tuple(items))


def help_section(inputs: MenuInputs, *, reachable: bool, update_at_top: bool) -> Node:
    """**Help ▸** — the links, the console link to paste, *Check for
    Updates…* (while no update is known), About."""
    items: List[Node] = [
        Node("Documentation", ("open_url", mm.DOCS_URL)),
        Node("Report a Problem…", ("open_url", mm.ISSUES_URL)),
        Node("Developer API Reference", ("open_url", inputs.base_url.rstrip("/") + "/docs")),
        Node("Copy Console Link", ("copy_console_link",)),
    ]
    if not update_at_top:
        items.append(SEP)
        items.append(update_node(inputs, reachable=reachable))
    items.append(SEP)
    items.append(Node(f"About {mm.APP_NAME}…", ("about",)))
    return Node("Help", children=tuple(items))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

UPDATE_TOP_PHASES = ("checking", "available", "updating", "installed")


def update_node(inputs: MenuInputs, *, reachable: bool) -> Node:
    snap = inputs.snap
    return Node(
        mm.update_label(inputs.update_phase, inputs.update_latest),
        ("check_update",),
        enabled=reachable and inputs.update_phase not in {"checking", "updating"} and not snap.update_job_running,
    )


def lifecycle_nodes(inputs: MenuInputs, *, reachable: bool, update_at_top: bool) -> Tuple[Node, ...]:
    """The last section: the update's state when one is known, Restart…,
    Quit (the product name here, the platform convention: quitting stops the
    gateway, not just the icon). Pause / Resume is the header's switch (or
    the `☑ Running` row under the text header), not a row here."""
    snap = inputs.snap
    st = snap.gateway_state
    out: List[Node] = []
    if update_at_top:
        out.append(update_node(inputs, reachable=reachable))
    out.append(Node("Restart…", ("restart",), enabled=reachable and snap.can_restart))
    if st == "unreachable":
        out.append(Node(f"Force Quit {mm.APP_NAME}…", ("force_quit",)))
    else:
        out.append(Node(f"Quit {mm.APP_NAME}…", ("quit",), enabled=snap.can_shutdown or not reachable))
    return tuple(out)


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


def build_menu(inputs: MenuInputs, *, native_header: bool = False) -> Tuple[Node, ...]:
    """`native_header=True` (macOS with AppKit): the first row is a
    `HeaderNode` the renderer draws as logo · name · subtitle · switch;
    otherwise the header is text lines."""
    snap = inputs.snap
    st = snap.gateway_state
    reachable = st not in {"unreachable", "starting", "restarting", "stopping"}
    observer = mm.observer_open_available(inputs.apps)
    update_at_top = inputs.update_phase in UPDATE_TOP_PHASES

    out: List[Node] = list(header_nodes(snap, native=native_header, update_latest=inputs.update_latest))
    meters = meters_line(snap)
    if meters:
        out.append(info(meters))
    if inputs.pending_label:
        out += [SEP, Node(f"Confirm: {inputs.pending_label}", ("confirm_pending",))]
    out.append(SEP)
    # Act: the two doors, then what needs a person.
    out.append(Node("Open Console", ("open_console",), default=True, enabled=reachable or st == "unreachable"))
    out.extend(assistant_door(inputs, reachable=reachable))
    needs = attention_rows(snap, observer=observer)
    if needs:
        out.append(SEP)
        out.extend(needs)
    out.append(SEP)
    # Look: what is going on, the standing orders, what is in memory, the apps.
    out.append(Node("Conversations", children=conversations_items(snap, observer=observer), enabled=reachable))
    out.append(Node("Automations", children=automations_items(snap, observer=observer), enabled=reachable))
    out.append(Node("Models", children=models_items(inputs, reachable=reachable), enabled=reachable))
    out.append(apps_section(inputs, reachable=reachable))
    out.append(SEP)
    # Set up.
    out.append(connect_section(inputs, reachable=reachable))
    out.append(settings_section(inputs, reachable=reachable))
    out.append(help_section(inputs, reachable=reachable, update_at_top=update_at_top))
    out.append(SEP)
    # Lifecycle.
    out.extend(lifecycle_nodes(inputs, reachable=reachable, update_at_top=update_at_top))
    return tuple(out)


def action_names(nodes: Sequence[Node]) -> set:
    return {n.action[0] for n in mm.iter_nodes(nodes) if n.action}


def top_level_labels(nodes: Sequence[Node]) -> List[str]:
    return [n.label for n in nodes if not n.separator]


__all__ = [
    "ATTENTION_ROWS",
    "CLASSIC",
    "STYLE",
    "STYLES",
    "HeaderNode",
    "HeaderView",
    "action_names",
    "activity_line",
    "attention_rows",
    "automations_items",
    "build_menu",
    "connect_section",
    "conversations_items",
    "header_line",
    "header_nodes",
    "header_title",
    "header_view",
    "help_section",
    "lifecycle_nodes",
    "menu_style",
    "meters_line",
    "models_items",
    "settings_section",
    "toggle_row",
    "top_level_labels",
]
