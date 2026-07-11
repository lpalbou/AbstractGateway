# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
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
