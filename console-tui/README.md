# abstractgateway-console

A keyboard-first terminal wizard to configure AbstractGateway — the TUI
sibling of the served web console (`GET /console`), so the
`abstractgateway` package serves both the web UI and the CLI for
configuring a gateway. Rendered by
[AbstractTUI](https://crates.io/crates/abstracttui) (`abstracttui`
0.3.6 — the engine's `PageHost` owns the tab bar/navigation and a
right `Drawer` hosts the entity inspector), talking to the gateway's admin HTTP API
(`/api/gateway/...`).

Screens 9 and 0 are not implemented here: they are AbstractCore's shared
**Models** and **Engines** screens, from the
[`abstractcore-console`](https://crates.io/crates/abstractcore-console)
crate (0.3), the same screens `abstractcore-console` shows over the
`abstractcore` CLI. This crate mounts them over its own HTTP transport
(`src/transport_http.rs`) against the gateway's mirrors of the
AbstractCore routes. Both crates build on one abstracttui (0.3.6).

Twelve screens, shared by a **setup guide** (the wizard: the web
console's five first-run steps — welcome, engines, model, apps, done —
on eight screens, through the same gateway routes) and a **browse mode**
(free tabs).

The guide walks Connection → **Setup** (welcome: this computer at a
glance, from `GET /api/gateway/host/state`) → Engines → Providers →
Routes (the default model: the recommended plan for this computer with
AbstractCore's fit warnings, `a` applies it, `D` downloads all of it in
one parent job, `C` cancels, `p` shows every word) → Models → Apps →
Review (**Finish** or **Skip setup**: `POST /api/gateway/host/first-run`,
verified by a GET and journaled). Like the web guide, no step is gated
except signing in. Without `--wizard`/`--browse`, the console reads
`GET /api/gateway/host/first-run` at connect and keeps the guide open
only for an admin whose first run is not completed; browse mode
otherwise. `Ctrl+G` reopens the guide from browse; in the guide it opens
the guide menu, like the web guide's step list: go to any step directly,
leave (not recorded, it opens again next start) or skip (recorded).

- **1 Connection** — base URL + token (masked; or launch with
   `--token <token>`), probe via `/ping` +
   `/me`, honest states: unreachable ≠ sign-in needed (no token sent) ≠
   token rejected ≠ connected — both 401s name where the admin token
   lives, `<data dir>/auth/bootstrap-admin-token` on the gateway host —
   with identity badges
   (admin, auth mode, routing mode), and the network panel (including
   the web console's "Look up my public address").
- **6 Providers** (MODELS) — provider endpoint profiles (create/edit/delete,
   write-only API keys with fingerprint display, model allowlists fed
   by live discovery, scope moves on edit, test-connection via live
   model discovery of the FORM's values) over the read-only
   discovered-provider inventory.
- **9 Multimodal** (MODELS; the former Routes screen) — the multimodal capability table (`input.text`,
   `output.voice`, …) with explicit default-vs-override editing:
   placeholder pickers, "Applies now:" resolution lines, model pickers
   that reset on provider switch — never a fabricated pair. The
   `output.voice` editor carries a per-pair voice picker (live
   `/voice/voices` catalog) that writes into the options JSON, and
   every editor has a **Test** verb — a real generation through the
   production lane (voice → `/runs/{…}/voice/tts`, everything else →
   `/sandbox/generate` with the route's own capability key).
- **2 Accounts** (ACCOUNTS) — one table of users and entities
   (`GET /admin/accounts`): kind, email address, mailbox, runtime and the
   **Active** switch (Space: deactivate a user, suspend an entity; your own
   row says why it can't). Per row: `@` email (your row: My email; another
   user: their address only and their mailbox status; an entity: why it has
   none), `l` activity (sign-ins, changes, runs, automations, email; `f`
   filters), `w` workspace policy, `t` rotate, `m` manage (entities), `d`
   delete; an action that can't apply says why. Also gateway user CRUD (create shows the token
   exactly once, with clipboard copy; the email address at the top level,
   runtime and tenant under Advanced; user/admin/readonly roles; the
   **Active** switch on the table, Space), the **Mailboxes for users**
   switch, **My email** (`@`: email address, mailbox, two notification
   switches, agent email tools), token rotation, your own workspace
   policy (`w`, as the web console's "My workspace"), runtime reservations
   (transfer/purge retained planes of deleted users, `v`), and the
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
- **4 Runtimes** (WORK) — the data-plane inventory (default / per-user /
   per-entity) with owners, sizes, liveness, the runtime-knobs
   surface (per-knob value + provenance; API-writable, no UI edits
   yet) with the Continuum backlog settings editor and the curated
   skills-shelf reseed (admin), recent root runs with **cancel** (`c`) and **steer** (`s`)
   via durable gateway commands, and the data-homes browser (`h`)
   with dry-run-gated purge.
- **3 Workflows** (WORK) — plain names, what each workflow does, the
   version, where it comes from (shipped with the gateway, imported,
   published from AbstractFlow) and the apps that use it; a second section,
   **Default workflow per app** (Tab to it, Enter picks, saved at once).
   Every workflow registered on the gateway, with
   published/draft version counts, per-version entrypoints and
   interfaces, and a `Not loaded` block naming versions the gateway is
   not serving and why. `e` exports a version to a local `.flow` file (the destination is shown
   and editable first; default `~/Downloads/abstractgateway-console/`),
   `d`/`D` delete a version / the whole workflow, `t` toggles draft
   visibility, `i` imports a `.flow` bundle from this machine, `L`
   reloads the registry from disk; import and the other writes follow
   the registry ownership rule (admin on the shared registry).
- **R Review & Test** (SYSTEM) — the session's change journal (every write +
   its verify-via-GET result) and the web console's sandbox workspace:
   every output mode (text, image, voice, music, SFX, video), file
   attachments, speak-this-reply, and the guide's Finish / Skip setup.
- **0 Resources** (SYSTEM) — live host residency: RAM/device/GPU gauges with
   degradation notes, the resident-model table (modality, tri-state
   residency, lock marker, context facts with calibration), and
   session prompt caches on sub-tabs, polled from
   `GET /api/gateway/host/state` every 4s while the screen is active.
   `w` warms up a model (provider + model form, optional
   lock-after-load), `k` locks/unlocks, `u` unloads (a locked model
   answers HTTP 409 and the screen offers a force unload), `e` asks
   for a context estimate, `c` clears the selected session's prompt
   caches.
- **7 Models** (MODELS; AbstractCore's shared screen, page id `catalog`) — the
   model catalog for the GATEWAY host: each model's artifacts per engine
   (Ollama, LM Studio, MLX, Hugging Face…), size, whether it fits the
   host's memory (`fits` / `tight` / `too large` / `partial offload`),
   and whether it is already on disk. `w` downloads (progress strip,
   toast on completion), `d` deletes after a confirm that names the
   blockers (loaded, shared cache), `/` filters, `f` fits only, `e`
   cycles the engine, `v` flips to what is installed, `c` cancels the
   running job.
- **8 Engines** (MODELS; AbstractCore's shared screen, page id `engines`) — the
   local engines on the gateway host: installed or not, version,
   running and reachable. `i` installs after a confirm showing the
   exact command and "runs on gateway host …" (dry run available),
   `o` opens the download page (LM Studio), `r` probes the local
   servers, `c` cancels. With a gateway that serves them, the screen
   also starts/stops engine servers, continues paused installs and
   shows the install location (abstractcore-console 0.4 verbs).
- **5 Apps** (WORK) — the web console's Apps tab: browser apps (Flow, Code,
  Observer…), the desktop Assistant and Node.js. `Enter`/`o` opens an
  app signed in (a one-time link), `i`/`u` install/update, `s`/`x`
  start/stop, `l` log, `c` cancel, `t`/`T` terminal apps, `n` Node.js,
  `y` copy, `r` check again (admin-only writes).
- **S Setup** (also `Ctrl+G`) — the guide's welcome step: computer,
  memory, graphics, data folder (and why), sign-in mode, whether the
  gateway starts at login, the first-run state, and the guide's steps.

Screens 9 and 0 read `GET /api/gateway/host/profile`, `/engines`,
`/models/catalog`, `/models/installed` and `/jobs/{id}`, and write
through `POST /models/download`, `/models/delete`, `/engines/{id}/install`
and `/jobs/{id}/cancel` (a download cancels through the web console's
`POST /models/download/{id}/cancel`; admin-only on the gateway). A
gateway without those routes shows each read as "not found" on those
two screens; the other screens are unaffected.

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
Legacy aliases, kept for existing scripts: `--token-file PATH` (the token read
from a file) and the `ABSTRACTGATEWAY_AUTH_TOKEN` environment variable.

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
take the caret by themselves once connected) · `1-9`, `0`, `A` (Apps), `N` (Network) screens (browse; the
screen bar is also clickable in browse) · `Ctrl+G` setup guide (browse:
reopen; guide: go to any step, leave or Skip setup) · `r` refresh · `F1` / `?` About ·
`F2` docs assistant (signed in; the web top bar's ✦ drawer) · `F3`
gateway host panel (**Workflows paused** and **Start at login** switches,
restart, quit, update check/install, tray; a paused banner shows on every
screen) · `Ctrl+L` repaint · `q`
(browse) / `Ctrl+C` quit. Per-screen actions sit in the footer, and a
refused action always SAYS why (toast + footer) instead of doing
nothing.

Start a dev gateway (loopback host + a ≥15-char token — serve fail-fasts
on weak tokens when binding non-loopback; port 8080 is often the
operator's own gateway — probe `lsof -nP -iTCP:8080 -sTCP:LISTEN` and
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
and restored. Coverage honesty: the profile-CRUD leg is proven by
keyboard-driven headless tests plus the live API E2E (writes + verify +
cleanup) — the pty smoke drives the route leg live but not the profile
form, so that one composition is proven in two halves rather than one
live keyboard pass.

## Layout

- `src/api.rs` — blocking HTTP client (ureq), error taxonomy
  (unreachable / 401 / 403 / detail-carrying HTTP / protocol).
- `src/store.rs` — signals + typed rows parsed from gateway payloads;
  `Loadable<T>` keeps the four honest states distinct.
- `src/worker.rs` — the one background thread owning HTTP; commands in
  via mpsc, results back as posted closures (`WakeHandle`). It publishes
  the verified client for the Models/Engines screens.
- `src/transport_http.rs` — `HttpTransport`, the `abstractcore-console`
  `ConsoleTransport` over `GatewayClient` (route and error mapping).
- `src/ui/` — root shell (wizard/browse) + one module per screen;
  shared components in `ui/util.rs`.
- `tests/headless_ui.rs` — the CaptureTerm suite (screens 9/0 over a
  mock transport and AbstractCore's contract fixtures in
  `tests/fixtures/`); `tests/http_transport.rs` — `HttpTransport`
  against a local fake gateway; `tests/live_e2e.rs` — the ignored live
  tests; `scripts/pty_smoke.py` — the pty proof.

This is the second **validator app** for the AbstractTUI engine (epic:
`abstracttui/docs/backlog/planned/ports/0215_gateway_config_wizard_app.md`).
Engine friction found during the build is filed in the engine repo under
`docs/backlog/proposed/field-gateway/` (band 0900–1010).
