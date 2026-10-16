"""The tray menu as DATA: `build_menu(inputs) -> (Node, ...)`.

Pure: no pystray, no network, no clock. `app.py` renders the nodes onto
pystray and maps each node's `action` tuple to a method; the unit tests read
the nodes; `render_text(...)` prints the menu as text. Three things the
pure model makes checkable that a pystray generator could not:

- **Nothing is silently dropped** (ADR-0026). A long list becomes submenus
  (`chunk`), never a truncated one; every installed model and every app has
  a row, and a row that cannot act says why (a disabled line).
- **Every backend gets the same menu.** pystray's macOS, Win32 and
  AppIndicator backends all draw submenus and check marks; a panel that drops
  submenus gets `flatten(...)` — the same rows, prefixed with their path
  ("Models › Load a Model › MLX › …") — chosen explicitly (tray prefs
  `flat_menu`), never guessed.
- **Model menu design.** "Models ▸" leads with what is IN memory (each loaded
  model is a submenu whose one action is Eject, so a click on a checkmarked
  row never unloads by surprise), then "Load a Model ▸": your configured
  defaults first (the only place a capability is KNOWN — it is the route you
  configured), then one submenu per engine, recognised catalog models first,
  then A–Z. Grouping by capability (text / image / voice / music) was
  evaluated on the real machine and rejected: 136 of 151 installed artifacts
  carry no capability metadata (contract D rows have none; the LLM discovery
  filter lists every Hugging Face repo as text), so a capability tree would be
  a guess dressed up as a fact. The engine is always known and it is what
  decides how a model loads.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .apps import AppEntry
from .sampler import ACTIVE_STATUSES, AutomationRow, ModelRow, RunRow, Snapshot, fmt_bytes, fmt_pct

APP_NAME = "AbstractGateway"
DOCS_URL = "https://www.lpalbou.info/AbstractGateway/"
ISSUES_URL = "https://github.com/lpalbou/abstractgateway/issues"
MENU_RECENT_ROWS = 5  # finished sessions shown; a menu is a glance, the console is the list
APPROVAL_BODY_MAX = 1500  # the approval dialog shows the arguments in full up to this
CHUNK = 30  # rows per submenu before a list is split (A–F, G–M, …)
PATH_SEP = " › "

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


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    label: str = ""
    action: Optional[Tuple[Any, ...]] = None
    enabled: bool = True
    checked: Optional[bool] = None  # None: not a check item
    children: Optional[Tuple["Node", ...]] = None
    default: bool = False
    separator: bool = False
    radio: bool = False  # drawn as a radio mark where the backend can (Win32, GTK); a check mark on macOS


SEP = Node(separator=True)


def info(label: str) -> Node:
    return Node(label, None, enabled=False)


# ---------------------------------------------------------------------------
# Copy helpers (shared with app.py and the monitor)
# ---------------------------------------------------------------------------


def middle_ellipsis(text: str, limit: int = 32) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return s[:head] + "…" + s[-tail:]


PROVIDER_LABELS = {"lmstudio": "LM Studio", "ollama": "Ollama", "mlx": "MLX", "huggingface": "Hugging Face", "openai": "OpenAI", "anthropic": "Anthropic", "vllm": "vLLM", "llamacpp": "llama.cpp"}
PROVIDER_ORDER = ("mlx", "lmstudio", "ollama", "huggingface")


def provider_label(provider: str) -> str:
    p = str(provider or "").strip()
    return PROVIDER_LABELS.get(p.lower(), p)


def short_model_name(model: str, limit: int = 40) -> str:
    """`mlx-community/Qwen3.6-27B-4bit` → `Qwen3.6-27B-4bit`: the org is noise in
    a menu already grouped by engine; the tail (`@q4_k_m`, `-4bit`) is what
    tells two builds apart, so the ellipsis goes in the middle."""
    s = str(model or "")
    if "/" in s:
        s = s.rsplit("/", 1)[1] or s
    return middle_ellipsis(s, limit)


def model_row_label(row: ModelRow) -> str:
    bits = [middle_ellipsis(row.name), fmt_bytes(row.size_bytes) if row.size_bytes is not None else "size unknown", provider_label(row.provider)]
    if row.locked:
        bits.append("kept in memory")
    return " · ".join(bits)


# A tray menu item is PLAIN TEXT on every platform pystray supports — there is
# no per-item colour to set. An emoji is the only badge that actually renders,
# and these five read at a glance without needing the word next to them.
RUN_BADGES = {
    "running": "🟢",
    "waiting": "🟡",  # needs a person: an approval or a question
    "paused": "⏸",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⚪️",
}
RUN_BADGE_UNKNOWN = "◦"
SCHEDULED_BADGE = "⏱"
#: The wait kinds a person must act on (GET /host/runs `wait.kind`); an event,
#: timer, job or sub-run wait is the run working on something.
ATTENTION_WAIT_KINDS = ("tool_approval", "ask_user")


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


def workflow_menu_name(workflow_id: str) -> str:
    """What a person calls this workflow (a generated hex flow id is dropped;
    a NAMED flow stays; the head is kept — it is the identity)."""
    name = str(workflow_id or "").strip()
    bundle, sep, flow = name.partition(":")
    if sep and flow and len(flow) >= 6 and all(c in "0123456789abcdef" for c in flow.lower()):
        name = bundle
    return name if len(name) <= 34 else name[:33] + "…"


def run_group(row: RunRow) -> str:
    """running | attention | paused | done — the Sessions section a row sits
    in. Only an approval or a question is "attention": an event, timer, job
    or sub-run wait is the run working on something and stays under Running;
    a paused run sits under Running too, with Resume."""
    if row.status in ACTIVE_STATUSES:
        if row.wait_kind == "paused":
            return "paused"
        if row.status == "waiting" and row.wait_kind in ATTENTION_WAIT_KINDS:
            return "attention"
        return "running"
    return "done"


def run_title(row: RunRow) -> str:
    """The SESSION's title (never the workflow id when the gateway served one)."""
    return row.title or workflow_menu_name(row.workflow_id)


def run_row_label(row: RunRow) -> str:
    """`🟢 <title> · step 4 · 2m 10s so far` / `🟡 <title> · approve \\`execute_command\\``
    / `⏸ <title> · paused · 5m 02s so far` / `✅ <title> · 7 steps · 1m 42s`."""
    group = run_group(row)
    title = run_title(row)
    duration = fmt_duration(row.duration_s)
    if group == "attention":
        if row.wait_kind == "tool_approval":
            ask = f"approve `{row.wait_tool}`" if row.wait_tool else "approve a tool call"
        else:
            ask = "a question"
        return f"{RUN_BADGES['waiting']} {title} · {ask}"
    if group == "paused":
        return f"{RUN_BADGES['paused']} {title} · paused" + (f" · {duration} so far" if duration else "")
    if group == "running":
        bits = [f"{RUN_BADGES['running']} {title}"]
        if row.steps:
            bits.append(f"step {row.steps}")
        if row.status == "waiting" and row.wait_summary:
            bits.append(row.wait_summary)
        if duration:
            bits.append(f"{duration} so far")
        return " · ".join(bits)
    badge = RUN_BADGES.get(row.status, RUN_BADGE_UNKNOWN)
    bits = [f"{badge} {title}"]
    if row.steps is not None:
        bits.append(f"{row.steps} step" + ("" if row.steps == 1 else "s"))
    if duration:
        bits.append(duration)
    return " · ".join(bits)


def recent_tally(rows: Sequence[RunRow]) -> str:
    """`Recent — 4 done · 1 cancelled` (the finished rows of the last 24 hours)."""
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    words = {"completed": "done", "failed": "failed", "cancelled": "cancelled"}
    parts = [f"{counts[st]} {words[st]}" for st in ("completed", "failed", "cancelled") if counts.get(st)]
    parts += [f"{n} {st}" for st, n in sorted(counts.items()) if st not in words]
    return "Recent — " + (" · ".join(parts) if parts else "none in the last 24 hours")


def automation_row_label(a: AutomationRow) -> str:
    """`⏱ <automation title> · Every day at 09:00 · next Fri 9 Oct 09:00` — the
    schedule words exactly as the gateway serves them (`schedule_text`); a
    paused schedule says so."""
    title = a.title or a.automation_id
    if a.status == "paused":
        return f"{RUN_BADGES['paused']} {title} · schedule paused" + (f" · {a.schedule_text}" if a.schedule_text else "")
    return f"{SCHEDULED_BADGE} {title}" + (f" · {a.schedule_text}" if a.schedule_text else "")


def approval_dialog(wait: Dict[str, Any], *, title: str) -> Tuple[str, str]:
    """(dialog title, body) for "Show and approve…": the tool name(s) and
    their arguments as pretty JSON, in full up to `APPROVAL_BODY_MAX`
    characters, then "…" and the one line that says where the rest is."""
    calls = [c for c in (wait.get("tool_calls") or []) if isinstance(c, dict)]
    names = [str(c.get("name") or "tool call") for c in calls]
    head = f"{title} asks to run " + (", ".join(names) if names else "a tool") + "."
    parts = []
    for c in calls:
        args = json.dumps(c.get("arguments") or {}, indent=2, ensure_ascii=False, sort_keys=True)
        parts.append(f"{c.get('name') or 'tool call'} {args}")
    text = "\n\n".join(parts)
    if len(text) > APPROVAL_BODY_MAX:
        text = text[:APPROVAL_BODY_MAX].rstrip() + "…\n(open in Observer for the rest)"
    if not names:
        dialog_title = "Approve this tool call?"
    elif len(names) == 1:
        dialog_title = f"Approve {names[0]}?"
    else:
        dialog_title = f"Approve {names[0]} and {len(names) - 1} more?"
    return dialog_title, head + ("\n\n" + text if text else "")


def state_lines(snap: Snapshot, *, update_phase: str = "idle", update_latest: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """(header, detail) — ONE disabled line at the top of the menu, and a
    second only when it carries news (steps in flight, pausing, paused,
    installing, stopping, unreachable). A running idle gateway has nothing
    to add: the Models submenu says what is loaded."""
    st = snap.gateway_state
    header = f"{APP_NAME} — {STATE_WORDS.get(st, st.title())}"
    detail: Optional[str]
    if st == "running":
        n = snap.inflight_ticks
        detail = f"Working on {n} step{'s' if n != 1 else ''}" if n > 0 else None
    elif st == "pausing":
        n = snap.inflight_ticks
        if snap.step_gate_supported is False:
            detail = f"Finishing {n} run{'s' if n != 1 else ''} (older runtime: up to 100 steps each), then pausing"
        else:
            detail = f"Finishing {n} run{'s' if n != 1 else ''} at the next step, then pausing"
    elif st == "paused":
        detail = "Paused — sessions wait until you resume"
    elif st in {"starting", "restarting"}:
        detail = "Usually a few seconds"
    elif st == "updating":
        detail = f"Installing {update_latest or 'the update'} · sessions keep running"
    elif st == "stopping":
        detail = "Stopping sessions and the console"
    else:
        detail = "Can't reach it on this computer · retrying"
    return header, detail


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

# Configured capability routes that name a model the residency API can
# preload, and the residency task it loads under. Only these carry a KNOWN
# capability; everything else is grouped by engine.
ROUTE_TASKS: Dict[str, Tuple[str, str]] = {
    "output.text": ("Text", "text_generation"),
    "input.text": ("Text", "text_generation"),
    "output.image": ("Image", "image_generation"),
    "output.voice": ("Voice", "tts"),
    "input.voice": ("Speech to text", "stt"),
    "output.music": ("Music", "music_generation"),
}


@dataclass(frozen=True)
class InstalledModel:
    provider: str
    model: str
    size_bytes: Optional[int]
    catalog_id: Optional[str] = None
    kind: str = ""  # "embedding" when the engine says so (LM Studio `type`), else ""

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class DefaultRoute:
    capability: str
    task: str
    provider: str
    model: str
    status: str = ""  # availability.status: installed | missing | unknown | …

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class ModelsView:
    installed: Tuple[InstalledModel, ...] = ()
    installed_error: Optional[str] = None
    defaults: Tuple[DefaultRoute, ...] = ()
    fetched: bool = False
    loading: Tuple[str, ...] = ()  # "provider/model" keys being loaded now
    ejecting: Tuple[str, ...] = ()  # loaded-row keys being ejected now


@dataclass(frozen=True)
class AutostartView:
    state: str  # on | off | broken | other | unknown
    summary: str = ""
    problems: Tuple[str, ...] = ()
    busy: bool = False
    experimental: bool = False


NETWORK_MODES: Tuple[Tuple[str, str], ...] = (
    ("localhost", "Localhost only"),
    ("lan", "Local network"),
    ("internet", "Internet"),
)
NETWORK_WORDS = {"localhost": "localhost only", "lan": "local network", "internet": "internet"}


@dataclass(frozen=True)
class NetworkAddress:
    kind: str  # loopback | lan | hostname | public
    url: str
    interface: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class NetworkView:
    """`GET /api/gateway/network`, as the menu reads it."""

    available: bool = False  # False: not fetched yet, or the gateway has no such route
    error: Optional[str] = None
    configured_mode: Optional[str] = None
    effective_mode: Optional[str] = None
    bind_host: Optional[str] = None
    port: Optional[int] = None
    overridden_by_cli: bool = False
    restart_required: bool = False
    auth_ok_for_mode: Optional[bool] = None
    auth_fix: Optional[str] = None
    addresses: Tuple[NetworkAddress, ...] = ()
    warnings: Tuple[str, ...] = ()
    copy_hint: Optional[str] = None
    busy: bool = False


def parse_network(payload: Any) -> NetworkView:
    if not isinstance(payload, dict):
        return NetworkView(available=False, error="the gateway answered without network settings")
    cfg = payload.get("configured") if isinstance(payload.get("configured"), dict) else {}
    eff = payload.get("effective") if isinstance(payload.get("effective"), dict) else {}
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    addrs: List[NetworkAddress] = []
    for a in payload.get("addresses") or []:
        if isinstance(a, dict) and a.get("url"):
            addrs.append(NetworkAddress(str(a.get("kind") or ""), str(a["url"]), str(a["interface"]) if a.get("interface") else None, str(a["note"]) if a.get("note") else None))
    port = eff.get("port") if eff.get("port") is not None else cfg.get("port")
    return NetworkView(
        available=True,
        configured_mode=str(cfg.get("mode")) if cfg.get("mode") else None,
        effective_mode=str(eff.get("mode")) if eff.get("mode") else None,
        bind_host=str(eff.get("bind_host")) if eff.get("bind_host") else None,
        port=int(port) if isinstance(port, int) and not isinstance(port, bool) else None,
        overridden_by_cli=bool(eff.get("overridden_by_cli")),
        restart_required=bool(payload.get("restart_required")),
        auth_ok_for_mode=auth.get("ok_for_mode") if isinstance(auth.get("ok_for_mode"), bool) else None,
        auth_fix=str(auth["fix"]) if auth.get("fix") else None,
        addresses=tuple(addrs),
        warnings=tuple(str(w) for w in (payload.get("warnings") or []) if str(w).strip()),
        copy_hint=str(payload["copy_hint"]) if payload.get("copy_hint") else (addrs[0].url if addrs else None),
    )


#: Where an address reaches, by the gateway's `kind` (GET /network `addresses`).
ADDRESS_WORDS = {"loopback": "this computer", "lan": "local network", "hostname": "this computer's name", "tailscale": "tailscale", "public": "internet"}


def address_label(a: NetworkAddress) -> str:
    """`http://192.168.1.23:8080 · local network (Wi-Fi)` — the URL is what
    gets copied, so it leads and is never shortened; the words after it say
    where it reaches."""
    where = ADDRESS_WORDS.get(str(a.kind or ""), str(a.kind or ""))
    label = f"{a.url} · {where}" if where else a.url
    if a.interface:
        label += f" ({a.interface})"
    if a.note:
        label += f" · {a.note}"
    return label


@dataclass(frozen=True)
class MenuInputs:
    snap: Snapshot
    update_phase: str = "idle"
    update_latest: Optional[str] = None
    pending_label: Optional[str] = None
    tk_available: bool = False
    memory_warn: bool = True
    autostart: Optional[AutostartView] = None
    apps: Tuple[AppEntry, ...] = ()
    apps_fetched: bool = False
    models: ModelsView = field(default_factory=ModelsView)
    base_url: str = "http://127.0.0.1:8080"
    network: NetworkView = field(default_factory=NetworkView)


def parse_installed(payload: Any) -> Tuple[Tuple[InstalledModel, ...], Optional[str]]:
    """`GET /models/installed` (contract D) → rows. Engine errors are kept as
    ONE line so a missing engine is visible, never an empty menu."""
    if not isinstance(payload, dict):
        return (), "the gateway answered without a model list"
    rows: List[InstalledModel] = []
    for r in payload.get("rows") or []:
        if not isinstance(r, dict) or not r.get("provider") or not r.get("artifact"):
            continue
        size = r.get("size_bytes")
        typ = str(r.get("type") or "").lower()
        rows.append(
            InstalledModel(
                provider=str(r["provider"]),
                model=str(r["artifact"]),
                size_bytes=int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) and size >= 0 else None,
                catalog_id=str(r["catalog_id"]) if r.get("catalog_id") else None,
                kind="embedding" if typ == "embedding" else "",
            )
        )
    errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
    err = "; ".join(f"{provider_label(k)}: {v}" for k, v in sorted(errors.items()) if v) or None
    return tuple(rows), err


def parse_defaults(payload: Any) -> Tuple[DefaultRoute, ...]:
    """Configured routes from `GET /models/availability` (deduped by task+model)."""
    if not isinstance(payload, dict):
        return ()
    out: List[DefaultRoute] = []
    seen = set()
    for r in payload.get("routes") or []:
        if not isinstance(r, dict) or not r.get("configured"):
            continue
        spec = ROUTE_TASKS.get(str(r.get("key") or ""))
        provider, model = str(r.get("provider") or ""), str(r.get("model") or "")
        if spec is None or not provider or not model:
            continue
        ident = (spec[1], provider, model)
        if ident in seen:
            continue
        seen.add(ident)
        av = r.get("availability") if isinstance(r.get("availability"), dict) else {}
        out.append(DefaultRoute(spec[0], spec[1], provider, model, str(av.get("status") or "")))
    return tuple(out)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _free_bytes(snap: Snapshot) -> Optional[int]:
    if snap.mem_total and snap.mem_used is not None:
        return max(0, int(snap.mem_total) - int(snap.mem_used))
    return None


# Below this the "gateway holds" figure is noise (Metal keeps a few MB of
# shader/library buffers in every process); above it, it is a model or a cache.
_HELD_NOTEWORTHY_BYTES = 256 * 1024 * 1024


def gateway_share_bytes(snap: Snapshot) -> Optional[int]:
    """What THIS gateway process occupies: its macOS physical footprint (which
    counts Metal/MLX buffers), else its RSS; None when neither was sampled."""
    for name in ("process_footprint", "process_rss"):
        v = getattr(snap, name, None)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
    return None


def gateway_share_words(snap: Snapshot) -> str:
    """'this gateway 2.6 GB' -- the machine-wide meter alone cannot show an
    eject: freeing a 16 GB model barely moves a 137 GB machine's gauge when
    other apps hold most of it (operator 2026-10-16: "unloading a model
    doesn't completely free the memory" over a gauge that was Safari's)."""
    share = gateway_share_bytes(snap)
    return f"this gateway {fmt_bytes(share)}" if share is not None else ""


def eject_receipt(name: str, *, before: Optional[int], after: Optional[int], size_bytes: Optional[int]) -> str:
    """The 'Model ejected' notification body: the gateway's MEASURED footprint
    before and after when both samples exist ('Qwen… · this gateway 18.4 GB → 2.6 GB'),
    else the catalog size the old text claimed as 'freed', else the name alone."""
    if before is not None and after is not None and before >= 0 and after >= 0:
        return f"{name} · this gateway {fmt_bytes(before)} → {fmt_bytes(after)}"
    if size_bytes is not None:
        return f"{name} · freed {fmt_bytes(size_bytes)}"
    return name


def _held_bytes(snap: Snapshot) -> Optional[int]:
    """Accelerator memory the gateway process pins (MLX live + cached buffers)
    when it is worth showing, else None. Older snapshots carry no figure."""
    held = getattr(snap, "device_held_bytes", None)
    if isinstance(held, int) and not isinstance(held, bool) and held >= _HELD_NOTEWORTHY_BYTES:
        return held
    return None


def held_lines(snap: Snapshot) -> List[str]:
    """What the held figure is and what holds it -- shown under "Gateway
    still holds N (no model listed)": the measurement basis, then the
    in-process holders ("[backend] model × N holders") or, when none
    reports anything, that the memory is not attributed to any model."""
    out: List[str] = []
    basis = getattr(snap, "device_held_basis", None)
    out.append(f"Measured by {basis or 'basis not reported'}")
    held_by = tuple(getattr(snap, "held_by", ()) or ())
    if held_by:
        out.append("Held by " + "; ".join(held_by))
    else:
        out.append("Not attributed to any model: no in-process model holder reports it")
    return out


def eject_lines(snap: Snapshot) -> List[str]:
    """The runtime's pending / failed / last ejects, verbatim sentences."""
    return [text for _tone, text in (getattr(snap, "eject_status", ()) or ())]


def _loaded_keys(snap: Snapshot) -> Dict[str, ModelRow]:
    """provider/model → loaded row (a runtime id is not what the list shows)."""
    out: Dict[str, ModelRow] = {}
    for row in snap.models:
        out[f"{row.provider}/{row.name}"] = row
    return out


def _is_loaded(key: str, loaded: Dict[str, ModelRow]) -> bool:
    """LM Studio lists `qwen/qwen3.8-27b@q4_k_m` on disk and `qwen/qwen3.8-27b`
    in memory: the build suffix is not part of the resident identity."""
    return key in loaded or key.split("@", 1)[0] in loaded or any(k.split("@", 1)[0] == key.split("@", 1)[0] for k in loaded)


def chunk(nodes: Sequence[Node], *, size: int = CHUNK, name: Any = None) -> Tuple[Node, ...]:
    """A list longer than `size` becomes consecutive submenus labelled by the
    first and last entries' initials ("A – F (30)"). Order and every entry
    are kept: a menu that scrolls past the screen edge loses entries on some
    backends, a submenu never does."""
    items = list(nodes)
    if len(items) <= size:
        return tuple(items)
    namer = name or (lambda n: n.label)
    out: List[Node] = []
    for i in range(0, len(items), size):
        part = items[i : i + size]
        a, b = str(namer(part[0])).strip(), str(namer(part[-1])).strip()
        # The shortest prefixes that tell the two ends apart ("A – F" when the
        # initials differ, "model-a – model-l" when a whole range shares them).
        k = 1
        while k < 12 and a[:k].lower() == b[:k].lower() and k < max(len(a), len(b)):
            k += 1
        first, last = (a[:k] or "?"), (b[:k] or "?")
        first, last = (first.upper(), last.upper()) if k == 1 else (first, last)
        label = f"{first} – {last} ({len(part)})" if first.lower() != last.lower() else f"{first} ({len(part)})"
        out.append(Node(label, children=tuple(part)))
    return tuple(out)


def models_section(inputs: MenuInputs, *, reachable: bool) -> Node:
    snap, mv = inputs.snap, inputs.models
    loaded = list(snap.models)
    total = snap.models_total_bytes
    free = _free_bytes(snap)
    held = _held_bytes(snap)
    head = (
        f"Loaded: {len(loaded)}"
        + (f" · {fmt_bytes(total)}" if total else "")
        + (f" · {fmt_bytes(free)} free" if free is not None else "")
        + (f" · gateway holds {fmt_bytes(held)}" if held else "")
    )
    items: List[Node] = [info(head)]
    if snap.models_error and not loaded:
        items.append(info(f"Loaded models unavailable: {middle_ellipsis(snap.models_error, 60)}"))
    if held and not loaded:
        # The process pins accelerator memory nothing in the list owns: say
        # so, with the only two honest ways out (eject what the console lists
        # as held, or restart). Never a silent "No models loaded".
        items.append(info(f"Gateway still holds {fmt_bytes(held)} (no model listed)"))
        for text in held_lines(snap):
            items.append(info(middle_ellipsis(text, 90)))
        items.append(info("Eject the held model in the Console, or restart the gateway to free it"))
    elif held:
        items.append(info(middle_ellipsis(f"Gateway memory measured by {snap.device_held_basis or 'basis not reported'}", 90)))
    for text in eject_lines(snap):
        items.append(info(middle_ellipsis(text, 90)))
    for key in mv.loading:
        items.append(info(f"Loading {short_model_name(key.split('/', 1)[-1])}…"))
    for row in loaded:
        busy = row.key in mv.ejecting
        size = fmt_bytes(row.size_bytes) if row.size_bytes is not None else "size unknown"
        label = f"✓ {short_model_name(row.name)} · {size} · {provider_label(row.provider)}" + (" · kept in memory" if row.locked else "")
        frees = f"frees {size}" if row.size_bytes is not None else "frees its memory"
        sub = [
            Node(f"Ejecting… ({frees})" if busy else f"Eject — {frees}", ("eject", row.key), enabled=reachable and not busy),
        ]
        if row.locked:
            sub.append(info("Locked: stays loaded until ejected here"))
        if row.size_source == "estimated":
            sub.append(info("Size estimated from the weights"))
        items.append(Node(label, children=tuple(sub)))
    if not loaded and not snap.models_error and not held:
        items.append(info("No models loaded"))
    items.append(SEP)
    items.append(Node("Load a Model", children=load_submenu(inputs, reachable=reachable), enabled=reachable))
    items.append(SEP)
    items.append(Node("Manage Models in Console…", ("open_console_tab", "models"), enabled=reachable))
    return Node("Models", children=tuple(items), enabled=reachable)


def _load_row(m: InstalledModel, *, loaded: Dict[str, ModelRow], loading: Iterable[str], free: Optional[int], reachable: bool) -> Node:
    size = fmt_bytes(m.size_bytes) if m.size_bytes is not None else "size unknown"
    name = short_model_name(m.model)
    if _is_loaded(m.key, loaded):
        return Node(f"{name} · {size} · loaded", None, enabled=False, checked=True)
    if m.key in set(loading):
        return info(f"{name} · loading…")
    if m.kind == "embedding":
        # The residency API has no embedding task: say so instead of offering a click that fails.
        return info(f"{name} · {size} · embeddings (load on use)")
    tight = " · more than free memory" if (free is not None and m.size_bytes is not None and m.size_bytes > free) else ""
    return Node(f"{name} · {size}{tight}", ("load", m.provider, m.model, "text_generation"), enabled=reachable)


def load_submenu(inputs: MenuInputs, *, reachable: bool) -> Tuple[Node, ...]:
    snap, mv = inputs.snap, inputs.models
    if not mv.fetched:
        return (info("Reading the installed models…"),)
    loaded = _loaded_keys(snap)
    free = _free_bytes(snap)
    by_key = {m.key: m for m in mv.installed}
    out: List[Node] = []
    if mv.defaults:
        out.append(info("Your defaults"))
        for d in mv.defaults:
            m = by_key.get(d.key)
            size = fmt_bytes(m.size_bytes) if (m and m.size_bytes is not None) else ""
            label = f"{d.capability}: {short_model_name(d.model)}" + (f" · {size}" if size else "") + f" · {provider_label(d.provider)}"
            if _is_loaded(d.key, loaded):
                out.append(Node(label + " · loaded", None, enabled=False, checked=True))
            elif d.key in set(mv.loading):
                out.append(info(label + " · loading…"))
            elif d.status and d.status not in {"installed", "unknown", "available"}:
                out.append(info(f"{label} · {d.status.replace('_', ' ')}"))
            else:
                out.append(Node(label, ("load", d.provider, d.model, d.task), enabled=reachable))
        out.append(SEP)
    groups: Dict[str, List[InstalledModel]] = {}
    for m in mv.installed:
        groups.setdefault(m.provider, []).append(m)
    order = [p for p in PROVIDER_ORDER if p in groups] + sorted(p for p in groups if p not in PROVIDER_ORDER)
    for prov in order:
        rows = groups[prov]
        known = sorted((m for m in rows if m.catalog_id), key=lambda m: short_model_name(m.model).lower())
        rest = sorted((m for m in rows if not m.catalog_id), key=lambda m: short_model_name(m.model, 200).lower())
        mk = lambda ms: [_load_row(m, loaded=loaded, loading=mv.loading, free=free, reachable=reachable) for m in ms]  # noqa: E731
        children: List[Node] = []
        if len(rows) > CHUNK:
            if known:
                children.append(Node(f"Recognised models ({len(known)})", children=tuple(mk(known))))
            children.extend(chunk(mk(rest)))
        else:
            children.extend(mk(known))
            if known and rest:
                children.append(SEP)
            children.extend(mk(rest))
        out.append(Node(f"{provider_label(prov)} ({len(rows)})", children=tuple(children)))
    if mv.installed_error:
        out.append(info(f"Not listed: {middle_ellipsis(mv.installed_error, 70)}"))
    if not mv.installed and not mv.installed_error:
        out.append(info("No installed models found"))
    out.append(SEP)
    out.append(Node("Download Models in Console…", ("open_console_tab", "catalog"), enabled=reachable))
    return tuple(out)


INSTALLS_OFF_LINE = "Installs are off for this gateway · Console → Apps"
INSTALLS_UNAVAILABLE_LINE = "Installs unavailable now · Console → Apps"


def apps_section(inputs: MenuInputs, *, reachable: bool) -> Node:
    """One short line per app, in stack order:
    running (the gateway's or started outside it) or installed -> "Open X"
    (starting it first when stopped); installable -> "Install X…"; anything
    else -> the name, greyed, with NO reason. When an install is blocked, ONE
    line at the bottom says so; the full reason lives in the console."""
    if not inputs.apps_fetched:
        return Node("Apps", children=(info("Looking for apps…"),))
    items: List[Node] = []
    blocked_by_policy = False
    blocked_other = False
    for a in inputs.apps:
        if a.id == "assistant":
            # A desktop app: launched here, on this machine;
            # installed through the gateway (into its own Python).
            items.append(SEP)
            if a.status in {"available", "running"}:
                items.append(Node(f"Launch {a.name}", ("assistant_launch",)))
                if a.notice:
                    items.append(info(a.notice))
            elif a.status == "installing":
                pct = f" {int(a.job_percent)}%" if isinstance(a.job_percent, (int, float)) else ""
                items.append(info(f"{a.name} — installing{pct}…"))
            elif a.status == "not_installed" and a.install_available:
                items.append(Node(f"Install {a.name}…", ("app_install", a.id), enabled=reachable))
            else:
                items.append(info(a.name))
                if a.status == "not_installed" and a.installs_off:
                    blocked_by_policy = True
            continue
        if a.status == "running" and a.source in {"gateway", "external"}:
            # Through the gateway's one-time sign-in handover, whoever started it.
            items.append(Node(f"Open {a.name}", ("app_open", a.id), enabled=reachable))
        elif a.status == "running":
            items.append(Node(f"Open {a.name}", ("app_open_url", a.id)))
        elif a.status == "starting":
            items.append(info(f"{a.name} — starting…"))
        elif a.status == "installing":
            pct = f" {int(a.job_percent)}%" if isinstance(a.job_percent, (int, float)) else ""
            items.append(info(f"{a.name} — installing{pct}…"))
        elif a.status == "stopped" and a.source == "gateway":
            items.append(Node(f"Open {a.name}", ("app_launch", a.id), enabled=reachable))
        elif a.status == "stopped":
            items.append(Node(f"Open {a.name}", ("app_launch_global", a.id)))
        elif a.status == "not_installed" and a.install_available:
            items.append(Node(f"Install {a.name}…", ("app_install", a.id), enabled=reachable))
        else:
            items.append(info(a.name))
            if a.status == "not_installed":
                if a.installs_off:
                    blocked_by_policy = True
                else:
                    blocked_other = True
        if a.tui_installed:
            # Its terminal version is on this machine (presence, reported by
            # the gateway): a new terminal window, signed in (launch-tui).
            items.append(Node(f"Open {a.name} in Terminal", ("app_launch_tui", a.id), enabled=reachable and a.tui_launch_available))
    if blocked_by_policy or blocked_other:
        items.append(SEP)
        items.append(info(INSTALLS_OFF_LINE if blocked_by_policy else INSTALLS_UNAVAILABLE_LINE))
    items.append(SEP)
    items.append(Node("Manage Apps in Console…", ("open_console_tab", "apps"), enabled=reachable))
    return Node("Apps", children=tuple(items))


def network_section(nv: NetworkView, *, reachable: bool) -> Node:
    label = "Network" + (" — restart required" if nv.restart_required else "")
    if not nv.available:
        why = nv.error or "Reading the network settings…"
        return Node(label, children=(info(middle_ellipsis(why, 80)),), enabled=reachable)
    items: List[Node] = []
    eff = NETWORK_WORDS.get(str(nv.effective_mode or ""), str(nv.effective_mode or "unknown"))
    where = f"{nv.bind_host}:{nv.port}" if nv.bind_host and nv.port else (f"port {nv.port}" if nv.port else "")
    items.append(info(f"Now: {eff}" + (f" · {where}" if where else "")))
    if nv.overridden_by_cli:
        items.append(info("Started with an explicit --host/--port: the setting below applies when started without them"))
    busy = nv.busy
    for mode, words in NETWORK_MODES:
        text = words + ("…" if mode == "internet" else "")
        items.append(Node(text, ("network_set", mode), checked=(nv.configured_mode == mode), radio=True, enabled=reachable and not busy))
    if nv.restart_required:
        items.append(SEP)
        items.append(Node(f"Restart to apply ({NETWORK_WORDS.get(str(nv.configured_mode), nv.configured_mode)})", ("network_restart",), enabled=reachable and not busy))
    if nv.auth_ok_for_mode is False:
        items.append(info("⚠ Sign-in is not set up for this mode" + (f": {middle_ellipsis(nv.auth_fix, 70)}" if nv.auth_fix else "")))
    for w in nv.warnings:
        items.append(info("⚠ " + middle_ellipsis(w, 80)))
    return Node(label, children=tuple(items), enabled=reachable)


def addresses_section(nv: NetworkView, base_url: str) -> Node:
    """**Addresses ▸**: one row per address the gateway answers on, each
    saying where it reaches; a click copies it."""
    if nv.available and nv.addresses:
        return Node("Addresses", children=tuple(Node(address_label(a), ("copy_address", a.url)) for a in nv.addresses))
    # No network route (older gateway) or not read yet: the URL this tray talks to is still an address.
    kids = [Node(f"{base_url.rstrip('/')} · {ADDRESS_WORDS['loopback']}", ("copy_address", base_url.rstrip("/")))]
    if nv.error:
        kids.append(info(middle_ellipsis(f"Other addresses: {nv.error}", 100)))
    return Node("Addresses", children=tuple(kids))


AUTOSTART_LABEL = f"Start {APP_NAME} at login"


def autostart_nodes(view: Optional[AutostartView]) -> Tuple[Node, ...]:
    if view is not None and view.busy:
        return (Node(AUTOSTART_LABEL + "…", None, enabled=False, checked=view.state == "on"),)
    if view is None or view.state == "unknown":
        return (Node(AUTOSTART_LABEL, None, enabled=False, checked=False),)
    if view.state == "on":
        return (Node(AUTOSTART_LABEL, ("toggle_autostart",), checked=True),)
    if view.state == "broken":
        why = view.problems[0] if view.problems else view.summary
        return (
            Node(AUTOSTART_LABEL + " — needs repair", ("toggle_autostart",), checked=False),
            # Head kept: the reason leads ("the program it starts is gone: /path…").
            info("   " + (why if len(why) <= 72 else why[:71] + "…")),
        )
    if view.state == "other":
        return (Node(AUTOSTART_LABEL + " (another gateway is registered)", ("toggle_autostart",), checked=False),)
    return (Node(AUTOSTART_LABEL, ("toggle_autostart",), checked=False),)


def observer_open_available(apps: Sequence[Any]) -> bool:
    """Observer opens from the tray when the gateway serves it or can start
    it (the same states the Apps submenu offers "Open Observer" for)."""
    for a in apps:
        if getattr(a, "id", None) == "observer":
            return getattr(a, "status", "") in {"running", "stopped"} and getattr(a, "source", "") in {"gateway", "external"}
    return False


def run_detail_line(row: RunRow) -> str:
    """The first line of a session's submenu: its state and when it started (local time)."""
    if row.started_at is None:
        return "Start time unknown"
    stamp = time.localtime(row.started_at)
    today = time.localtime()
    when = time.strftime("%H:%M", stamp) if stamp[:3] == today[:3] else time.strftime("%b %d, %H:%M", stamp)
    group = run_group(row)
    if group == "done":
        return f"Started {when}"
    word = {"running": "Running", "paused": "Paused", "attention": "Waiting"}[group]
    return f"{word} · started {when}"


ANOTHER_ACCOUNT_LINE = "Another account's session — act on it from its own app"


def run_submenu(row: RunRow, *, observer: bool, reachable: bool) -> Tuple[Node, ...]:
    """What a session's row opens. Running: Pause · Stop…; paused: Resume ·
    Stop…; an approval: what is asked, **Show and approve…** (a dialog with
    the full arguments — never an approve from a bare click), Deny; a
    question: the question (free-text answers stay in the clients); finished:
    when it started. Every row: Open in Observer. The commands are greyed on
    another account's row and say why."""
    group = run_group(row)
    can = reachable and row.commandable
    open_node = Node("Open in Observer", ("open_run_observer", row.run_id), enabled=bool(observer and reachable and row.observer_path))
    out: List[Node] = []
    if group == "attention":
        out.append(info(row.wait_summary or ("approve a tool call" if row.wait_kind == "tool_approval" else "a question")))
        if row.wait_kind == "tool_approval":
            out.append(Node("Show and approve…", ("run_approve_show", row.run_id), enabled=can))
            out.append(Node("Deny", ("run_deny", row.run_id), enabled=can))
        out.append(open_node)
    elif group == "paused":
        out += [info(run_detail_line(row)), open_node, Node("Resume", ("run_resume", row.run_id), enabled=can), Node("Stop…", ("run_stop", row.run_id), enabled=can)]
    elif group == "running":
        out += [info(run_detail_line(row)), open_node, Node("Pause", ("run_pause", row.run_id), enabled=can), Node("Stop…", ("run_stop", row.run_id), enabled=can)]
    else:
        out += [info(run_detail_line(row)), open_node]
    if group != "done" and not row.commandable:
        out.append(info(ANOTHER_ACCOUNT_LINE))
    return tuple(out)


def automation_submenu(a: AutomationRow, *, observer: bool, reachable: bool, observer_path: Optional[str]) -> Tuple[Node, ...]:
    """A Scheduled row's submenu: the schedule words, Open in Observer (the
    Automations page), Run now, Pause schedule / Resume schedule — each
    offered only when the gateway lists the capability."""
    caps = set(a.capabilities)
    words = a.schedule_text or ("legacy schedule" if a.legacy else "no schedule words from the gateway")
    out: List[Node] = [
        info(f"{a.status.title() or 'Scheduled'} · {words}"),
        Node("Open in Observer", ("open_automations_observer",), enabled=bool(observer and reachable and observer_path)),
        Node("Run now", ("auto_run_now", a.automation_id), enabled=reachable and "run_now" in caps),
    ]
    if a.status == "paused":
        out.append(Node("Resume schedule", ("auto_resume", a.automation_id), enabled=reachable and "resume" in caps))
    else:
        out.append(Node("Pause schedule", ("auto_pause", a.automation_id), enabled=reachable and "pause" in caps))
    return tuple(out)


def sessions_items(snap: Snapshot, *, observer: bool = False) -> Tuple[Node, ...]:
    """**Sessions ▸**: Running (running, waiting-for-something and paused
    rows), Need attention (an approval or a question, only when there is
    one), Scheduled (the automations, only when there are any), Recent (the
    last 24 hours, at most `MENU_RECENT_ROWS` rows then ONE line naming the
    rest), and the console door. Every section header is an information
    line; nothing is hidden without saying so."""
    reachable = snap.reachable
    groups: Dict[str, List[RunRow]] = {"running": [], "attention": [], "paused": [], "done": []}
    for row in snap.runs:
        groups[run_group(row)].append(row)
    out: List[Node] = []
    if snap.runs_error and not snap.runs:
        out += [info("Session list unavailable"), info(middle_ellipsis(str(snap.runs_error), 60))]
    else:
        running = groups["running"] + groups["paused"]
        out.append(info(f"Running ({len(running)})" if running else "Running — none"))
        for row in running:
            out.append(Node(run_row_label(row), children=run_submenu(row, observer=observer, reachable=reachable)))
        if groups["attention"]:
            out.append(SEP)
            out.append(info(f"Need attention ({len(groups['attention'])})"))
            for row in groups["attention"]:
                out.append(Node(run_row_label(row), children=run_submenu(row, observer=observer, reachable=reachable)))
    scheduled = [a for a in snap.automations if a.status in {"active", "paused"}]
    if scheduled or snap.automations_error:
        out.append(SEP)
        if scheduled:
            out.append(info(f"Scheduled ({len(scheduled)})"))
            for a in scheduled:
                out.append(Node(automation_row_label(a), children=automation_submenu(a, observer=observer, reachable=reachable, observer_path=snap.observer_automations_path)))
        else:
            out.append(info(middle_ellipsis(f"Scheduled unavailable: {snap.automations_error}", 80)))
    if not (snap.runs_error and not snap.runs):
        done = groups["done"]
        out.append(SEP)
        out.append(info(recent_tally(done)))
        for row in done[:MENU_RECENT_ROWS]:
            out.append(Node(run_row_label(row), children=run_submenu(row, observer=observer, reachable=reachable)))
        if len(done) > MENU_RECENT_ROWS:
            out.append(info(f"…and {len(done) - MENU_RECENT_ROWS} more"))
    out.append(SEP)
    out.append(Node("Open Sessions in Console", ("open_runs",), enabled=reachable))
    return tuple(out)


def update_label(update_phase: str, update_latest: Optional[str]) -> str:
    if update_phase == "checking":
        return "Checking for Updates…"
    if update_phase == "available" and update_latest:
        return f"Update to {update_latest}…"
    if update_phase == "updating":
        return "Updating…"
    if update_phase == "installed":
        return "Restart to Finish Update…"
    return "Check for Updates…"


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


def build_menu(inputs: MenuInputs) -> Tuple[Node, ...]:
    snap = inputs.snap
    st = snap.gateway_state
    reachable = st not in {"unreachable", "starting", "restarting", "stopping"}
    header, detail = state_lines(snap, update_phase=inputs.update_phase, update_latest=inputs.update_latest)
    out: List[Node] = [info(header)]
    if detail:
        out.append(info(detail))
    if inputs.pending_label:
        out += [SEP, Node(f"Confirm: {inputs.pending_label}", ("confirm_pending",))]
    out.append(SEP)
    # ONE door to the console; it opens SIGNED IN.
    out.append(Node("Open Console", ("open_console",), default=True, enabled=reachable or st == "unreachable"))
    if inputs.tk_available:
        out.append(Node("Show Activity Window…", ("show_activity",)))
    out.append(apps_section(inputs, reachable=reachable))
    out.append(addresses_section(inputs.network, inputs.base_url))
    out.append(SEP)
    out.append(Node("Sessions", children=sessions_items(snap, observer=observer_open_available(inputs.apps)), enabled=reachable))
    # The gateway-wide pause (the step gate; the console keeps answering).
    if snap.paused or st == "pausing":
        out.append(Node(f"▶ Resume {APP_NAME}", ("resume",), enabled=reachable))
    else:
        out.append(Node(f"⏸ Pause {APP_NAME}", ("pause",), enabled=reachable))
    out.append(SEP)
    meters: List[str] = []
    if snap.mem_supported and (snap.mem_used is not None and snap.mem_total):
        meters.append(f"Memory {fmt_bytes(snap.mem_used)} of {fmt_bytes(snap.mem_total)} ({fmt_pct(snap.mem_pct)})")
    elif snap.mem_pct is not None:
        meters.append(f"Memory {fmt_pct(snap.mem_pct)}")
    share = gateway_share_words(snap)
    if share and meters:
        meters.append(share)
    if snap.gpu_supported and snap.gpu_pct is not None:
        meters.append(f"GPU {fmt_pct(snap.gpu_pct)} busy")
    if meters:
        out.append(info(" · ".join(meters)))
    out.append(models_section(inputs, reachable=reachable))
    out.append(SEP)
    out.append(Node(update_label(inputs.update_phase, inputs.update_latest), ("check_update",), enabled=reachable and inputs.update_phase not in {"checking", "updating"} and not snap.update_job_running))
    out.append(Node(f"Restart {APP_NAME}…", ("restart",), enabled=reachable and snap.can_restart))
    out.extend(autostart_nodes(inputs.autostart))
    out.append(network_section(inputs.network, reachable=reachable))
    out.append(
        Node(
            "Help",
            children=(
                # NO "(needs internet)" suffixes.
                Node("Documentation", ("open_url", DOCS_URL)),
                Node("Report a Problem…", ("open_url", ISSUES_URL)),
                Node("Developer API Reference", ("open_url", inputs.base_url.rstrip("/") + "/docs")),
                Node("Copy Console Link", ("copy_console_link",)),
                Node("Warn when memory is almost full", ("toggle_memory_warn",), checked=bool(inputs.memory_warn)),
                SEP,
                Node(f"About {APP_NAME}…", ("about",)),
            ),
        )
    )
    out.append(SEP)
    # NO "Hide the icon" item.
    if st == "unreachable":
        out.append(Node(f"Force Quit {APP_NAME}…", ("force_quit",)))
    else:
        out.append(Node(f"Quit {APP_NAME}…", ("quit",), enabled=snap.can_shutdown or not reachable))
    return tuple(out)


# ---------------------------------------------------------------------------
# Degrade + render
# ---------------------------------------------------------------------------


def flatten(nodes: Sequence[Node], prefix: str = "") -> Tuple[Node, ...]:
    """Same rows, no submenus: each child is prefixed with its path. For panels
    that drop submenus (chosen explicitly: tray prefs `flat_menu`)."""
    out: List[Node] = []
    for n in nodes:
        if n.separator:
            if out and not out[-1].separator:
                out.append(SEP)
            continue
        label = f"{prefix}{n.label}"
        if n.children is not None:
            out.append(replace(n, label=label, children=None, action=None, enabled=False, default=False))
            out.extend(flatten(n.children, prefix=label + PATH_SEP))
        else:
            out.append(replace(n, label=label))
    return tuple(out)


def iter_nodes(nodes: Sequence[Node]) -> Iterable[Node]:
    for n in nodes:
        yield n
        if n.children:
            yield from iter_nodes(n.children)


def render_text(nodes: Sequence[Node], indent: int = 0) -> str:
    """The menu as text (the report, and a debugging aid): `▸` submenu,
    `☑/☐` check item, `[…]` disabled line, `(default)` the click action."""
    lines: List[str] = []
    pad = "    " * indent
    for n in nodes:
        if n.separator:
            lines.append(pad + "────────")
            continue
        mark = "" if n.checked is None else (("● " if n.checked else "○ ") if n.radio else ("☑ " if n.checked else "☐ "))
        text = f"{mark}{n.label}"
        if n.children is not None:
            lines.append(pad + text + " ▸" + ("" if n.enabled else "  [disabled]"))
            lines.append(render_text(n.children, indent + 1))
            continue
        if not n.enabled:
            text = f"[{text}]"
        if n.default:
            text += "   (default)"
        lines.append(pad + text)
    return "\n".join(line for line in lines if line != "")
