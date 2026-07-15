from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def _is_within_dir(path: Path, base_dir: Path) -> bool:
    """Return True if path is located under base_dir (after resolution)."""
    try:
        path.relative_to(base_dir)
        return True
    except Exception:
        return False


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(raw: Optional[str], default: int) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _as_float(raw: Optional[str], default: float) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except Exception:
        return default


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return v
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return v2
    return None


def entity_iterations_ceiling() -> Optional[int]:
    """Operator ceiling on agent-loop ITERATIONS for entity runs (laurent
    c786: "hard ceiling at 100 calls / turn … customizable, for instance in
    the gateway/console"; axis = max_iterations, concurred c805/c807/c809).

    Served into run vars as `_limits.max_iterations_ceiling` at ENTITY-run
    creation (summon + visit open); abstractruntime's Runtime.start() is the
    ONE enforcement site (seam (b), c805/c809): a workflow declaring
    max_iterations above the ceiling refuses LOUD before the run exists —
    never mid-run truncation. The value is server-declared so consoles render
    it; a later config-object field supersedes the env as the operator
    surface.

    Returns None when the operator DISABLES enforcement (0 / off / none /
    disabled / false) — absence semantics: no field in run vars = no
    enforcement (the runtime never invents a ceiling). Unset/invalid env =
    the ruled default 100."""
    raw = (os.getenv("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING") or "").strip()
    if not raw:
        return 100
    if raw.lower() in {"0", "off", "none", "disabled", "false"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return 100
    return value if value > 0 else None


def entity_create_quota() -> Optional[int]:
    """Per-data-root ceiling on entity HOMES (adversary F2, 2026-07-12).

    Entity creation is deliberately user-level (P1-1 asymmetry) but an
    entity is a PERMANENT resource: a home directory that can never be
    deleted (never-purge is structural) plus a door-global principal in the
    shared users registry. Without a bound, any authenticated principal
    could flood the host disk and the shared users.json with unbounded,
    un-prunable records. The quota counts existing homes in the CALLER'S
    registry root (per-principal scoping = per-principal quota) before any
    write; hitting it refuses loudly (HTTP 429 at the route).

    Env: ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA. Unset/invalid = default 20.
    0 / off / none / disabled / false = no quota (single-operator posture
    where everyone at the door is the operator)."""
    raw = (os.getenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA") or "").strip()
    if not raw:
        return 20
    if raw.lower() in {"0", "off", "none", "disabled", "false"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return 20
    return value if value > 0 else None


def declared_door_address() -> Optional[str]:
    """The door's ONE operator-declared canonical address (plan item 5,
    GW-F): `ABSTRACTGATEWAY_DECLARED_ADDRESS`, e.g. "127.0.0.1:8080" or
    "entities.example.org". Unset = the door claims no address (handles are
    simply not rendered — the door never guesses what it cannot know; it
    binds 0.0.0.0 and clients dial many routes).

    Lives in config (renaming.md, approved c398): this is door-wide SERVING
    config, not an entity concept — the next consumer (federation links,
    webhooks, rendered URLs) must not import an entity module to read it.
    `render_handle()` (entity-flavored) stays in `entities.py`.

    RELOCATION-STABLE KEYS (core C1 pin): this value renders HANDLES ONLY.
    Nothing at rest — entity_id, stamps, gradation targets, cache keys,
    marker streams — may ever derive from it; localhost -> VPS must stay
    one config edit with zero records touched (laurent's consequence (d))."""
    raw = (os.getenv("ABSTRACTGATEWAY_DECLARED_ADDRESS") or "").strip()
    return raw or None


def detected_lan_ip() -> Optional[str]:
    """The gateway host's current LAN IP, best-effort (never loopback).

    Detection = the UDP-connect trick: connecting a datagram socket sends
    no packets but forces the kernel to pick the outbound interface, whose
    address is the machine's LAN identity. Returns None when nothing
    non-loopback exists (an offline box) — callers fall back honestly."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("192.168.255.255", 1))  # LAN-shaped target first
            ip = str(s.getsockname()[0] or "")
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))  # any-route fallback
            ip = str(s.getsockname()[0] or "")
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None


def resolved_door_address() -> Optional[str]:
    """The address entity handles render with: the operator-declared knob
    when set, else the CURRENT LAN IP (laurent's DM ruling 2026-07-15
    20:32: entity id = <name>@<gateway lan ip> — "ip is the current lan ip
    of the gateway; gateway is their home"). This SUPERSEDES the earlier
    never-guess posture for the rendering path only; the declared knob
    stays the override, and the address remains reachability, never
    identity at rest (the C1 pin is untouched)."""
    declared = declared_door_address()
    if declared:
        return declared
    return detected_lan_ip()


def _default_flows_dir() -> str:
    package_root = Path(__file__).resolve().parent
    candidates = [
        package_root / "flows" / "bundles",
        package_root.parent.parent / "flows" / "bundles",
    ]
    for candidate in candidates:
        try:
            if (candidate / "basic-agent.flow").is_file():
                return str(candidate)
        except Exception:
            continue
    raise RuntimeError(
        "AbstractGateway default bundle registry is missing the shipped basic-agent bundle. "
        "Reinstall or rebuild abstractgateway, or set ABSTRACTGATEWAY_FLOWS_DIR to a bundle directory containing basic-agent."
    )


# The interface the DEFAULT framework agent must declare (flow's Phase-B
# precondition audit c713 item 1: the shipped basic-agent declares it on the
# default entrypoint at manifest + VisualFlow level).
BASIC_AGENT_INTERFACE = "abstractcode.agent.v1"


def verify_basic_agent_loadable(flows_dir: Path) -> None:
    """Boot LOADABILITY check for the default framework agent (flow c756 →
    gateway c758, option (b): the check folds into the gateway boot path).

    'Published basic-agent' means LOADS + DECLARES the agent interface —
    file-presence is not the invariant (flow's audit item 4: a corrupt file
    passed boot then died later as a load warning, and the three flows-dir
    envs bypassed even the presence check). Phase B makes basic-agent every
    entity's default agency loop, so a gateway that boots without a loadable
    default agent fails every summon later, in a confusing place.

    Three distinct postures, deliberately:
    - basic-agent.flow ABSENT from an operator-provided dir: boot PROCEEDS
      with a loud warning — pointing the gateway at a custom-bundles-only
      directory is a legitimate operator choice (the env bypass has always
      allowed it); the change is that it is no longer SILENT.
    - PRESENT but UNLOADABLE (corrupt zip / invalid manifest): boot REFUSES
      loudly with the repair hint — the loader itself could never serve this
      bundle for ANY run; boot-then-die-later is the audited bug class.
    - PRESENT + loadable but the default entrypoint is unresolvable or does
      not declare the agent interface: LOUD WARNING, boot proceeds. The
      interface REFUSAL belongs to the pick-time interface gate (the ruled
      enforcement point, c721/c722: config PUT edit-time + phase-session
      open run-time) — refusing at boot too would be a second refusal site
      for the same fact AND would break legitimate non-agent deployments
      whose basic-agent stand-in never serves entity summons.
    """
    bundle_path = Path(flows_dir) / "basic-agent.flow"
    if not bundle_path.is_file():
        import logging

        logging.getLogger("abstractgateway.config").warning(
            "flows dir %s carries no basic-agent.flow — the default framework agent "
            "is unavailable (entity summons and default runs that rely on it will "
            "refuse); this is allowed for custom-bundle deployments, but if it is "
            "not deliberate, unset ABSTRACTGATEWAY_FLOWS_DIR or point it at a "
            "directory containing the shipped basic-agent",
            flows_dir,
        )
        return

    from abstractruntime.workflow_bundle import open_workflow_bundle

    def _refuse(reason: str, cause: Optional[BaseException] = None) -> None:
        raise RuntimeError(
            f"basic-agent bundle at {bundle_path} is present but NOT USABLE as the default "
            f"framework agent: {reason}. A broken default agent would fail every entity "
            "summon at run-time; refusing at boot. Reinstall/rebuild abstractgateway, or "
            "repair/replace the bundle (set ABSTRACTGATEWAY_FLOWS_DIR to a directory with "
            "a valid basic-agent)."
        ) from cause

    import logging

    log = logging.getLogger("abstractgateway.config")

    def _warn_interface(reason: str) -> None:
        log.warning(
            "basic-agent bundle at %s loads but %s — agent lanes (entity summons, "
            "default agency runs) will be refused by the pick-time interface gate "
            "at session open; non-agent workflow runs are unaffected. If this "
            "gateway serves entities, repack basic-agent with the %r interface "
            "declared on its default entrypoint.",
            bundle_path,
            reason,
            BASIC_AGENT_INTERFACE,
        )

    try:
        bundle = open_workflow_bundle(bundle_path)
    except Exception as e:  # zip/json/manifest-shape corruption — refuse, never boot-then-die-later
        _refuse(f"not loadable ({type(e).__name__}: {e})", e)
        return  # unreachable; keeps type-checkers honest

    manifest = bundle.manifest
    entrypoints = list(getattr(manifest, "entrypoints", None) or [])
    if not entrypoints:
        # The reader's own manifest validation normally rejects this shape;
        # reaching here means a loadable-but-defaultless bundle — warn, the
        # pick-time gate refuses agent lanes at open.
        _warn_interface("declares no entrypoints")
        return
    default_id = str(getattr(manifest, "default_entrypoint", "") or "").strip()
    selected = None
    if default_id:
        selected = next((e for e in entrypoints if str(e.flow_id).strip() == default_id), None)
        if selected is None:
            _warn_interface(f"default_entrypoint {default_id!r} matches no entrypoint")
            return
    elif len(entrypoints) == 1:
        selected = entrypoints[0]
    else:
        _warn_interface(
            f"has {len(entrypoints)} entrypoints with no default_entrypoint (the default "
            "agent is not resolvable without a caller-provided flow_id)"
        )
        return
    interfaces = [str(i).strip() for i in (getattr(selected, "interfaces", None) or [])]
    if BASIC_AGENT_INTERFACE not in interfaces:
        _warn_interface(
            f"its default entrypoint {str(selected.flow_id)!r} does not declare the "
            f"{BASIC_AGENT_INTERFACE!r} interface (declares: {interfaces or 'none'})"
        )


@dataclass(frozen=True)
class GatewayHostConfig:
    """Process-level configuration for the AbstractGateway host."""

    data_dir: Path
    flows_dir: Path
    framework_flows_dir: Optional[Path] = None
    root_data_dir: Optional[Path] = None
    tenant_id: str = "default"
    user_id: str = "admin"
    runtime_id: str = ""
    store_backend: str = "file"  # file|sqlite
    db_path: Optional[Path] = None

    runner_enabled: bool = True
    poll_interval_s: float = 0.25
    command_batch_limit: int = 200
    tick_max_steps: int = 100
    tick_workers: int = 4
    run_scan_limit: int = 200

    @staticmethod
    def from_env() -> "GatewayHostConfig":
        # NOTE: We intentionally use ABSTRACTGATEWAY_* as the canonical namespace.
        # For a transition period, we accept legacy ABSTRACTFLOW_* names as fallbacks.
        data_dir_raw = _env("ABSTRACTGATEWAY_DATA_DIR", "ABSTRACTFLOW_RUNTIME_DIR") or "./runtime"
        flows_dir_raw = (
            _env("ABSTRACTGATEWAY_FLOWS_DIR")
            or _env("ABSTRACTFRAMEWORK_WORKFLOWS_DIR")
            or _env("ABSTRACTFLOW_FLOWS_DIR")
            or _default_flows_dir()
        )
        # Boot LOADABILITY check on the EFFECTIVE flows dir (flow c756 →
        # gateway c758 option (b)): runs for all four sources — the default
        # dir AND the three env overrides that used to bypass even the
        # presence check. Present-but-broken refuses boot; absent-from-an
        # -operator-dir warns loudly and proceeds (custom-bundle posture).
        verify_basic_agent_loadable(Path(flows_dir_raw).expanduser())

        store_backend = str(_env("ABSTRACTGATEWAY_STORE_BACKEND") or "file").strip().lower() or "file"
        db_path_raw = _env("ABSTRACTGATEWAY_DB_PATH")

        enabled_raw = _env("ABSTRACTGATEWAY_RUNNER", "ABSTRACTFLOW_GATEWAY_RUNNER") or "1"
        runner_enabled = _as_bool(enabled_raw, True)

        poll_s = _as_float(_env("ABSTRACTGATEWAY_POLL_S", "ABSTRACTFLOW_GATEWAY_POLL_S"), 0.25)
        tick_workers = _as_int(_env("ABSTRACTGATEWAY_TICK_WORKERS", "ABSTRACTFLOW_GATEWAY_TICK_WORKERS"), 4)
        tick_steps = _as_int(_env("ABSTRACTGATEWAY_TICK_MAX_STEPS", "ABSTRACTFLOW_GATEWAY_TICK_MAX_STEPS"), 100)
        batch = _as_int(_env("ABSTRACTGATEWAY_COMMAND_BATCH_LIMIT", "ABSTRACTFLOW_GATEWAY_COMMAND_BATCH_LIMIT"), 200)
        scan = _as_int(_env("ABSTRACTGATEWAY_RUN_SCAN_LIMIT", "ABSTRACTFLOW_GATEWAY_RUN_SCAN_LIMIT"), 200)

        data_dir = Path(data_dir_raw).expanduser().resolve()
        flows_dir = Path(flows_dir_raw).expanduser().resolve()
        db_path = Path(db_path_raw).expanduser().resolve() if db_path_raw else None

        if store_backend == "sqlite":
            effective_db = (db_path if db_path is not None else (data_dir / "gateway.sqlite3")).expanduser().resolve()
            if not _is_within_dir(effective_db, data_dir):
                raw = str(db_path_raw or "").strip() or "<unset>"
                raise SystemExit(
                    "Invalid sqlite configuration: ABSTRACTGATEWAY_DB_PATH must point to a file under ABSTRACTGATEWAY_DATA_DIR.\n"
                    f"  ABSTRACTGATEWAY_DATA_DIR={data_dir}\n"
                    f"  ABSTRACTGATEWAY_DB_PATH={raw}\n"
                    f"  effective_db_path={effective_db}\n"
                    "\n"
                    "Fix: unset ABSTRACTGATEWAY_DB_PATH or set it to e.g. \"$ABSTRACTGATEWAY_DATA_DIR/gateway.sqlite3\".\n"
                    "This prevents cross-wiring UAT/prod durable state."
                )

        return GatewayHostConfig(
            data_dir=data_dir,
            flows_dir=flows_dir,
            framework_flows_dir=flows_dir,
            root_data_dir=data_dir,
            tenant_id="default",
            user_id="admin",
            runtime_id="default",
            store_backend=store_backend,
            db_path=db_path,
            runner_enabled=bool(runner_enabled),
            poll_interval_s=float(poll_s),
            command_batch_limit=max(1, int(batch)),
            tick_max_steps=max(1, int(tick_steps)),
            tick_workers=max(1, int(tick_workers)),
            run_scan_limit=max(1, int(scan)),
        )
