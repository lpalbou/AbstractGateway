# Consoles: web (`/console`) and terminal (`abstractgateway-console`)

AbstractGateway ships two operator consoles over the same admin HTTP API. Both
edit the same stores through the same endpoints with the same request bodies, so
a change made in one is immediately visible in the other.

| | Web console | Terminal console |
|---|---|---|
| Delivery | served by the gateway at `GET /console` (part of the `abstractgateway` Python package) | Rust crate [`abstractgateway-console`](https://crates.io/crates/abstractgateway-console), installed with `cargo install` |
| Sign-in | Gateway browser session (user id + token) | base URL + bearer token (`ABSTRACTGATEWAY_AUTH_TOKEN` or the Connection screen) |
| Best for | day-to-day administration in a browser, sandbox chat with media previews | SSH sessions, headless hosts, keyboard-only setup |

For how the stores behind these screens are owned (Gateway vs AbstractCore),
see [configuration.md](./configuration.md). For the endpoints themselves, see
[api.md](./api.md).

## Web console (`/console`)

Start the gateway and open `http://<host>:<port>/console`:

```bash
abstractgateway serve --host 127.0.0.1 --port 8081
# then open http://127.0.0.1:8081/console
```

The console uses the current origin, so it never asks for a gateway URL. With
user auth enabled, the first-login token is written to
`<ABSTRACTGATEWAY_DATA_DIR>/auth/bootstrap-admin-token`. The console covers:

- **Users & runtimes:** user records, token rotation, retained runtime
  reservations, the runtime inventory with sessions, data and caches.
- **Providers:** provider connections (OpenAI, Anthropic, OpenRouter, Portkey,
  LM Studio, Ollama, custom OpenAI-compatible endpoints) with write-only keys.
- **Multimodal capabilities:** capability route defaults, the text reasoning
  effort, and the MTP (speculative decoding) default.
- **Workflows:** every registered workflow with versions and entrypoints,
  import/export/delete, and versions that are not served (with the reason).
- **Sandbox:** quick chat and media generation against the configured defaults.
- **Resources:** memory/GPU meters, resident models (warm up, lock, unload),
  and session prompt caches.
- **Entities:** summoned-entity roster and management (see
  [entities.md](./entities.md)).
- **Models:** browse models that fit this machine, download them, and delete
  the ones you no longer need (details below).
- **Engines:** the local engines on the gateway host (Ollama, LM Studio, MLX,
  llama.cpp, vLLM, Hugging Face): installed or not, running or not, and a
  one-click install (details below).

### Models and Engines tabs

These two tabs are AbstractCore's own Models and Engines screens, embedded in
the gateway console. The same screens appear in `abstractcore serve`'s console
and, as terminal screens, in both terminal consoles, so they look and behave the
same everywhere. Everything they show and do happens **on the gateway host**:
the machine that runs `abstractgateway serve`, not the computer your browser
runs on.

**Models** (tab id `catalog`; the older **Resources** tab keeps id `models`):

- A line describing this machine (accelerator, memory the models can use, free
  disk).
- The catalog: model families with one row per downloadable artifact (engine,
  quantization, download size), whether its weights are already here
  (`installed`, `not downloaded`, `unknown`, `remote`), and a fit verdict
  (`fits`, `tight`, `too large`, `partial offload`, `unknown`). **Fits this
  machine** is on by default. Search, filter by engine or modality, or tick
  **Search Hugging Face** to include hub results (slower).
- The models already installed, per engine, with their size and location.
- Actions: **Download** and **Delete** (with a confirmation that names the
  model and any blocker, such as a model that is loaded right now). Downloads
  and deletes run as jobs with progress; you can cancel them.

**Engines** (tab id `engines`):

- One row per engine: supported on this host, installed, version, running,
  reachable, base URL and model count.
- **Install** opens a confirmation that shows the exact command it will run
  and the host it runs on, with a **Preview (dry run)** button that asks the
  gateway what it would run without running it.
  **Open download page** links to the vendor page (LM Studio is installed from
  its download page).

Keys (when the tab is visible and you are not typing in a field): `/` search,
`f` fits-only on/off, `r` refresh; on a focused row `w` download, `d` delete,
`i` install, `o` open the download page, `c` cancel the row's job.

Who can do what:

- Every signed-in user can browse both tabs.
- Download, Delete, Install and Cancel are for **admins** only; the buttons are
  disabled for other users, and the gateway refuses those calls from them.
- Installing an engine also needs the gateway setting
  [`allow_engine_install`](./configuration.md#allow_engine_install). It lives in
  the runtime configuration and is on by default only when the gateway listens
  on this machine only (`127.0.0.1`); a gateway reachable from the network
  refuses installs until an admin turns it on. A dry run (**Preview**) is
  always allowed.
- Every action is recorded in the gateway's audit log, and each job card shows
  the equivalent command, for example
  `abstractgateway models download ollama qwen3:8b`.

If the gateway's AbstractCore is older than 2.14.0, both tabs show a card saying
so, with the version installed and the upgrade command
(`pip install -U "abstractcore>=2.14.0"`); the rest of the console works as
before.

## Terminal console (`abstractgateway-console`)

Install it from crates.io (Rust 1.87 or newer):

```bash
cargo install abstractgateway-console
```

Connect it to a running gateway. Pass the token through the environment rather
than on the command line:

```bash
ABSTRACTGATEWAY_AUTH_TOKEN=... abstractgateway-console --url http://127.0.0.1:8081
abstractgateway-console --help
```

It opens as a guided wizard on first run and as free tabs afterwards, with eight
screens: Connection, Providers, Routes (capability defaults, including the MTP
selector and a Test verb per route), Users & Entities, Runtimes (runs with
cancel and steer, data homes), Workflows, Review & Test (the session's change
journal), Resources, Models and Engines. Every write is verified with a
follow-up read and recorded in the journal. Keys: `Tab` focus, `Enter`
activate, `1`-`9` and `0` screens, `r` refresh, `q` quit; each screen lists its
actions in the footer.

### Models and Engines in the terminal console

Screens 9 (**Models**) and 0 (**Engines**) are AbstractCore's own screens,
taken from the `abstractcore-console` crate rather than rebuilt, so they look
and behave the same in `abstractcore-console` and here. In the gateway console
they act on the gateway's host, through the gateway's
`/api/gateway/host/profile`, `/engines`, `/models/catalog`,
`/models/installed`, `/models/download`, `/models/delete`,
`/engines/{id}/install` and `/jobs/{id}` routes:

- **Models:** browse the catalog with a fit verdict for the gateway host,
  download (`w`), delete after a confirm that lists any blocker (`d`), filter
  (`/`), fits only (`f`), engine (`e`), installed view (`v`), cancel (`c`).
- **Engines:** see which engines are installed and running; install one (`i`)
  after a confirm that shows the exact command and the host it runs on (a dry
  run is offered), or open its download page (`o`).

Downloads, deletes and installs are admin-only and run on the gateway host; a
refusal (for example installs disabled on a remote gateway, or a loaded model)
is shown with the gateway's reason.

The terminal console needs no gateway-side component beyond the admin API. The
crate version is independent of the Python package version; see
[`console-tui/CHANGELOG.md`](../console-tui/CHANGELOG.md).

## Model residency from a shell

The same model routes the consoles drive are available from the Python CLI
against a running gateway:

```bash
abstractgateway models loaded --url http://127.0.0.1:8081
abstractgateway models load   --url http://127.0.0.1:8081 --provider ollama --model qwen3:4b
abstractgateway models unload --url http://127.0.0.1:8081 --provider ollama --model qwen3:4b
```

The token comes from `--token` or `ABSTRACTGATEWAY_AUTH_TOKEN`, and the URL from
`--url` or `ABSTRACTGATEWAY_URL`. The command prints the gateway's JSON answer
and exits non-zero when the gateway reports a failure. `unload --force` unloads
a locked model; in-flight calls on the model are cancelled first.
