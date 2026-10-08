# abstractgateway-console

The terminal console for AbstractGateway: the same pages, words and gateway
routes as the web console (`GET /console`), in a terminal — over SSH on a
headless server, too, where it also runs the first-run setup guide. It is
rendered by [AbstractTUI](https://crates.io/crates/abstracttui) and talks to
the gateway's admin HTTP API (`/api/gateway/...`).

It works with the mouse and with the keyboard alike: every button, switch,
row and tab can be clicked, and every one of them has a key.

## What you see

- **Screens** in the web console's sidebar order and groups, each with a fixed
  key: **1** Connection (the terminal's sign-in); ACCOUNTS **2** Accounts;
  WORK **3** Workflows, **4** Skills & MCP, **5** Runtimes, **6** Apps;
  MODELS **7** Providers, **8** OpenAI API, **9** Models, **0** Multimodal;
  SYSTEM **H** Resources, **T** Sandbox, **N** Network; then **S** Setup and
  **I** About. A grouped rail lists them on terminals of 120×32 and larger, a
  one-row tab strip below that; click one, press its key, or use `←` / `→`.
- **Header**: the mode, the memory and compute line once signed in (it opens
  Resources), your identity (it opens Connection), **✦ Docs** (the docs
  assistant, also `F2`) and the ☾ / ☼ theme switch (also `Ctrl+T`).
- **Row actions as buttons** in the web's order and words — glyph buttons
  where the web shows icons, labelled buttons where it shows labels — with the
  web's tooltip on hover or keyboard focus. An action that cannot apply stays
  visible, faint, and a click says why.
- **Switches** (`━●` on, `●─` off), **choices** (segmented options or
  pickers) and **dialogs** that edit several settings at once with the web's
  apply model. Closing a dialog with unsaved edits asks **Discard changes?**.
- **Confirmations** with the web's question and buttons named after what they
  do, for example [Rotate] [Cancel]; a destructive one opens on Cancel.
- **`?`** lists every key of the current screen; **`F1`** About; **`F3`** the
  gateway host panel (Workflows paused, Start at login, restart, quit,
  update).

The **setup guide** (an admin's first run: Connection → Setup → Providers →
Multimodal → Models → Apps → Sandbox, with **Finish** or **Skip setup**
recorded on the gateway) and **browse mode** (free tabs) share the same
screens; `--wizard` / `--browse` choose at launch and `Ctrl+G` reopens the
guide. Every write is verified with a follow-up read and journaled; refusals
show the gateway's own sentence. The pages are usable at 80×24, in a light or
a dark theme (`--theme gateway-light` / `gateway-dark`).

The screen-by-screen guide — every table, button, dialog and confirmation —
is in [docs/console.md](https://github.com/lpalbou/abstractgateway/blob/main/docs/console.md#terminal-console-abstractgateway-console).

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

Run `abstractgateway-console --help` for the full list of flags, screens and
keys; inside the console, `?` lists the keys of the current screen.

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
- `src/ui/` — the shell (`ui/mod.rs` root + `ui/shell.rs`: header, rail or
  tab strip, status bar, `?` keys panel) and one module per screen.
  `src/ui/w/` is the shared widget layer every screen uses: actions and row
  buttons with tooltips, switches, segmented choices, the data table, form
  dialogs, the confirmation dialog, toasts and the two themes.
- `tests/headless_ui.rs` — the CaptureTerm suite; `tests/r15_click_*.rs` — a
  synthesized mouse click for every action of every screen, each with a test
  that fails when an offered action has no click test; `tests/r15_wording.rs`
  and the per-screen wording fixtures — the web console's sentences, checked
  byte for byte against `scripts/extract_web_wording.py`; `tests/r7w2_*.rs` —
  the web-parity pages; `tests/http_transport.rs` — `HttpTransport` against a
  local fake gateway; `tests/live_*.rs` — the ignored live tests;
  `scripts/pty_smoke.py` — the pty proof.

This is the second **validator app** for the AbstractTUI engine (epic:
`abstracttui/docs/backlog/planned/ports/0215_gateway_config_wizard_app.md`).
Engine friction found during the build is filed in the engine repo under
`docs/backlog/proposed/field-gateway/` (band 0900–1010).
