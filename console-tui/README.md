# abstractgateway-console

A keyboard-first terminal wizard to configure AbstractGateway — the TUI
sibling of the served web console (`GET /console`), so the
`abstractgateway` package serves both the web UI and the CLI for
configuring a gateway. Rendered by
[AbstractTUI](https://crates.io/crates/abstracttui) (`abstracttui`
0.3.6 — the engine's `PageHost` owns the tab bar/navigation and a
right `Drawer` hosts the entity inspector), talking to the gateway's admin HTTP API
(`/api/gateway/...`).

The pages follow the web console's sidebar, in its order and groups, each
with a fixed key: **1** Connection (the terminal's sign-in), ACCOUNTS **2**
Accounts, **W** Workspaces, WORK **3** Workflows, **4** Skills & MCP, **5** Runtimes, **6** Apps,
MODELS **7** Providers, **8** OpenAI API, **9** Models, **0** Multimodal,
SYSTEM **H** Resources, **T** Sandbox, **N** Network, then **S** Setup and
**I** About. They are shared by a **setup guide** (the wizard: the web
console's five first-run steps — welcome, engines, model, apps, done — on
seven pages, through the same gateway routes) and a **browse mode** (free
tabs). Every page reads and writes the routes its web page uses.

The guide walks Connection → **Setup** (welcome: this computer at a
glance, from `GET /api/gateway/host/state`, and the recommended models) →
Providers (local engines, then cloud keys) → Multimodal (the default model:
the recommended plan for this computer with AbstractCore's fit warnings, `a`
applies it, `D` downloads all of it in one parent job, `C` cancels, `p` shows
every word) → Models → Apps → Sandbox (**Finish** or **Skip setup**: `POST
/api/gateway/host/first-run`, verified by a GET and journaled). Like the web
guide, no step is gated except signing in. Without `--wizard`/`--browse`, the
console reads `GET /api/gateway/host/first-run` at connect and keeps the guide
open only for an admin whose first run is not completed; browse mode
otherwise. `Ctrl+G` reopens the guide from browse; in the guide it opens the
guide menu, like the web guide's step list: go to any step directly, leave
(not recorded, it opens again next start) or skip (recorded).

Conventions on every page: dialogs are full-width overlays that `Esc` closes;
on/off settings are switches labelled by their feature (`[x]` on, `[ ]` off,
`[-] reason` when unavailable); tables wrap their cells instead of cutting
them and `Enter` opens a row's details; destructive actions confirm inline
(`y` / `n`); the key-hint bar at the bottom lists the page's keys first. The
pages are usable at 80×24.

- **1 Connection** — base URL + token (masked; or launch with
   `--token <token>`), probe via `/ping` +
   `/me`, honest states: unreachable ≠ sign-in needed (no token sent) ≠
   token rejected ≠ connected — both 401s name where the admin token
   lives, `<data dir>/auth/bootstrap-admin-token` on the gateway host —
   with identity badges
   (admin, auth mode, routing mode), and the network panel (including
   the web console's "Look up my public address").
- **2 Accounts** (ACCOUNTS) — one table of users and entities
   (`GET /admin/accounts`): Name (and kind), Email (address and mailbox
   state in one column), Runtime and the **Active** switch (Space: deactivate a user, suspend an entity; your own
   row says why it can't). Per row: `@` email (your row: My email; another
   user: their address only and their mailbox status; an entity: its own
   mailbox form), `l` activity (sign-ins, changes, runs, automations, email; `f`
   filters; `o` opens a run in Observer), `w` opens **Workspaces** on that
   account, `o` OpenAI API (user rows), `t` rotate, `m` manage (entities), `d`
   archive / unarchive (asks inline), `g` opens **Runtimes** filtered to the
   account (`GET /admin/runtimes?account=`), `h` show archived; the line under
   the table lists the selected row's actions, Enter shows why any can't apply. Rows wrap instead of cutting text.
   Also create and edit users (create shows the token
   exactly once, with clipboard copy; the email address at the top level,
   runtime and tenant under Advanced; user/admin/readonly roles; the
   **Active** switch on the table, Space), **Email for everyone** (`Tab`:
   **Mailboxes for users**, **Agent email tools for users**, **Sign-in by
   email**), **My email** (`@`: email address, mailbox, two notification
   switches, agent email tools), token rotation, runtime reservations
   (`v`: transfer a retained runtime to a user; data is never deleted), and the
   entity roster with a per-entity **manage menu** (`m`): state
   wake/sleep(+dream)/pause, mind substrate, voice triple, work order,
   own-time grant + loop start/stop/freeze, tool policy (per-phase
   grants from the live capability matrix; emptied phases ask
   reset-vs-deny-all), prompt overlay (per-layer multi-line editing),
   candidates review (promote/reject with journaled reasons), re-embed
   (danger-gated), verify chain, identity card, and a voice
   **audition** (the unsaved triple spoken as the entity; the audio is
   saved to a file and played only when a local player exists). As in
   the web console you can also **summon** a new entity (`n`: template +
   name → dry-run validate → a confirm naming the permanence → create,
   with admin-only substrate / birth embedder / per-phase capabilities),
   manage **spark templates** (`s`: view, edit as a new version, new
   from selected — saving is admin-only), and **talk** with an entity
   (`c`: open a hosted visit, send turns, close with the reflection
   pass; one visit at a time).
- **W Workspaces** (ACCOUNTS) — which folders agents may read and write: the
   gateway policy and every user account's (own policy or follows the
   gateway), the effective policy in one line on top. `Enter` opens the
   editor: **Access** (Allow my list / Allow everything except), **Trust the
   launch folder**, the allowed and refused folders as rows (`Enter` edits in
   place, **+ Add a folder**, `x` removes; `POST /workspace/path-check` first),
   **Follow the gateway policy** for an account with its own. Each change
   applies at once (`POST /admin/runtime-config`, `PUT
   /admin/user-workspace-policy`, `PUT /workspace/policy/self` for a
   non-admin).
- **3 Workflows** (WORK) — the web console's Workflows page in three tabs
   (`Tab`): **Workflows** (the "Shared with everyone" and "Mine" groups,
   search `/`, `t` drafts, `o` older versions (each its own row), `h` show
   archived; rows never expand, the selected row's actions sit on one line:
   `x` export `.flow`, `f` open in AbstractFlow, `d` archive / unarchive, `e`
   edit the description in place (`PATCH /bundles/{id}`, when
   `actions.can_edit_description`), Space **Available to users**; `i` import
   `.flow`), **Default workflow per app**
   (Enter picks, saved at once; `s` Streamed replies) and **Broken
   workflows** (`d` archives). Same routes as the web page.
- **4 Skills & MCP** (WORK) — the skills shelf (search, show archived, view /
   save / duplicate, import a `.zip` or folder, export `.zip`, archive) and
   the MCP servers (add / edit, test, **Enabled for agents**, archive); one
   row under the skills shows the shelf folder (`f` edits it in place, `u`
   **Refresh curated shelf**).
- **5 Runtimes** (WORK) — the web console's run table (Run, Workflow,
   Status, Node, Session, Updated) as wrapping rows, `Enter` opens a run's
   details in place, `t` **Root runs only**; the data-plane inventory
   (default / per-user / per-entity) with owners, sizes, liveness;
   Artifacts, Cache and Logs as wrapping tables; the runtime-knobs
   surface (per-knob value + provenance; API-writable, no UI edits
   yet) with the Continuum backlog settings editor and the curated
   skills-shelf reseed (admin), **cancel** (`c`, asks inline) and **steer** (`s`)
   via durable gateway commands, and the data-homes browser (`h`)
   with dry-run-gated purge.
- **6 Apps** (WORK) — the web console's Apps tab: browser apps (Flow, Code,
  Observer…), the desktop Assistant and Node.js. `Enter`/`o` opens an
  app signed in (a one-time link), `i`/`u` install/update, `s`/`x`
  start/stop, `l` log, `c` cancel, `t`/`T` terminal apps, `n` Node.js,
  `y` copy, `r` check again, `a` **Apps settings**, `g` the Continuum card's
  settings (backlog folder, exec runner, process manager; each row applies
  on its own) (admin-only writes).
- **7 Providers** (MODELS) — the web console's Providers page in three
   sections (`v` switches): **Local providers**, one row per engine on the
   gateway host (`GET /engines`) with state, version and models — `Enter`
   details, `i` install after an inline confirm showing the plan (and the
   location for app engines), `s`/`x` start/stop its server, `b` browse its
   models on the Models page, `c` cancel an install; **Remote providers**,
   the cloud and OpenAI-compatible presets (`Enter`/`a` opens the connection
   form); **Available Providers**, the endpoint profiles table
   (create/edit/delete, write-only API keys with fingerprint display, model
   allowlists, `t` test).
- **8 OpenAI API** (MODELS) — the OpenAI-compatible API at `/v1`: status and
   base URL, your API key (the token this console signed in with; masked, `v`
   shows, `y` copies, `n` new key shown once), examples with the real base URL
   (`s` picks, `c` copies), recent requests (`Enter` opens the recorded request
   and response, `f` the full record, `o` copies the Observer link); for an
   admin the Endpoint switch, Restart, Check setup, Authentication, Who can
   connect and the Open-mode account.
- **9 Models** (MODELS) — the web console's Models page: one list with a
   header line per model and a row per build (engine, id, quantization,
   size, status), including the downloaded models the catalog does not know
   (**Not in the catalog**); filters (`/` search, `z` quantization, `p`
   provider, `t` capability, `s` Downloaded/All, `f` fits, `x` clear, `m`
   Hugging Face); `w` download with progress (`c` cancels), `d` delete after
   an inline confirm sized by a dry run (`POST /models/delete-download`), `u`
   use a downloaded text model as the default.
- **0 Multimodal** (MODELS) — the multimodal capability table (`input.text`,
   `output.voice`, …) with explicit default-vs-override editing:
   placeholder pickers, "Applies now:" resolution lines, model pickers
   that reset on provider switch — never a fabricated pair. The
   `output.voice` editor carries a per-pair voice picker (live
   `/voice/voices` catalog) that writes into the options JSON, and
   every editor has a **Test** verb — a real generation through the
   production lane (voice → `/runs/{…}/voice/tts`, everything else →
   `/sandbox/generate` with the route's own capability key).
- **H Resources** (SYSTEM) — live host residency: RAM/device/GPU gauges with
   degradation notes, the resident-model table (modality, tri-state
   residency, lock marker, context facts with calibration), and
   session prompt caches on sub-tabs, polled from
   `GET /api/gateway/host/state` every 4s while the screen is active.
   `w` warms up a model (provider + model form, optional
   lock-after-load), `k` locks/unlocks, `u` unloads (a locked model
   answers HTTP 409 and the screen offers a force unload), `e` asks
   for a context estimate, `c` clears the selected session's prompt
   caches.
- **T Sandbox** (SYSTEM) — the web console's sandbox workspace: every
   output mode (text, image, voice, music, SFX, video), file attachments,
   speak-this-reply; the session's change journal (every write + its
   verify-via-GET result) and the guide's Finish / Skip setup.
- **N Network** (SYSTEM) — who can reach the gateway (Localhost only, Local
  network, Internet) with the web page's words, every address the gateway
  detects (this computer, local network, the Bonjour name, the Tailscale
  name) with Works now / Not in this mode and copy, "Reached through another
  address?", and **Advanced**: allowed origins (add / remove, the gateway's
  validation sentence verbatim) and the **Trust proxies on other machines**
  switch (`gateway_network_v1`).
- **S Setup** (also `Ctrl+G`) — the guide's welcome step: computer, memory,
  graphics, data folder (and why), sign-in mode, whether the gateway starts
  at login, the first-run state, and **Recommended for this computer** (`a`
  Use recommended defaults, then Replace mine too inline; `D` Download all).
- **I About** (also `F1` / `?` as an overlay) — the shared About card: this
  console's name and version, the AbstractFramework and AbstractGateway
  versions from `GET /api/gateway/about`, the links and the licence line.

Parity contract with the web console: every write targets the SAME
endpoint with the same body shape — changing a parameter here or in
the web UI has the same target and the same effect.

Every write is three-phase in the worker: write → **verify via GET** →
journal. Failures surface the gateway's `detail` text verbatim; `ok:false`
inside a 200 renders as failure (body over transport).

## Install

```sh
cargo install abstractgateway-console
# the admin token is printed by `abstractgateway serve` when it starts
abstractgateway-console --gateway-url http://127.0.0.1:8080 --token <token>
```

`--url` is an alias of `--gateway-url`. Without it the console uses
`ABSTRACTGATEWAY_URL` (legacy alias), else `~/.abstractframework/gateway.json`
(the address this computer's gateway records), else `http://127.0.0.1:8080`.
Give the token with `--token <token>` or paste it on the Connection screen.

The crate is released from the AbstractGateway repository
(`console-tui/`); see [CHANGELOG.md](CHANGELOG.md). The gateway-side guide is
[docs/console.md](https://github.com/lpalbou/abstractgateway/blob/main/docs/console.md).

## Run from source


```sh
cargo build
cargo run -- --help
# against a local gateway:
cargo run -- --gateway-url http://127.0.0.1:8090 --token <token>
cargo run < /dev/null   # headless: prints a skip line, exits 0
```

Keys: `Tab` focus · `Enter` activate · `Ctrl+N` next step / `Ctrl+P`
back (always work — `]`/`[` are alternates that text fields swallow) ·
`←` / `→` previous / next screen, wrapping (browse; a focused text field,
radio list, tabs bar or scrolling pane keeps the arrows for itself) ·
`Esc` back / close modal (in a screen's text field, the first `Esc`
releases the caret so screen keys work again; page text fields never
take the caret by themselves once connected) · `1-9`, `0`, `H` (Resources), `T` (Sandbox), `N` (Network), `S` (Setup), `I` (About) screens (browse; the
screen bar is also clickable in browse) · `Ctrl+G` setup guide (browse:
reopen; guide: go to any step, leave or Skip setup) · `r` refresh · `F1` / `?` About ·
`F2` docs assistant (signed in; the web top bar's ✦ drawer) · `F3`
gateway host panel (**Workflows paused** and **Start at login** switches,
restart, quit, update check/install, tray; a paused banner shows on every
screen) · `Ctrl+L` repaint · `q`
(browse) / `Ctrl+C` quit. Per-screen actions sit in the key-hint bar, and a
refused action always SAYS why (toast + footer) instead of doing
nothing.

Start a dev gateway (loopback host + a ≥15-char token — serve fail-fasts
on weak tokens when binding non-loopback; port 8080 is often the
your own gateway — probe `lsof -nP -iTCP:8080 -sTCP:LISTEN` and
pick a free port; never kill the existing listener):

```sh
ABSTRACTGATEWAY_AUTH_TOKEN=console-dev-secret-0123456789 \
  python -P -m abstractgateway serve --host 127.0.0.1 --port 8090
```

## Test

```sh
cargo test                 # headless CaptureTerm suite + the HTTP transport
                           # against a local fake gateway (no real network)
# live end-to-end (writes + verify-via-GET + cleanup; needs a gateway):
# (live suites need ABSTRACTGATEWAY_URL of a HERMETIC gateway; 8080/8081 are refused)
ABSTRACTGATEWAY_URL=http://127.0.0.1:18868 ABSTRACTGATEWAY_AUTH_TOKEN=... \
  cargo test --test live_e2e -- --ignored --nocapture
# keyboard-driven pty smoke against a live gateway (writes + restores a route):
ABSTRACTGATEWAY_AUTH_TOKEN=... python3 scripts/pty_smoke.py --url http://127.0.0.1:8080
```

The headless tests drive the real UI through `testing::CaptureTerm` +
`app::Driver` — the config-editing forms (profiles, routes,
users, tool policy, entity state) are driven with fixture payloads
mirroring live gateway shapes; the entity-manage sub-forms and the
runtimes actions are proven live by the pty smoke rather than pinned
headless. The pty smoke proves the definition of
done end to end: boot → probe → keyboard-driven route override → journal
shows the GET verification → gateway state asserted over HTTP → cleared
and restored. The profile forms are covered by keyboard-driven headless
tests plus the live API E2E (writes + verify + cleanup).

## Layout

- `src/api.rs` — blocking HTTP client (ureq), error taxonomy
  (unreachable / 401 / 403 / detail-carrying HTTP / protocol).
- `src/store.rs` — signals + typed rows parsed from gateway payloads;
  `Loadable<T>` keeps the four honest states distinct.
- `src/worker.rs` — the one background thread owning HTTP; commands in
  via mpsc, results back as posted closures (`WakeHandle`). It publishes
  the verified client for the screens that use the shared transport.
- `src/transport_http.rs` — `HttpTransport`, the `abstractcore-console`
  `ConsoleTransport` over `GatewayClient` (route and error mapping).
- `src/api_json.rs`, `src/store_json.rs`, `src/worker_json.rs` — the plain
  JSON lane the parity pages (OpenAI API, Models, Providers' local engines,
  Network) use: a web-console route in, its JSON body out, keyed slots.
- `src/ui/` — root shell (wizard/browse) + one module per screen; shared
  widgets in `ui/kit.rs` (overlay, wrapping table, inline confirm, key-hint
  bar), `ui/switch.rs` and `ui/util.rs`.
- `tests/headless_ui.rs` — the CaptureTerm suite; `tests/r7w2_*.rs` — the
  web-parity pages (snapshots per state at 80×24 and 120×40, the route and
  body of every action), with their live drives in `tests/live_r7w2_*.rs`;
  `tests/http_transport.rs` — `HttpTransport`
  against a local fake gateway; `tests/live_e2e.rs` — the ignored live
  tests; `scripts/pty_smoke.py` — the pty proof.

This is the second **validator app** for the AbstractTUI engine (epic:
`abstracttui/docs/backlog/planned/ports/0215_gateway_config_wizard_app.md`).
Engine friction found during the build is filed in the engine repo under
`docs/backlog/proposed/field-gateway/` (band 0900–1010).
