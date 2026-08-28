# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **The shared workflow registry is administered by the operator (2026-08-21).**
  Installing, replacing, removing, deprecating and reloading workflows in the
  gateway's own bundle directory now require an admin principal. The rule is
  ownership rather than role: when hosted user auth is enabled each principal
  writes its own registry and is unaffected; only the directory every user
  shares is restricted. Reads are unchanged, so any user can still list and run
  the shared workflows. One check now covers every route that writes the
  registry — `POST /bundles/upload`, `DELETE /bundles/{bundle_id}`,
  `POST /bundles/reload` and `POST /visualflows/{flow_id}/publish` — so a
  workflow cannot be replaced through one door while another is guarded.
  Non-admin callers receive `403` with the admin route to take instead.
- **`POST /models/download` and `POST /config/capability-defaults/apply-recommended`
  now require an admin principal (2026-08-21).** Both act on host-wide
  resources: the first spends disk on the shared machine, the second rewrites
  the capability defaults every user inherits. The download progress poll
  `GET /models/download/{job}` remains user-level.

### Added
- **A Workflows tab in both consoles (2026-08-21).** The web console gains a
  `Workflows` section and the console-TUI a `Workflows` screen (step 6), listing
  every workflow registered on the gateway. A row is a bundle, and the version
  cell carries two numbers — published and draft — so a registry that is mostly
  drafts does not read as a short list. Selecting a row shows that workflow's
  versions and its entrypoints with their declared interfaces. Versions the
  gateway is not serving appear in a separate `Not loaded` block with the reason
  and the file path, instead of being absent.

  The tab covers the registry's lifecycle: **import** installs one or more
  `.flow` bundles and reports, per file, whether the gateway is actually serving
  the result; **export** downloads a version's original bytes; **delete** removes
  a single version or a whole workflow behind a confirmation that states what is
  irreversible and what it can determine about runs referencing it. In the TUI,
  `e` exports to a local file, `d` deletes the selected version, `D` deletes the
  workflow, and `t` toggles draft visibility. Listing and exporting are available
  to any user; the write actions follow the registry ownership rule.
- **`GET /api/gateway/bundles/{bundle_id}/download` (2026-08-21).** Returns one
  bundle version as its original `.flow` bytes with
  `Content-Disposition`, `Content-Length` and an `X-AbstractGateway-Bundle-Sha256`
  checksum, so an exported workflow can be verified and re-installed unchanged.
- **Every console search box now reads `*.jpg` (2026-08-20).** The runtime
  tabs' filters were plain substring matches, so a filetype pattern matched
  nothing at all — the `*` was compared literally and no row can carry one.
  A query with no `*` and no `?` is still a case-insensitive substring
  (`06-13` still finds a June 13th row); a query carrying either wildcard is
  now a case-insensitive glob, anchored to the whole value and retried
  against its basename, so `*.jpg` finds `photo.jpg` and
  `/var/lib/gw/runs/run-1/photo.jpg` alike and `bundle-x@*:root` finds a
  workflow. `*` crosses `/` deliberately: these boxes filter a flat list of
  rows, they do not walk a tree. `[` stays literal. The rule is the same on
  all four runtime tabs and in both consoles — `/artifacts/search?query=` and
  `/runs?query=` on the server, the Cache and Logs filters in the web console
  and the console-TUI, which carry transcriptions of the same matcher
  (`makeNeedle` in `console.py`, `console-tui/src/query.rs`); their agreement
  is a test, not a convention.

### Fixed
- **A workflow version that cannot be served is now reported, not dropped
  silently (2026-08-21).** A bundle version that misses its declared
  `metadata.min_runtime` floor or fails to compile is still excluded from the
  runnable set, and the file is left untouched on disk — but the reason is now
  kept and surfaced. `GET /bundles` returns a `skipped` array carrying each
  affected `bundle_id`, `bundle_version`, on-disk `path` and the reason;
  `POST /bundles/reload` reports the same in its result. Version retention is
  the point: a version other runs still reference must remain accountable, so
  it can be repaired or deliberately removed rather than quietly disappearing
  at the next reload.
- **Installing a workflow reports whether it can actually run (2026-08-21).**
  `POST /bundles/upload` and `POST /visualflows/{flow_id}/publish` return a
  `loaded` field alongside `ok`: `true` when the gateway is serving the exact
  version that was installed, `false` with a `skipped` reason when it is not,
  and `null` when the request did not reload the gateway and loadability is
  therefore unverified. `ok` is `false` only when the version is proven not to
  be served. This also covers the case where another file in the bundle
  directory claims the same bundle id and shadows the new version.
- **The default framework agent cannot be deleted out from under the gateway
  (2026-08-21).** `DELETE /bundles/{bundle_id}` returns `409` when the target
  is the `basic-agent.flow` the gateway verifies at startup, because removing
  it prevents the next start and bundle removal has no undo. Replacing the
  default agent is still supported: install the replacement first, then remove
  the old file.
- **The container now serves the shipped workflows, and builds from source
  again (2026-08-02).** The compose profile bound a host directory over the
  registry unconditionally, so a deployment that did not run from a repo
  checkout came up with no workflows at all. `ABSTRACTGATEWAY_FLOWS_DIR` is now
  unset by default and the image serves what it ships; to serve your own
  bundles, point `ABSTRACTGATEWAY_HOST_FLOWS_DIR` at them and set
  `ABSTRACTGATEWAY_FLOWS_DIR=/data/flows`
  ([docs/deployment.md](docs/deployment.md)). Separately, a source-mode image
  build (`ABSTRACTGATEWAY_INSTALL_MODE=local`) failed outright on both the base
  and NVIDIA images, which copied a retired `dp-research@0.1.0` bundle and
  omitted `llms.txt`; each now copies exactly the packaged set.

### Changed
- **The fresh-install capability seed belongs to the INSTALL, not to every
  scope (2026-08-01).** AbstractCore now seeds its recommended routes (text
  `lmstudio/qwen/qwen3.5-9b`, voice `supertonic/supertonic-3`, image
  `mlx-gen/AbstractFramework/flux.2-klein-4b-8bit`) into a config file that has
  never existed, so a new install works out of the box. The Gateway reads
  per-principal and per-runtime stores as OVERLAYS, where an absent file has
  always meant "this scope overrides nothing" — left alone, the seed would fire
  on every one of them and each newly created user would silently shadow the
  operator's gateway-wide default with the framework recommendation, with no
  way for an admin to set a default that new users inherit.
  `core_config._load_configured_routes_from_core_config` now answers "no
  routes" for an absent scope file. The install-level read still gets the seed,
  so a fresh Gateway serves the recommendation and its users inherit it
  normally (`test_a_fresh_gateway_serves_the_recommended_seed_and_users_inherit_it`).
- **`GET /api/gateway/config/capability-defaults` carries the seed provenance.**
  The payload now has `seeded: "recommended-v1"` when its rows came from the
  fresh-install seed, read through the new
  `config_facade.capability_defaults_seed_marker` (the Gateway still never
  imports AbstractCore directly). Informational only — the seeded rows are
  ordinary rows, overridable and clearable from either entry point and always
  beaten by a request pin — so a surface can label them "recommended" instead
  of implying the operator chose them. A corrupt store is never seeded and
  never marked; it keeps serving its loud `errors` line.
- **The summoned-entity context gate is a 40k RECOMMENDATION, not a wall
  (operator re-ruling 2026-08-01, superseding maintainer round 8).** Operator
  verbatim intent: "it should be 40k and ... more a soft than a hard limit.
  the idea is that the entity is gonna be more efficient if it tries to keep
  the context around 40k ... but if it needs to grow, it needs to grow." Three
  hard gates became soft, labeled warnings:
  - the summon door's 409 refusal for declared windows below the floor is now
    a `#RECOMMENDED` warning riding the summon response (the summon proceeds);
    the undeclared-window `#FALLBACK` warning names the recommendation instead
    of a guaranteed floor (`routes/entities.py`);
  - `StartLoopRequest.context_window` dropped its `ge=20000` hard 422
    (`ge=1` now); a sub-recommendation loop start succeeds and carries a
    `#RECOMMENDED` warning in the response;
  - the engine side (`abstractmemory.ENTITY_CONTEXT_FLOOR`, one constant both
    sides import) is now `40_000` with the honest alias
    `ENTITY_CONTEXT_RECOMMENDED`, and `entity_recall_budget` no longer raises
    below it (the 2400 token-budget starvation guard is the only floor left).
    `entity_gate.py` re-exports `SUMMON_CONTEXT_RECOMMENDED_TOKENS` beside the
    legacy `SUMMON_CONTEXT_FLOOR_TOKENS` name.
  Growth above 40k was and remains unblocked everywhere (no upper cap exists
  on the context lanes; defaults stay wide at 65536). KNOWN REMAINDER outside
  this repo: `abstractruntime/identity/life.py` `main()` still hard-refuses
  `--context-window` below the imported constant (now 40k) at the CLI start
  door — the loop lane's spawned process dies with exit 2 for explicit
  sub-40k windows until that gate is softened in runtime's lane.

### Added
- **A fresh install now serves a coder, deep research, and co-scientist (2026-08-02).**
  The packaged bundle registry adds `coding-agent@0.2.6` (its `coder`
  entrypoint declares `abstractcode.agent.v1`, so chat clients pick it up
  directly) and `co-scientist@0.2.0`, alongside the `deep-research@0.1.7`,
  `basic-agent`, `docs-qa`, and native-loop bundles it already carried. You get
  these without installing any bundle by hand; setting
  `ABSTRACTGATEWAY_FLOWS_DIR` still replaces the registry with your own. The
  same set rides the wheel, the sdist, and the container image. See
  [docs/shipped-workflows.md](docs/shipped-workflows.md).
- **The reasoning effort for text generation is editable from the Gateway (2026-08-01).**
  `PUT /api/gateway/config/capability-defaults/output/text` accepts a `reasoning`
  field, and the console's capability-defaults panel offers it on the text route.
  It is stored on AbstractCore's text route, so both entry points read and write
  the same value.
- **One seam for AbstractCore-owned configuration (2026-08-01).**
  `abstractgateway/core_config.py` is the only Gateway module that reads or writes
  AbstractCore's configuration — capability routes, the reasoning effort, route
  options, Core-held provider API keys, the host's mail connection, and the
  maintenance-triage LLM settings — with read-through freshness, write-through
  live refresh, and the split-server posture in one place. A test fails any other
  Gateway module that reaches AbstractCore config off the seam, whether through
  the config facade, the integration package's flat re-exports, a dynamic import,
  a re-export shim, or a shell-out to `abstractcore config`.
  **Migration:** `abstractgateway/capability_defaults.py` is removed; import
  `gateway_capability_defaults_payload`, `save_gateway_capability_default`,
  `clear_gateway_capability_default`, `capability_defaults_config_signature` and
  the `core_server_*` helpers from `abstractgateway.core_config`.

### Fixed
- **A Gateway write no longer discards fields it did not name (2026-08-01).**
  AbstractCore stores a capability route as one row, so a save of provider and
  model replaced the whole row and cleared a reasoning effort or route options
  set through `abstractcore config set-default`. A `PUT` is now a partial update:
  omitted fields keep their stored value and `""` clears a field, so the two entry
  points cannot overwrite each other. This holds in split deployments too
  (`ABSTRACTCORE_SERVER_BASE_URL`): the write resolves the merge against the row
  the AbstractCore server serves and sends the whole resolved row.
- **The console save sends only the fields its modal controls (2026-08-01).**
  It echoed back the `base_url` and `options` of the row the grid last rendered,
  so a save that meant to change the model could roll back a change made through
  `abstractcore config` between render and save. It now sends provider and model,
  the reasoning select on text routes, and the options dict only on the voice
  routes whose picker edits it; every other field is left unset and preserved.
- **The Gateway reads the mail connection and triage settings AbstractCore stores
  (2026-08-01).** The email bridge resolved IMAP host, port, username, folder and
  password-variable name from `ABSTRACT_EMAIL_*` alone, and the maintenance triage
  assistant resolved its six provider settings from `ABSTRACT_TRIAGE_LLM_*` alone,
  while AbstractCore holds both in its `email` and `maintenance` config sections
  and its own tools resolve from them. Both now read that store, with the same
  environment variables kept as the override rung above it, so a connection
  configured once through AbstractCore does not have to be stated again.
- **An entity's substrate refusal names where the choice lives (2026-08-01).**
  The 400 returned when no mind substrate is chosen now names `substrate.yaml`,
  the `ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER` / `_MODEL` host-wide fallback, and
  states that AbstractCore's `output.text` default is deliberately not consulted
  for it — so an operator who set that default learns why it does not apply.

- **A console default is USED — immediately, without a restart (2026-08-01).**
  The operator's ruling is that the gateway default provider/model they set in
  the console is what runs, unless an app overrides it. It was not. The default
  was resolved ONCE at bundle load (`hosts/bundle_host.py`, via
  `resolve_gateway_provider_model`) and baked into the constructed runtime, so
  `PUT /api/gateway/config/capability-defaults/{kind}/{modality}` persisted the
  change to `abstractcore.json` and nothing else. Live-reproduced: after a
  console-path PUT of the text default, the very next unpinned run still routed
  to the previous provider/model. New `WorkflowBundleGatewayHost.
  refresh_capability_defaults()` re-runs the SAME cascade as load and re-points
  the live runtime in place — both the pooled LLM client (what `llm_call` nodes
  use) and `RuntimeConfig` (what `start()` seeds into `_runtime.provider|model`,
  which every Auto Agent node reads); refreshing only one had the two node kinds
  on different defaults. The capability-defaults PUT/DELETE routes call it and
  report the outcome as `runtime_refresh` in their response. No bundle recompile,
  no disturbance to in-flight runs, and a refresh failure never fails a save that
  already landed on disk.
- **The text-generation default is read BY NAME (2026-08-01).**
  `provider_defaults._gateway_capability_text_default` found the default by
  iterating `for kind in ("output", "input")` over the route rows and taking the
  first text row — correct only by iteration order, and it named no key, so
  neither the code nor the config told you what the default WAS. It now reads
  the canonical route key `output.text` (the catalog's `text_generation` route),
  falling back to the legacy storage key `input.text` so an unmigrated config
  keeps working, and the resolution `source` now NAMES the key that answered
  (`abstractcore.capability_defaults:output.text`). Both keys exist because
  AbstractCore deliberately stores ONE text provider/model under `input.text`
  and derives `output.text` as a read-only view of it.

- **VisualFlow CRUD accepts the flow function library** (flow editor
  adversary P0-1, 2026-07-27): `POST/PUT /visualflows` request models were
  `extra="forbid"` without a `functions` field, so any editor save carrying
  flow-level functions got 422 — and since the client omitted them, saves
  silently STRIPPED the whole library while toasting success. Both models
  now carry `functions` ({name, code, kind?, description?} entries),
  `_coerce_visualflow` validates the shape loudly, and the create/update
  handlers persist it. Live-verified round-trip (POST with functions → GET
  returns them → PUT can clear them).

### Documented
- **The provider/model cascade contract, in one place**
  (`src/abstractgateway/provider_defaults.py` module docstring): explicit
  request pins > flow defaults > gateway console default > flow-scanned
  bootstrap. A default never clobbers an app override, and applies whenever
  nothing overrides. The apparent inversion (an authored flow default outranks
  the console default, but a flow-SCANNED pair does not) is spelled out: what an
  author wrote travels with the call; what a scanner guessed about flows must
  never outrank the operator's own setting.

### Added
- **Session history bloc endpoint** (bloc-streaming unit 4, c5551): `GET
  /sessions/{session_id}/history/bloc` returns one cursor-bounded bloc of root
  session turns with inline `history_bundle` exports (`detail=replay` default,
  ISO `before` cursor, `older_remaining` count). Capabilities advertise
  `runs.session_history_bloc`; docs and integration tests added.
- **Run history bundle capabilities** (session-replay-honesty unit 3):
  `capabilities.contracts.common.runs.history_bundle` now advertises
  `detail_modes: ["full", "replay"]` and `warnings_in_band: true` so thin
  clients can feature-detect replay profiles and in-band honesty without
  reading `docs/api.md`. Contract test updated.
- **Run history bundle contract** (session-replay-honesty unit 2): `docs/api.md`
  documents `GET /runs/{run_id}/history_bundle` query params (`detail=full|replay`,
  ledger tail knobs), the in-band `warnings[]` vocabulary, and gzip guidance for
  thin clients.
- **Run history bundle warnings pin** (session-replay-honesty unit 1): integration
  test asserts `warnings` is always a list on full and replay bundles.
- **Native-loop bundle input_schema stub** (native-loop-packs unit 3): manifest-only
  native-loop entrypoints (`react|codeact|memact`) now serve
  `GET /bundles/{bundle_id}/flows/{flow_id}/input_schema` with a versioned
  stub (`prompt` required; `provider`/`model` optional; `native_loop_factory`
  in the response) instead of 404. VisualFlow bundles unchanged.
  Tests: `tests/test_gateway_native_loop_bundle_loader.py`.
- **Native-loop bundle loader** (loop-surfacing unit 4): bundles may declare
  `metadata.native_loop_factory` (`react|codeact|memact`) with empty
  `manifest.flows`; the gateway materializes an abstractagent
  `WorkflowSpec` at load time (react wired today; codeact/memact skip until
  agent pack lands). Invalid native declarations skip per-bundle without
  bricking boot; empty flows without a native declaration still refuse loudly.
  Tests: `tests/test_gateway_native_loop_bundle_loader.py`.
- **Shipped `react-agent@0.1.0` native-loop bundle** (loop-surfacing unit 5):
  manifest-only bundle with `native_loop_factory: react` and
  `abstractcode.agent.v1` entrypoint; built via
  `scripts/build_react_agent_bundle.py`, force-included in wheel/sdist.
  Running gateway must restart (or reload bundles) to pick up the new file
  on `:8080`.
- **In-process local providers surface in the unified provider list**
  (operator report 2026-07-26: "we are missing providers… where is our
  ollama?"): the reachable-default auto-probe lane now covers IN-PROCESS
  backends (`mlx`, `huggingface`) alongside the server-shaped locals
  (lmstudio/ollama). In-process specs carry `in_process=True` — no
  server, no base_url, no key; "reachable" means the discovery facade
  lists local artifacts (MLX cache / local HF cache), and an empty
  listing keeps them absent. They surface ONLY through the probe lane
  (the configured-rows fold skips them — with no key/URL requirement to
  fail they would otherwise emit always-present rows for backends that
  may not be usable). Both consoles (web + TUI) render the new rows with
  zero client changes — they already render this payload. Note: the
  ollama half of the report was environmental — the ollama server was
  simply not running; once started, the existing probe surfaced it.
- **Structural graph editing through the blueprint overlay** (operator build,
  laurent dm#276 via c4837; schema confirmed both ends c4865/c4870): `PUT
  /api/gateway/entities/spec/phases` now accepts `graph: {"edge_ops": [...]}`
  beside `tunables` — add/remove/redirect ops over the entity state graph,
  validated against the vendored artifact's OWN `graph_overlay_contract`
  (per-edge `edit_policy`, `cause_evaluators` status registry, refusal-code
  vocabulary — rules read from the artifact, never a second copy; validator
  in `phase_edge_ops.py`). Document-ownership semantics: `graph` present =
  the list replaces the stored ops wholesale (empty list clears); absent =
  stored ops ride along unchanged but ALWAYS re-validate against the current
  artifact, so a dials-only edit after a re-vendor cannot silently carry
  now-illegal ops (409 naming the drift). Refusals return the artifact's
  code + message (`code` body sibling + `X-Gateway-Error-Code`). The derived
  effective file now carries merged `transitions`, the `graph` block
  (runtime's idempotent re-derivation input), and top-level
  `structural_sha256` — runtime's H2 handshake: a consumer whose vendored
  sha differs falls back to dials-only. One derivation point
  (`_derive_effective_doc`) shared by PUT, GET, and boot; crash-window +
  vendor-drift reconcile heals the effective file at boot and on GET (stored
  ops that no longer validate degrade the file to dials-only LOUDLY — a
  detached loop never reads an incoherent graph). `blueprint_edited` markers
  carry `graph_ops` counts. A fable5 adversary returned DO-NOT-SHIP on the
  first cut; four defects fixed + pinned (30 green): (P0) an `add`
  duplicating a locked edge's exact from/to/cause overwrote it in the merge
  (guards dropped, policy flipped, instruction smuggled) — add now refuses
  any existing edge_id, checked AFTER the shape rules so target_visit/
  kill_switch still win; (P0) a redirect onto a locked sibling's identity
  collapsed onto the locked fallback (layering a guard onto the ungated
  crash-recovery path) — redirect refuses locked-identity collisions,
  editable-sibling collisions stay the pinned dedup; (P1) NaN/Infinity
  `bound_h` slipped the sub-tick check (`nan/inf < 0.01` are both False)
  into the durable file — `math.isfinite` gate added first; and a
  remove+re-add policy-downgrade in one batch is refused. `compute_effective_
  transitions` gained a defense-in-depth belt (never drop/overwrite a locked
  edge even if handed unvalidated ops) and now carries the post-redirect
  `edge_id` on redirected rows (`**base` copied the original — internally
  keyed right, but consumers read the field).

### Added
- **The cognition map, served** (operator correction c5070: the editable
  graph is the MEMORY-COGNITION state graph — passive/active memory
  construction and what it creates: lessons, world models, the gradual
  opinion system, safe identity updates — not the simple phase graph).
  `GET /api/gateway/entities/spec/cognition-graph` serves a vendored byte
  copy of entity's `spec/cognition_graph.json` with sha256 + node/edge
  counts, closing the "bundled-only, unserved" one-truth gap. SERVE-ONLY
  today with an honest `edit_status`: the overlay/proposal door reuses the
  phase-lane machinery but is gated on entity widening the artifact with a
  `graph_overlay_contract` and the operator's question-1 ruling
  (engine-consult-first for structural edits); until then cognition
  structure is routed proposals per the three-tier law. Drift pin holds the
  vendored bytes to entity's pen (the phase-graph pin's twin).

### Fixed
- **Guard-merge law (artifact v21) implemented in the effective-graph
  merge**: entity ruled the gateway's c4934 guardmerge question into the
  artifact — an editable-onto-editable redirect collision now carries the
  UNION of both edges' guards in `compute_effective_transitions` (guards
  are conjunctive preconditions; a collision merges laws and never silently
  erases either side — pre-v21 the redirected source's guards overwrote the
  target's, the reported P2). The deliberate supersede path stands:
  removing the target edge in the same batch means no union (its guards die
  by a recorded act). v21 re-vendored; pinned both directions.
- **Command lane: resume/cancel progress through a starved tick pool**
  (backlog 0152, framework c4988 wedge; gateway claim c4998). The runner's
  fixed worker pool can be fully starved by hung ticks — a tool subprocess
  that outlives its timeout (an un-reaped headless Chrome from an
  `execute_command` heredoc) or a no-progress LLM call pins a worker for its
  whole duration, and a Python thread is not killable from outside. When
  every general worker was pinned, an operator's resume was queued behind
  them and never ran ("accepted, not ticked"); and above `run_scan_limit`
  RUNNING runs, a resumed run outside the window was never submitted at all.
  Fix: a RESERVED `command_tick_workers` executor for command-triggered runs
  (resume/cancel/inject_guidance/update_schedule), and those runs bypass the
  scan window via a priority drain in `_schedule_ticks` (load-verified +
  gateway-owned-gated so a stale/session id is a no-op, never a false
  FAILED). The shared in-flight set is the single-tick guard across both
  lanes, so no run is ever double-ticked. `runner_status` now reports
  `command_lane_available` when the general pool is fully wedged (the escape
  hatch an operator has short of a bounce) and `command_ticks_pending`. This
  is the gateway MITIGATION; the starvation ROOT — `execute_command` reaping
  its child process TREE on timeout and a no-progress timeout on the LLM
  effect — is runtime/core's, routed at c4998. 4 new pins incl. a
  real-executor proof that a resumed run ticks through a fully-starved
  general pool.
- **Gateway import boundary restored in `tool_catalog.py`** (backlog-0059
  pin): the full-catalog build imported `abstractcore` directly in 8 places
  (comms callables, builtin inventory, capability facts, plugin errors,
  `derive_risk`). All routed through AbstractRuntime facades now: registry
  rows + facts join via `core_registry_tool_rows` (comms rows there are
  spec-grade — facts, description, parameters, risk trio — so the disabled
  lane and the partial-comms remainder build rows with zero callable
  imports), factless clamp via `derive_risk_assessment({})`. Camera
  capability facts + capability plugin errors have NO runtime facade yet:
  getattr-probed with labeled degradation until runtime ships them (asked
  c4899); camera discovery rows honestly derive unvetted in the interim.
  The comms kind→names map is drift-pinned by a test asserting equality
  against runtime's composed toolset. Also: `PUT /tool-grants/default`
  gained its route-authorization row (admin — widening the default grant
  widens what agents auto-run gateway-wide), and three capability-catalog
  tests were isolated from the HOST's real abstractcore config
  (config-first voice-engine resolution leaked the operator's configured
  engine into exact provider-list assertions — the ambient-escape class).
- **Eager run rehydration + self-healing runner start** (backlog 0063,
  theme-2 "entity lives survive anything"): in multi-user mode per-principal
  runners started only on that user's first request, so after a
  crash/redeploy every idle tenant's parked and scheduled runs stayed paused
  until the user hit an endpoint (WAIT_UNTIL/event deadlines could miss their
  windows indefinitely). Boot now warms each registered runtime's runner so
  parked work resumes immediately, and a cached service whose runner lost the
  singleton-lock race (returned dead) is re-started on the next access
  instead of being served permanently stalled. Fable5 adversary
  (SHIP-WITH-FIXES) folded same wave: the sweep runs on its OWN daemon thread
  — NOT inline in the boot gate (P0-1: inline, the N heavy per-principal
  builds held `/api/health` on `_boot_state="starting"` for the whole sweep,
  parking every request and re-opening the false-recycle window the
  background-boot fix just closed); `stop_gateway_runner` sets a shutdown
  event checked per iteration and snapshots+clears the service caches
  atomically under the lock (P1-1: an unsignalled sweep kept building after
  shutdown, orphaning runners that flock-refuse the next boot's own twins);
  the sweep skips `role=entity` principals and any principal whose runtime
  dir does not already exist (P1-2: warming a never-run user/entity mkdir'd a
  phantom runtime tree + standing threads scaling with registrations, not
  parked work); filters enabled+non-entity BEFORE the cap (P2-1); returns
  early in a no-runner split-mode process; deliberately-stopped runners are
  never restarted (`stopped_deliberately` now on `runner_status`); the
  outcome is surfaced on `/api/health` as `rehydration` (P1-3); and the
  health snapshot uses a bounded `_service_lock` acquire that reports
  `building: true` on contention instead of blocking a liveness probe behind
  a build. 18 pins.

### Fixed
- **Camera-only (deterministic) flows register the TOOL_INVOKE handler**
  (flow c4316, adversary-verified): a flow whose only tool work is
  deterministic tool-invoke nodes (the operator's dm#49 camera shape:
  `wait_event -> camera_open -> camera_capture_photo ->
  camera_analyze_media`, no llm/agent node) fell to the bare runtime with no
  `TOOL_INVOKE` handler and failed at execution ("No effect handler
  registered for tool_invoke"). `_flow_uses_tools` matched only
  `{tool_calls, agent}`, so `needs_tools` was False and neither the tool
  executor nor the handler was wired; an llm+camera flow masked it because
  the LLM branch's `build_effect_handlers` registers both. Now
  `_flow_uses_tools` also detects TOOL_INVOKE-emitting node types (camera_*,
  call_tool, tool_invoke) and the tools-only runtime branch registers
  `EffectType.TOOL_INVOKE` alongside `TOOL_CALLS`. 5 pins.
- **Entity roster latency — seq-cache the per-row drives fold** (code-tui
  c4307 P1, live-reproduced: `GET /api/gateway/entities` took 5.6s then
  three consecutive >20s timeouts while per-entity `/state`/`/visit`/
  `/cognition` stayed <30ms): the roster ran `cognition_drives()` — the same
  engine fold that measures ~90s on a large home — PER WARM ROW on EVERY
  list call, uncached, stacking N homes' reads into one request on the
  shared sync threadpool. Now seq-keyed cached (`_ROSTER_DRIVES_CACHE`,
  entity-dir + journal-seq, matching the `/cognition` and communities
  caches): a pure-read fold is served once per home per journal advance and
  shared across every thin client. Render-when-present and warm-homes-only
  contracts unchanged. Pinned.

### Added
- **Env-kill phase 0 — the declared environment-variable registry**
  (`env_registry.py`; the shared-contract keystone, c4174/c4280): every env
  var the gateway reads (~300 names) is declared and classified by the
  three-test rule (behavior/deployment/secret) plus the migration classes
  (foreign = owner-facade reads, legacy_alias = warn-when-winning), each row
  carrying owner, scope, effectiveness (the deferred-flip contract), the
  planned `console_path`, and operator-ruling citations (dm#201 keys pinned
  SECRET, TOOL_MODE=behavior, USER_AUTH=deployment-circular, voice/vision
  namespaces=FOREIGN). CI-enforced by `test_gateway_env_registry.py`: a
  grep over every env READ SITE in `src/` fails on any undeclared name —
  the inventory is an invariant, not a one-time audit (it caught 3
  undeclared names on its first run). Unblocks the boot env scanner, the
  TOOL_MODE/base_url/voice-alias migrations, and the dm#194 console+CLI
  parity gate.

### Fixed
- **Voice/STT bare requests resolve the gateway default, never hardcoded
  openai** (laurent dm#28 "you MUST always use the gateway defaults";
  fable5-hardened): the console sends TTS requests bare (no provider),
  trusting the gateway to fill its default — but the TTS **stream** lane
  never merged the `output.voice` capability default, so a provider-less
  request fell through to abstractvoice's hardcoded openai engine → the
  operator's openai quota → 429. New `_configured_voice_output_defaults`
  reads the console-set `output.voice`/`input.voice` route and fills the
  provider+model+voice triple; wired on BOTH TTS lanes (artifact + stream)
  AND the STT transcribe lane (adversary P1: STT had the identical hole,
  and the assistant's default dictation shape is bare). Fills are
  **ALL-OR-NOTHING** (adversary P0): only a request naming NONE of
  provider/model/voice/profile resolves the default — a field-independent
  fill re-minted the 2026-07-17 cross-provider leak (request `provider=piper`
  + no voice → filled a supertonic `voice=M2` → "Unknown voice_id"). A
  provider-less default row is not fillable (voice-only onto openai is the
  same leak). The capability read is short-TTL cached (5s) and run off the
  event loop (a split-mode read is an 8s-timeout HTTP GET — a per-request
  read on the loop starves SSE). 5 pins. Follow-up filed to the runtime
  seat: `stream_tts` should consult the runtime output-route merge so an
  in-process stream caller (none today) can't fall back to hardcoded openai.
- **Operator sleep is a composite act — disarm grant + clear orders** (spec
  v17, laurent dm#127 "sleep is sleep, it can't wake up on personal time if
  I put it to sleep"; gateway owns the composite write): `POST
  /entities/{name}/state` with `asleep` (the operator sleep click) now
  DISARMS the personal grant and CLEARS standing work orders in the SAME
  act, before writing the state, named in one biography moment. Ordering is
  the crash invariant — disarm first, so a crash before the state write
  leaves a bare desk (which the need-check re-sleeps), never asleep+armed.
  A corrupt `phases.yaml` REFUSES the sleep (409) rather than writing
  asleep over an armed grant. This closes the incident where Ephemeral woke
  to personal time ~1h after an operator sleep: the ~1h bounded-sleep wake
  landed on a still-armed July-15 grant because the sleep click wrote
  asleep-only. Self-elected/cycle sleeps are untouched (this governs the
  operator's click only); the composite is surfaced in the response
  (`composite_sleep`) and absent when there was nothing to disarm. Spec v17
  re-vendored. 2 pins.
- **Bulk provider discovery inlines endpoint-profile models** (code-tui
  c4235, first post from the new seat): `GET /discovery/providers?include_
  models=true` returned `models: []` for provider-endpoint profiles with no
  declared `allowed_models` while the per-provider route probed their
  upstream fine — every thin client grew the same fallback loop. The bulk
  route now probes such profiles inline with the SAME facade call the
  per-provider route uses — concurrent (wall time ≈ one probe timeout, not
  N), best-effort (a dead endpoint yields `[]` + `models_error`, never
  fails the route), sorted for shape parity, and only under
  `include_models=true` (the documented may-be-slow arm; the cheap arm
  stays probe-free — test-pinned).
- **Env-var kill, named incident — voice engine config-first** (operator
  dm#177: "REMOVE ALL UNNECESSARY ENV VARIABLES … ABSTRACTVOICE_TTS_ENGINE
  keeps screwing up OUR GATEWAY DEFAULT"): the gateway's TTS/STT engine
  resolution used `_env_first("ABSTRACTGATEWAY_VOICE_TTS_ENGINE",
  "ABSTRACTVOICE_TTS_ENGINE")` across 6 sites, so an exported
  `ABSTRACTVOICE_*` shell var (another package's namespace) silently
  overrode the gateway-configured engine. New `_resolved_voice_engine(kind)`
  inverts precedence: the console-editable capability-defaults route
  (`output.voice`/`input.voice` provider) WINS; the env chain is a labeled
  `#FALLBACK` below it, and a set-but-shadowed env is logged once so a stale
  export is visible. Because the capability route is the same config
  runtime/core execute from, advertising now aligns with execution (closes
  the 2026-07-17 "advertised M1 vs executed M2" divergence class). Empty
  config ⇒ env still resolves (no-silent-flip: a deployment that only sets
  env is unchanged). 7 pins. First migration of the env-var kill lead
  (design + migration transition adversaries folded; shared classification/
  precedence/no-silent-flip contract posted for all seats).
- **Runtime-config corrupt-store write-wipe** (env-kill design adversary
  P0, existing code): `write_runtime_config` did read-mutate-replace and
  `_read_store` returned `{}` on a corrupt file, so saving one knob silently
  wiped every other stored choice — harmless at today's 4 knobs, a
  catastrophe as the env→config migration grows the store. The write path
  now refuses on a corrupt store (`RuntimeConfigStoreCorrupt` → 409, file
  left intact for repair), every valid write rotates a `.json.bak`, and the
  read path degrades loudly (`#FALLBACK` log, never a silent `{}`). 2 pins.
- **Verified-token cache** (live idle-CPU incident, framework c4144: the
  freshly-deployed gateway burned ~95-107% CPU at ZERO runs/clients —
  sample dominated by PBKDF2/SHA256): under user auth, every bearer
  request ran the registry scan first — one 260k-iteration PBKDF2
  verification per enabled record per request (~0.5s CPU on the live
  5-user registry), so a handful of connected app pollers (observer
  board, entity app, assistant) burned a full core before the cheap
  static-token compare even ran. Successful verifications now cache on
  (token sha256 → principal) keyed to the registry FILE identity
  (mtime_ns, ino, size): rotation/revocation invalidates at the very next
  request exactly as before, failed tokens are never cached (the lockout
  layer owns brute force; a failure cache would be attacker-fillable).
  PBKDF2's cost stays where it matters — at rest against an exfiltrated
  users.json — instead of per poll. 3 pins in the resilience suite.
- **Event-driven ledger streaming** (backlog 0075 + 0082 harness; operator
  TOP PRIORITY per the c4089 shortlist ruling — "if you are the one
  responsible for 100% cpu usage, definitely put that as a top priority"):
  the SSE run-ledger stream no longer re-reads the ledger per poll. New
  `ledger_tail.py`: `JsonlLedgerTail` (byte-offset incremental reads — an
  idle poll is ONE stat(), news costs O(new bytes); complete-lines-only so
  torn concurrent appends are held, never dropped or emitted; recovery
  parity with `list()` for concatenated-object lines; geometric window
  growth over the 8MB per-read cap so an oversized line delivers instead
  of stalling), `SeqLedgerTail` (SQLite `WHERE seq > cursor` indexed reads
  — the dormant `list_after` put into service), `ListSliceTail`
  (count-gated fallback for plain stores). `stream_ledger` rewired: the
  dormant `ObservableLedgerStore.subscribe` is now a coalescing wakeup
  (appends wake streams instantly in-process; the 0.25s fallback poll
  stays for split-runner cross-process truth but costs a stat, not a
  file scan), `Last-Event-ID` reconnects resume exactly, terminal runs
  close with a `done` frame only after a genuinely progress-less final
  drain. Fable5 adversary (standing rule) found 2 P0s in the byte cap —
  oversized-line stall and premature `done` on skip-swallowed catch-up
  windows — both folded with pins; read errors now propagate loudly
  (a masked error used to close a terminal stream cleanly over a
  truncated replay). 0082's replay-equivalence invariants pinned in
  `test_gateway_ledger_stream_event_driven.py` (22 tests: tail==list on
  both backends, exact resume, recovery parity, torn-append hold,
  idle-poll-is-stat-only, end-to-end SSE on file AND sqlite backends).
  Honest limits on record: the wire cursor remains the dense record INDEX
  (coincides with SQLite seq; JSONL carries no record-level seq — no
  schema change, coordination contract with runtime c4104/c4105/c4107),
  and `migrate.py` cursor preservation across file→SQLite stays a named
  follow-up.
- **Gateway-internal resilience wave** (2026-07-21, operator order dm#150 via
  framework c4036 — "the gateway must be extremely resilient to failure";
  two adversarial reviews, process-lifecycle + subsystem-cascade angles):
  (P0-1) the GatewayRunner worker thread is now SELF-HEALING — an unhandled
  exception in the acquire/loop/yield scaffolding used to kill the thread
  permanently while the HTTP process kept serving and `/api/health` said
  healthy forever (runs accepted, ticked by nobody, invisibly; adversary
  executed the proof); the guard logs, drains bounded, releases the flock
  (also closing the fd — P0-1b: the leaked flock refused even the SAME
  process's retry), backs off exponentially (1s→30s cap) and re-enters,
  with recovery visible as `loop_restarts` + `last_loop_error` on
  `runner_status()`. Health-honesty belt: an enabled runner whose thread
  died without a deliberate `stop()` now reports `status="dead_worker"` and
  the health snapshot flips degraded (deliberate stops stay `inactive` — no
  supervisor restart storms). (P1-2) per-principal service creation is
  pre-warmed OFF the event loop (`ServicePrewarmMiddleware`, inside the
  security middleware): a new user's first touch used to run the whole
  heavy boot (stores, bundle compilation, entity routing) ON the ASGI event
  loop, stalling every other user's requests and `/api/health` behind it.
  (P1-3) the Telegram bridge isolates per-update — one malformed update /
  store error used to kill the `telegram-bot-bridge` thread permanently and
  invisibly (email/agora bridges already isolated; telegram was the
  outlier). (P1-4) shutdown is bounded end-to-end: `EntityChatHost.
  close_all(budget_s=60)` acquires each session's turn_lock with a timeout
  (a wedged in-flight turn is skipped — the next open's pending-lookback
  salvage owns the debt) and skips reflection past the budget; uvicorn
  `timeout_graceful_shutdown` defaults to 120s
  (`ABSTRACTGATEWAY_GRACEFUL_SHUTDOWN_S`) so SIGTERM can never hang forever
  on a wedged provider call. (P2-5) long-lived worker threads (entity chat/
  visit reapers, self-repair sweeper, telegram bridge) register in a
  process-wide `worker_registry`; `/api/health` renders `workers` +
  `dead_workers` and degrades when one dies (deliberate stops unregister).
  (P2-6) partial-boot honesty: best-effort factory steps that fail-and-
  continue (process-manager env, data-homes registry, shipped-catalog
  publish, self-repair sweeper) record labeled `#FALLBACK` lines surfaced
  as `boot_warnings` on the health snapshot, embeddings errors included —
  a boot that silently lost a subsystem is now visible on the probe.
  (P2-7) exceptions raised INSIDE the security middleware now return a
  JSON 500 through the middleware stack (CORS headers ride it) instead of
  re-raising past CORSMiddleware into a raw 500 browsers mask as "Failed
  to fetch". Second adversary (subsystem cascade) folds: (B-P0-1) the
  entity subsystem was the ONE unguarded boot dependency — an entity
  import/collision failure aborted lifespan startup and NOTHING served,
  plain workflow runs included; the block now degrades to labeled-disabled
  (entity routes 503, runs keep serving, `#FALLBACK` on health). Principle
  stated in code: only the store layer and the runner are load-bearing for
  "serve runs"; everything else degrades loudly. (B-P1-2) same treatment
  for bridges: an enabled-but-misconfigured telegram/email/agora bridge
  used to raise out of the composition root and kill all serving — now
  disabled + labeled. (B-P0-2) voice synthesis admission is bounded
  (`ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY`, default 4): `asyncio.to_thread`
  rides the loop's default executor (~22 threads shared with SSE ledger
  reads and every other to_thread site), and during a backend wedge each
  retry parked another shared-pool thread — the July watchdog unblocked the
  CALLER but not the cascade; callers past the ceiling now get an honest
  503, and on watchdog timeout the admission permit rides the wedged
  THREAD (released at true completion), never the request. (B-P1-1)
  wedged-tick visibility: `_inflight` carries tick start times;
  `runner_status()` reports `inflight_ticks` + `wedged_ticks` (>600s,
  `ABSTRACTGATEWAY_TICK_WEDGE_AFTER_S`) and `all_tick_workers_wedged`
  degrades health — four no-timeout provider calls used to freeze ALL run
  progression with zero signal.   (B-P0-3) `/api/health` is now
  subsystem-aware end-to-end: runner (incl. wedge state), worker registry
  (reapers, sweeper, telegram/email/agora bridges), boot warnings incl.
  embeddings, and the backlog exec runner when enabled. Supervisor-seam
  follow-up (framework's live kill-proof, c4063): lifespan now yields
  IMMEDIATELY and the heavy boot (entity-home load included) runs on a
  background thread — uvicorn accepts no connections until lifespan
  yields, so a long boot left `/api/health` connection-refused and the
  supervisor counted probe misses against a healthy-but-loading gateway
  (>60s boot = false recycle); health answers `status="starting"` during
  boot, a failed boot degrades loudly with the error, and `/api/gateway/*`
  requests gate on boot completion OFF the event loop (probes never queue
  behind them). 16 tests in `test_gateway_resilience_wave.py`; full suite
  912 green.
- **Mutual-exclusivity write-side wave** (2026-07-21, laurent dm#94 via
  entity's four-adversary write audit — "the 4 states are mutually
  exclusive; an entity being visit can NOT be on personal time"): (a) BOTH
  visit lanes (hosted chat + durable visits) now write the visiting posture
  (`asleep` + `mode=visiting`, `written_by=visit-door`) UNCONDITIONALLY at
  open — previously only when the own-time loop was running, so a visit on
  an awake loop-less entity left nothing durable and the composite folds
  rendered "personal · resting" beside a live chat; the operator's
  pre-visit word is recorded and restored at close/terminal/abort. (b) The
  unguarded awake writers are gated: `POST /{name}/state awake` refuses 409
  under a LIVE visit (stale postures stay repairable — the operator door
  never wedges); the summon wake-write refuses on a visiting posture
  (the /loop/start registration-window pattern). (c) `/life_state`'s
  durable widening serves `posture="visiting"` beside the phase and the
  phase is the ONE graph word (`visit` — the widening used to say
  "visiting" while the hosted arm of the same endpoint said "visit").
  (d) Postures carry the visit's OWN identity (`[visit <chat_id|visit_id>]`)
  and closes/terminals/aborts restore by OWNERSHIP token, never vocabulary;
  the durable preflight refuses opens under a live hosted chat (cross-lane
  one-life). Door-half adversary folded same-wave: a freshness gate at both
  stale-posture adoption branches (a posture younger than the reaper grace
  reads as MID-OPEN on the other lane and refuses — adopting it destroyed a
  live visit's token), ownership-checked abort restores (an operator
  pause/sleep landed mid-open-window stands), turn-failed runs finalize
  (the posture no longer strands until the reaper), and the stamp mint
  moved inside the restore window. Spec v10 re-vendored (sha b81e00f2).
  Same-wave follow-up (runtime c343 seam): `prior_state` carries `wake_at`
  and every restore passes it through — a BOUNDED operator sleep
  interrupted by a visit keeps its wake deadline instead of restoring as a
  default-bound sleep (an unattended entity could sleep past its 6h
  need-check); pinned by
  `test_bounded_sleep_keeps_its_wake_deadline_through_a_visit`.

### Changed
- **Durability-relevant exception swallows are loud** (2026-07-21, backlog
  0070 M half): 13 silent `except Exception: pass` sites adjacent to durable
  writes now log with context and consequence instead of vanishing — the
  command-cursor save (a failing save means restart replays commands), the
  terminal-subworkflow wait-repair pass, promote-to-FAILED after a tick
  exception, parent-resume after a terminal child, compacted-vars persistence
  (a lost save silently discarded a paid LLM compaction), auto-compact guard
  saves, backlog-exec run/ledger persistence, and the session-memory anchor
  save. Marker-only best-effort writes keep their swallow but log at DEBUG.
  The loop still survives every failure (a broken disk must not kill
  ticking); `tests/test_gateway_runner_swallow_audit.py` pins survive+log on
  the two backlog-named paths.

### Changed
- **Blueprint edit lane rebuilt to the v12 overlay shape** (2026-07-21,
  entity's design adversary P0-3 — the whole-file PUT "inverted the
  one-graph handshake and put two pens near one counter"): operator edits
  are now a TUNABLES OVERLAY beside the structural graph. The structural
  `{spec, sha256}` is untouched by modulation (drift warns key on the
  structural sha alone); GET serves `tunables_overlay` /
  `effective_tunables` / `overlay.edit_seq` beside it; PUT is
  tunables-only (the full-replace arm is DELETED — structure goes through
  the entity pen), CAS-guarded (`if_match` = edit_seq, 409 on race), and
  validated known-keys-only with bounds from `tunables_meta` (an unknown
  key is a typo'd dial — refuse, never default silently). The overlay
  source persists at `config/entity_phases_overlay.json`; the DERIVED
  effective spec is atomically rewritten at `config/entity_phases.json` —
  the exact file runtime's loop hook already reads. Spec re-vendored
  v12→v13 same-hour (entity's healthy bumps; drift pin green each time).

### Added
- **Unattended need-check, loop-less host half** (2026-07-21, spec v13
  `wake_conditions`; entity c356: "two hosts, ONE law — the loop's
  need-check when a process is alive, YOUR SWEEPER for loop-less homes"):
  the self-repair sweep now runs the cadence need-check on loop-less,
  UNARMED, unstamped sleeps — zero-token by law (a read over the standing
  sets, no summon, no LLM). A standing work order or pending tasks start
  the loop (its own top gate opens the work day, v9b); nothing sanctioned
  = silent re-sleep (sidecar timestamp only — no marker churn, the same
  sleep continues). The cadence is read from the served blueprint's
  `unattended_wake_cadence_h` dial, never a constant; armed-grant sleeps
  belong to the cycle/stamped sources and are skipped. 4 pins.
- **Entity loop self-repair** (2026-07-21, laurent's DM redirect: "the
  point is more that you should self-repair the entity then?"): a gateway
  daemon sweep (default 5 min; `ABSTRACTGATEWAY_ENTITY_SELF_REPAIR=0`
  disables) respawns own-time loops that died WITHOUT the operator's word —
  failure-culled (`stopped_by=failures`, the 03:03 incident's 40-minute
  blind window) or crashed (status says day/between, pid dead). The guards
  are the design: paused (kill switch) and lapsed personal grants block
  repair; deliberate stop words never repair; a visit defers to the next
  sweep; ONE repair per death (signature dedup) with a 30-min circuit
  breaker — a second death stays down and notifies the operator (email
  best-effort when configured). Respawns are faithful: `start_loop` now
  records its spawn parameters to `<home>/loop_spawn.json` and the repair
  replays them. Every repair/suppression lands a `personal_started`
  biography marker (channel=self-repair). 12 pins in
  `tests/test_gateway_entity_self_repair.py`.
- **Editable blueprint lane** (2026-07-21, laurent dm#104 via entity c348:
  "make those blueprints editable by me... this MUST become the source of
  truth"): `PUT /entities/spec/phases` (admin-gated by policy row) persists
  the operator's edit to `<data_dir>/config/entity_phases.json` — tunables
  patches deep-merge (the dials laurent freely modulates); full-spec
  replacement must keep the ruled-four phases. Every edit bumps
  `_operator.rev`, recomputes the served sha, and records a
  `blueprint_edited` host marker in EVERY entity's biography (write-first,
  then markers — a marker claiming an edit that never landed would be a
  false biography entry). GET serves the operator copy when present
  (`operator_edited`/`operator_rev` on the wire); the detached own-time
  loop reads the same FILE (no HTTP auth dependency). Spec re-vendored to
  v11 (the personal↔sleep maintenance cycle + tunables block).
- **skill requires_mcp/requires_tools consumer** (2026-07-21, abstractskill
  0008 consumer half; laurent ruled it active): `resolve_run_skills` — the
  ONE gate run-start, workforce spawn, and the entity lane ride — now checks
  each active skill's declared dependencies against the gateway's DECLARED
  MCP registry and the run-lane tool universe. Unmet => the skill drops from
  active with a labeled verdict naming the missing dependency (honest
  wording: "not declared on this gateway" — no probe lane exists, so "not
  reachable" would be fabricated) + a structured `requires_unmet` map; never
  a silent activation, never a run block; check-substrate failures skip with
  `#FALLBACK`. The entity render serves requires/requires_unmet on resolved
  rows and unmet matrix cells go `structurally_unavailable` with the
  dependency in reason. Pinned by
  `tests/test_gateway_skill_requires_consumer.py` (6 tests).
- **Console overhaul wave 3** (2026-07-21, framework card 015; laurent ruled
  it active): page-wide inline-SVG icon registry with boot hydration (lock/
  warn emoji replaced; no VS15 platform dependence), one chip recipe over
  the six pill families, re-embed relocated to Substrate behind a
  danger-zone disclosure, loading rows on runs/data-homes/entities tables,
  radius normalization onto the token scale, and create-flow staging (the
  dry-run's warnings ride the confirm — reviewed BEFORE the irreversible
  birth). Cross-repo flags posted to uic (`.af-dialogue` transcript recipe;
  light-theme accent proposal — vendored-token pen discipline). Pinned by
  `tests/test_gateway_console_wave3.py`.
- **Camera toolset door-half pins** (2026-07-21, camera 0012 gateway half;
  laurent's two-adversary condition met c3903/c3915, door half unblocked
  c3917; own adversary folded; RE-BASED same day after the operator killed
  the env gate — dm:camera--laurent#10, verbatim "i don't like those stupid
  variables, remove it! there is a reason why EACH APP can decide which
  tools run, STOP DUPLICATING gating"): `tests/test_gateway_camera_surfacing.py`
  pins the composition — run lanes derive camera exposure from runtime's
  `default_approval_policy_sets()` fold through the real gateway surfaces
  (`/discovery/tools` handler + default-constructed `ToolApprovalPolicy`),
  ABSENT PACKAGE means no camera name anywhere (simulated via an import
  blocker in a fresh probe interpreter; installed = registered is the only
  gate left), the walled entity surfaces (inventory, phase matrix, home
  grants) stay structurally camera-free even with the package installed,
  and no `abstractcamera` import exists in the gateway tree. Zero
  gateway-side camera code. The probe now mirrors the parent's sys.path
  into the child (pytest's `pythonpath=["src"]` never reached
  subprocesses). HONEST SCOPE from the door-half adversary: the workplace
  summon lane runs on the shared bundle host whose run-lane tool map
  carries camera when installed (home per-phase grants are not consulted
  there). RULED 2026-07-21 (laurent, user-right reading, commons c3938):
  that exposure ships AS BUILT — camera is a tool like any other; capture
  and detect verbs ask BY DEFAULT (a default, not a floor — users may
  auto-accept camera per-run like any tool); reference/status tools
  auto-approve per camera's own classification. 0012 complete.
- **Route authorization contract test** (2026-07-21, backlog 0070 S half):
  `tests/test_gateway_route_authorization_contract.py` pins the whole-app
  authorization invariant in three layers — (1) every served route lives
  behind the `/api/gateway` security-middleware boundary or on an explicit
  public allowlist with a recorded reason; (2) every write route has an
  explicit decision (a `GATEWAY_ROUTE_POLICIES` admin row or a
  rationale-grouped user-level allowlist entry, pinned in both directions,
  with session/login the only public write and a dead-policy-row check);
  (3) served-surface proof — a real non-admin principal is 403'd through the
  real middleware on a representative route of every policy family, and
  user-level writes pass authorization. A new route now lands RED until its
  author decides which side of the table it belongs to. The
  exception-swallowing audit (0070's M half) remains open.
- **Count-weighted drive groups served** (2026-07-20, laurent 277; memory
  c296): `/cognition`'s `drive_pressure` block passes through the engine's
  new `groups` fold render-when-present — `[{family, members, size,
  exemplar, shared_terms}]`, largest-first — so a family of many similar
  questions surges above a lone one. A group is a VIEW over the open drives
  (the numeric counts are byte-unchanged); absent on engines predating
  grouping.
- **Night-voice narration sweep** (2026-07-20, wave-5; the emission seam
  runtime ruled at c3750): runtime owns `<home>/night_narrations.jsonl`
  (append-only, in the home, travels on copy); `sweep_night_narrations()`
  reads it and interleaves `night_voice` host markers into the gateway's
  host stream, deduped on `dream_record_id` (the ≥20h throttle = one
  narration per dream). Recorded when present — idempotent, run before the
  marker read on both `/replay` and `/replay/stream`, best-effort (a torn
  or unlinked line is skipped, never breaks serving). The marker registers
  the `night_voice` kind (a derived, host-authored, self-labeled artifact —
  never enters the store, so recall stays clean) carrying
  `{dream_record_id, narration, self_label, narrated_at, trigger}`; entity's
  render keys on `dream_record_id`. Works for gateway-hosted AND detached
  home-direct lives (a life that never met a gateway keeps its narrations;
  the stream picks them up when it first serves the home).

### Changed
- **Act-only ref layer removed from the visit tool path** (2026-07-20,
  laurent's A ruling; runtime c273 deleted `$act_only` refs +
  `ACT_ONLY_TOOLS` + the `ToolDescriptor.act_only` flag): the HOME is the
  privacy boundary (`runtime_<slug>.sqlite3` rests beside the book), so a
  `diary_read` result resting in the home run store is inside the boundary,
  not a leak. The door's TOOL_CALLS handler no longer intercepts diary
  tools as act-only — `diary_read` materializes normally through
  `execute_tool_elections` (reading from the home's own book) and the
  turn-detail modal serves results verbatim (the operator diary-door
  right covers the read). The reader-side private-gist containment in
  SEARCH results and the write-boundary capture (fences to the book before
  the result rests) are unchanged; the surviving audience boundary is the
  served replay redaction + operator-only verbatim door. Dropped the dead
  `act_only` rider from the tool-inventory MatrixCell and entity tool
  declarations.

### Added
- **Sleep-candidate review desk** (2026-07-20, W3 second half — unblocked
  ahead of memory's W2 miner: the engine verbs shipped first): `GET
  /entities/{name}/candidates` lists the sleep passes' inactive
  review-gated candidates (public TripleQuery surface, bounded); `POST
  .../candidates/{record_id}/promote` (independence test — ≥2 corroborating
  records of distinct origins; engine refusals pass through verbatim) and
  `POST .../reject` (mandatory reason; hide is a separate stated act) wrap
  the engine verbs with the principal-stamped actor, acting in the
  candidate's OWN scope (never caller-claimed). Console overview gains the
  review desk (list + promote/reject). Sleep proposes; waking evidence
  disposes — these doors are the disposal surface.
- **Multi-root backlog serving** (2026-07-20, continuum c3583; laurent
  dm#110 "I must have proper access to everything in the board"): the read
  routes (`GET /backlog/{kind}`, `GET .../content`) fold the umbrella +
  every immediate child repo carrying `docs/backlog`; `package` on each
  summary is the repo DIRECTORY name (one authority — header labels stay
  parse-side); content resolves across roots with `?package=` as the
  collision disambiguator; `ABSTRACTGATEWAY_BACKLOG_ROOTS` overrides
  discovery. Write/exec lanes stay umbrella-scoped deliberately.
- **The one state graph, served** (2026-07-20, laurent dm#79 via c3562):
  `GET /entities/spec/phases` serves a vendored byte copy of entity's
  `spec/entity_phases.json` (`{spec, vendored, sha256, source}`); a drift
  test byte-compares the vendored copy against the pen whenever the
  checkout is present; a serving-boundary pin asserts every `/cognition`
  phase word is a graph word. Bump protocol: entity announces, gateway
  re-vendors same-day.
- **DoR gate default-ON** (2026-07-20, skill c3546: both co-signed sources
  teach a wall — a raw curl silently bypassing it contradicted them):
  `POST /backlog/{kind}/{filename}/execute` (and `execute_batch`) now
  evaluate the Definition-of-Ready gate when the `dor` param is absent;
  `dor=skip` bypasses EXPLICITLY and is recorded as `dor_overridden`
  (a bypass is a choice, never a default); `dor=check` stays accepted.
  Also fixed same report: the backlog parser's type enum was missing
  `improvement` (coerced to task upstream, making the DoR type refusal
  unreachable over HTTP) — the ruled four now hold in the parser, the
  title regex, the API docs, and `_BACKLOG_TASK_TYPES`.
- **Chat-drawer sleep restore** (2026-07-20, skill c219 audit): the hosted
  chat drawer's close now restores `asleep` when THAT visit woke an
  operator-asleep entity (`woke_for_visit`), guarded on the state still
  being the visit-authored wake — leaving `awake` standing minted the
  exact unphased dwelling laurent's c203 retires. Operator writes landed
  mid-visit stay authoritative.
- **drive_pressure serves the engine read** (2026-07-20, memory c215):
  `/cognition`'s `drive_pressure` block now folds abstractmemory's
  `drive_pressure()` composition (exact counts, six drive kinds incl.
  `unresolved_tensions`) with the threshold IMPORTED from
  `DRIVE_PRESSURE_BOUND` (one 20, never a second copy); the gateway adds
  only the gate semantics (`over`, `should`, `idle`).
- **Phase is total while alive — wave 1 of the lifecycle ruling**
  (2026-07-20, laurent c203 "awake is NOT a state; the entity at all times
  must be either visit/work/personal/sleep"): the `/cognition` phase fold
  no longer serves `null` for an idle entity — idle folds to `sleep`, the
  resting default (`sleep_detail: resting|dreaming|bounded` keeps the
  render honest; a resting default never claims consolidation). A running
  loop day with a standing work order now reads `work` (labeled
  `phase_source: "derived"` until runtime's loop_status carries day_kind).
  `/life_state`'s `awake` floor dies the same way (`sleep` replaces it).
  POST `/state` accepts the phase-vocabulary verbs `sleep|restore` as
  aliases (legacy `awake|asleep|paused` accepted forever; the at-rest
  state words and every engraved marker are byte-unchanged — the fold is
  serve-side only, and doors already wake resting sleepers so gate
  behavior is untouched). `/cognition` also gains `drive_pressure`
  ({over, counts, threshold, should, idle} — laurent's never-hanging
  drives rule as a SURFACED SIGNAL; auto-action deliberately withheld
  until the operator answers the cost fork). Console: the green
  "no phase active (idle)" badge and awake state chips are gone —
  resting renders as SLEEP (resting), never a celebrated idle.
- **Default entity skills + birth map install** (2026-07-19, laurent seq 156
  "all entities must know how their memory work and how to leverage it";
  skill c161): entity create() with no skills field defaults the selection
  to `entity-self-knowledge` (every phase) and installs
  `capability_map.md` from the abstractskill shelf at birth, marker-first
  (`skills_selection_changed` + `capability_map_changed`, `at_birth: true`).
  Explicit selections are honored verbatim; explicit `skills: []` opts out
  of both and writes no skills.yaml (the honest exists:false). Shelf
  unreachable degrades to a labeled warning — a birth never fails over
  teaching. All five existing homes backfilled live (map sha `820b6373`,
  one marker each).
- **Work-order write surface** (2026-07-19, laurent seq 155 "the entity
  must be able to work and execute commands when it works"; runtime shipped
  the loop half — `work_order.md` presence shifts the next day-open to
  `phase=work`, the WORK column of tool_policy.yaml applies incl.
  `execute_command` where granted): `GET/PUT /entities/{name}/work-order`
  is the operator's write door (the entity never writes its own order —
  the tool_policy.yaml authority split). Set is marker-first
  (`work_order_changed`, text never on the marker); clear archives to
  `work_order.done.md` (never a silent delete) and personal time returns;
  the GET serves the standing order + the archived verdict history. Console
  Capabilities panel gains the work-order textarea + set/clear + history,
  admin-gated. The WORK-column executability tooltip's "no work lane exists
  yet" is now false — the lane ships.
- **W3 canonical night** (2026-07-20, wave-4 dispatch c3291): the operator
  sleep verb (`set_state(dream=True)`) now runs the engine's FULL
  `sleep_pass` (resolve → tend → dream — the same night the loop's
  `on_sleep` runs), never the bare `dream_pass` that silently skipped
  tending (adversary A's two-different-nights divergence). Older engines
  without `sleep_pass` degrade to dream-only with a labeled `#FALLBACK`.
- **Voice inheritance is rendered resolved** (2026-07-19, laurent dm#68:
  "any entity should by default inherit the gateway default"):
  `GET /entities/{name}/voice` serves `effective` — for an unset entity,
  the fully-resolved triple it would actually speak with (the
  `output.voice` capability default incl. the voice id the catalog alone
  cannot name), `source: "gateway-default"`; no configured default
  degrades to an honest engine-decides note, never a fabricated triple; a
  set entity's `effective` is its own choice. Console renders the
  inherited triple on the unset line.
- **Entity voice picker on the console** (2026-07-19, laurent's
  entity-personal-voice directive — the Jul-17 server half had no UI): the
  entity manage page's Substrate panel gains a Voice section — current
  triple + source line, cascading provider/model/voice selects fed from
  the same capability discovery the defaults modal uses, Audition (hears
  the CURRENT UNSAVED selection through the entity's own TTS lane — the
  fabricated-selection lesson: a picker default is never presented as
  configuration), Save (PUT, marker-first `voice_changed`), and Clear
  (falls back down the gateway default chain, marked distinctly from
  never-set). Save/Clear admin-gated in the UI as at the server.
- **Diary → verbatims trail on the operator door** (2026-07-19, laurent's
  diary---verbatims directive, lane C): `GET /entities/{name}/diary/{entry_id}`
  serves an additive `trail` block — the entry's graph projection, its
  `written_amid` episodes (what he attended at write time) and
  `reflected_in` episodes (the conversation that led to the entry), each
  with title/date/kind and verbatim availability — so the entity app
  renders entry → verbatim click-through with the existing
  `/records/{graph_id}/verbatim` endpoint. Pure reads over edges that
  already stand; render-when-present (a projection-less old-vintage entry
  serves its words with no trail, never an invented one; a graph hiccup
  degrades to a labeled warning, never a failed book read). Live-verified
  on Ephemeral's store: his "What does presence look like?" question
  traces to the exact episode with a loadable verbatim.

### Fixed
- **Memory tools now execute on the visit lane — the granted-but-never-
  offered audit** (2026-07-18, entity c69 / laurent's dashboard-vs-effective
  directive; Ephemeral was right): `search_memory` / `read_memory` /
  `recent_memories` were granted by the tool policy but never OFFERED in
  visits — the door's hand-copied declaration map carried 7 of 10 walled
  tools on a blocker comment that went stale the day runtime shipped the
  session-free `HomeMemoryReader` (2026-07-10). Declarations now DERIVE
  from runtime's `walled_tool_rows()` (offered ⇔ executable by
  construction; the hand copy is dead) and the door executor wires the
  reader with driver parity: ONE `tag_map` per visit (persisted in
  `_visit.memory_tag_map`, so a #tag from turn 2 resolves in turn 5) and
  PRIVATE-WORD CONTAINMENT — the durable lane's tool results rest (ledger
  + cycle vars), so the reader gets a diary view whose private entries
  carry a word-free marker instead of their gist; matching still sees the
  text (a private entry is FOUND, its words stay in the book behind the
  act-only `diary_read` hop). "Diary words never rest outside the book"
  holds with zero runtime changes.
- **Dashboard executability truth** (same audit): the capability matrix's
  `executable: True` per-cell hardcode is dead — cells serve the lane
  truth (visit/personal execute the walled set; sleep/work have no
  tool-running lane, `ok=false` with the honest reason);
  `GET /entities/{name}/tool-policy` serves the same per-cell
  `executable` map (entity's c72 wire shape); `POST /visit/open` states a
  narrowed grant (`allowlist_pruned`) when one exists — empty by
  construction on a current stack, it appears exactly on version skew.
- **Visit tool results now served — the "no result recorded" incident**
  (2026-07-18, operator report: every lookup in a visit turn rendered "No
  result recorded for this tool — the gateway did not return what the
  lookup produced" while the run's ledger held 19 successful web_search
  results): the durable visit lane served `tool_details: []` as a named
  follow-up that never landed. The door now folds tool details from the
  run's OWN ledger — turn-id-keyed via `answer_user` records (never
  positional resume counting, which misattributes on the history sliding
  window and empty-message resumes), args harvested from started twins
  (`$slim` replaces >4KB completed payloads), results VERBATIM (the
  2026-07-09 operator-transparency ruling — never gated, never truncated;
  hosted-lane parity), act-only tools serve a label (their words never
  rest in the ledger by design), failed calls serve their error text, and
  executed-but-empty outputs serve an explicit marker. Served on the turn
  probe (`POST /visit/{run_id}/turn`) and per-assistant-turn on
  `GET /visit/{run_id}/transcript` (rehydration data; the entity app
  consuming it there is entity's half). A failed ledger read degrades to
  `[]` with a labeled notice, never a failed turn.
- **Visit-close lesson elections were silently lost** (same forensics —
  Ephemeral ledger seqs 119/121): runtime's lesson election (the cognition
  directive's lessons-gap fix) forms `kind=lesson` into LIFE scope at the
  close's APPLY stage, but the close-reflection segment's closed act set
  predated lessons and refused every one (absorbed, invisible to the
  visitor). `lesson→life` joins `interest→self` and
  `summary-with-summarizes-edges` in the segment's act set; identity kinds
  (value/purpose/trait) stay refused through every visit channel.

### Added
- **Drive ratios on the cognition wire** (2026-07-18, cognition-health
  directive — G1 serving half): `GET /entities/{name}/cognition` carries a
  `drives` block from memory's `cognition_health()` fold over the home's
  full ladder (questions open/resolved, problems open/repaired, interests
  open/explored; empty category ⇒ `ratio: null`, never a fabricated 100%).
  The roster (`GET /entities`) carries the same block for warm homes only
  (the roster stays file-cheap — it never opens a store; absent ≠ zero).
  Render-when-present with labeled `#FALLBACK` degrade on version skew or
  read failure. The gateway console renders two ratio bars in the entity
  Overview panel from the same read — both counts always visible, amber
  never-100% cue at saturation ("nothing open — no pull forward"), honest
  "none yet" on empty categories, no stale bars on a failed read. The
  cross-key fold divergence the adversary found (card discharged questions
  via either `answers`/`resolves` ref while `cognition_health` folded
  `answers` only) was reported to memory and fixed engine-side the same
  day (`_REF_ATTRS` union) — the bar and the card agree by construction.
- **Maintenance-hold race hardening** (same wave, adversary finding): both
  registry caches (`get_home`, `get_entity_runtime`) re-check the
  maintenance hold INSIDE the open lock on the miss path — a hold armed
  mid-call can no longer cache a warm handle across the maintenance window.

### Fixed
- **Static operator token principal split** (2026-07-17, commons c2690 —
  live-confirmed by code: thin clients on `ABSTRACTGATEWAY_AUTH_TOKEN`
  authenticated into an EMPTY per-principal world, zero entities, stale run
  store, while the browser admin session saw the real root): the legacy-token
  principal now carries the SAME identity as the user-registry admin
  (`user_id=admin`, `tenant_id=default`, `runtime_id=default` — was
  local/local-admin/local-admin). One operator, one identity, one data world,
  under either `ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME` posture. Actor
  stamps from static-token acts now read `person:admin`. Sessions persisted
  under the old identity keep their old routing until they expire; the old
  `users/local/local-admin` world stays on disk, dormant.

### Added
- **Voice TTS fail-loud watchdogs** (2026-07-17 outage: one wedged synthesis
  — abstractvoice holds a per-VoiceManager lock across whole streams — left
  child runs `running` for 2h+ and every later call queueing silently
  forever): `POST /runs/{id}/voice/tts` now bounds synthesis
  (`ABSTRACTGATEWAY_VOICE_TTS_TIMEOUT_S`, default 300s, <=0 disables) and
  answers 504 naming the request while best-effort cancel-commanding the
  stuck child run; `POST /runs/{id}/voice/tts/stream` pulls events through a
  feeder thread with bounded waits — an idle gap over the ceiling emits a
  terminal error event (`watchdog_timeout: true`) plus the same child cancel
  instead of a silent forever-stream. Honest limit: a truly wedged synthesis
  thread cannot be killed; fail-loud unblocks the caller with the truth and
  keeps durable state from reading `running` forever.

### Added
- **Per-entity voice + console Test button** (2026-07-17, operator directive
  dm#10; design converged through two adversarial reviews):
  - Each entity can have its own voice: the choice is a FULL
    `{provider, model, voice}` triple in the home (`<home>/voice.yaml` — a
    bare voice id recreates the cross-provider leak class), managed by
    `GET/PUT /api/gateway/entities/{name}/voice` (admin-gated, marker-first
    `voice_changed`, `clear: true` to unselect). New entity-owned TTS lanes
    `POST /entities/{name}/voice/tts[/stream]` resolve the voice LATE-BOUND
    server-side under the anti-mixing rule (the home triple applies only
    when the request names no voice fields — partial requests pass through
    untouched), mint their session-memory scope server-side, and delegate
    to the same production machinery as the generic routes; responses carry
    `voice_source` (request|entity|unset). The generic `/runs/{id}/voice/tts*`
    routes stay entity-blind by design. Per-USER voice remains the
    per-principal `output.voice` capability default — one resolution rule,
    two runtime planes; the gateway default stays the last resort.
  - Console multimodal modal gains a **Test** button (before Save): voice
    routes audition the CURRENT UNSAVED selection through the production
    TTS lane and play the audio inline (authenticated artifact fetch);
    text routes run a tiny sandbox generation; other modalities defer to
    the Sandbox tab (honest — no probe theater). Single-flight, stale
    players clear on any selection change, and a failed voice discovery
    now degrades to an honest "Voice discovery failed" label instead of a
    stuck "Loading voices…".
  - `VoiceTTSRequest.timeout_s`: per-request synthesis deadline, clamped to
    the server watchdog (a client may tighten, never widen) — the Test
    button sends 25s so a wedged synthesis fails fast with the watchdog's
    honest 504 instead of hanging the modal.

### Fixed
- **TTS watchdog child correlation** (adversarial find on the 2026-07-17
  watchdog): the stuck-child cancel now correlates by `request_id` from the
  child's trace metadata before falling back to newest-first — repeated
  interactive attempts against a wedged backend previously risked
  cancelling a LATER attempt's child while the timed-out request's child
  stayed running forever.

### Added
- **Per-entity skills selection** (2026-07-17, laurent c2857 "work on this
  now"; the c2838 committed shape): `GET/PUT /api/gateway/entities/{name}/skills`
  manages WHAT an entity is taught beyond the capability map. Selection
  persists in the home (`<home>/skills.yaml` — `[{name, phases?}]`, phases
  validated against runtime's ruled four, absent = everywhere) and travels
  on copy; PUT is admin-gated, whole-document replace, marker-first
  (`skills_selection_changed`, old/new names+phases, principal-stamped);
  `POST /entities` accepts a birth selection (validated before anything is
  created; the created entity stands with a labeled warning if the
  selection half fails). GET serves ONE server-resolved truth for both UIs:
  the stored selection, roster rows through the same trust gate as every
  skills lane (default-requested never trust-bypassed; unresolvable names
  are labeled verdicts visible at write time), and the
  PhaseCapabilityMatrix payload (global selection renders the ruled four
  with identical cells per uic's recommendation). Delivery into entity
  prompts is deliberately absent until runtime elects the composition slot.

### Added
- **Per-entity task inbox — the G3 door half** (2026-07-17, plan v18
  gateway §1; unblocks continuum origination, code's `/entity task` verb,
  and runtime's R-C loop half): tasks left with an entity are durable
  FACTS in the home — append-only events in `<home>/task_inbox.jsonl`
  (flock-guarded appends; state is a fold; torn tail lines skip with a
  labeled warning), written by `POST /entities/{name}/tasks` (admin-gated,
  origin STAMPED from the authenticated principal — no payload origin
  field exists to forge), `POST .../tasks/{task_id}/status`
  (pending|taken|done|parked), and visit close (`tasks` on the close
  request → origin `visit:<run_id>`; recorded only after a COMPLETED
  close, failures surface as a labeled response warning, never a 5xx over
  a finished close). Every write is marker-first (`task_inbox_changed`,
  refusing when the marker cannot land). `GET /entities/{name}/tasks`
  serves the fold; the entity roster carries `pending_tasks`
  render-when-present (absent when no inbox exists — the c2665/c2801
  three-consumer contract; `phase` deliberately waits for the phase
  machine). Ruling-neutral under D1: the door records facts — who opens
  the work phase is runtime's ruled behavior. The file schema is the
  cross-package contract for runtime's R-C day-open reader.

### Added
- **Capability-map install lane** (2026-07-17, laurent c2710 / skill's
  entity-self-knowledge teaching): `GET/PUT
  /api/gateway/entities/{name}/capability-map` manages the per-home
  memory-teaching file (`<home>/capability_map.md`) that runtime's
  `compose_system_base` presents verbatim on every summon surface. PUT is
  admin-gated and marker-first — a `capability_map_changed` host marker
  (old/new sha256 + size, principal-stamped, never the text) lands BEFORE
  the atomic file replace, and a marker failure refuses the write: what a
  mind is TAUGHT changing between sessions is the same auditable class as a
  substrate swap. GET reads honestly (`installed: false` for an uninstalled
  map, never a 404 on an existing entity).

### Changed
- **Telegram bridge rides durable session replay** (2026-07-17, the
  durable-sessions review's named follow-up): the bridge's private transcript
  (`binding["history"]` in `telegram_bridge_state.json`, shipped into every
  run as client `context.messages`) is RETIRED — it was the last
  second-source-of-truth transcript in the gateway tree. Runs now opt into
  the server-side seed (`use_session_history: true`,
  `session_history_max_messages` from `ABSTRACT_TELEGRAM_MAX_HISTORY_MESSAGES`,
  default 30) under the same stable per-chat session id; `/reset` already
  rotates the session id, which is what clears replayed history. Behavior
  deltas, deliberate: failed turns no longer replay into later prompts
  (the old transcript kept "Sorry — the run failed" lines), and mid-run
  ask-user exchanges live inside their run rather than being re-shipped
  verbatim into every later run. Stale `history` keys in existing state
  files are ignored.

### Added
- **Durable session conversation replay, seed side** (2026-07-16, operator
  directive, agora `durable-sessions` contract v1): `start_run` with
  `input_data.use_session_history` truthy and a `session_id` seeds
  `context.messages` from the session's prior COMPLETED root runs via
  `abstractruntime.session_history.session_chat_messages` — server-owned
  conversation replay for thin clients (the assistant regression where every
  turn started blank). Client-provided `context.messages` always win; explicit
  `session_history_max_messages: 0` disables replay per run; caps come from
  input (`session_history_max_messages`, `session_history_max_chars`) then
  env (`ABSTRACTGATEWAY_SESSION_HISTORY_MAX_MESSAGES`, `..._MAX_CHARS`) then
  defaults (40 messages / 24k chars); failures degrade to a labeled
  `_runtime.session_history` `#FALLBACK` note plus a WARNING log — never a
  blocked start. Opted-in runs always get a `context.messages` LIST (even
  empty) so the session's first turn classifies as a chat turn for later
  reads. Requires AbstractRuntime>=0.4.30 (dependency floor bumped).

### Added
- Run-level skills selection COMPLETE end-to-end (card 0087, both halves):
  the gateway half (input_data.skills → trust-gated resolution →
  `_runtime.skills_block` + `skills_resolution` + `read_skill` executor)
  now composes with the runtime half (Agent-node subruns inherit the block
  verbatim; `read_skill` joins explicit child allowlists, empty stays
  registry-defaults). End-to-end pin drives the real gateway host from
  start_run through the Agent-node spawn and asserts the child run's vars.
  docs/api.md documents the field + trust semantics. Card moved to
  completed.

### Changed
- Entity recall shelf default 36 -> 50 (operator directive c2468,
  2026-07-15: "increase the max memories from 30 to 50" — the observed
  "30" was 36 minus the 6 identity seats, which render in the prelude).
  One constant (`DEFAULT_ENTITY_CHAT_SHELF_SIZE`), chat + loop lanes
  inherit. Arithmetic per memory's live-trace check: observed ~77-token
  digests seat 50 in ~3,850 of the 7,864-token budget (2x headroom);
  if richer digests pin tokens_used, the companion knob is the recall
  budget's token_fraction 0.12 -> 0.16.

### Changed
- `dp-*` vocabulary retired for `deep-*` (operator ruling via flow c2559):
  docs/dp-research.md renamed to docs/deep-research.md (contents synced to
  deep-research@0.1.6 / abstractresearch.deep.v1), the bundle contract
  suite ported (test_deep_research_bundle_contract.py — the archive move
  of dp-research@0.1.x had turned it red), wheel/sdist force-include now
  ships deep-research@0.1.6 (the old entries pointed at the MOVED file —
  a wheel build would have shipped without the research bundle), and the
  install-profile pins updated. Noted back to flow: 0.1.6's manifest
  metadata block is empty where 0.1.0 carried family/tool_policy/etc.

### Fixed
- World-model cards and lessons are viewable (operator ask via entity
  c2529: "we should be able to view the world model cards, fix it"):
  `world_model` and `lesson` joined the verbatim endpoint's born-digest
  kinds — the sleep pass births world cards as words (no payload_ref by
  design), so the endpoint now answers 200 + `born_digest: true` with
  the complete digest text instead of a 404 that read like an error.
  Verbatim-backed lessons (payload_ref present) keep serving their
  artifact; every other payload_ref-less kind keeps the honest 404.
  Live-verified on Ephemeral's real world card.
- Stranded visits and orphaned yield postures now self-heal (entity
  forensics c2465 ask 1, from the operator's 22:38 "false sleep"
  incident): the durable visit host gained a daemon-clock REAPER —
  (A) parked visits whose idle deadline passed are driven with no client
  alive (indexed due query per home store, never a full parse; the idle
  close stays graceful: reflection runs, prior state restored, yielded
  loop woken; RUNNING-at-rest crash orphans are deliberately left for
  the explicit /tick recovery verb); (B) a home stuck at
  asleep(mode=visiting) with NO live session anywhere (the
  restart-orphan class: a killed gateway leaves the hosted lane's
  auto-yield posture forever) is restored to awake — only when the
  wired chat probe answers definitively, no durable visit is open, and
  the posture outlived a 3-minute open-registration grace window; the
  repair lands as a wake host marker (channel=reaper). Also from the
  same forensics: /life_state's durable-visit fold failure is now a
  LABELED #FALLBACK warning instead of a silent pass (a broken visit
  host silently painted "asleep" over an OPEN visit), the composite
  serves mode=visiting as phase "yielded" (visit bookkeeping was
  rendering as sleep), and the operator sleep verb sets mode=dreaming
  only WHILE the dream pass runs (present-tense honesty — the badge
  claimed consolidation for whole naps).
- Run-ledger SSE catch-up batches ~256KB per send (the same per-record
  streaming tax found on entity replay — entity's c2394 profile, sweep per
  laurent's "other apps may have the issue"): one ASGI send per ledger
  record throttled catch-up for every ledger-following client (flow,
  continuum, assistant). Per-event `id:` lines are preserved for exact
  reconnect cursors; SSE framing is byte-identical.
- Entity visit idle auto-close default raised 15min -> 1h
  (`DEFAULT_VISIT_IDLE_S`), implementing the operator's ruled target
  (relayed via assistant, entity-society 314): a visit ends on explicit
  close or ~1h inactivity — never per-turn. The idle close stays graceful
  (reflection runs); v0 ticking is request-driven so the deadline fires at
  the next door touch; the standing reaper remains the GW-D/E lane.
- Personal time is ONE CLICK (operator ruling 2026-07-15 21:38: "on
  entity app, i click on 'personal', and the entity is then authorize to
  tick itself... SIMPLIFY, do not put excessive guardrails"). The old
  flow refused an unarmed `/loop/start` with `not_granted` and told the
  operator to arm `phases.personal` on a different surface — a
  confirm-your-own-choice ceremony. Now the operator's authenticated
  start IS the grant: an unarmed (or lapsed-timer) bucket is armed
  `until_revoked` in the same act (marker-first, `granted_by` = the
  acting principal), and an operator-asleep entity is WOKEN by the same
  click (doors wake, they don't refuse — B1 extended). An armed timer is
  never rewritten by start. Paused (hard freeze) still refuses before
  any grant write; visit-overlap guards unchanged; entity/visit/harness
  paths still arm nothing; the runtime loop gate still re-checks the
  grant at every day-open. The `personal-grant` surface remains for
  timers and revocation; the console's own-time button drops its
  two-act confirm ceremony. Live-verified: unarmed entity, one
  loop/start, 200 + armed by `person:admin`.
- Gateway no longer burns ~100% CPU on idle file-store deployments, and
  entity replay serves ~14x faster (entity's live profile, 2026-07-15):
  two compounding defects. (1) The runner's 0.25s poll ran three
  scarce-match scans per iteration; on a JsonFileRunStore with thousands
  of terminal runs each scan parsed the WHOLE store (matches scarce, and
  the store's 512-entry LRU cannot hold the directory — the scan evicts
  everything it caches), pegging a worker thread in json.loads forever
  and taxing every request with GIL contention. The scheduling pass is
  now gated: a cheap mtime fingerprint (count, max, sum over run_*.json)
  skips the pass while nothing changed; a wait-deadline horizon wakes it
  by TIME (deadlines change no bytes); commands, run-starts
  (`runner.nudge()`), and finished ticks force the next pass. Non-file
  stores are ungated (indexed scans are cheap; a constant fingerprint
  would skip forever). Measured: idle ~100% -> ~7% of one core on a
  3,241-file/659MB store. (2) `/entities/{name}/replay` sent ONE
  envelope per ASGI chunk — a threadpool hop + middleware crossing +
  send per line (~14-20 MB/s ceiling). NDJSON and the SSE catch-up
  phase now batch ~256KB per send, byte-identical content; SSE events
  also carry their OWN seq in `id:` (the old chunk-final id could skip
  envelopes on a mid-chunk reconnect). Measured: castor 12.5MB/8,871
  envelopes 13.7-17.5s -> 0.9-1.25s.
- Entity ID rendered as the agreed handle, `<name>@<gateway lan ip>`
  (operator ruling 2026-07-15: "entity id = <entity_name>@<gateway_ip>…
  ip is the current lan ip of the gateway. gateway is their home").
  The console previously showed the internal manifest string
  (`entity:<slug>@home-<hash>`, a crash-safe birth marker) as "Entity
  ID". Now: `config.resolved_door_address()` resolves the declared
  address knob when set, else DETECTS the current LAN IP (UDP-connect
  trick, never loopback); `render_handle()` renders `<slug>@<address>`;
  the entities table ID column and the manage card "Entity ID" row show
  the handle, with the manifest string demoted to an explicit "Internal
  ID (birth marker)" card line. The address is still never written at
  rest (C1 relocation pin unchanged); an offline box with no declared
  address shows no handle rather than a fabricated loopback.
- Auth lockout no longer punishes valid credentials or presence (operator
  incident 2026-07-15 19:40, "all apps: Too Many Requests (auth
  lockout)"): three defects in `security/gateway_security.py` —
  (1) the lockout gate ran BEFORE credential verification, so once the
  shared loopback IP locked, VALID credentials were 429'd too (the
  operator's suspicion "you log a successful request as one of those"
  was exactly right), including the very sign-in that would have cleared
  the state — a deadlock on one-box deployments where every app shares
  the IP; (2) requests presenting NO credential (thin-client feature
  probes before sign-in) counted as auth failures — collective
  punishment; (3) failure counts never decayed, so a background app
  retrying a stale cookie ratcheted the exponential backoff all day.
  Now: credentials verify FIRST (valid always passes and clears the
  state); only PRESENTED-and-invalid credentials count as guesses; bare
  probes are 401 login prompts, never counted, never 429'd; failures
  decay after a 15-minute quiet window; default threshold 5→10
  presented-invalid attempts (`ABSTRACTGATEWAY_LOCKOUT_AFTER`). Sustained
  credential guessing still trips the exponential lock (test-pinned).
  Tests: `tests/test_gateway_auth_lockout_semantics.py` (5 pins);
  live-verified on the relaunched stack (12 invalid tokens → valid
  credential answers 200; bare probe answers 401).

### Added
- Run-level skills selection, gateway half (card 0087; flow's c2254
  transport ruling on the operator's 16:22 skills directive):
  `POST /runs/start` now resolves `input_data.skills` (a list of skill
  NAMES) through abstractskill's trust gate — the SAME shelf and
  `select_skills_for_context` gate as `/skills` and the workforce spawn
  lane — into agent's named slot `_runtime.skills_block` (resolved once at
  start; byte-stable per run per the cache contract), with bookkeeping in
  `_runtime.skills_resolution` (requested/active/verdicts/tree hashes;
  held/blocked/missing ride as labeled verdicts — default-REQUESTED, never
  trust-bypassed). The `read_skill` progressive-disclosure tool gets both
  halves: the schema joins the agent-workflow tool registry (the allowlist
  normalizer prunes unregistered names, so it must live there) and the
  gateway tool executor maps it to the shelf with a TRUST RE-CHECK at read
  time (a blocked skill's body never reaches a model even if a stale block
  lists it) and bounded output (labeled #TRUNCATION). A caller-set
  `_runtime.skills_block` is never overwritten (input key ignored with a
  labeled verdict); a caller allowlist is extended with `read_skill` when
  a block attaches. KNOWN GAP, on the record: visual Agent-node subruns
  build fresh `_runtime` in abstractruntime's compiler, so the root-run
  slot does not yet propagate to them — the propagation half is
  abstractruntime's lane (asked on commons); end-to-end for Agent-node
  workflows lands with it. Tests:
  `tests/test_gateway_run_start_skills.py` (10 pins).
### Fixed
- Console themes = the framework's themes (operator catch 2026-07-15
  16:56; uic backlog card 0023): the console's Appearance dialog offered a
  hand-copied 6-theme fork while the abstractuic kit serves 21. The
  console cannot import the kit (served HTML, no npm build), so the kit's
  `theme.ts` THEME_SPECS + `theme.css` per-theme token blocks are now
  GENERATED verbatim into `console_themes.py`
  (`python -m abstractgateway.console_theme_sync`) and spliced into the
  served page; a drift-pin test regenerates from the kit checkout and
  fails loud on any divergence (skips honestly outside the monorepo), so
  the copy can never rot silently again. The hand-tuned per-theme console
  overrides (tokyo-night's custom button hue etc.) are deleted — console
  aliases now DERIVE from kit tokens via `color-mix` at the alias layer
  (`--panel-2`, `--danger-2`, `--subtle`), so all 21 themes style the
  console with zero per-theme console CSS, and the dropdown renders the
  kit's Dark/Light groups. Tests:
  `tests/test_gateway_console_theme_sync.py` (drift pin, list⇄CSS
  coverage, served-page splice, no-hand-tuning pins).

### Added
- Skills + MCP inventories for launch surfaces (operator directive
  2026-07-15 16:22 via observer c2233): `GET /api/gateway/skills` serves
  the abstractskill shelf with trust verdicts — the ruled roster row
  (decision:workforce-capabilities-homes): `{name, description,
  trust_level, blocked, requires_review, tree_hash, source, has_scripts,
  reasons}`; shelf resolution reuses the skills-union rule
  (`ABSTRACTGATEWAY_SKILLS_SHELF` > the triage repo's
  `abstractskill/registry`) so pickers list the same shelf the workforce
  lane resolves against; an empty/failed trust registry fails CLOSED
  (everything unverified/requires_review), and the word "safe" never
  renders. `GET /api/gateway/mcp/servers` serves the declared registry at
  `<data_dir>/config/mcp_servers.json` with declared fields only and
  `probed: false` (connect state/tool counts are a later probe lane and
  are never faked); malformed rows become labeled warnings, never drops.
  Composition in `capability_inventories.py`; tests in
  `tests/test_gateway_capability_inventories.py`; docs/api.md discovery
  section updated.
- Shipped-catalog boot publish (card 013, fresh-install UX): the wheel now
  carries `docs-qa@0.1.0.flow` (pyproject force-include) and boot
  idempotently ensure-publishes it into the tenant catalog
  (`shipped_catalog.py`, hooked in `create_default_gateway_service` before
  the host scans the catalog dir; per-tenant services created lazily run
  the same hook for their tenant). Semantics keep admin authority intact:
  publish-IF-ABSENT by exact version (restarts never churn records, never
  touch `updated_at`, never overwrite publisher attribution — publisher is
  `system:gateway-boot`); `make_default=False` (the store assigns a default
  only when none exists, so an admin-moved pointer is never moved back);
  tombstoned versions are never resurrected; a sha conflict (rebuilt
  artifact at the same version) warns loudly naming the repair and never
  blocks boot; the one repair case (record present, catalog bundle FILE
  wiped) restores the bytes preserving attribution. BOOT-NEUTRALITY gate:
  the publish is skipped (honest note) when the deployment's private
  registry carries no LLM-bearing flow — publishing the llm_call-bearing
  docs-qa there would CREATE a boot requirement the deployment never had
  (the gate reads flow content via the host's own node scanner, not
  filenames). Kill switch: `ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED=0`
  restores the documented-curl-only posture. The allowlist is an explicit
  named tuple (docs-qa only — basic-agent/orchestrator/dp-research ride
  the private runtime registry and never needed the catalog). Tests:
  `tests/test_gateway_shipped_catalog_publish.py` (13 pins incl. the
  fresh-install HTTP receipt); docs/api.md §2d updated.

### Fixed
- Host-marker lane capacity + flood detection (card 014, 2026-07-14
  marker-flood incident follow-up): one journal base could absorb at most
  999 markers (`base + n/1000`), after which EVERY later host moment at
  that base raised — the incident wedged a life's audit stream for hours
  and made state verbs look broken. New writes now mint `1/10000` ticks
  (9999 slots per base); the next slot derives from the max EXISTING seq
  at the base compared in final float space, so engraved legacy `1/1000`
  markers and new fine-grained ones coexist in one file in strict
  ascending order and no historical float is rewritten. True exhaustion
  still refuses loudly (never collides, never spills into `base + 1`).
  Same-`(kind, reason)` bursts above 20 markers/60s log a loud warning
  naming the caller-visible signature — detection only, the append always
  proceeds (marker coalescing changes read-visibility granularity and
  stays maintainer-gated). Read bounds fixed alongside: the hand-tuned
  epsilons (`+ 0.9995` on the card's moments, `+ 0.9999` on
  `merged_replay`) are replaced by the exact `marker_window_end(base)`
  (largest float strictly below `base + 1`) — the old card epsilon would
  have silently dropped high-tick markers under the finer granularity.
  Tests: `tests/test_gateway_marker_lane_capacity.py` (8 pins: dense base
  >999, legacy/new coexistence order, float-subtraction artifact ladder,
  flood warning + quiet negatives + stale-window, exhaustion refusal,
  exact window end).
- GatewayRunner singleton-lock hardening (stuck-run incident, root-cause
  lane): when two `abstractgateway serve` processes shared one data_dir (an
  orphaned older process surviving a launcher port replace), the port-serving
  process's runner lost the one-shot `gateway_runner.lock` flock race and
  bailed with a single invisible `logger.warning` — every run it accepted was
  ticked by NOBODY and hung forever on its entry node with zero ledger
  records. Layered fix, flock stays the sole mutual-exclusion primitive
  (verified: the kernel frees a dead holder's flock even on SIGKILL, so
  refusal always means a LIVE holder):
  - the runner worker thread now RETRIES acquisition (≤1s cadence) instead of
    giving up once, so a freed lock is picked up without a process restart;
  - a newly-starting process writes a one-shot takeover request
    (`gateway_runner.takeover`); a live holder's loop drains in-flight ticks,
    releases the flock, and drops to passive standby (yielded runners never
    request takeovers — no ping-pong; simultaneous multi-worker startups
    converge to exactly one stable ticker);
  - the holder heartbeats the lock file mtime every loop iteration so any
    process can distinguish "live peer ticks this data_dir" (benign standby)
    from "nobody ticks" (degraded);
  - loud surfaces: `GET /api/health` now reports per-service
    `runner.runners[]` status (`active` / `standby_peer_active` /
    `degraded_no_ticker` / `disabled`, holder pid, heartbeat age) and degrades
    top-level `status` (still HTTP 200 — liveness contract); `StartRunResponse`
    gains an additive optional `runner_warning` populated only when the run
    was accepted while nobody provably ticks the data_dir.
  Env: `ABSTRACTGATEWAY_RUNNER_LOCK_STALE_S` (heartbeat staleness threshold,
  default 10s). Tests: `tests/test_gateway_runner_singleton_lock.py`.
- GatewayRunner silent-swallow hardening (stuck-run incident, reviewer-B
  lane): a RUNNING run whose workflow could not be resolved
  (`runtime_and_workflow_for_run` raising — deleted draft, tombstoned
  catalog version, principal-scoped bundle not loaded) was caught at
  DEBUG and re-submitted every 0.25s poll forever: stuck RUNNING, zero
  ledger, no error — the same user-visible symptom as the refused
  runner-singleton lock, reachable with a healthy lock. `_tick_run` now
  counts consecutive resolution failures per run (one WARNING per streak)
  and promotes the run to FAILED with a
  `system:workflow_resolution:<run_id>` ledger record after
  `workflow_resolution_failure_limit` consecutive failures (default 40 ≈
  10s — tolerates catalog-loading startup races). Only RUNNING runs are
  promoted (parity with the tick-exception path). Additionally, the
  per-run `runtime_and_workflow_for_run` calls inside
  `_repair_terminal_subworkflow_waits`, `_resume_subworkflow_parents`,
  and `_apply_emit_event` are now guarded per-run: one unresolvable run
  no longer silently aborts repair/resume/event-delivery for every other
  run behind it in the loop. Tests:
  `tests/test_runner_tick_resolution_failure.py`.
- Crash-orphan visit recovery (walkthrough gate re-run, agency c505): a
  SIGKILL landing mid-tick (post-ANSWER, pre-park) leaves the visit run
  RUNNING, not WAITING — a plain `/turn` resume 409'd "Run is not waiting"
  and forced the visitor client to know the `/tick` host-internal. `/turn`
  now drives a non-terminal RUNNING run to its next park FIRST, then takes
  the message (a WAITING run drives zero steps; a terminal run falls
  through to the honest 409). The one-life-one-visit gate and `GET /visit`
  already consult the DURABLE per-entity store on every call (no
  in-memory-only registry exists) — pinned now by a host-amnesia
  regression that proves a second open still refuses after the in-memory
  host is forgotten. Tests:
  `test_turn_recovers_a_crash_orphaned_running_run`,
  `test_one_life_gate_consults_the_durable_store_after_amnesia`.
- Adversarial-review wave over the visit tool wiring (two fable5
  adversaries, both P0s fixed same-night): (1) `diary_read` through
  TOOL_CALLS now returns the canonical `$act_only` REFERENCE frame instead
  of the book's words — the effect result rests in the per-home run
  ledger/vars (surfaces that travel on directory copy), so materialized
  text there was a G1 privacy leak AND unusable (the react observe
  fail-safe suppressed it); words now resolve only at send time. Pinned by
  a byte-grep over the WAL-checkpointed run store. (2) The shared entity
  router (`install_entity_routing`) now caches (home, handlers) PAIRS and
  rebuilds on home identity change — reembed's eviction closed the cached
  engine and left the router serving handlers over a closed connection
  until process restart. (3) An operator ZERO grant (`visit: []`) now
  denies all tool calls — the empty allowlist used to fall OPEN to tier-1
  through `native_tool_elections`' `or`-default. (4) ONE per-turn tool
  budget bounds a turn across effects, from runtime's ruled
  `MAX_TOOL_BLOCKS_PER_TURN` (20, maintainer 2026-07-11 05:25) —
  imported, not a second literal; the earlier ad-hoc 24 + hidden 8/batch
  sub-cap are gone. (5) The react-middle build moved inside the
  yielded-loop restore window (a 503 at open used to strand the own-time
  loop asleep in visiting posture). (6) `EntityHome.close()` closes the
  book ledger's database too (reembed leaked one connection per pass).
  Scripted-LLM test fixtures now RAISE on script exhaustion so call-count
  drift fails loudly (two pause tests' skip_reflection proofs were
  previously vacuous).
- Entity visits can now actually USE TOOLS (the Mnemosyne fabrication
  incident, maintainer 2026-07-11 — root cause three-bench-converged:
  native-tool-channel substrates essentially never write fenced tool
  text): the door's per-entity LLM_CALL handler forwards payload `tools`
  + `params` and returns `tool_calls`/`finish_reason`/`usage` (empty
  content is not a failure when tool calls arrived), and a per-entity
  TOOL_CALLS effect handler now exists — native calls execute through
  runtime's OWN entity executors (`native_tool_elections` +
  `execute_tool_elections`: web_search/fetch_url/diary_list/diary_read/
  workspace trio; diary reads join the verified path through the
  routing-wrapped DIARY_READ handler). Grant authority is
  `<home>/tool_policy.yaml` phase=visit (read FRESH per call — per-phase
  edits persist across restarts by construction); effective allowlist =
  grant ∩ payload; TOOL_CALLS is stamp-gated like LLM_CALL. Fixture
  proof: a visit that writes a real file through a granted native
  write_file call (`test_visit_actually_writes_a_file_through_the_grant`).
- Reembed repair verb no longer trips the M1 mismatch it exists to repair
  (walkthrough catch #2, agency c424): the pass now opens the home in the
  REPAIR POSTURE (no embedder — always legal under M1, memory's c454
  contract), runs `reembed_store` with the target embedder under the
  maintenance lease, and evicts the door's cached home/runtime handles so
  the next open binds the NEW pin. An anonymous embedder with no explicit
  target warns loudly (pin records model_id=None; dimension-only
  enforcement). Regression: `test_reembed_repairs_across_a_route_flip`.

### Changed
- React is UNCONDITIONAL for entity visits (maintainer ruling 2026-07-11
  00:49: "all summoned entities are by definition react agents. it's not
  even a choice — remove that parameter"): the
  `ABSTRACTGATEWAY_VISIT_REACT_MIDDLE` env knob is DELETED; every new
  visit/meet open builds abstractagent's ReAct cycle as runtime's
  `react_middle`, with the entity's GRANTED tools declared natively in
  the payload (declarations match runtime's entity executor arg shapes;
  `act_only=True` on diary_read; read/search_memory not declared until
  their driver resolvers are reachable outside ChatSession). The arm
  stays recorded per run; a pre-ruling run that recorded v0 still
  rebuilds its own graph (durable-arm contract — never swap a graph
  under a parked run). Missing abstractagent refuses visit opens loudly
  (503).

### Added
- ONE mind substrate per entity (maintainer ruling 2026-07-09 06:32: "i
  don't see the point in having potentially different models for visit and
  own time"): the operator's provider+model choice persists in the entity's
  home (`substrate.yaml`, operator-owned like `tool_policy.yaml`), exposed
  via `GET`/`PUT /{name}/substrate`, and resolved identically by chat opens
  AND loop starts: request override > home file > operator env > loud
  refusal — still no code default anywhere. Tests:
  `tests/test_gateway_entity_substrate.py`.

### Changed
- Renaming sign-off executed (approved by the maintainer, commons c398):
  `declared_door_address()` moved `entities.py` -> `config.py` (door-wide
  serving config, not an entity concept; `render_handle` stays
  entity-homed). Same-day consumer syncs: all gateway lease call sites now
  import `abstractruntime.storage.lease` (`DirectoryLease*`,
  `acquire/read_directory_lease` — runtime's re-home, dotfile
  `.writer_lease`), and the reembed verb calls memory's `reembed_store`
  (was `reembed_home`).
- Entity mind substrate has NO code default anymore (maintainer ruling
  2026-07-09 04:26: "I decide which provider and model is used … NO
  FALLBACK"): `DEFAULT_ENTITY_CHAT_PROVIDER`/`_MODEL` (which silently
  elected OVH `gpt-oss-120b`) are removed; `resolve_substrate` resolves
  request body > operator env (`ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER`/
  `_MODEL`) and otherwise REFUSES with a 400 naming the picker, on both
  `chat/open` and `loop/start`. Thin clients (AbstractObserver) present
  discovery-fed provider/model dropdowns and always send the explicit
  choice.
- Entity attention defaults (maintainer ruling 2026-07-09): the recall shelf
  default widens 24 -> 36 ("it needs to retrieve more memories to function";
  at 65536 the 12% token budget still seats 36 rich digests).

### Fixed
- Entity chat idle-reaper now runs on its own daemon-thread clock instead of
  only inside `open()`: an idle web visit used to hold the entity's yielded
  own-time loop asleep indefinitely when no new visitor arrived (live
  failure 2026-07-09 — Castor stuck asleep ~3h after a browser tab idled).
  Idle sessions now close (with reflection) and wake the loop on time.

### Added
- Persistent-shell teardown on the tools-only bundle host path (agency-parity 0220): the
  Runtime built without an LLM client now also registers `register_shell_session_teardown`,
  so run-scoped shell sessions are reaped at terminal transitions on every gateway runtime
  construction path (the LLM paths inherit it from the abstractruntime factories).
- `inject_guidance` runner command (agency-parity 0217): steers a running agent by appending
  operator guidance to the target run's (and descendants') durable `_runtime.inbox`, which the
  ReAct loop drains at its next `reason` cycle — mid-run redirection without cancel/restart.
  Hardened against a durable-state race found in adversarial review: a terminal-status guard +
  re-check-before-save mean a stale RUNNING snapshot can never resurrect a COMPLETED/FAILED/
  CANCELLED run (a narrow best-effort loss window remains; full single-writer routing is a
  follow-up). Tests: `tests/test_gateway_inject_guidance.py`.
- Entity FREEZE (admin hibernation): `POST /{name}/loop/stop` accepts
  `{"mode": "freeze", "reason": ...}` — kills the loop process immediately
  via `life.hard_stop_loop` (no boundary wait, no ceremony, nothing changes
  in the memory graph), sets the entity state to `paused` (the door refuses
  visits until an admin wakes him), and records the `own_time_frozen`
  biography marker. Default mode stays `graceful` (durable boundary-honored
  command). The three verbs — freeze / sleep / graceful stop — are
  documented in `docs/guide/summoned-entities.md` §7.
- Entity own-time loop control is now command-based end to end (maintainer
  ruling 2026-07-08): `entity_loop.py` is a thin adapter over the runtime's
  life-loop surface — start via `spawn_loop_process`, stop via
  `request_loop_stop` (a durable command in the home's inbox, consumed at the
  loop's next boundary), status via `loop_process_status`. The gateway no
  longer writes STOP sentinel files or parses loop internals; the runtime
  owns the home's files (single-writer discipline). New host-marker kinds
  `own_time_started` / `own_time_stop_requested` are registered (they were
  silently dropped before — the marker try/except hid the kind-vocabulary
  gap). Tests: `tests/test_gateway_entities_loop.py`.
- Wide attention defaults for summoned-entity surfaces (maintainer ruling
  2026-07-08, after the live A/B on Castor): shelf_size 24 and
  context_window 65536 are now CODE defaults
  (`DEFAULT_ENTITY_CHAT_SHELF_SIZE` / `DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW`
  in `entity_chat.py`), resolved request-body > env
  (`ABSTRACTGATEWAY_ENTITY_CHAT_*`) > default on BOTH the chat door and the
  loop-start route. Previously the env vars were the only way to escape the
  20k floor and the 12-seat shelf ("only 6 memories" ceiling).
- Durable event delivery for `emit_event` commands (`payload.durable: true`):
  after resuming parked `WAIT_EVENT` listeners (unchanged), the runner now
  also appends the event envelope — with a per-run monotonic `seq` — to the
  `events_inbox` run var of every non-terminal run declaring the mailbox
  (`events_mailbox` var == event name; string or list of names). This closes
  the busy-listener drop: events posted while a run works are queued and
  drained at the run's next loop boundary (see
  `docs/guide/event-inbox-agent.md` at the repo root and the
  `event-inbox-react-agent` AbstractFlow example). Inbox capped at 500
  (drop-oldest, `events_inbox_dropped` counter). Same best-effort concurrency
  posture as `inject_guidance`. Tests:
  `tests/test_runner_emit_event_durable.py`.
- Added summoned-entity lifecycle (a2a 0004: the gateway owns entity lifecycle;
  no new package). Entity homes live under `<data_dir>/entities/<slug>/`
  (attested `spark.yaml` stored byte-verbatim, per-entity `memory.sqlite3`
  graph+journal, `home.sqlite3` diary book, gateway `manifest.json` with
  reserved key-id fields). Composes shipped AbstractMemory/AbstractRuntime
  pieces only — `lint_spark`/`engram`/`self_records`/`gradation`/
  `open_questions`, `DiaryStore`/`render_summon_prelude`/chain verifiers.
- Added `abstractgateway entity chat <name>` — the one-command summon
  (resolves the home from the registry and forwards to the runtime chat
  driver, so the two entry points cannot drift).
- Added `abstractgateway entity create|list|inspect|verify` CLI and
  `POST/GET /api/gateway/entities[...]` endpoints. There is deliberately no
  delete surface anywhere (never-purge is structural). Inspection surfaces
  the wake-reason triad (open questions / open problems / incubating ideas —
  the autonomy drivers the future heartbeat will read).
- Added `POST /api/gateway/entities/{name}/summon`: renders the identity
  prelude (pure read; a refused prelude aborts the summon with the reason
  verbatim — no truncated-header fallback) and starts the run with the
  reserved-seats posture (`self_fraction > 0`) stamped host-side.
- Added the identity floor + entity-elected hyperfocus rule to the deposit
  gate: no channel may summon below the engine-exported
  `SELF_FRACTION_FLOOR` (0.05); reductions below the posture default (0.5)
  are accepted only from the entity-reflection channel (the entity
  consciously electing hyperfocus); raising identity presence stays
  unrestricted. Seat rendering is guaranteed engine-side.
- Added the 20,000-token context floor for summons (maintainer ruling):
  declared windows below `ENTITY_CONTEXT_FLOOR` are refused; undeclared
  windows proceed with a labeled warning. Summons derive the session's
  recall budget from memory's context-scaled `entity_recall_budget` profile
  (posture applied) and carry it in the stamp for in-session injection.
- Added the entity deposit gate (`entity_gate.py`): actors derive from the
  CHANNEL (workplace / entity-reflection / operator), never from payloads.
  Summon stamps are HMAC-signed over (entity, channel, session, run id) and
  verified at the routing layer before any home opens; payload-claimed
  actors fail loudly; identity-kind writes, diary forgery via formation,
  self-scope belief revision, foreign scope ladders, and beyond-journal
  `as_of` anchors are rejected from workplace sessions; verified
  participants are door-stamped into recall and formation.
- Added the entity replay serving end (a2a 0005): `GET
  /api/gateway/entities/{name}/replay` (bounded NDJSON history read) and
  `GET /api/gateway/entities/{name}/replay/stream` (SSE live tail,
  `Last-Event-ID` resume) serve memory's frozen stream v1 merged with
  gateway host markers (`family="host"`: summon / prelude_refused /
  session_closed at fractional seq positions). Diary display blocks stay
  redacted for all HTTP audiences.
- Added sleep/wake/pause (a2a 0008): `abstractgateway entity
  sleep|wake|pause <name>` and `POST/GET
  /api/gateway/entities/{name}/state` write through the runtime's single
  state writer, host-mark every transition into the replay stream, refuse
  summons against non-awake entities (the no-summon window enforced by
  state), and run the dream pass inside `sleep --dream`. `entity
  list`/`inspect` surface the state; rest remains the entity's own
  election.
- Added verbatim-on-click: `GET
  /api/gateway/entities/{name}/records/{graph_id}/verbatim` serves the
  lossless exchange behind a memory record from the home's own artifact
  store. Pure read (no access recording); diary records refuse via
  multi-signal detection (kind, scope, private flag, entry_id) and a
  content backstop refuses any verbatim carrying an unstripped diary
  fence (the known formation-leak class). Identity records
  (values/purposes/traits) serve the attested spark text — the engram
  sets their `payload_ref` to the spark file, so their verbatim IS the
  seed document; other non-artifact refs answer with an honest 404
  instead of an unhandled traceback (live-incident fix). Born-digest
  kinds (interest, dream) answer 200 with the digest text and
  `born_digest: true` — their digest IS their complete text, so "the
  words you see are all the words there are" is an answer, not a 404.
- Added the operator diary door (maintainer ruling — reads are visible
  events): `GET /api/gateway/entities/{name}/diary/{entry_id}?reason=...`
  serves a book entry (private included) to the operator channel; the
  `reason` is required and every disclosure lands a `diary_read` host
  marker (entry id + reason) in the replay stream before the words
  return. The record-verbatim endpoint's structural diary refusal is
  unchanged.
- Unhandled exceptions now return JSON 500s through the middleware stack
  so CORS headers ride error responses — browsers previously rendered
  such failures as a generic "Failed to fetch", masking the real status.
- Added the identity card (a2a 0009, maintainer: "something to know our
  companion"): `GET /api/gateway/entities/{name}/card` and
  `abstractgateway entity card <name>` serve the memory engine's
  `entity_card` compositor (identity, age+context, current state as a
  window, likes/dislikes with G+/G− channels separate, open/resolved
  questions via the entity's own resolves convention, key moments,
  discoveries — every section carrying provenance; `?as_of=<seq>` anchors
  the whole card at a journal moment) plus the gateway overlays: manifest
  name/birth/age, operator state, mind substrate (newest substrate-stamped
  record), and host moments merged from the marker stream and the home's
  `state_history.jsonl` (door transitions deduped; anchored cards filter
  markers by seq and honestly omit the timestamp-only history ledger).
  The gateway's initial thin composition was replaced by the engine read
  the same day it shipped (one compositor; a second truth can drift).
- Added hosted entity chat (a2a 0007, the maintainer's chat drawer):
  `POST /api/gateway/entities/{name}/chat/open|.../turn|.../close` +
  `GET .../chat` host the runtime chat driver's `ChatSession` behind the
  operator-authed HTTP surface — the web chat and the `entity chat` CLI
  share one turn loop. Auto-yield of the entity's own-time loop mirrors
  `--pause-loop` (HTTP-bounded wait, loud 409 on timeout, stale
  auto-yield adopted with the wake duty); one live session per home
  (409 otherwise); paused/asleep entities refuse with the recorded
  reason; a refused prelude aborts the open verbatim; the close runs the
  reflection pass and wakes the loop; idle sessions are reaped by the
  next open. Turn responses carry `tools_ran` as a driver-authored data
  field (the marker-imitation lesson: never derived from reply prose).
  Visits land `summon`/`session_closed` host markers on the replay
  stream.
- Added `POST /api/gateway/entities/auth/probe` (the observer's
  control-strip gate): a deliberately WRITE-classed probe — dev postures
  can exempt loopback GETs from auth, so only a write-classed request
  answers "would the state/chat/diary doors accept me?". Returns the
  caller's principal when the write middleware accepts; controls stay
  hidden on 401/403.
- Operator-access ruling (maintainer, 2026-07-08): the operator sees
  everything. The record-verbatim endpoint's diary-shaped 403 and
  unstripped-diary-fence 403 are removed — diary-shaped records serve
  the book entry, host-marking a `diary_read` into the stream first
  (visibility kept, friction gone); the diary door's `reason` is now
  optional (defaults to "operator review"). The entity-adjacent
  boundary at the effect layer (deposit gate; workplace channels cannot
  read the book) is unchanged.
- The web chat open now runs the reflection-loss salvage (a2a 0007):
  if a previous session died unreflected (the driver's write-ahead
  `pending_reflection.json` marker), its look-back runs as the open's
  first act — over the ENDED session's own sheet, attributed to its own
  session id — and the response carries it as `salvage`. Version-
  tolerant (older drivers skip with a labeled warning); a salvage
  failure never blocks the new visit.
- Declared `pyyaml` as a direct dependency (previously transitive via
  `uvicorn[standard]`); the attested spark document makes it load-bearing.
- Added the shipped `dp-research@0.1.0.flow` WorkflowBundle: a `dp-` production
  research family with planning, an enforced investigate/review loop,
  three-lens adversarial review, structured audit outputs, and timestamped
  Markdown/PDF/DOCX export paths.

## [0.2.29] - 2026-08-27

### Added
- **`GET /api/gateway/host/state` — one-call host snapshot.** Memory, GPU,
  resident models, and session prompt caches, plus byte totals, in a single
  authenticated read. Every section is independently best-effort: a missing
  facade method or a failed probe nulls that section and names it in
  `degraded` (with a `reasons` map saying why) instead of failing the
  snapshot; the route never returns a 500. `totals.models_resident`
  (additive) counts only rows with `resident: true` so every client can show
  a truthful "N loaded" — `totals.models` counts every known row,
  configured / cached included, and must not be presented as "loaded".
- **`GET /api/gateway/host/metrics/memory`.** Host RAM/process/device memory
  snapshot relayed from the Runtime host facade, with the same
  `supported: false` degraded style as `GET /host/metrics/gpu`. The snapshot
  exposes both `process.rss_bytes` and `device.allocated_bytes`;
  `device.allocated_bytes` is the signal that verifies an in-process unload
  freed device memory, since freed buffers can keep process RSS unchanged.
- **Frozen `model_residency_row_v1` row schema.** `GET /models/loaded` now
  also returns `rows` — normalized records (`runtime_id`, `task`,
  `provider`, `model`, `source`, `resident`, `state`, `pinned`, `default`,
  `size_bytes`, `size_vram_bytes`, `expires_at`, `context_length`,
  `loaded_at`, `last_used_at`, `locked`, `lockable`, `modalities`,
  `calibrated_context_length`, `context_calibrated`, `host_id`, `host_name`,
  `details`) — and `row_schema`, alongside the unchanged raw `models`
  records. Residency truth is provider-first:
  `provider_resident`/`provider_loaded` outrank runtime lease booleans, state
  strings can confirm residency but never deny it, and unknown values stay
  `null`. The schema is additive-tolerant: fields beyond the original 16 are
  optional and `null` when the runtime does not report them. Rows and the
  `GET /host/state` snapshot (its optional top-level `host` block) carry a
  host identity as the aggregation seam for a proposed multi-machine model
  resource pool
  ([backlog 0093](docs/backlog/proposed/0093_multi_machine_model_resource_pool.md)).
- **Model residency locks.** Admin-only `POST /api/gateway/models/lock` and
  `POST /api/gateway/models/unlock` pin a resident model against unload and
  release that pin, selecting the target like unload does (`runtime_id` or
  `provider`+`model`). Lock requires provider-verified residency: a
  configured or merely-warm model refuses with an
  `error: "model_not_resident"` payload (load with `lock: true` instead),
  and unlock always works — even for a since-evicted model — so locks are
  never stranded. `POST /models/unload` answers **HTTP 409** with
  the normalized `model_locked` refusal payload when the target is locked,
  and the unload request gains `"force": true` to unload anyway; every other
  unload outcome stays in-band at 200. Rows report `locked`/`lockable` so
  clients can render lock state and offer the right verb.
- **`GET /api/gateway/models/context_estimate`.** Context/KV memory estimate
  for a `provider`+`model` (optional `context_length` >= 1), relayed from the
  Runtime host facade with in-band `confidence` (`calibrated` | `estimated` |
  `unknown`) and fields such as `predicted_max_context` (the context that
  fits beside the weights), the tri-state `fits_weights` /
  `fits_requested_context` split, and `budget_bytes` (real-ceiling budget;
  basis and reserve stated in `notes`). Advisory only — no load path gates
  on it. Available to any
  authenticated principal; degrades at 200 with
  `code="context_estimate_unavailable"`/`"context_estimate_error"` like the
  other host relays.
- **A Resources surface in both consoles.** The web console gains a
  `Resources` tab and the console-TUI a `Resources` screen (8): memory/GPU
  meters with
  degradation notes, the resident-model table (modality chips/labels from
  the shared `modality_ui` palette, tri-state residency, lock state, context
  facts with calibration), and session prompt caches with per-session clear.
  The web table defaults to provider-verified RESIDENT rows only — the
  section header counts resident rows, and configured / cached rows
  (labeled "configured — not in memory", Estimate only, no Unload/Lock)
  appear behind a "Show configured / cached (N)" toggle; the TUI totals line
  counts resident rows apart from the row total. Default ≠ loaded: a
  configured capability default is never presented as loaded.
  Admins additionally get warm-up (with an optional lock-after-load and a
  live context-estimate hint), lock/unlock, and unload — a locked model's
  409 refusal triggers an explicit force-unload confirmation instead of a
  dead end. Reads render for every authenticated user; mutation controls are
  admin-gated. The web tab polls `/host/state` every 5s while active
  (stale responses are discarded), the TUI every 4s while the screen is
  active.
- **Session prompt-cache enumeration lane.**
  `GET /api/gateway/sessions/prompt_cache?session_id=` lists the prompt
  caches the runtime actually minted, with session/run/workflow/node
  attribution, and admin-only
  `POST /api/gateway/sessions/{session_id}/prompt_cache/clear_all` unloads
  every cache for a session in one call. This lane is recommended over the
  identity-derived per-session lifecycle endpoints, which are unchanged.
- **Discovery contract additions.** `capabilities.contracts.common` gains
  `host_state` and `session_caches` descriptors, and the `model_residency`
  descriptor now names its `row_schema`, lists the `lock`/`unlock`/
  `context_estimate` endpoints, and carries `modality_ui` — the canonical
  modality color map (`{version: 1, colors: {...}}`, one `{color, label}`
  entry per residency task plus an `unknown` fallback) every residency
  client renders with instead of hardcoding its own palette. `modality_ui`
  is a rendering contract and is served even when the runtime facade is
  absent.

### Changed
- **Host and residency reads are user-level.** `GET /models/loaded`,
  `GET /models/context_estimate`, `GET /host/state`, `GET /host/metrics/*`,
  and `GET /sessions/prompt_cache` serve any authenticated principal.
  Mutations — `POST /models/load|unload|lock|unlock|download` and every
  prompt-cache mutation, including the new `clear_all` — remain admin-only,
  and anonymous requests are still rejected.
- Raised the AbstractRuntime dependency floor to `AbstractRuntime>=0.4.31`
  across the base, `apple`, and `gpu` profiles; that release provides the
  host facade methods (memory snapshot, session-cache enumeration) these
  endpoints relay.

## [0.2.28] - 2026-06-14

### Changed
- Raised the Gateway dependency floors to `AbstractRuntime>=0.4.29`, `abstractagent>=0.3.12`, and `abstractcore[embeddings]>=2.13.38` across the base and hardware profiles so published installs consume the released Runtime/Core/Agent contract from this wave.
- Release packaging now ships only the supported Gateway bundles `basic-agent.flow` and `abstractassistant-orchestrator@0.0.0.flow`; local draft bundles under `flows/bundles/` are ignored by default and no longer ride along into sdists, wheels, or Docker source copies.

## [0.2.27] - 2026-06-06

### Added
- Added `POST /api/gateway/runs/{run_id}/images/upscale`, backed by Runtime's durable `AbstractCoreRunFacade.upscale_image(...)` child-run path.
- Added `upscaled_image` media capability/readiness contract entries and `task=image_upscale` Vision provider-model discovery.
- Added `GET /api/gateway/vision/adapters`, backed by Runtime's public discovery facade, so thin clients can query compatible installed adapters for image/video tasks.
- Direct image/video routes now return plural artifact fields (`image_artifacts`, `video_artifacts`) for batch generation while preserving the existing singular compatibility fields.

### Changed
- Raised the Runtime floor to `AbstractRuntime>=0.4.28` across Gateway base, Apple, and GPU profiles so Gateway installs always include the Runtime `read_pdf` / `write_pdf` nodes and their permissive `pypdf` / `reportlab` dependencies.
- Forwarded newer Runtime/Core/Vision request controls such as image/video batch `count` / `n`, `seeds`, ordered `lora_adapters`, video `flow_shift`, and image-upscaler parameters through Gateway direct media routes.
- Raised the `abstractcore[embeddings]` optional profile floor to `>=2.13.37`, matching Runtime's Core floor used by the base, Apple, and GPU Gateway profiles.

### Fixed
- Added Gateway bundle execution coverage for writing a real PDF artifact, reading it back through Runtime's PDF node, and exposing the extracted text through `On Flow End`.
- Bundle-mode VisualFlow execution preserves Runtime structured LLM `data` outputs through data edges and Break Object while leaving `response` as text.
- Bundle-mode structured LLM outputs can now drive `Answer User` and `Switch` nodes through `Break Object` without dropping the parsed data payload.
- Gateway now reuses Runtime's published workspace-path and file-filter helpers, and the published package/HTTP app versions are aligned to `0.2.27` while the base/Apple/GPU dependency floor for `abstractagent` stays on the latest PyPI release line.
- Gateway provider/model resolution now falls back to the service store base directory when embedded hosts expose stores without a full host config object, keeping backlog-assist and other hosted endpoints usable in lightweight service contexts.

## [0.2.26] - 2026-06-03

### Added
- `abstractgateway serve` now auto-ensures the `default/admin` Gateway user and writes the bootstrap browser-login token when user auth is enabled, matching the Docker first-run path for native pip installs.
- Added runtime-scoped Core config storage for Gateway capability defaults:
  Gateway baseline defaults live in `<ABSTRACTGATEWAY_DATA_DIR>/config/abstractcore.json`
  and user runtime overrides live in
  `<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant>/<runtime>/runtime/config/abstractcore.json`.

### Changed
- Gateway Console now presents provider endpoint profiles as provider connections for OpenAI, Anthropic, OpenRouter, Portkey, LM Studio, Ollama, and custom OpenAI-compatible endpoints, with clearer endpoint/key hints and model discovery.
- Gateway configuration docs now distinguish browser user tokens from the legacy server/operator `ABSTRACTGATEWAY_AUTH_TOKEN`.

### Removed
- BREAKING: removed legacy Gateway `config/capability_defaults.json` overlay support. Gateway capability defaults now use only scoped Core config files (`config/abstractcore.json`). Existing overlay files are ignored; recreate those defaults with `abstractgateway-config set-default ...`.

## [0.2.25] - 2026-05-31

### Changed
- Set Gateway container defaults for host-native LM Studio and Ollama endpoints so named provider discovery does not default to `localhost` inside the container.
- Updated Docker deployment docs to use `LMSTUDIO_BASE_URL` for LM Studio and `OPENAI_BASE_URL` for generic OpenAI-compatible endpoints.

### Fixed
- Fixed Gateway Console capability-default model discovery so the Base URL field is forwarded to the provider model catalog before saving.
- Fixed Docker Compose/OpenAI-compatible documentation drift where `OPENAI_COMPATIBLE_BASE_URL` was shown as the primary AbstractCore discovery variable even though AbstractCore uses `OPENAI_BASE_URL`.

## [0.2.24] - 2026-05-31

### Added
- Added `abstractgateway-config bootstrap-admin` to create or recover a file-backed `default/admin` Gateway user for hosted/container user-auth deployments.
- Added a Gateway Docker entrypoint that bootstraps the admin user token into `/data/auth/bootstrap-admin-token` before starting the server.
- Added first-class GHCR tags for `ghcr.io/lpalbou/abstractgateway:<version>`, `latest`, `<version>-gpu`, and `gpu-latest`, while preserving the legacy `abstractgateway-server` tags during transition.

### Changed
- Gateway Docker and Compose defaults now use `/data`, enable hosted user auth, and build release images from the just-published PyPI wheel instead of local source.
- Gateway startup now accepts hosted user-auth deployments without the legacy shared `ABSTRACTGATEWAY_AUTH_TOKEN`.

### Fixed
- Fixed the PyPI/GHCR release path so container images can start cleanly from the published Gateway wheel and still provide an initial admin login token.

## [0.2.23] - 2026-05-31

### Fixed
- Fixed local-source Gateway container builds so the packaged `basic-agent` workflow bundle is present when Hatch builds the wheel inside the release image.

## [0.2.22] - 2026-05-31

### Added
- Added hosted user-principal auth with `GET /api/gateway/me`, admin-only `/api/gateway/admin/users` CRUD, and a file-backed user registry storing bearer-token hashes.
- Added request-scoped Gateway service routing so hosted user-auth mode maps each principal to a separate GatewayService data plane under `<DATA_DIR>/users/<tenant_id>/<runtime_id>/`.
- Added the built-in Gateway Console at `/console` for browser-session sign-in, account/runtime summary, admin user management, token rotation, and per-principal capability default editing.
- Added per-principal capability-default overlays in hosted user-auth mode so users can set provider/model defaults for their own runtime without mutating the global AbstractCore config.
- Added provider endpoint profiles for Gateway-stored OpenAI-compatible or hosted endpoints. Profiles keep API keys server-side, discover endpoint models on demand, and surface as virtual providers in Gateway defaults and Flow node selectors.

### Changed
- Raised dependency floors to `AbstractRuntime>=0.4.26`, `abstractagent>=0.3.10`, and `abstractcore[embeddings]>=2.13.31` so Gateway installs inherit the latest light-profile, media, and provider-profile contracts.

### Fixed
- Fixed the Gateway Console sign-in page so generated inline JavaScript parses correctly, the sign-in form posts to `/api/gateway/session/login`, and signed-out users see only the same-origin Gateway user/token login card.
- Made `abstractgateway.security` export session and middleware helpers lazily so direct `abstractgateway.users` imports are not order-sensitive.
- Kept the base `pip install abstractgateway` remote-light on Linux while relying on the base `AbstractRuntime` install for MCP and remote multimodal routing. Local sentence-transformer embeddings moved behind `abstractgateway[embeddings]`, and Gateway no longer declares direct base `sentence-transformers` or `numpy` dependencies, avoiding PyTorch/NVIDIA CUDA runtime wheels unless an explicit local-engine profile is selected.
- Kept remote/provider-backed embeddings in the base light profile through `embedding.text` routes and remote AbstractCore delegation, while surfacing embedding setup errors instead of reporting a generic missing integration.
- Gateway admin user routes now fail closed when request principal context is absent while Gateway security is enabled.
- Gateway route-family authorization now keeps operator/admin surfaces and server-workspace file helpers admin-only in hosted user-auth mode while regular users remain able to operate within their own runtime data plane.

## [0.2.21] - 2026-05-29

### Added
- Gateway artifact search/import/export endpoints for thin clients, including scoped artifact lookup by run, session, or all stored artifacts with modality, content type, text, and tag filters.
- Capability discovery now advertises artifact search, workspace import, and workspace export descriptors in the shared thin-client contract.

### Changed

- Removed legacy compatibility install extras (`abstractgateway[http]`, `[server]`, `[multimodal]`, `[memory]`, `[voice]`, `[vision]`, `[telegram]`, `[visualflow]`, `[all]`, `[all-apple]`, `[all-gpu]`, `[server-nvidia]`). The supported install surface is now:
  - `pip install abstractgateway`
  - `pip install "abstractgateway[apple]"`
  - `pip install "abstractgateway[gpu]"`
- Raised dependency floors to `AbstractRuntime[multimodal,mcp-worker]>=0.4.25` and `abstractagent>=0.3.9`.
- KG memory readiness now treats a resolvable fresh persistent AbstractMemory store as available, so empty stores return empty query results instead of hiding Flow authoring surfaces.

### Fixed
- Media model-residency discovery now keeps image editing distinct from image generation when Runtime/Core expose task-specific residency state.

## [0.2.20] - 2026-05-26

### Added
- Direct Runtime-backed video generation routes:
  - `POST /api/gateway/runs/{run_id}/videos/generate` for text-to-video
  - `POST /api/gateway/runs/{run_id}/videos/from_image` for image-to-video
- Thin-client capability contracts and readiness metadata now advertise `generated_video` and `image_to_video`, including `provider_models_task` values and `abstract.progress` child-run progress events.
- Model-residency capability reporting now includes video tasks (`text_to_video`, `image_to_video`, and `video_generation`) when Runtime/Core expose them.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.24`.
- Gateway documentation now describes direct video routes, video provider/model catalog tasks, and progress-event handling for long-running media jobs.

## [0.2.19] - 2026-05-26

### Added
- Gateway capability-default routing and configuration helpers so downstream thin clients can discover provider/model defaults without hardcoded fallbacks.
- Run-retention cleanup support for draft and ephemeral Flow runs.

### Changed
- Raised dependency floors to `AbstractRuntime[multimodal,mcp-worker]>=0.4.23` and `abstractagent>=0.3.8`.
- Refined Gateway model-residency and catalog proxy responses around Runtime/Core discovery truth, including the latest MLX-Gen vision and OmniVoice catalog surfaces.
- Refreshed Docker and deployment docs for the new release image tags.

### Fixed
- Removed brittle catalog payload assertions by normalizing Gateway-owned catalog envelopes at the route boundary.

## [0.2.18] - 2026-05-23

### Added
- Catalog and provider discovery routes now include a stable Gateway-owned envelope (`catalog.contract=gateway_catalog_v1`, `catalog.version=1`) plus one canonical `items` array, while preserving legacy lower-layer fields for compatibility.
- Capability discovery now also exposes `common.readiness` (`gateway_surface_readiness_v1`): a compact surface-level summary derived from endpoint descriptors, memory readiness, prompt-cache, media gates, and Runtime/Core truth.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.22`.
- Removed VisualFlow directory mode and fully removed the `abstractflow` package dependency from Gateway. VisualFlow JSON is stored/published via Gateway endpoints and executed as `.flow` WorkflowBundles (bundle mode).

## [0.2.17] - 2026-05-22

### Added
- Gateway now exposes Runtime-backed image editing for thin clients through `POST /api/gateway/runs/{run_id}/images/edit`.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.21`.
- Gateway capability discovery and thin-client contracts now advertise edited-image and generated-music availability, richer voice `tts|stt|listen` contracts, and Runtime-backed model residency truth instead of hard-coded media support flags.
- Direct STT now forwards `prompt`, `response_format`, `temperature`, and source `format` hints through the Runtime transcription surface.
- Release-facing docs now describe the current higher-app surface more precisely, including the stable route/contract layer and the current best-effort catalog payload limitation.

## [0.2.16] - 2026-05-21

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.20` across the base, Apple, and GPU install profiles.
- Gateway's legacy prompt-cache snapshot aliases, `GET /api/gateway/prompt_cache/saved` and `POST /api/gateway/prompt_cache/save|load`, now delegate to Runtime's public host facade instead of using provider-private prompt-cache state directly.
- Local bundle runtimes now keep host-local prompt-cache exports under `<DATA_DIR>/prompt_cache_exports` through Runtime's export root policy.

### Fixed
- Removed the last Gateway-side prompt-cache boundary bypass (`runtime._abstractcore_llm_client`, direct provider-instance access, and provider-private `_prompt_cache_store` / GGUF cache hooks) from the public route surface.
- Removed the stale internal Core catalog proxy module after discovery routing fully moved to Runtime's public discovery facade.

## [0.2.15] - 2026-05-21

### Added
- Added Runtime-backed durable bloc prompt-cache control-plane routes under `/api/gateway/blocs/*`, including KV manifest/list/ensure/load/delete/prune helpers for exact-reuse workflows.
- Added Gateway-owned workspace file helper support plus focused route and contract coverage for durable blocs, model residency, notifier behavior, and Runtime-backed capability discovery.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.19` and moved Gateway's public provider/media/tool boundary behind Runtime facades rather than direct package imports.
- Updated Apple/GPU install profiles to cascade through Runtime's aggregate extras and excluded internal `tests/`, `flows/`, and backlog notes from source distributions.
- Expanded the docs and capability contract to cover durable blocs, media/model residency, Runtime-backed email/Telegram helpers, and the current Docker/runtime dependency shape.

### Fixed
- Gateway no longer reads AbstractCore config for LLM helper defaults; provider/model resolution now follows request values, Gateway env, and flow defaults with a clear config error when unset.
- Gateway's operator email, Telegram, and notification paths now use Runtime's AbstractCore host facades, while local file/workspace helpers stay owned by Gateway.
- Capability discovery and prompt-cache readiness reporting now better reflect the actual state of generated-media, voice/audio, and provider-backed cache controls.

## [0.2.14] - 2026-05-19

### Fixed
- Gateway now carries explicit modern OpenAI/httpx/anyio dependency bounds in its base install metadata, preventing Python 3.10 resolver backtracking while preserving the Apple/GPU profile cascade into `[all-apple]` and `[all-gpu]` framework dependencies.

### Changed
- Raised the Runtime floor to `AbstractRuntime>=0.4.14` so Gateway profiles consume Runtime's resolver bounds for AbstractCore provider/tool extras.

## [0.2.13] - 2026-05-19

### Fixed
- Gateway's base install now avoids mixing Core's narrow base media/embeddings extras with Core `[all-apple]` and `[all-gpu]` profile dependencies, while still installing the media, compression, and embeddings dependency set needed by the remote-capable base package.
- Gateway's base media dependency set now uses a Python-3.10-compatible `unstructured` line and bounds `python-pptx` to supported modern releases so document-capable installs do not backtrack into broken legacy setup packages.
- Gateway's base web dependency set now prefers current compatible FastAPI/Uvicorn/Requests/urllib3 releases to keep CI and user installs out of unnecessary resolver backtracking.
- Gateway now applies a compatible setuptools lower bound so Apple/GPU installs satisfy Torch's `<82` constraint without resolving into ancient broken setuptools releases.

### Changed
- Raised the Runtime floor to `AbstractRuntime>=0.4.13` so Gateway profiles consume Runtime's updated multimodal dependency metadata, and raised the Music floor to `abstractmusic>=0.1.2`.

## [0.2.12] - 2026-05-19

### Fixed
- Gateway Apple install profiles now preserve the entrypoint contract by cascading `[all-apple]` through Runtime, Agent, Core, Vision, Voice, Music, and Memory dependencies; GPU profiles continue to cascade `[all-gpu]`.

### Changed
- Gateway's base remote-capable install now includes Core embeddings dependencies alongside remote providers, media, tools, tokens, compression, voice/audio, and vision while preserving the published Core dependency floor.

## [0.2.11] - 2026-05-19

### Fixed
- Gateway voice, TTS, STT, and vision catalog routes now use the AbstractCore capability abstractions as the source of truth for provider and provider-model discovery.
- Direct Gateway TTS and STT routes now dispatch through the AbstractCore capability registry, preserving explicitly selected media providers and models through execution.
- Gateway LLM provider/model discovery can proxy configured AbstractCore Server catalog routes while keeping Flow's existing response contract.

### Changed
- Raised dependency floors to Runtime `>=0.4.12`, Core `>=2.13.15`, Flow `>=0.3.11`, Vision `>=0.3.6`, and Voice `>=0.10.3`.

## [0.2.10] - 2026-05-13

### Fixed
- Gateway capability discovery now builds its embedded capability registry with Gateway-scoped media configuration, keeping discovery contracts aligned with the concrete voice, TTS, STT, and image catalog routes.
- Gateway media catalog proxy calls now avoid forwarding unset optional query params, preventing stale `None` values from breaking downstream capability discovery.

### Changed
- Raised dependency floors to Runtime `>=0.4.11`, Core `>=2.13.14`, Flow `>=0.3.11`, Vision `>=0.3.5`, and Voice `>=0.9.4`.


## [0.2.9] - 2026-05-12

### Added
- Gateway discovery now advertises `/api/gateway/audio/transcriptions/models` for STT catalog lookup.
- Added local and proxied STT model catalog responses backed by AbstractCore/AbstractVoice.

### Fixed
- Gateway capability catalogs now map Gateway-scoped voice and vision env vars into the embedded capability registry, so local Gateway deployments expose configured voice/TTS/STT/image models without requiring duplicate lower-level env names.
- Catalog proxy calls now omit unset optional query params instead of forwarding `None` values.

### Changed
- Raised dependency floors to Runtime `>=0.4.10`, Core `>=2.13.13`, Flow `>=0.3.10`, and Voice `>=0.9.3`.

## [0.2.8] - 2026-05-10

### Added

- Capability discovery now advertises
  `capabilities.contracts.common.runs.input_data` and
  `capabilities.contracts.common.runs.history_bundle` so thin clients can
  feature-detect the run input and RunHistoryBundle endpoints from the shared
  Gateway contract.

## [0.2.7] - 2026-05-10

### Updated

- Bumped abstractagent floor to >=0.3.6 to match the new abstractagent release that requires abstractruntime>=0.4.9.

## [0.2.6] - 2026-05-09

### Fixed

- Raised the AbstractVision floor to `abstractvision>=0.3.4` across Gateway
  install profiles so `abstractgateway[gpu]` and the NVIDIA image inherit the
  stable-diffusion.cpp binding constraint that avoids the broken
  `stable-diffusion-cpp-python==0.4.6` Linux sdist.
- Updated release-facing Docker examples and package metadata from `0.2.5` to
  `0.2.6`.
- Release/CI installs now bypass the restored pip dependency cache for editable
  dependency resolution, avoiding stale package indexes immediately after
  lower-package releases.

## [0.2.5] - 2026-05-09

### Changed

- Promoted the base `abstractgateway` install to the remote-light HTTP/SSE
  server profile. It now includes Runtime multimodal support, AbstractAgent,
  AbstractCore remote/media/tools/tokens/compression/vision/voice/audio,
  AbstractVision, AbstractVoice, AbstractFlow compatibility,
  AbstractMemory/LanceDB KG support, FastAPI, multipart uploads, and Uvicorn.
- Raised Runtime and Agent floors to `AbstractRuntime>=0.4.9` and
  `abstractagent>=0.3.6`.
- Simplified install guidance around `abstractgateway`, `abstractgateway[apple]`,
  and `abstractgateway[gpu]`. The older `http`, `server`, `multimodal`,
  `memory`, `voice`, `vision`, `all`, and `server-nvidia` extras remain as
  compatibility aliases.
- The NVIDIA Docker image now installs `abstractgateway[gpu]`; `server-nvidia`
  remains only as a compatibility alias.

## [0.2.4] - 2026-05-08

### Added

- Explicit install profiles for the Gateway package: minimal base,
  `http`, `multimodal`, `server`, `memory`, `apple`, `gpu`, `all-apple`,
  `all-gpu`, and `server-nvidia`.
- `abstractgateway-config` plus `abstractgateway config` for operator status and
  private `.env` bootstrap without taking ownership of AbstractCore provider
  configuration.
- Gateway memory store resolver for AbstractMemory-backed LanceDB, SQLite, and
  in-memory stores, including `/kg/query` store metadata.
- Core catalog proxy endpoints for thin clients:
  `GET /api/gateway/voice/voices`,
  `GET /api/gateway/audio/speech/models`, and
  `GET /api/gateway/vision/provider_models`.
- Added a `server-nvidia` extra plus an experimental CUDA/PyTorch-based
  `abstractgateway-server-nvidia` Docker image recipe for full NVIDIA machines.
- Release and manual GHCR image workflows now publish the light default server
  image and attempt an experimental best-effort NVIDIA full image.

### Changed

- Base installs are now intentionally minimal again:
  `AbstractRuntime>=0.4.8` only.
- Server and multimodal profiles now use the aligned Runtime/Core/Voice/Vision
  floors: `AbstractRuntime>=0.4.8`, `abstractcore>=2.13.12`,
  `abstractvision>=0.3.3`, and `abstractvoice>=0.9.2`.
- Server, native Apple, native GPU, and NVIDIA profiles now require
  `abstractagent>=0.3.5`, so Gateway-hosted agent nodes resolve against the
  same Core/Runtime baseline as Gateway itself.
- Release tests now reset Gateway's process-global service between cases and
  pass explicit provider/model overrides for ledger summary/chat generation
  tests.
- Native Python hardware profiles are full deployment aggregates:
  `abstractgateway[apple]` and `abstractgateway[all-apple]` install the
  Apple-local stack and all relevant non-NVIDIA framework capabilities, while
  `abstractgateway[gpu]` and `abstractgateway[all-gpu]` install the matching
  local GPU stack.
- Gateway-owned runtime handoff now seeds `_runtime.prompt_cache`,
  `_runtime.max_attachment_bytes`, and `_runtime.workflow_bundles_dir` from
  Gateway configuration.
- Gateway LLM helper defaults now resolve through the same deployment cascade as
  runtime execution instead of hardcoded local model fallbacks.
- Docker Compose local builds can override `ABSTRACTGATEWAY_EXTRAS`; the
  default examples use port `8080`, and an NVIDIA compose overlay is available
  for GPU hosts.
- The default Docker server image now composes `abstractgateway[server,memory]`
  so KG workflows and `/kg/query` have the AbstractMemory/LanceDB store package
  available without making memory a base-package dependency.
- The `memory` profile now depends on `AbstractMemory[lancedb]>=0.2.6`.

### Fixed

- `memory_kg_*` effects and `/kg/query` no longer assume LanceDB directly;
  in-memory stores work, SQLite structured queries work when the installed
  AbstractMemory build exposes `SQLiteTripleStore`, and semantic queries fail
  clearly when the selected store has no vector/search capability.
- Dynamic voice/audio/vision catalog discovery now delegates to the AbstractCore
  server catalog boundary when configured, with bounded static fallback when it
  is not.
- Observer/chat/backlog/discovery helpers now return a clear provider/model
  configuration error when no request, Gateway env, or AbstractCore default is
  available.

### Notes

- The default Docker image remains the release-grade light, portable image for
  `linux/amd64` and `linux/arm64`. The NVIDIA image is `linux/amd64` only and
  is experimental/best-effort because vLLM/Torch/Diffusers dependency
  resolution is much heavier than the default server profile and still needs a
  CUDA host smoke gate before production positioning.
- There is no practical MLX Docker image target for Apple Silicon today: MLX
  depends on Apple's Metal stack and Docker Desktop runs Linux containers
  without Metal/MPS device access. Apple local inference should stay native on
  macOS, not containerized; the Gateway container can point at Docker Model
  Runner, native LM Studio, `mlx_lm.server`, or Ollama OpenAI-compatible
  endpoints via `model-runner.docker.internal` or `host.docker.internal`.

## [0.2.3] - 2026-05-08

### Added

- Versioned thin-client capability contracts for Gateway common features, AbstractFlow editor/runtime support, AbstractAssistant media/cache controls, and AbstractCode-facing prompt-cache controls.
- AbstractFlow gateway-first editor contract validation, including VisualFlow CRUD/publish/start/observe coverage and a bundled flow input-schema endpoint.
- Gateway-owned session prompt-cache lifecycle routes:
  - `GET /api/gateway/sessions/{session_id}/prompt_cache/status`
  - `POST /api/gateway/sessions/{session_id}/prompt_cache/prepare`
  - `POST /api/gateway/sessions/{session_id}/prompt_cache/rebuild`
  - `POST /api/gateway/sessions/{session_id}/prompt_cache/clear`
- Generated-media contract fields in capability discovery, including direct-vs-workflow generated-image availability.
- Direct generated-image route, `POST /api/gateway/runs/{run_id}/images/generate`, backed by Runtime/Core image output selectors, artifact storage, and `abstract.media.image.generated` ledger events.
- Backlog completion ledger for the capability contract, Flow editor contract, session prompt-cache lifecycle, and generated-media gateway contract.

### Changed

- Capability discovery now truthfully reports provider-level and session-level prompt-cache controls, plus direct Gateway voice/audio/image endpoints where configured.
- API, configuration, deployment, Docker, README, FAQ, and LLM ingestion docs now describe generated images as both workflow-backed and directly available through the Gateway route when a Runtime/Core image backend is installed and configured.
- Docker/Compose release examples now point at the `0.2.3` server image.

### Fixed

- Fixed stale release-facing docs that said Gateway had no direct image-generation endpoint after the direct route landed.
- Fixed an order-dependent test import leak so the full local pytest suite can run cleanly after the AbstractFlow editor contract tests.

### Notes

- Direct image generation still depends on a configured Runtime/Core/AbstractVision-compatible backend; Gateway does not bundle heavy local image engines.
- Session prompt-cache lifecycle is Gateway-owned naming and orchestration over provider/model controls. It is not a provider-independent local KV cache or full CachedSession persistence system.

## [0.2.2] - 2026-05-06

### Added

- MkDocs Material configuration for the documentation site.
- CI docs build job and release docs gate.
- Release workflow deployment to GitHub Pages via `mkdocs gh-deploy`.
- PyPI-backed GHCR server image publishing for `ghcr.io/lpalbou/abstractgateway-server`.
- CI validation build for the local server Docker image recipe.
- Docker server image, Compose profile, and deployment documentation.
- `docs`, `server`, `vision`, and `multimodal` optional dependency extras.
- Discovery metadata for AbstractCore capability plugins (`voice`, `audio`, `vision`, and future `music`).

### Changed

- Version metadata aligned across `pyproject.toml`, package `__version__`, and FastAPI app metadata.
- The server install profile now mirrors the newer AbstractRuntime/Core multimodal stack: `AbstractRuntime[multimodal]>=0.4.6`, `abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]>=2.13.10`, `abstractvision>=0.3.1`, and `abstractvoice>=0.9.0`.
- The server Docker/Compose profile now documents workflow-backed image generation through AbstractVision, direct Gateway TTS/STT through AbstractVoice, and provider-dependent prompt-cache controls.
- Gateway voice/audio endpoints now accept AbstractVoice's newer local/remote backend environment knobs in addition to the existing Gateway-scoped settings.

### Notes

- Release scope is intentionally explicit: TTS and STT have direct Gateway endpoints; generated images are available through Runtime/Core workflows with AbstractVision installed and configured, but Gateway does not yet expose a direct image-generation HTTP endpoint.
- Prompt-cache support is provider-level control-plane support. This release does not add a Gateway-owned CachedSession lifecycle API.
- `flows/bundles/article@dev.flow` was inspected and left untracked. It is a local `dev` bundle generated by the Gateway publisher, not a release artifact.

## [0.2.1] - 2026-02-09

### Changed

- Dependency bumps (see `pyproject.toml`):
  - `AbstractRuntime>=0.4.2` (and `AbstractRuntime[abstractcore]>=0.4.2` for HTTP/voice/telegram/all extras)
  - `abstractagent>=0.3.1`, `abstractvoice>=0.6.3`, `abstractflow>=0.3.7`
  - `abstractcore[media,tools]>=2.11.8` (via `abstractgateway[all]`)
- Documentation refresh for external users:
  - added explicit AbstractFramework ecosystem context
  - updated minimum versions in install snippets to match `pyproject.toml`
  - kept the architecture diagram as the canonical “shape of the system”
- Version metadata alignment:
  - `pyproject.toml`, `src/abstractgateway/__init__.py`, and `src/abstractgateway/app.py` now agree on `0.2.1`

## [0.1.1] - 2026-02-04

### Changed

- Documentation refresh for external users:
  - new FAQ (`docs/faq.md`)
  - clarified quickstart + smoke checks in `README.md`
  - tightened getting started, configuration, security, and API overview docs
  - improved cross-linking in `CONTRIBUTING.md` and `SECURITY.md`
  - refreshed `llms.txt` / `llms-full.txt` for agent ingestion (index + full snapshot)
- Version bump to reflect the documentation release (`0.1.0` → `0.1.1`).

### Notes

- No intentional runtime behavior changes in this release; it is documentation-focused.

## [0.1.0] - 2026-02-03

### Added

- Initial public package for AbstractGateway (`abstractgateway`).
