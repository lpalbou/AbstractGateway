# LAUNCH PROMPT — abstractgateway-console

You are the builder session for `abstractgateway-console`: an intuitive
terminal wizard to configure AbstractGateway, "similar to gateway/console
but improved" (the maintainer's words). It lives inside the
`abstractgateway` Python package (this directory) so the package serves
both the web UI and the CLI for configuring a gateway. You build it on
**AbstractTUI** (`abstracttui = "0.2.8"`, crates.io) against the
gateway's **existing** admin HTTP API — zero gateway-side changes.

This is also the second **validator app** for the engine (epic:
`/Users/albou/tmp/abstractframework/abstracttui/docs/backlog/planned/ports/0215_gateway_config_wizard_app.md`
— read it). Nearly all engine field evidence so far comes from one
chat/composer app (abstractcode-tui); this app exercises the
form/wizard/select/table class instead. **Filing engine findings is as
much the mission as shipping the app** — see §7.

The scaffold in this directory compiles and runs (`cargo build`, `q`
quits, headless exit 0). Replace `src/main.rs` with the real app;
`README.md` here describes the repo layout.

---

## 1. What to build

A keyboard-first wizard that walks an operator through configuring a
gateway, plus a browse/edit surface for the same data. Wizard-first
order:

1. **Connection** — base URL + admin token, probe, honest state.
2. **Providers** — see what's available, add/edit provider endpoint
   profiles (base URL + API key), discover models, test with a real
   generation.
3. **Multimodal capability routes** — the default-vs-override table
   (`input.text`, `output.voice`, …): set provider/model per route,
   clear back to engine defaults.
4. **Users & entities basics** — list/create/edit gateway users
   (token shown once), list entities with their state.
5. **Review + apply** — what changed, verify via GET after writes.

The reference UX is the served web console (`GET /console` on a running
gateway — five tabs: Users & Entities, Runtimes, Providers, Multimodal
["defaults"], Sandbox): wide tables with state badges
(configured / covered / not-configured; enabled / asleep) and per-row
actions (Edit / Clear / Override / Configure). "Improved" means: a
guided first-run path (the web console is browse-only), honest
resolution lines everywhere, and no fabricated selections (§4).

A `--wizard` first-run flow and a tabbed browse mode can share every
screen component; how you factor that is yours. Start with connection +
providers + one capability route end-to-end (the epic's validation
path), then widen.

## 2. Gateway API grounding (studied from source, 2026-07-23)

Everything below was read from
`abstractgateway/src/abstractgateway/routes/gateway.py` (router prefix
`/gateway`, mounted at `/api` → base path **`/api/gateway`**) and
`routes/entities.py` (base path **`/api/gateway/entities`**). Server:
FastAPI/uvicorn, default bind 0.0.0.0:8080 — the exact dev command you
can actually run is at the end of §2.1. The web console is one served
HTML page at `/console` — open it beside your TUI to compare semantics.

### 2.1 Connection + auth

- All requests: `Authorization: Bearer <token>`. Two token kinds, one
  identity: the static operator token (env `ABSTRACTGATEWAY_AUTH_TOKEN`
  on the server) and user-registry tokens (`agw_…`, minted by user
  create). Both resolve to the admin principal world (tenant `default`,
  user `admin`) — principal.py documents this deliberately. Session
  cookies exist for the browser console; the TUI uses bearer only.
- **Probe**: `GET /api/gateway/ping` → `{ok, status:"healthy", service,
  time}`. Its docstring is a law for you: *thin clients must not use
  capability/model discovery as their login check* — discovery can
  legitimately skip offline providers or time out. Ping validates
  reachability + auth, nothing else.
- **Identity**: `GET /api/gateway/me` → `{ok, principal:{user_id,
  tenant_id, roles, admin, …}, auth:{mode: "users"|"legacy-token"},
  routing:{mode: "per-principal"|"single-user"}}`. Show this on the
  connection screen — admin-ness gates the users screen (admin routes
  403 otherwise), and auth mode is worth a badge.
- 401 → wrong/missing token; connection refused → gateway down. Two
  different honest states, never one "error".
- Dev server for your live testing — exact, verified against `cli.py`'s
  startup self-checks (run it before writing any Rust):

  ```sh
  export ABSTRACTGATEWAY_AUTH_TOKEN=console-dev-secret-0123456789
  /Users/albou/tmp/abstractframework/.venv/bin/python -P -m abstractgateway serve --host 127.0.0.1 --port 8080
  # first probe, same token (expect {"ok":true,"status":"healthy",...}):
  curl -s -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" http://127.0.0.1:8080/api/gateway/ping
  ```

  Four parts are load-bearing. (1) The interpreter: bare `python` will
  NOT import abstractgateway — use the workspace venv above (it holds
  the editable install), or `pip install -e .` into your own. (2)
  `--host 127.0.0.1` plus a ≥15-char token: serve fail-fasts at startup
  on weak tokens when binding its default 0.0.0.0 — `dev-token` is
  literally in the denylist (`cli.py::_is_weak_token`; the refusal
  reads "weak auth token detected while binding to a non-loopback
  host"). (3) `-P`: without it a repo-root cwd shadows installed
  packages — a known operational footgun, not yours to fix. (4) The
  port: 8080 is routinely ALREADY TAKEN on this machine by the
  operator's own gateway — probe first
  (`lsof -nP -iTCP:8080 -sTCP:LISTEN`) and if it's occupied pick a free
  `--port` (live-verified 2026-07-23: `--port 8090` behaves
  identically); NEVER kill the existing listener — it is the
  operator's, not yours.

  Boot patience: the server loads embedding stores before it binds —
  the port can take a minute or more to start listening, and the only
  early output may be a LanceDB `#FALLBACK` RuntimeWarning (normal,
  not an error). Retry the ping probe until it returns 200; connection
  refused during boot is not failure.

### 2.2 Providers

- `GET /api/gateway/discovery/providers?include_models=true` →
  `{items: [{name, …, models?, models_error?}], …}`. Items merge two
  worlds: locally-known providers (lmstudio, ollama, openai, …) and
  **provider endpoint profiles** as virtual providers named
  `endpoint:<id>`. `include_models=true` is documented "may be slow"
  (live probes, concurrent, best-effort — a dead endpoint yields `[]` +
  `models_error`, never fails the route).
- `GET /api/gateway/discovery/providers/{name}/models` — per-provider
  model list. Keep an empty-inline → per-provider fallback loop
  (pre-2026-07-22 gateways served `models: []` inline for profiles).
- Endpoint profile CRUD (`routes/gateway.py:22381-22730`):
  - `GET /api/gateway/config/provider-endpoint-profiles` →
    `{ok, profiles:[public rows], can_create_gateway_scope}` — **public
    rows never contain the API key** (secrets are write-only).
  - `POST /api/gateway/config/provider-endpoint-profiles` body:
    `{id, display_name?, description?, provider_family, base_url,
    api_key?, scope: "user"|"gateway", capabilities?, allowed_models?,
    enabled?}`. Gateway scope requires admin (403).
  - `PUT /api/gateway/config/provider-endpoint-profiles/{profile_id}` —
    same fields plus `clear_api_key: true` to remove a stored key.
    Absent `api_key` = keep the stored one (edit forms must NOT display
    or resubmit the secret; show "key stored" / "no key").
  - `DELETE /api/gateway/config/provider-endpoint-profiles/{profile_id}`.
  - `POST /api/gateway/config/provider-endpoint-profiles/discover-models`
    body `{profile_id?}` or a draft `{provider_family, base_url,
    api_key?}` → `{ok, models, available, error?, base_url_configured,
    api_key_set}`. This is your "Test connection" for a profile form
    **before saving** — use it.
  - 400 with `{"detail": "..."}` carries validation errors (bad base
    URL, bad id) — surface detail text verbatim.

### 2.3 Multimodal capability routes (the "defaults" tab)

- `GET /api/gateway/config/capability-defaults` → `{ok, authority,
  writable, source, routes: [...], errors?, config_hint?}`.
  - Row shape: `{key, kind, modality, task?, label?, configured: bool,
    source, provider?, model?, base_url?, options?}` plus coverage
    decorations on some input rows (input.image/video/sound/music can
    read "covered by the text route" with `overrideable`/`read_only`
    flags — render covered ≠ configured ≠ empty as distinct states).
  - **Authority matters**: the gateway is the control plane, not the
    persistence owner — writes land in AbstractCore config (local or
    gateway-scoped) or proxy to a remote AbstractCore server. When the
    remote core is unreachable the route returns HTTP 200 with
    `ok:false` + `errors` + `writable:false`. **Transport success is
    never operation success — always read the body** (a recurring
    framework lesson; it has bitten every thin client that skipped it).
- Route key vocabulary (`abstractcore/config/capability_defaults.py`):
  kinds `input|output|embedding|rerank`; modalities
  `text|image|video|voice|sound|music|scene3d`; optional tasks
  `text_to_image|image_to_image|image_upscale|text_to_video|
  image_to_video|text_to_scene3d|image_to_scene3d`. Key =
  `kind.modality[.task]`. Enumerate rows from the GET — never hardcode
  the route list.
- Writes:
  - `PUT /api/gateway/config/capability-defaults/{kind}/{modality}`
    and `.../{kind}/{modality}/{task}` body `{provider?, model?,
    base_url?, options?}` → returns the full refreshed payload (use it
    to re-render; no second GET needed).
  - `DELETE` on the same paths clears the route back to unconfigured.
  - 400 = validation, 502 = persistence backend unreachable.

### 2.4 Users & entities

- Users (all admin-only, 403 otherwise; `tenant_id` query param
  defaults `"default"`):
  - `GET /api/gateway/admin/users` → `{users: [public rows]}`.
  - `POST /api/gateway/admin/users` body `{user_id, tenant_id?, roles?,
    scopes?, enabled?, runtime_id?, email?, token?}` → `{user, token}`.
    **The token is returned exactly once.** Show it prominently with a
    copy affordance and say it will not be shown again.
  - `PATCH /api/gateway/admin/users/{user_id}?tenant_id=…` body may
    include `rotate_token: true` → response carries the new `token`
    (once, same rule).
  - `DELETE /api/gateway/admin/users/{user_id}?tenant_id=…`.
- Entities (read-only basics for this app):
  `GET /api/gateway/entities` (list), `GET /api/gateway/entities/{name}`,
  `.../{name}/card`, `.../{name}/state`. List name + state badge —
  operator states are exactly `awake|asleep|paused`
  (`abstractruntime/identity/life.py:797`); a `mode` field
  (resting/dreaming/visiting) may accompany, render it as secondary
  text if present. Entity lifecycle (create, summon,
  visits, loops) is deliberately **out of scope** — it is a deep,
  ritual-laden surface; do not put write buttons on it.

### 2.5 Runtimes (inventory / review screen)

- `GET /api/gateway/admin/runtimes?include_sizes=true` — "the
  runtimes-first inventory": every data plane on the gateway (default,
  per-user, per-entity) with owners, sizes, entity liveness. Cheap by
  design. Render as a read-only table; drill-in runs
  (`/admin/runtimes/{kind}/{tenant_id}/{runtime_id}/runs`) is optional.
- `GET /api/gateway/admin/runtime-config` / `POST` same path — the
  runtime knobs surface, admin-only. Read it before deciding whether to
  expose editing; read-only display is an acceptable v1.

### 2.6 Sandbox = the wizard's "Test" verb

- `POST /api/gateway/sandbox/generate` body `{capability:
  "output.text", provider, model, prompt? or messages?, system_prompt?,
  temperature?, max_tokens?}` → `{ok, response, usage, routed_provider,
  provider_endpoint_profile?}`. Text-only by design (400 for other
  capabilities); provider + model required (400). 502 carries the
  provider's real error in `detail`.
- Use it after provider/model selection ("Test this pair") and as the
  optional live check in review+apply. A test that fails with the
  provider's own error text is worth ten green checkmarks.

### 2.7 Error handling laws

- FastAPI errors are `{"detail": "…"}` with 4xx/5xx. Surface `detail`
  verbatim — the gateway writes actionable messages.
- Some 200 payloads carry `ok:false` + `errors` (capability defaults,
  discovery degradations). Read the body, always.
- Timeouts: discovery with `include_models=true` and sandbox generate
  can take tens of seconds. Never block the UI (§5); show elapsed time
  on long calls; a model call >60s deserves a visible note (precedent:
  abstractcode-tui's long-call strip).

## 3. Wizard shape (suggested, not mandated)

Connection → Providers → Routes → Users/Entities → Review. Each step:
a validation gate before "Next" (can't leave connection until ping
succeeds or the user explicitly chooses offline/draft mode), Esc backs
up one step, everything reachable by keyboard alone. Browse mode after
first-run: tab bar over the same five screens. Persist nothing secret
client-side; if you persist connection state (base URL) put it in a
plain config file and say so on screen. The token: keep it in memory;
offer an env fallback (`ABSTRACTGATEWAY_AUTH_TOKEN`) so operators can
avoid typing secrets.

## 4. Design laws (paid-for lessons — do not relearn)

- **The fabricated-selection lesson** (assistant settings incident,
  2026-07-17): populating a picker from a catalog and letting index 0
  sit selected PRESENTS the first entry as configuration — an
  unconfigured route once displayed a keyless provider pair, one
  accidental Save from being live. Therefore: route editors have an
  explicit **default-vs-override mode**; placeholder items occupy
  index 0 ("Choose a provider…", value empty); pick controls are
  disabled in default mode; the resolved state renders as an
  **"Applies now: provider/model (source)"** line or an honest
  "nothing configured — engine decides". A saved model is only
  selectable under its own saved provider; switching provider resets
  the model picker to placeholder — never a fabricated pair.
- **Body over transport** (§2.7): render `ok:false` inside a 200 as
  failure with the payload's `errors`.
- **Secrets discipline**: masked input for tokens/keys
  (`TextInput::masked`), never echo a stored secret into an edit form,
  never log request headers. The one deliberate exception: the
  create-user/rotate-token response token, shown once with copy.
- **Honest states everywhere**: unreachable ≠ unauthorized ≠ empty ≠
  loading. Each screen needs all four rendered distinctly. Never
  invent a value to fill a cell; `—` with a reason beats a guess.
- **Verify after write**: the epic's validation is apply → **verify via
  GET**. Re-render from the write response or a follow-up GET, never
  from optimistic local state.

## 5. Engine guide (abstracttui 0.2.8)

Docs: https://docs.rs/abstracttui, plus the engine repo
(`/Users/albou/tmp/abstractframework/abstracttui`): `llms-full.txt` at
the root (the whole API surface, AI-readable), `docs/`,
`docs/design/01-damage-contract.md` (the frame law). MSRV 1.87.

**Read these examples first** (engine repo `examples/`):
- `decide.rs` — ChoicePrompt/ChoiceSequence: modal decision gates with
  shortcut letters, danger tint, "Other" free text, must-choose mode.
  Your apply/confirm/destructive-delete gates are exactly this.
- `components.rs` — THE component pattern: a component is a plain
  `fn(cx: Scope, t: &TokenSet, props…) -> View`; props are arguments
  (owned data + `Signal<T>`), children are `View` args, events are
  `impl FnMut` callbacks. Build every screen this way.
- `reader.rs` — scroll + content rendering at scale.
- `hello.rs` — the app skeleton (already your scaffold).

**Load-bearing APIs for this app** (all verified present in 0.2.8):
- `app::select` — `Select` / `Combobox` / `MultiSelect` +
  `SelectHandle` (programmatic open, `src/app/select_handle.rs`):
  provider and model pickers. Combobox for long model lists (typing
  filters).
- `ChoicePrompt` + `ChoiceSequence` (`src/app/choice_prompt.rs`):
  decision gates; `ChoiceSequence` for short multi-question flows.
- `TextInput` — `.masked(true)` for tokens/API keys,
  `.placeholder_while_focused(true)` for teach-beside-the-caret;
  `TextArea` for anything multiline.
- `widgets::Table` — the routes/users/runtimes tables. Note: rich
  cells/badges/row-actions (app-kits 0530) do NOT exist yet —
  hand-roll badges as styled cells and file what you wish existed.
- `reactive::connection` + `Backoff` — the gateway link: state as
  signals (`ConnState`), full-jitter reconnect; the engine does no I/O,
  your dial fn is the seam.
- `channel_source` / `latest_source` / `bounded_source` — the
  thread→signal bridges. **All HTTP runs on background threads**; a
  posted closure or source delivers results into signals. Never call
  the network inside the render loop or an event handler.
- `app::use_caps`, `app::request_full_redraw`,
  `set_redraw_on_focus_gained` — terminal capability signal + repaint
  verbs.
- Theme: `use_theme(cx)` → `TokenSet`. **Tokens only** — no hardcoded
  colors, ever. `ABSTRACTTUI_THEME=<id>` must restyle the whole app.

**Known engine state you will feel** (this is the validator's point):
- app-kits **0510 (form kit)** and **0520 (wizard flow)** are proposed,
  NOT shipped — this build is their promotion trigger. Hand-roll field
  rows, per-step validation gating, and the step container; file
  findings describing what a form/wizard kit should have given you.
- **Stacked modals disagree** on paint order vs key ownership (same-z
  hazard, documented in
  `docs/backlog/completed/app-kits/0500_select_combobox_family.md`
  "Follow-ups revealed"). Avoid stacking two `Modal`s; sequence them or
  use ChoiceSequence.
- First-app findings worth skimming before you trip on the same stones:
  `docs/backlog/proposed/first-app/README.md` +
  `docs/backlog/completed/first-app/` (autofocus-in-dyn_view, modal
  focus, List on_select vs on_activate, placeholder clipping…).

**Headless testing pattern**: `abstracttui::testing::CaptureTerm` +
`abstracttui::app::Driver` — drive the REAL UI through the capture
harness, no pty; assert on rendered frames. The model to copy is
`/Users/albou/tmp/abstractframework/abstractcode-tui/tests/headless_ui.rs`
(their `Harness` struct: App + CaptureTerm + Driver + a dummy command
channel standing in for the network thread). Put yours in `tests/` at
this crate's root (Cargo integration tests — same layout as the
model). Every wizard step's form logic gets headless coverage; network
calls are injected fixtures in tests.

## 6. HTTP + JSON dependency policy

The engine holds a five-dep posture; **apps may take deps** — that
posture is the engine's, not yours. Precedent (abstractcode-tui, the
first shipped app, read-only at
`/Users/albou/tmp/abstractframework/abstractcode-tui/Cargo.toml`):

```toml
ureq = { version = "2.12", default-features = false, features = ["tls", "gzip"] }
serde_json = "1"
```

**Recommended: the same pair.** ureq is blocking (fits the
background-thread + source-bridge model), rustls means remote HTTPS
gateways work with zero system OpenSSL, and serde_json because gateway
payloads carry arbitrary user text. A hand-rolled HTTP/1.1 client over
`std::net::TcpStream` is the house-style zero-dep option and fine for
localhost-only, but the gateway is legitimately remote+TLS in real
deployments — take ureq. Do not take an async runtime; nothing here
needs one.

## 7. THE FEEDBACK PROTOCOL (critical — the other half of the mission)

When the engine blocks, annoys, or surprises you:

1. **Ship an app-side workaround** — never stall the build on the
   engine.
2. **File a finding** in
   `/Users/albou/tmp/abstractframework/abstracttui/docs/backlog/proposed/field-gateway/`
   — band **0900+** (one file per finding,
   `09NN_snake_case_title.md`), and add its row to that directory's
   `README.md` table.

House grammar (copy the shape of
`docs/backlog/completed/first-app/0220_autofocus_in_dyn_view_panics.md`
or any sibling): Metadata (created/status), Context (what you were
building, the exact composition), Current code reality (**cite engine
`file:line` against 0.2.8**), the repro, the app-side workaround (so
the engine fix can delete it), and a class — bug / footgun / API gap /
capability gap / UX defect / feature.

The minimum skeleton, inline so you never guess (severity rides
Metadata: **P1** blocks the build / **P2** costs real time, workaround
holds / **P3** paper cut):

```markdown
# Proposed: Combobox filter loses the query on theme switch

## Metadata
- Created: 2026-MM-DD
- Status: Proposed (field-gateway, gateway-console build)
- Severity: P2 — cost ~40min; app-side workaround holds
- Class: footgun

## Context
Provider step: a Combobox over ~200 discovered models inside a form
row, theme-keyed dyn_view — the exact composition, code shape included.

## Current code reality
- `src/app/select_core.rs:NNN` — what the engine does today (0.2.8).

## Repro
Minimal steps or a ~10-line snippet against `abstracttui = "0.2.8"`.

## Workaround in the field (delete when fixed)
What the app ships instead, and what the engine fix would let it delete.
```

Its README-table row: `| 09NN | <title> | footgun | P2 |`.

This loop is fast and real: the first app round-tripped **19 findings**,
with engine fixes released same-day (0.2.x waves). Expected finding
sources for THIS app: form-field focus order, per-step validation
gating, Select/Combobox in dense forms, Table badge/action gaps
(0530-shaped), masked-input UX, wizard resume (control-plane 0340 is
unshipped — note where persistence would have saved you). File
generously; a "this cost me 20 minutes" footgun is a valid item.

The only engine-repo files you touch: `docs/backlog/proposed/field-gateway/*`
(your findings + its README table). No engine code changes.

## 8. Quality bar

- `cargo build` + `cargo test` green after every meaningful change;
  keep clippy clean (the engine holds zero warnings — match it).
- Headless UI tests (§5 pattern) for every wizard step's form logic;
  network responses are fixtures in tests.
- Keyboard-first: every flow completable without a mouse. `q`/Ctrl+C
  quit from browse; Esc backs up; visible key hints on every screen.
- Theme tokens only; honest states (§4); zero idle cost (no polling
  loops — timers via the engine's `reactive::interval` if you need
  refresh, and nothing ticking when nothing changes).
- Headless guard stays: piped/CI runs print a skip line and exit 0.
- No git operations — the maintainer owns commits and pushes. Build,
  test, report.

## 9. Non-goals

- **No gateway-side changes.** You consume the existing admin API; if
  the API itself is awkward, note it in your final report — do not
  patch the Python.
- **Not a monitoring dashboard** — no run streams, no live ledgers, no
  entity visit surfaces. Wizard + browse/edit of configuration only.
- No write operations beyond what the web console exposes (entity
  lifecycle writes are out; user CRUD, profiles, capability routes,
  sandbox tests are in).
- No cookie/session auth, no login form for the user registry — bearer
  token only.
- No wizard-state persistence machinery (control-plane 0340 is
  unshipped); losing wizard progress on quit is acceptable v1 — file
  the finding instead.

## 10. Definition of done (from the 0215 epic)

The wizard configures a **real local gateway** end-to-end, driven only
by the keyboard: connection (probe + honest state) → create/edit a
provider endpoint profile (discover models, sandbox-test it) → set one
multimodal capability route with correct default-vs-override semantics
→ apply → **verify via GET**. Headless CaptureTerm coverage for every
step's form logic. Engine findings filed for every friction point hit
along the way (an empty findings directory after this build would
itself be a surprising claim — say so explicitly in your report if it
happens).

Final report: what shipped, the live end-to-end evidence (which
gateway, which writes, the verifying GETs), test counts, and the list
of field-gateway findings filed.
