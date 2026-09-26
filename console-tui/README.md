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
crate (0.2), the same screens `abstractcore-console` shows over the
`abstractcore` CLI. This crate mounts them over its own HTTP transport
(`src/transport_http.rs`) against the gateway's mirrors of the
AbstractCore routes. Both crates build on one abstracttui (0.3.6).

Ten screens, shared by a **guided wizard** (gated steps, first-run) and
a **browse mode** (free tabs):

1. **Connection** — base URL + admin token (masked; env fallback
   `ABSTRACTGATEWAY_AUTH_TOKEN`), probe via `/ping` + `/me`, honest
   states: unreachable ≠ unauthorized ≠ connected, with identity badges
   (admin, auth mode, routing mode).
2. **Providers** — provider endpoint profiles (create/edit/delete,
   write-only API keys with fingerprint display, model allowlists fed
   by live discovery, scope moves on edit, test-connection via live
   model discovery of the FORM's values) over the read-only
   discovered-provider inventory.
3. **Routes** — the multimodal capability table (`input.text`,
   `output.voice`, …) with explicit default-vs-override editing:
   placeholder pickers, "Applies now:" resolution lines, model pickers
   that reset on provider switch — never a fabricated pair. The
   `output.voice` editor carries a per-pair voice picker (live
   `/voice/voices` catalog) that writes into the options JSON, and
   every editor has a **Test** verb — a real generation through the
   production lane (voice → `/runs/{…}/voice/tts`, everything else →
   `/sandbox/generate` with the route's own capability key).
4. **Users & Entities** — gateway user CRUD (create shows the token
   exactly once, with clipboard copy; advanced tenant/runtime bindings;
   user/admin/readonly roles), token rotation, runtime reservations
   (transfer/purge retained planes of deleted users, `v`), and the
   entity roster with a per-entity **manage menu** (`m`): state
   wake/sleep(+dream)/pause, mind substrate, voice triple, work order,
   own-time grant + loop start/stop/freeze, tool policy (per-phase
   grants from the live capability matrix; emptied phases ask
   reset-vs-deny-all), prompt overlay (per-layer multi-line editing),
   candidates review (promote/reject with journaled reasons), re-embed
   (danger-gated), verify chain. Entity creation/summon/visits stay
   deliberately outside this console (rituals, not configuration).
5. **Runtimes** — the data-plane inventory (default / per-user /
   per-entity) with owners, sizes, liveness, the runtime-knobs
   surface (per-knob value + provenance; API-writable, no UI edits
   yet), recent root runs with **cancel** (`c`) and **steer** (`s`)
   via durable gateway commands, and the data-homes browser (`h`)
   with dry-run-gated purge.
6. **Workflows** — every workflow registered on the gateway, with
   published/draft version counts, per-version entrypoints and
   interfaces, and a `Not loaded` block naming versions the gateway is
   not serving and why. `e` exports a version to a local `.flow` file,
   `d`/`D` delete a version / the whole workflow, `t` toggles draft
   visibility; import and the other writes follow the registry
   ownership rule (admin on the shared registry).
7. **Review & Test** — the session's change journal (every write +
   its verify-via-GET result) and a live sandbox generation test.
8. **Resources** — live host residency: RAM/device/GPU gauges with
   degradation notes, the resident-model table (modality, tri-state
   residency, lock marker, context facts with calibration), and
   session prompt caches on sub-tabs, polled from
   `GET /api/gateway/host/state` every 4s while the screen is active.
   `w` warms up a model (provider + model form, optional
   lock-after-load), `k` locks/unlocks, `u` unloads (a locked model
   answers HTTP 409 and the screen offers a force unload), `e` asks
   for a context estimate, `c` clears the selected session's prompt
   caches.
9. **Models** (AbstractCore's shared screen, page id `catalog`) — the
   model catalog for the GATEWAY host: each model's artifacts per engine
   (Ollama, LM Studio, MLX, Hugging Face…), size, whether it fits the
   host's memory (`fits` / `tight` / `too large` / `partial offload`),
   and whether it is already on disk. `w` downloads (progress strip,
   toast on completion), `d` deletes after a confirm that names the
   blockers (loaded, shared cache), `/` filters, `f` fits only, `e`
   cycles the engine, `v` flips to what is installed, `c` cancels the
   running job.
0. **Engines** (AbstractCore's shared screen, page id `engines`) — the
   local engines on the gateway host: installed or not, version,
   running and reachable. `i` installs after a confirm showing the
   exact command and "runs on gateway host …" (dry run available),
   `o` opens the download page (LM Studio), `r` probes the local
   servers, `c` cancels.

Screens 9 and 0 read `GET /api/gateway/host/profile`, `/engines`,
`/models/catalog`, `/models/installed` and `/jobs/{id}`, and write
through `POST /models/download`, `/models/delete`, `/engines/{id}/install`
and `/jobs/{id}/cancel` (admin-only on the gateway). A gateway without
those routes shows each read as "not found" on those two screens; the
other eight are unaffected.

Parity contract with the web console: every write targets the SAME
endpoint with the same body shape — changing a parameter here or in
the web UI has the same target and the same effect.

Every write is three-phase in the worker: write → **verify via GET** →
journal. Failures surface the gateway's `detail` text verbatim; `ok:false`
inside a 200 renders as failure (body over transport).

## Install

```sh
cargo install abstractgateway-console
ABSTRACTGATEWAY_AUTH_TOKEN=... abstractgateway-console --url http://127.0.0.1:8081
```

The crate is released from the AbstractGateway repository
(`console-tui/`); see [CHANGELOG.md](CHANGELOG.md). The gateway-side guide is
[docs/console.md](https://github.com/lpalbou/abstractgateway/blob/main/docs/console.md).

## Run from source


```sh
cargo build
cargo run -- --help
# against a local gateway (token via env, preferred over argv):
ABSTRACTGATEWAY_AUTH_TOKEN=... cargo run -- --url http://127.0.0.1:8080
cargo run < /dev/null   # headless: prints a skip line, exits 0
```

Keys: `Tab` focus · `Enter` activate · `Ctrl+N` next step / `Ctrl+P`
back (always work — `]`/`[` are alternates that text fields swallow) ·
`Esc` back / close modal · `1-9`, `0` screens (browse; the screen bar is
also clickable in browse) · `r` refresh · `F1` / `?` About · `Ctrl+L` repaint · `q`
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
ABSTRACTGATEWAY_AUTH_TOKEN=... cargo test --test live_e2e -- --ignored --nocapture
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
