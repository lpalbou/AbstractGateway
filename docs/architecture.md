# AbstractGateway — Architecture

AbstractGateway is a **durable run gateway** for AbstractRuntime, and the
control plane of an AbstractFramework installation:

- clients **start runs** (and optionally schedule them);
- clients act through **durable commands** (`pause`, `resume`, `cancel`,
  `emit_event`, …);
- clients **replay** the durable ledger and optionally **stream** it (SSE);
- operators manage users, providers, capability defaults, local engines,
  models, browser apps and network exposure from one place (the web console,
  the terminal console, the CLI, or the desktop tray).

This page describes the components in this repository and how they connect.
For the endpoints, see [api.md](./api.md); for settings, see
[configuration.md](./configuration.md).

## Ecosystem placement (AbstractFramework)

AbstractGateway sits between **clients** (browser apps, terminal apps, the
desktop Assistant, scripts) and **AbstractRuntime**:

- **AbstractGateway** (this package): HTTP/SSE API, durability glue, security,
  and the operator control plane.
- **AbstractRuntime** (required, 0.4.37 or later): run model, tick loop,
  workflow registry, stores, live token deltas and the workspace-scoped tools.
  The gateway checks the installed runtime when it builds its workflow host and
  refuses to start on an older one, naming the version to install.
- **AbstractCore** (required, reached through Runtime facades): providers,
  tools, media capabilities, capability-route defaults, the model catalog,
  engine detection and host jobs (downloads, deletes).
- **AbstractAgent** and **AbstractMemory** (required by the default install):
  agent nodes and KG memory for bundle execution.
- Higher-level apps (optional): AbstractFlow (authoring), AbstractCode,
  AbstractObserver, AbstractContinuum, AbstractEntity and AbstractAssistant.

## System overview

```mermaid
flowchart LR
  subgraph Clients["Clients"]
    Browser["Browser: /console"]
    AppSrv["Browser app servers on 127.0.0.1<br/>(Flow, Code, Observer, Continuum, Entity)"]
    TUI["Terminal apps: abstractgateway-console, Code"]
    Asst["Desktop Assistant"]
    Tray["Desktop tray helper (separate process)"]
    CLI["abstractgateway CLI"]
  end

  subgraph GW["AbstractGateway process (abstractgateway serve)"]
    Sec["GatewaySecurityMiddleware: auth, origins, limits, audit log"]
    Same["Same-machine rule: is the caller at this computer?"]
    Routes["FastAPI routes /api/gateway/*"]
    Console["/console (web console)"]
    Handover["Sign-in hand-overs: apps, terminal, desktop"]
    Principals["Principal routing: one data plane per user"]
    Defaults["Default agent workflow resolver (@default)"]
    Browse["Workspace browser: /runs/{id}/workspace/*"]
    Host["Workflow host: .flow bundles + workflow catalog"]
    Guard["Run workspace guard: a folder and the built-in deny rules for every run"]
    Hub["Live-delta hub: llm.delta frames per root run"]
    Runner["GatewayRunner: command inbox + ticks"]
    Settings["Runtime settings: network, apps.*, agents.*, skills.shelf, workspace, backlog"]
    Skills["Skills shelf (seeded from AbstractSkill)"]
    AppsMgr["Apps manager: Node.js, npm installs, app processes"]
    Jobs["Engine installs and model download jobs"]
    HostCtl["Host control: pause, restart, update"]
  end

  subgraph Lower["Framework packages"]
    RT["AbstractRuntime: Runtime.tick, stores, workspace-scoped tools"]
    Core["AbstractCore: providers, catalog, host jobs"]
  end

  Data[("Data dir: runs, ledgers, commands, artifacts, auth, settings")]
  WS[("Run workspace folders")]

  Browser -->|HTTP| Sec
  Browser -->|app pages| AppSrv
  AppSrv -->|"same-origin proxy: X-Forwarded-For + X-AbstractFramework-App-Proxy"| Sec
  TUI -->|HTTP| Sec
  Asst -->|HTTP, gateway session| Sec
  CLI -->|HTTP or data dir| Sec
  Tray -->|loopback HTTP, ephemeral token| Sec
  Sec --> Routes
  Sec --> Console
  Routes -.->|installs, open folder, workspace host| Same
  Routes --> Principals --> Host
  Routes --> Defaults --> Host
  Routes --> Browse --> WS
  Routes --> Settings
  Routes --> Skills
  Routes --> AppsMgr
  Routes --> Jobs
  Routes --> HostCtl
  Host --> Guard --> RT
  Runner --> Host
  Runner --> RT
  RT -->|file tools confined to| WS
  RT -->|live text of LLM calls| Hub
  Hub -->|"SSE on /runs/{id}/ledger/stream"| Routes
  RT --> Data
  Routes --> Data
  Jobs --> Core
  Host --> Core
  AppsMgr -->|starts and supervises| AppSrv
  AppsMgr -->|launches with a hand-over file| Asst
  Handover --> AppSrv
  Handover --> Asst
```

Browser apps reach the gateway through their own app server on this computer,
which relays every call with the browser's address (`X-Forwarded-For`) and its
marker header (`X-AbstractFramework-App-Proxy`), so the gateway can tell a
browser on this computer from one elsewhere on the network
([security.md](./security.md#callers-on-this-computer)).

## Core components (code-mapped)

- **HTTP app** (`src/abstractgateway/app.py`): mounts the routers under `/api`
  (`/api/gateway/*` is the main surface), `/api/health`, `/console`,
  `/docs` (Swagger UI) and the app handover routes.
- **Security layer** (`src/abstractgateway/security/`): the
  `GatewaySecurityMiddleware` protects `/api/gateway/*` with user or token
  auth, an origin allowlist, request limits, auth lockouts and an audit log.
  Browser sessions, principals and the route-family authorization table live
  in the same package. See [security.md](./security.md).
- **Composition root** (`src/abstractgateway/service.py`): builds the stores,
  the workflow host and the runner. With user auth on, each principal is
  routed to its own service and data plane under
  `<data dir>/users/<tenant>/<runtime>/`.
- **Durable stores** (`src/abstractgateway/stores.py`): file-backed (default)
  or SQLite, using AbstractRuntime's RunStore, LedgerStore, CommandStore and
  ArtifactStore.
- **Workflow host** (`src/abstractgateway/hosts/bundle_host.py`): loads `.flow`
  WorkflowBundles and compiles their VisualFlow JSON with
  `abstractruntime.visualflow_compiler`. Bundle mode is the only workflow
  source; store VisualFlows through `/api/gateway/visualflows/*` and publish
  them as bundles.
- **Workflow catalog** (`src/abstractgateway/workflow_catalog.py`): shared,
  immutable workflow versions with admin-managed default pointers and ACLs.
  Catalog runs execute in the caller's runtime; the gateway signs the
  catalog's workflow policy before Runtime receives it.
- **Runner** (`src/abstractgateway/runner.py`): polls the durable command
  inbox, applies commands and ticks RUNNING runs. A filesystem lock
  (`gateway_runner.lock`) prevents double-ticking when API and runner run as
  separate processes; lock state is reported on `GET /api/health`.
- **Runtime settings** (`src/abstractgateway/runtime_config.py`): one store,
  `<data dir>/config/runtime_config.json`, for the settings the consoles, the
  tray and the CLI change: network exposure, `apps.*`, `allow_engine_install`,
  `agents.default_workflow.<interface>`, `agents.streaming_default`,
  `skills.shelf`, `workspace_builtin_deny`, the backlog folder, the backlog
  exec runner, the process manager and the stop kill switch. A write that
  names an unknown setting is refused whole; writers take a file lock. See
  [configuration.md](./configuration.md).
- **Default agent workflows** (`src/abstractgateway/agent_defaults.py`):
  resolves `flow_id: "@default"` plus an agent `interface` to the workflow
  saved under `agents.default_workflow.<interface>` (or the built-in default)
  at every run start, for the HTTP routes, the Telegram bridge and the backlog
  advisor alike, and records the choice as `resolved_workflow`. See
  [configuration.md](./configuration.md#default-agent-workflow).
- **Run workspace guard** (`src/abstractgateway/run_workspace_guard.py`):
  called from the workflow host's `start_run` for every run, whatever started
  it. It gives a run that named no folder its conversation's gateway-made
  folder, and adds the built-in deny rules (see
  [Workspace guard](#workspace-guard-every-run-start)).
- **Workspace browser** (`src/abstractgateway/workspace_browse.py`): lists
  and serves the files of a run's workspace folder to the person who started
  the run, confined to that folder, with the built-in deny list applied at
  every call. See [api.md](./api.md#a-runs-workspace-folder-browse-and-preview).
- **Live replies** (`src/abstractgateway/live_deltas.py`): the sink the
  gateway registers on every runtime it builds, the in-memory hub keyed by
  root run, and the file sink and tailer used when API and runner are separate
  processes (see [Live replies](#live-replies-token-deltas)).
- **Skills shelf** (`src/abstractgateway/skills_shelf.py`): resolves the
  folder skills are read from (`skills.shelf`, else the gateway's own copy in
  `<data dir>/skills/registry`, refreshed from AbstractSkill's curated shelf
  at each start). See [configuration.md](./configuration.md#skills-shelf).
- **Same-machine rule** (`src/abstractgateway/security/same_machine.py`):
  one answer to "is this caller sitting at the gateway computer?", used for
  installs, opening folders and the workspace routes' host facts. See
  [security.md](./security.md#callers-on-this-computer).
- **Network exposure** (`src/abstractgateway/network_exposure.py`): resolves the
  bind (`localhost`, `lan`, `internet`) at `serve` time, checks that the auth
  posture allows it, discovers the addresses, and serves the reverse-proxy
  settings the security middleware reads per request.
- **Engines and model downloads** (`src/abstractgateway/engines_install.py`,
  `src/abstractgateway/model_downloads.py`, `routes/engines.py`): engine
  installs are gateway jobs; model downloads, deletes and the catalog come from
  AbstractCore's host job registry and model catalog, which the gateway serves
  unchanged and extends with parent jobs, cancel and an event stream. See
  [engines.md](./engines.md) and [model-downloads.md](./model-downloads.md).
- **Apps manager** (`src/abstractgateway/apps_manager.py`, `apps_desktop.py`,
  `routes/apps.py`): installs Node.js when needed, installs the browser apps
  from npm, runs them as child processes of the gateway, detects apps started
  elsewhere, installs Code's terminal app, and opens apps signed in through
  one-time handover codes; it starts the desktop Assistant signed in through a
  private hand-over file (see [Desktop hand-over](#desktop-assistant-hand-over)).
  See [apps.md](./apps.md).
- **Host control** (`src/abstractgateway/host_control.py`,
  `self_update.py`): process-wide pause, graceful restart, and in-place
  update checks.
- **Desktop tray** (`src/abstractgateway/tray_supervisor.py`, `tray/`): a
  helper process started by `serve` on a desktop session. It talks to the
  gateway over loopback with a per-process token handed over on stdin. See
  [tray.md](./tray.md).
- **Login service** (`src/abstractgateway/os_service.py`, `autostart.py`):
  the per-user LaunchAgent, systemd user unit, XDG autostart entry or Windows
  Run entry that starts plain `abstractgateway serve` at login. See
  [first-run.md](./first-run.md#4-start-the-gateway-at-login-optional).
- **Summoned entities** (`src/abstractgateway/entities.py`,
  `routes/entities.py`): persistent entity homes and their lifecycle. See
  [entities.md](./entities.md).
- **Operator tooling** (`src/abstractgateway/maintenance/`): reports, triage,
  backlog browsing, the backlog exec runner and the process manager. See
  [maintenance.md](./maintenance.md).

## Durable contract (replay-first)

The gateway is **replay-first**:

- the **durable ledger** is the source of truth;
- SSE (`/ledger/stream`) is an optimization; clients reconnect by replaying
  from their last cursor.

```mermaid
sequenceDiagram
  participant C as Client
  participant G as Gateway API
  participant S as Durable stores
  participant R as Runner
  participant RT as AbstractRuntime

  C->>G: POST /api/gateway/runs/start
  G->>S: create run (RUNNING)
  G-->>C: run_id
  C->>G: POST /api/gateway/commands (pause, resume, cancel, emit_event)
  G->>S: append command to the inbox
  loop every poll
    R->>S: read new commands, apply them
    R->>RT: Runtime.tick(run)
    RT->>S: append StepRecords to the ledger
  end
  C->>G: GET /runs/{run_id}/ledger?after=N (replay)
  G-->>C: items + next_after
  C->>G: GET /runs/{run_id}/ledger/stream?after=N (SSE, optional)
```

Evidence: `src/abstractgateway/routes/gateway.py` (ledger endpoints, SSE,
commands) and `src/abstractgateway/runner.py` (command application, ticks).

## Live replies (token deltas)

A run started with `input_data._runtime.stream: true` (or, for an interactive
`POST /runs/start` that does not say, with the `agents.streaming_default`
setting on) also delivers the model's text while it is written. The text is a
live preview, never a ledger record: the durable `llm_call` record still
carries the answer, and it is written before the call's `llm.delta_end`.

```mermaid
sequenceDiagram
  participant C as Client
  participant G as Gateway API (ledger stream)
  participant H as Live-delta hub
  participant RT as AbstractRuntime (LLM call)
  participant S as Durable stores

  C->>G: POST /runs/start (input_data._runtime.stream: true)
  C->>G: GET /runs/{run_id}/ledger/stream?after=N
  G->>H: subscribe to the run and every run below it
  H-->>G: one snapshot per open call (snapshot: true)
  G-->>C: event: llm.delta (snapshot)
  loop while the model writes
    RT->>H: llm.delta {call_id, seq, text, channel}
    H-->>G: frames since the subscriber's cursor
    G-->>C: event: llm.delta (no id: line)
  end
  RT->>S: llm_call record with the final answer
  G-->>C: event: step (id: N+1)
  RT->>H: llm.delta_end {reason}
  G-->>C: event: llm.delta_end
  Note over H: when the run ends with a call still open,<br/>the hub closes it (synthetic: true)
  G-->>C: event: done
```

- The hub is keyed by the root run, so a subscription to a root sees the
  replies of its sub-runs, and a subscription to a child sees that child's
  subtree. Another user's run answers 404.
- Each subscriber holds at most one pending entry per call (a cursor into
  that call's text), so a slow client catches up instead of overflowing a
  queue; nothing is capped.
- A run that ends while a call is open (stopped, failed, killed by the stop
  kill switch, or failed by the runner) gets a synthetic `llm.delta_end` for
  that call, and its state is freed.

With API and runner in separate processes, the runner's model calls write
their events to a private file and the API process reads them from there:

```mermaid
flowchart LR
  subgraph RunnerP["abstractgateway runner"]
    Call["LLM call in AbstractRuntime"] --> Sink["file sink"]
  end
  Sink --> File[("data dir/live/ROOT_RUN_ID.deltas.jsonl<br/>(0600, deleted when the root run ends)")]
  subgraph ApiP["abstractgateway serve --no-runner"]
    Tail["file tailer"] --> Hub2["Live-delta hub"] --> Stream["GET /runs/{run_id}/ledger/stream"]
  end
  File --> Tail
```

The frames, fields and end reasons are in
[api.md](./api.md#4b-live-replies-token-deltas-on-the-same-stream).

## Workspace guard (every run start)

Every run the gateway starts works in a folder, and its file tools are kept
out of the gateway's data folder and the account's credential folders:

```mermaid
flowchart TB
  Http["Client doors: POST /runs/start, POST /runs/schedule, entity summons"] --> Policy["Workspace policy check: workspace_root inside the allowed roots and not inside the data folder (except the caller's own conversation folder), else 400"]
  Policy --> Start["Workflow host start_run"]
  Internal["Gateway-made starts: Telegram, email and agora bridges, sandbox routes, schedule children"] --> Start
  Start --> Ensure["No folder named: the conversation's gateway-made folder (or a per-run folder)"]
  Ensure --> Deny["Built-in deny rules: workspace_builtin_deny_prefixes (data folder + credential folders), workspace_builtin_allow (the run's own folder); client-sent values dropped"]
  Deny --> Tools["AbstractRuntime file tools enforce the rules; nothing is written into the model's prompt"]
```

The rules are whole-folder prefixes, never a listing of a folder's contents,
so the model's prompt stays the same from turn to turn. An admin can turn the
run-side rules off (`workspace_builtin_deny`); the workspace browser keeps
hiding those folders. Shell commands a run may execute are not confined by
these rules. Details: [configuration.md](./configuration.md#workspace-policy-filesystem-scope).

## Desktop Assistant hand-over

Opening the Assistant from the console or the tray starts it on the gateway
computer already signed in as the person who clicked, without a code or token
on its command line or in its environment:

```mermaid
sequenceDiagram
  participant U as Admin (console Open, tray Launch Assistant)
  participant G as Gateway
  participant F as Hand-over file (0600)
  participant A as Desktop Assistant

  U->>G: POST /api/gateway/apps/assistant/launch
  G->>F: write {schema, code, base_url, expires_at, user_id}<br/>in the data dir handover folder
  G->>A: start with --gateway-url URL --gateway-handover-file FILE
  A->>F: read, then delete
  A->>G: POST /api/gateway/apps/desktop-handover {code}<br/>(direct loopback call)
  G-->>A: {base_url, session_id, csrf_token, user_id, expires_at}
  Note over G,A: single use, two minutes. A relayed call gets 403,<br/>a used or expired code 410
```

An Assistant that is already running is brought to the front and receives no
code. See [apps.md](./apps.md).

## Thin-client control plane

Higher-level apps use the gateway instead of importing Runtime or Core:

- `GET /api/gateway/discovery/capabilities` exposes a versioned shared
  contract: run input/history access, media endpoints, voice contracts,
  prompt-cache surfaces, host state, session caches, model residency, and
  `common.readiness` (a compact summary of Gateway-owned surface readiness).
- Provider, model and voice catalogs are routed through the gateway and carry
  a `gateway_catalog_v1` envelope (`catalog` plus canonical `items`) next to
  the lower-layer fields.
- Direct run-scoped media routes cover TTS, STT, image generation, edit and
  upscale, text-to-video, image-to-video, and music.
- Voice listen is a host-capture contract: clients capture audio and then
  upload it or emit an event.
- Model residency is Runtime/provider-owned and Gateway-normalized:
  `GET /models/loaded` and `GET /host/state` relay Runtime's host records and
  add `model_residency_row_v1` rows. The gateway never fabricates residency,
  memory or GPU facts; unavailable sections degrade in-band.
- Reads (`/models/loaded`, `/host/state`, `/host/metrics/*`,
  `GET /sessions/prompt_cache`) serve any authenticated principal; mutations
  (model load/unload/lock/download, prompt-cache clearing, installs) require an
  admin.

## Deployment shapes

```mermaid
flowchart TB
  subgraph Desktop["Your own computer"]
    LS["Login service (optional)"] --> Serve1["abstractgateway serve<br/>API + runner + tray"]
    Serve1 --> AppsLocal["Browser apps on 127.0.0.1"]
  end
  subgraph Server["Server or container"]
    API["abstractgateway serve --no-runner"] --- DD[("shared data dir")]
    Worker["abstractgateway runner"] --- DD
    Proxy["TLS reverse proxy or tunnel"] --> API
  end
```

- **Single process**: `abstractgateway serve` starts the HTTP API and the
  runner. On a desktop session it also starts the tray helper, and it starts
  the browser apps you enabled.
- **Split API and runner**: `abstractgateway runner` (worker) and
  `abstractgateway serve --no-runner` (API) share one data dir, so you can
  restart the API without pausing durable execution. Live replies pass through
  `<data dir>/live/` in this shape.
- **Container**: the GHCR image runs `serve` with user auth; see
  [deployment.md](./deployment.md).

Evidence: `src/abstractgateway/cli.py` (flags), `src/abstractgateway/runner.py`
(lock file).

## Security model (summary)

`GatewaySecurityMiddleware` applies to paths starting with `/api/gateway`:

- **Authentication**: Gateway user accounts (browser sessions or user bearer
  tokens), or a shared server/operator token.
- **Origin allowlist**: the loopback origins, the gateway's own LAN origins in
  a network mode, and the `allowed_origins` setting.
- **Abuse resistance**: body size caps, concurrency caps, auth lockouts, audit
  log.
- **Callers on this computer**: one rule decides who counts as sitting at the
  gateway computer (installs, opening folders); a browser that reaches the
  gateway through an app server counts only when the browser itself is on this
  computer.
- **Workspace guard**: every run works in a folder, and its file tools never
  reach the gateway's data folder or the account's credential folders.

`session_id`, `run_id`, `artifact_id` and memory owner ids are references,
not authorization proofs. With user auth on, each principal runs on its own
data plane; browser apps exchange a user token for an HTTP-only session cookie
plus CSRF token. See [security.md](./security.md).

## Evidence (jump-to-code)

- Composition root: `src/abstractgateway/service.py`
- API surface: `src/abstractgateway/routes/` (`gateway.py`, `apps.py`,
  `engines.py`, `network.py`, `entities.py`)
- Runner: `src/abstractgateway/runner.py`
- Stores: `src/abstractgateway/stores.py`
- Security: `src/abstractgateway/security/`
- Settings: `src/abstractgateway/runtime_config.py`
- Default agent workflows: `src/abstractgateway/agent_defaults.py`
- Live replies: `src/abstractgateway/live_deltas.py`
- Workspace guard and browser: `src/abstractgateway/run_workspace_guard.py`,
  `src/abstractgateway/workspace_browse.py`
- Same-machine rule: `src/abstractgateway/security/same_machine.py`
- Desktop hand-over: `src/abstractgateway/apps_desktop.py`,
  `src/abstractgateway/apps_manager.py`, `src/abstractgateway/routes/apps.py`
- CLI: `src/abstractgateway/cli.py`

## Related docs

- [getting-started.md](./getting-started.md): run the gateway and choose stores
- [configuration.md](./configuration.md): every setting and environment variable
- [api.md](./api.md): the client contract
- [security.md](./security.md): auth, origins, network exposure
- [deployment.md](./deployment.md): containers and Compose
- [faq.md](./faq.md) and [troubleshooting.md](./troubleshooting.md)
