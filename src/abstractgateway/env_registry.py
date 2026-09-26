"""The declared gateway environment-variable registry (env-kill phase 0).

Operator order dm#177 ("REMOVE ALL UNNECESSARY ENV VARIABLES, THEY MUST ALL BE
PROPERLY CONFIGURABLE ON THE GATEWAY/CONSOLE"), refined by dm#194 (console
config supersedes everything; a CLI front door edits the SAME store) and
dm#201 (cloud API keys stay env-inheritable; config-set keys supersede).

This module is PHASE 0 of the shared transition contract (commons c4174):
pure observability, zero behavior change. It declares every environment
variable the gateway reads, classified by the contract's three-test rule:

    A var is DEPLOYMENT iff it fails one of three tests, else BEHAVIOR:
    1. BOOTSTRAP  - needed before the config store can be read/trusted
                    (data dirs, bind, store backend, the core-server URL that
                    locates the config authority, auth topology).
    2. SECRET     - credentials/signing material (env-or-secret-file only;
                    consoles show presence, never values).
    3. PROCESS-ROLE - which role THIS process plays (e.g. the runner flag the
                    CLI flips per process).

Extra classes carried by the registry (they shape the migration, not the
rule): FOREIGN (read by the gateway but owned by another package - the
gateway migrates its READ to the owner's facade, never forks the value) and
LEGACY_ALIAS (a second spelling of a registered var - warn-when-winning,
remove in phase 4).

The registry is the source of truth three consumers score against:
- the CI orphan pin (tests/test_gateway_env_registry.py): an env read in
  src/ that is not declared here fails the suite - the inventory stops being
  a one-time audit and becomes an enforced invariant;
- the boot env scanner (phase 1+): foreign/retired/unknown names in the
  process env get a boot banner + /api/health warning (the AGORA_API_KEY
  contamination class);
- the settings-registry migration (phase 1+): behavior rows gain store keys
  + console/CLI fields; `console_path` names the planned section.

Classification decisions follow the design adversary's hardest-ten rulings
(2026-07-21) and the operator rulings verbatim; disputed rows carry a note.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Classes.
BEHAVIOR = "behavior"          # migrates to console/CLI config; env demotes to #FALLBACK then dies
DEPLOYMENT = "deployment"      # legitimately env (bootstrap / process-role / diagnostics)
SECRET = "secret"              # env-or-secret-file only; console shows presence
FOREIGN = "foreign"            # another package owns it; gateway reads via the owner's facade
LEGACY_ALIAS = "legacy_alias"  # second spelling; warn-when-winning, phase-4 removal

_CLASSES = {BEHAVIOR, DEPLOYMENT, SECRET, FOREIGN, LEGACY_ALIAS}


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    klass: str
    owner: str = "gateway"           # owning seat (foreign rows name the real owner)
    scope: str = "shared"            # shared | per-principal | process
    effective: str = "next-request"  # immediate | next-request | restart-required
    console_path: str = ""           # planned console/CLI section (phase 1+)
    alias_of: str = ""               # for legacy_alias rows
    note: str = ""
    # The stored setting that replaces this env var: where a user
    # changes it (console/TUI/CLI). The env var stays readable as the labeled
    # fallback (behavior rows) or override (deployment carve-out rows).
    superseded_by: str = ""

    def __post_init__(self) -> None:
        if self.klass not in _CLASSES:
            raise ValueError(f"unknown env class {self.klass!r} for {self.name}")


def _spec(name: str, klass: str, **kw) -> EnvVarSpec:
    return EnvVarSpec(name=name, klass=klass, **kw)


# ---------------------------------------------------------------------------
# Family rules: (prefix -> template). Explicit SPECS below always win over a
# family match. Families keep the table maintainable at ~300 names; the CI
# orphan pin keeps it complete.
# ---------------------------------------------------------------------------

_FAMILY_RULES: Tuple[Tuple[str, EnvVarSpec], ...] = (
    # Legacy alias families (single-reader chains where the gateway name wins).
    ("ABSTRACTFLOW_GATEWAY_", _spec("*", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_*",
                                    note="abstractflow-era spelling; warn-when-winning, phase-4 removal")),
    ("ABSTRACT_BACKLOG_", _spec("*", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_BACKLOG_*",
                                note="unprefixed backlog twin")),
    ("ABSTRACT_TRIAGE_", _spec("*", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_TRIAGE_*",
                               note="unprefixed triage twin")),
    # Bridges: BEHAVIOR by default (service policy - allowlists, poll cadence,
    # budgets); secret/deployment rows are explicit below.
    ("ABSTRACT_TELEGRAM_", _spec("*", BEHAVIOR, console_path="bridges.telegram",
                                 effective="restart-required",
                                 note="bridge config snapshots at boot (P0-1 effectiveness contract)")),
    ("ABSTRACT_EMAIL_", _spec("*", BEHAVIOR, console_path="bridges.email",
                              effective="restart-required")),
    # Backlog/exec + triage-LLM: BEHAVIOR (which executor/model/policy).
    ("ABSTRACTGATEWAY_BACKLOG_", _spec("*", BEHAVIOR, console_path="backlog")),
    ("ABSTRACTGATEWAY_TRIAGE_", _spec("*", BEHAVIOR, console_path="triage")),
    # Agora bridge behavior (residents/cadence); state path explicit below.
    ("ABSTRACTGATEWAY_AGORA_", _spec("*", BEHAVIOR, console_path="bridges.agora",
                                     effective="restart-required")),
    # Voice/vision FOREIGN namespaces: core/voice/vision own execution; the
    # gateway reads them only as labeled #FALLBACK below the capability
    # facade (dm#28 config-first fix; alias-order flip agreed with core c4228).
    ("ABSTRACTVOICE_", _spec("*", FOREIGN, owner="core+voice",
                             note="execution-owned; gateway reads via capability facade, env is labeled #FALLBACK")),
    ("ABSTRACTVISION_", _spec("*", FOREIGN, owner="core+vision",
                              note="execution-owned; migrate reads to capability facade (core c4264 shipped its half)")),
    ("ABSTRACTCORE_VISION_", _spec("*", FOREIGN, owner="core")),
    ("ABSTRACTCORE_DISCOVERY_", _spec("*", FOREIGN, owner="core",
                                      note="discovery timeout knobs; core-owned semantics")),
    # Gateway voice knobs: BEHAVIOR (advertising defaults; capability route is
    # the store — these envs are the #FALLBACK rung, ratchet-to-delete).
    ("ABSTRACTGATEWAY_VOICE_", _spec("*", BEHAVIOR, console_path="multimodal.voice",
                                     note="capability route output.voice/input.voice is the store")),
    ("ABSTRACTGATEWAY_VISION_", _spec("*", BEHAVIOR, console_path="multimodal.vision")),
    # UAT harness ports/dirs: deployment/test-seam.
    ("ABSTRACTGATEWAY_UAT_", _spec("*", DEPLOYMENT, scope="process", note="UAT harness seam")),
    # Report-to-backlog handling policy (caught by the orphan pin's first run).
    ("ABSTRACTGATEWAY_REPORT_", _spec("*", BEHAVIOR, console_path="reports")),
)

# ---------------------------------------------------------------------------
# Explicit rows (win over family rules).
# ---------------------------------------------------------------------------

_EXPLICIT: Tuple[EnvVarSpec, ...] = (
    # --- BOOTSTRAP (deployment): locate/trust the stores + serve topology ---
    _spec("ABSTRACTGATEWAY_DATA_DIR", DEPLOYMENT, note="bootstrap: locates every store"),
    _spec("ABSTRACTGATEWAY_FLOWS_DIR", DEPLOYMENT, note="bootstrap: bundle dir"),
    _spec("ABSTRACTGATEWAY_VISUALFLOWS_DIR", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_WORKFLOW_CATALOG_BUNDLES_DIR", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_WORKFLOW_CATALOG_FILE", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_WORKFLOW_SOURCE", DEPLOYMENT, note="bundle is the only supported source"),
    _spec("ABSTRACTGATEWAY_STORE_BACKEND", DEPLOYMENT, note="bootstrap: file|sqlite"),
    _spec("ABSTRACTGATEWAY_DB_PATH", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_MEMORY_BACKEND", DEPLOYMENT, note="store-layout bootstrap"),
    _spec("ABSTRACTGATEWAY_MEMORY_PATH", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_MEMORY_STORE_PATH", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR", BEHAVIOR, console_path="memory",
          note="strictness toggle, not a path"),
    _spec("ABSTRACTGATEWAY_USER_AUTH", DEPLOYMENT,
          note="auth topology: selects data-root LAYOUT — a stored copy is circular (adversary P0-4)"),
    _spec("ABSTRACTGATEWAY_MULTI_USER", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_USER_AUTH"),
    _spec("ABSTRACTGATEWAY_USER_AUTH_AUTO", DEPLOYMENT, note="bootstrap admin auto-create"),
    _spec("ABSTRACTGATEWAY_AUTH_MODE", DEPLOYMENT, note="auth topology"),
    _spec("ABSTRACTGATEWAY_SECURITY", DEPLOYMENT, note="security middleware master switch (escape hatch)"),
    _spec("ABSTRACTGATEWAY_USERS_FILE", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_SESSIONS_FILE", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME", DEPLOYMENT, note="runtime-layout topology"),
    _spec("ABSTRACTGATEWAY_DECLARED_ADDRESS", DEPLOYMENT, note="the operator-declared public address (GW-F)"),
    _spec("ABSTRACTCORE_SERVER_BASE_URL", DEPLOYMENT, owner="core",
          note="the address of the config AUTHORITY itself — a store rung cannot point at its own store"),
    _spec("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY", SECRET),
    _spec("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN", SECRET),
    _spec("ABSTRACTCORE_AUTH_TOKEN", SECRET, owner="core"),
    _spec("ABSTRACTCORE_SERVER_API_KEY", SECRET, owner="core"),

    # --- SECRETS ---
    _spec("ABSTRACTGATEWAY_AUTH_TOKEN", SECRET),
    _spec("ABSTRACTGATEWAY_AUTH_TOKENS", SECRET),
    _spec("ABSTRACTGATEWAY_SESSION_SECRET", SECRET),
    _spec("ABSTRACTGATEWAY_SESSION_SECRET_FILE", SECRET),
    _spec("ABSTRACTGATEWAY_ENTITY_STAMP_SECRET", SECRET),
    _spec("ABSTRACTGATEWAY_WORKFLOW_POLICY_SECRET", SECRET),
    _spec("ABSTRACTGATEWAY_WORKFLOW_POLICY_SECRET_FILE", SECRET),
    _spec("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN", SECRET),
    _spec("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN_FILE", SECRET),
    _spec("ABSTRACTGATEWAY_TRIAGE_ACTION_SECRET", SECRET),
    _spec("ABSTRACTGATEWAY_TRIAGE_LLM_API_KEY", SECRET),
    _spec("ABSTRACT_TELEGRAM_BOT_TOKEN", SECRET),
    _spec("ABSTRACT_EMAIL_IMAP_PASSWORD_ENV_VAR", SECRET, note="indirection var naming the secret var"),
    _spec("ABSTRACT_EMAIL_SMTP_PASSWORD_ENV_VAR", SECRET),
    # Cloud API keys: RULED exception (dm#201) — env-inheritable by default,
    # config-set key ALWAYS supersedes (pinned in test_gateway_api_key_precedence).
    _spec("OPENAI_API_KEY", SECRET, owner="shared", note="dm#201 ruled: inherit by default, config supersedes"),
    _spec("ANTHROPIC_API_KEY", SECRET, owner="shared", note="dm#201"),
    _spec("OPENROUTER_API_KEY", SECRET, owner="shared", note="dm#201"),
    _spec("PORTKEY_API_KEY", SECRET, owner="shared", note="dm#201"),
    _spec("ABSTRACTVOICE_OPENAI_API_KEY", SECRET, owner="core+voice"),

    # --- PROCESS-ROLE + diagnostics (deployment) ---
    _spec("ABSTRACTGATEWAY_RUNNER", DEPLOYMENT, scope="process",
          note="process-role: the CLI flips it per process (API vs runner)"),
    # Written by `serve` for the app it hosts (network_exposure.py): the bind
    # this process was given and where each half came from. Never operator input;
    # the network exposure SETTING lives in the runtime-config store (no env rung).
    _spec("ABSTRACTGATEWAY_BIND_HOST", DEPLOYMENT, scope="process", note="serve export: the bound host"),
    _spec("ABSTRACTGATEWAY_BIND_PORT", DEPLOYMENT, scope="process", note="serve export: the bound port"),
    _spec("ABSTRACTGATEWAY_BIND_SOURCE", DEPLOYMENT, scope="process",
          note="serve export: host=cli|setting|default;port=cli|setting|default"),
    _spec("ABSTRACTGATEWAY_NETWORK_EXPORTS", DEPLOYMENT, scope="process",
          note="serve export: env names set for the network setting, forgotten and re-derived at every start"),
    _spec("ABSTRACTGATEWAY_AUTH_MODE_SOURCE", DEPLOYMENT, scope="process",
          note="serve export: loopback_default | network_setting (why user auth was turned on)"),
    _spec("ABSTRACTGATEWAY_LOG_LEVEL", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_CONSOLE_LOG_LEVEL", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_REQUEST_TIMING", DEPLOYMENT, scope="process", note="diagnostics"),
    _spec("ABSTRACTGATEWAY_SLOW_REQUEST_MS", DEPLOYMENT, scope="process", note="diagnostics"),
    _spec("ABSTRACTGATEWAY_SILENCE_GPU_METRICS_ACCESS_LOG", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_GPU_METRICS_PROVIDER", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_RUNNER_LOCK_STALE_S", DEPLOYMENT, scope="process", note="diagnostics threshold"),
    _spec("ABSTRACTGATEWAY_TICK_WEDGE_AFTER_S", DEPLOYMENT, scope="process", note="report-only threshold"),
    _spec("ABSTRACTGATEWAY_GRACEFUL_SHUTDOWN_S", DEPLOYMENT, scope="process",
          note="pairs with the supervisor's kill timeout (design adversary ruling)"),
    _spec("ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX", BEHAVIOR, console_path="runtime.boot",
          note="design adversary ruled BEHAVIOR, restart-required", effective="restart-required"),
    _spec("ABSTRACTGATEWAY_AUDIT_LOG", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_AUDIT_LOG_HEADERS", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_AUDIT_LOG_MAX_BYTES", DEPLOYMENT, scope="process"),
    _spec("ABSTRACTGATEWAY_AUDIT_LOG_ROTATIONS", DEPLOYMENT, scope="process"),
    _spec("SHELL", DEPLOYMENT, owner="os", note="host shell (process-manager env plumbing)"),
    _spec("FORWARDED_ALLOW_IPS", DEPLOYMENT, owner="uvicorn",
          note="uvicorn's trusted-proxy list; serve warns when it is '*' (peer address spoofable)"),
    _spec("ABSTRACTGATEWAY_URL", DEPLOYMENT,
          note="client-side default gateway base URL for `abstractgateway models` (never read by serve)"),

    # --- BEHAVIOR: service posture / limits / toggles (settings-registry rows) ---
    _spec("ABSTRACTGATEWAY_TOOL_MODE", BEHAVIOR, console_path="tools.mode",
          note="SECURITY posture — one export flips approvals gateway-wide; shared-only scope, loud console warning"),
    _spec("ABSTRACTGATEWAY_PROMPT_CACHE", BEHAVIOR, console_path="runtime.prompt_cache"),
    _spec("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", BEHAVIOR, console_path="workspace",
          note="security-adjacent; shared-only"),
    _spec("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", BEHAVIOR, console_path="workspace"),
    _spec("ABSTRACTGATEWAY_WORKSPACE_DIR", DEPLOYMENT, note="bootstrap path"),
    _spec("ABSTRACTGATEWAY_WORKSPACE_MOUNTS", DEPLOYMENT, note="bootstrap paths"),
    _spec("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", BEHAVIOR, console_path="maintenance.process_manager",
          note="already stored>env>default in runtime_config.py"),
    _spec("ABSTRACTGATEWAY_PROCESS_MANAGER_CONFIG", DEPLOYMENT, note="config file path"),
    _spec("ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED", BEHAVIOR, console_path="catalog"),
    _spec("ABSTRACTGATEWAY_AUTO_BOOTSTRAP_ADMIN", DEPLOYMENT, note="bootstrap"),
    _spec("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN", DEPLOYMENT, note="legacy alias of `serve --print-token`"),
    _spec("ABSTRACTGATEWAY_DEV_READ_NO_AUTH", DEPLOYMENT,
          note="dev posture; security-inversion carve-out — a config write must not weaken auth"),
    _spec("ABSTRACTGATEWAY_ALLOWED_ORIGINS", DEPLOYMENT,
          superseded_by="network.allowed_origins (POST /api/gateway/network; `abstractgateway network set --allowed-origins`)",
          note="browser-origin security; the stored setting is the door, the env var an explicit start-time override "
               "reported as overridden_by_env (security carve-out)"),
    _spec("ABSTRACTGATEWAY_TRUST_PROXY", DEPLOYMENT,
          superseded_by="network.trust_proxy (POST /api/gateway/network; `abstractgateway network set --trust-proxy`)",
          note="start-time override reported as overridden_by_env"),
    _spec("ABSTRACTGATEWAY_PROTECT_READ", DEPLOYMENT, note="auth topology"),
    _spec("ABSTRACTGATEWAY_PROTECT_WRITE", DEPLOYMENT, note="auth topology"),
    _spec("ABSTRACTGATEWAY_SESSION_COOKIE", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_SESSION_HEADER", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_CSRF_COOKIE", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_CSRF_HEADER", DEPLOYMENT),
    _spec("ABSTRACTGATEWAY_SESSION_TTL_S", BEHAVIOR, console_path="security.sessions"),
    _spec("ABSTRACTGATEWAY_REMEMBER_SESSION_TTL_S", BEHAVIOR, console_path="security.sessions"),
    _spec("ABSTRACTGATEWAY_MAX_SESSIONS", BEHAVIOR, console_path="security.sessions"),
    _spec("ABSTRACTGATEWAY_LOCKOUT_AFTER", BEHAVIOR, console_path="security.lockout"),
    _spec("ABSTRACTGATEWAY_LOCKOUT_BASE_S", BEHAVIOR, console_path="security.lockout"),
    _spec("ABSTRACTGATEWAY_LOCKOUT_MAX_S", BEHAVIOR, console_path="security.lockout"),
    # Browser apps (apps_manager.py): which Node.js, where the apps listen, and
    # the package mirrors. Service policy => BEHAVIOR (console section "apps").
    _spec("ABSTRACTGATEWAY_APPS_NODE", BEHAVIOR, console_path="apps",
          superseded_by="runtime-config apps.node (`abstractgateway apps config set node`)",
          note="auto | managed | system | /path/to/node"),
    _spec("ABSTRACTGATEWAY_APPS_PORTS", BEHAVIOR, console_path="apps",
          superseded_by="runtime-config apps.ports (`abstractgateway apps config set ports`)"),
    _spec("ABSTRACTGATEWAY_APPS_HOST", BEHAVIOR, console_path="apps",
          superseded_by="runtime-config apps.host (`abstractgateway apps config set host`)",
          note="default 127.0.0.1; anything else exposes the apps"),
    _spec("ABSTRACTGATEWAY_APPS_NPM_REGISTRY", BEHAVIOR, console_path="apps",
          superseded_by="runtime-config apps.npm_registry (`abstractgateway apps config set npm_registry`)"),
    _spec("ABSTRACTGATEWAY_APPS_PYPI_URL", BEHAVIOR, console_path="apps",
          superseded_by="runtime-config apps.pypi_url (`abstractgateway apps config set pypi_url`)"),
    # Request/limit knobs: change request outcomes => BEHAVIOR.
    _spec("ABSTRACTGATEWAY_MAX_BODY_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_UPLOAD_BODY_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_BUNDLE_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_CONCURRENCY", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_SSE", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_CHAT_THREAD_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_MAX_BACKLOG_ATTACHMENT_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTGATEWAY_IMAGE_MAX_BYTES", BEHAVIOR, console_path="limits"),
    _spec("ABSTRACTCORE_IMAGE_MAX_BYTES", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_IMAGE_MAX_BYTES"),
    _spec("ABSTRACTGATEWAY_SESSION_HISTORY_MAX_MESSAGES", BEHAVIOR, console_path="sessions.history"),
    _spec("ABSTRACTGATEWAY_SESSION_HISTORY_MAX_CHARS", BEHAVIOR, console_path="sessions.history"),
    _spec("ABSTRACTGATEWAY_VOICE_TTS_TIMEOUT_S", BEHAVIOR, console_path="multimodal.voice",
          note="request outcome (504) => behavior"),
    _spec("ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY", BEHAVIOR, console_path="multimodal.voice",
          note="request outcome (503) => behavior; hot path needs the cached resolver"),
    _spec("ABSTRACTGATEWAY_PROVIDER_MODELS_TIMEOUT_S", BEHAVIOR, console_path="discovery"),
    _spec("ABSTRACTGATEWAY_PROVIDER_AUTOPROBE_TIMEOUT_S", BEHAVIOR, console_path="discovery"),
    _spec("ABSTRACTGATEWAY_DISCOVERY_MAX_CONCURRENCY", BEHAVIOR, console_path="discovery",
          note="admission bound for discovery probes; keeps a wedged provider from "
               "parking every shared to_thread pool thread"),
    _spec("ABSTRACTGATEWAY_DISCOVERY_TIMEOUT_S", BEHAVIOR, console_path="discovery"),
    _spec("ABSTRACTGATEWAY_DISCOVERY_MODEL_TIMEOUT_S", BEHAVIOR, console_path="discovery"),
    _spec("ABSTRACTGATEWAY_DRAFT_RUN_TTL_S", BEHAVIOR, console_path="retention"),
    _spec("ABSTRACTGATEWAY_DRAFT_RUN_RETENTION_TTL_S", BEHAVIOR, console_path="retention"),
    _spec("ABSTRACTGATEWAY_FILE_INDEX_MAX_FILES", BEHAVIOR, console_path="workspace"),
    _spec("ABSTRACTGATEWAY_FILE_INDEX_TTL_S", BEHAVIOR, console_path="workspace"),
    _spec("ABSTRACTGATEWAY_DRIVE_PRESSURE_THRESHOLD", BEHAVIOR, console_path="maintenance"),
    _spec("ABSTRACTGATEWAY_DOCS_CORPUS", DEPLOYMENT, note="docs corpus path"),
    _spec("ABSTRACTGATEWAY_SKILLS_SHELF", DEPLOYMENT, note="shelf path"),
    # Runner cadence knobs (host config; construction-time).
    _spec("ABSTRACTGATEWAY_POLL_S", BEHAVIOR, console_path="runtime.runner", effective="restart-required"),
    _spec("ABSTRACTGATEWAY_TICK_WORKERS", BEHAVIOR, console_path="runtime.runner", effective="restart-required"),
    _spec("ABSTRACTGATEWAY_TICK_MAX_STEPS", BEHAVIOR, console_path="runtime.runner", effective="restart-required"),
    _spec("ABSTRACTGATEWAY_RUN_SCAN_LIMIT", BEHAVIOR, console_path="runtime.runner", effective="restart-required"),
    _spec("ABSTRACTGATEWAY_COMMAND_BATCH_LIMIT", BEHAVIOR, console_path="runtime.runner", effective="restart-required"),
    # Stop kill switch (stop_kill_switch.py): the runtime-config key
    # stop_kill_switch_s supersedes it; read at every Stop, so a change
    # applies to the next one.
    _spec("ABSTRACTGATEWAY_STOP_KILL_SWITCH_S", BEHAVIOR, console_path="runtime.stop_kill_switch_s",
          effective="next-request", note="seconds after a cancel before a still-decoding model call is killed in process (never the gateway); 0 disables"),
    # Entity lane.
    _spec("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", BEHAVIOR, console_path="entities.substrate",
          note="operator-env rung of the ruled substrate chain (request > home > env > refusal); console rung replaces THIS rung, never reorders (adversary P0-3)"),
    _spec("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", BEHAVIOR, console_path="entities.substrate", note="see PROVIDER row"),
    _spec("ABSTRACTGATEWAY_ENTITY_CHAT_BASE_URL", BEHAVIOR, console_path="entities.substrate"),
    _spec("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW", BEHAVIOR, console_path="entities.substrate"),
    _spec("ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE", BEHAVIOR, console_path="entities.substrate"),
    _spec("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", BEHAVIOR, console_path="entities.quotas"),
    _spec("ABSTRACTGATEWAY_ENTITY_MAX_ITERATIONS_CEILING", BEHAVIOR, console_path="entities.quotas"),
    _spec("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR", BEHAVIOR, console_path="entities.self_repair"),
    _spec("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR_INTERVAL_S", BEHAVIOR, console_path="entities.self_repair"),
    _spec("ABSTRACTRUNTIME_GLOBAL_MEMORY_RUN_ID", FOREIGN, owner="runtime"),
    # Agora bridge state path (deployment) — behavior family covers the rest.
    _spec("ABSTRACTGATEWAY_AGORA_STATE_PATH", DEPLOYMENT, note="state file path"),
    _spec("ABSTRACTGATEWAY_AGORA_BRIDGE", BEHAVIOR, console_path="bridges.agora",
          effective="restart-required", note="bridge enable toggle"),
    _spec("ABSTRACT_TELEGRAM_BRIDGE", BEHAVIOR, console_path="bridges.telegram",
          effective="restart-required", note="bridge enable toggle"),
    _spec("ABSTRACT_EMAIL_BRIDGE", BEHAVIOR, console_path="bridges.email",
          effective="restart-required", note="bridge enable toggle"),
    _spec("ABSTRACT_TELEGRAM_STATE_PATH", DEPLOYMENT, note="state file path"),
    _spec("ABSTRACT_EMAIL_ACCOUNTS_CONFIG", DEPLOYMENT, note="config file path"),
    # Desktop session + per-OS user directories (READS OF THE OS, not settings):
    # the gateway asks the operating system where the user's session and
    # directories are. Nothing here is a gateway knob and nothing migrates to
    # the console; DEPLOYMENT keeps them out of the boot scanner's warnings.
    _spec("DISPLAY", DEPLOYMENT, owner="desktop-session", scope="process",
          note="desktop session read (default: unset = no X11 session); engines_install.detect_host "
               "(gui_session), apps_manager (can a browser open), tray_supervisor (can a tray icon show)"),
    _spec("WAYLAND_DISPLAY", DEPLOYMENT, owner="desktop-session", scope="process",
          note="desktop session read (default: unset = no Wayland session); same readers as DISPLAY"),
    _spec("XDG_CACHE_HOME", DEPLOYMENT, owner="desktop-session", scope="process",
          note="per-user directory read, Linux (default ~/.cache; relative values ignored per the XDG spec); "
               "host_paths.user_cache_dir -> engine installer downloads"),
    _spec("XDG_DATA_HOME", DEPLOYMENT, owner="desktop-session", scope="process",
          note="per-user directory read, Linux (default ~/.local/share); host_paths.user_data_dir"),
    _spec("XDG_CONFIG_HOME", DEPLOYMENT, owner="desktop-session", scope="process",
          note="per-user directory read, Linux (default ~/.config); os_service autostart entry path"),
    _spec("LOCALAPPDATA", DEPLOYMENT, owner="desktop-session", scope="process",
          note="per-user directory read, Windows (default ~/AppData/Local); host_paths data + cache dirs, "
               "apps_manager Node.js lookup"),
    _spec("APPDATA", DEPLOYMENT, owner="desktop-session", scope="process",
          note="per-user directory read, Windows (default ~/AppData/Roaming); os_service legacy Startup shortcut"),
    # Foreign singles.
    _spec("OPENAI_BASE_URL", FOREIGN, owner="core", note="provider base url; core owns execution"),
    _spec("ANTHROPIC_BASE_URL", FOREIGN, owner="core"),
    _spec("OPENROUTER_BASE_URL", FOREIGN, owner="core"),
    _spec("PORTKEY_BASE_URL", FOREIGN, owner="core"),
    _spec("OPENAI_IMAGE_MODEL", FOREIGN, owner="core"),
    _spec("OPENAI_IMAGE_MODEL_ID", FOREIGN, owner="core"),
    _spec("ABSTRACTFLOW_RUNTIME_DIR", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_DATA_DIR"),
    _spec("ABSTRACTFLOW_FLOWS_DIR", LEGACY_ALIAS, alias_of="ABSTRACTGATEWAY_FLOWS_DIR"),
    _spec("ABSTRACTFRAMEWORK_WORKFLOWS_DIR", DEPLOYMENT, note="framework-level bundle dir"),
    _spec("ABSTRACTFRAMEWORK_DATA_REGISTRY", DEPLOYMENT, note="machine-level data-home registry path"),
    # Test/UAT seams read by maintenance code.
    _spec("ABSTRACTCODE_WEB_UAT_PORT", DEPLOYMENT, scope="process", note="UAT seam"),
    _spec("ABSTRACTFLOW_BACKEND_UAT_PORT", DEPLOYMENT, scope="process", note="UAT seam"),
    _spec("ABSTRACTFLOW_FRONTEND_UAT_PORT", DEPLOYMENT, scope="process", note="UAT seam"),
    _spec("ABSTRACTOBSERVER_UAT_PORT", DEPLOYMENT, scope="process", note="UAT seam"),
    _spec("ABSTRACTUIC_SRC", DEPLOYMENT, scope="process", note="UI kit source path (build seam)"),
    _spec("ABSTRACTCODE_SKILLS_ROOTS", FOREIGN, owner="skill"),
    _spec("ABSTRACTCODE_SKILLS_VALIDATIONS", FOREIGN, owner="skill"),
    _spec("ABSTRACTCODE_SKILLS_ADVISORIES", FOREIGN, owner="skill"),
)

_EXPLICIT_BY_NAME: Dict[str, EnvVarSpec] = {s.name: s for s in _EXPLICIT}


def declared_explicit_names() -> List[str]:
    """Every explicitly-declared env var name (the scanner's union half:
    declared rows OUTSIDE the framework prefixes — OPENAI_*/ANTHROPIC_* base
    URLs etc. — must still be scanned; adversary F1 2026-07-22)."""
    return list(_EXPLICIT_BY_NAME.keys())


def classify_env_var(name: str) -> Optional[EnvVarSpec]:
    """The declared spec for an env var name: explicit row first, then the
    longest matching family prefix; None = UNDECLARED (the orphan-pin fails)."""
    key = str(name or "").strip()
    if not key:
        return None
    hit = _EXPLICIT_BY_NAME.get(key)
    if hit is not None:
        return hit
    best: Optional[Tuple[str, EnvVarSpec]] = None
    for prefix, template in _FAMILY_RULES:
        if key.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, template)
    if best is None:
        return None
    tpl = best[1]
    return EnvVarSpec(
        name=key,
        klass=tpl.klass,
        owner=tpl.owner,
        scope=tpl.scope,
        effective=tpl.effective,
        console_path=tpl.console_path,
        alias_of=tpl.alias_of,
        note=tpl.note,
    )


def registry_rows(names: List[str]) -> List[Dict[str, str]]:
    """Classified rows for a list of names (the console/CLI/scanner shape)."""
    out: List[Dict[str, str]] = []
    for name in sorted(set(str(n).strip() for n in names if str(n).strip())):
        spec = classify_env_var(name)
        if spec is None:
            out.append({"name": name, "class": "UNDECLARED"})
            continue
        row = {"name": name, "class": spec.klass, "owner": spec.owner, "scope": spec.scope,
               "effective": spec.effective}
        if spec.console_path:
            row["console_path"] = spec.console_path
        if spec.alias_of:
            row["alias_of"] = spec.alias_of
        if spec.note:
            row["note"] = spec.note
        if spec.superseded_by:
            row["superseded_by"] = spec.superseded_by
        out.append(row)
    return out
