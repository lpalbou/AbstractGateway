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
abstractgateway serve
# then open http://127.0.0.1:8080/console
```

On a first run, `serve` prints a one-time link that signs you in
([first-run.md](./first-run.md)); `abstractgateway claim --open` prints a new
one. The console uses the current origin, so it never asks for a gateway URL.
You can also sign in with a user id and token (the admin's first token is in
`<data dir>/auth/bootstrap-admin-token`).

The sidebar lists these tabs:

| Tab | What it covers |
|---|---|
| **Users & Entities** | user records, token rotation, retained runtime reservations, and the summoned-entity roster ([entities.md](./entities.md)) |
| **Runtimes** | execution planes: runs (cancel, steer), sessions, data and caches |
| **Workflows** | every registered workflow with versions and entrypoints, import, export, delete, and versions that are not served (with the reason) |
| **Providers** | provider connections (OpenAI, Anthropic, OpenRouter, Portkey, LM Studio, Ollama, custom OpenAI-compatible endpoints) with write-only keys |
| **Multimodal** | capability route defaults, the text reasoning effort, the MTP (speculative decoding) default, and model weights per route |
| **Sandbox** | quick chat and media generation against the configured defaults |
| **Resources** | memory and GPU meters, resident models (warm up, lock, unload), session prompt caches, and the **Gateway** card (pause, update, restart, desktop icon) |
| **Models** | browse models that fit this machine, download them, delete installed ones (below) |
| **Engines** | the local engines on the gateway host: installed or not, running or not, install, start, stop (below) |
| **Apps** | the browser apps, Code's terminal app and the desktop Assistant (below), plus *Advanced: apps settings* and *Advanced: backlog settings (Continuum)* |
| **Network** | who can reach the gateway (localhost only, local network, internet), its addresses, and *Advanced: reverse proxy* ([configuration.md](./configuration.md#network-exposure-localhost--local-network--internet)) |

The **Technical details** switch at the bottom of the sidebar shows commands,
route ids and other technical information throughout the console. The top bar
holds the docs assistant (answers grounded on this gateway's documentation),
the appearance settings, the **Setup** button that reopens the first-run guide
(admins), and the sign-out control.

### Models and Engines tabs

Everything these two tabs show and do happens **on the gateway host**: the
machine that runs `abstractgateway serve`, not the computer your browser runs
on. The catalog data, the presence checks and the fit verdicts come from
AbstractCore (`GET /api/gateway/models/catalog`, contract `model_catalog_v1`);
downloads are the gateway's own jobs (see [Model downloads](model-downloads.md)).

**Models** (tab id `catalog`) shows the catalog as **one card per model**:

- The card header: the model's name, organisation, parameter count and
  licence, its capabilities (Text, Thinking, Tools, Vision, Audio, Embedding,
  Voice, Image) and a **Starter** badge for the models of the recommended
  starter set.
- The card body: one row per downloadable build (artifact) of that model: the
  provider (MLX, Ollama, LM Studio, Hugging Face, ...), the artifact id (long
  ids are shortened with "..."; hover to read the whole id, click it to copy
  it), the quantization (4-bit, 8-bit, 16-bit, ... and its bits per weight),
  the download size ("about" when the size is estimated from the parameter
  count), whether the weights are already here (Downloaded, Not downloaded,
  Unknown, Remote) and whether it fits this machine (Fits, Tight, Partial
  offload, Too large; hover the pill for the numbers behind it). The build
  recommended for this computer comes first and is marked; the others are
  quieter.
- One action per row: **Download** (a download shows its progress bar with
  bytes, speed and time left, and **Cancel**, the same progress display as the
  setup guide), then **Use as default** once a text model is downloaded (it
  sets the default text model, like the Multimodal tab).
- Many models have 8-bit builds next to the 4-bit ones when upstream publishes
  them (MLX `-8bit` repositories, Ollama `-q8_0` tags, GGUF `Q8_0` files).
  Every build the catalog knows is listed; nothing is cut off.

The filter bar above the cards:

- **Search** matches model names, organisations and artifact ids (every word
  must match). **Escape** clears it.
- **Quantization**: All, 4-bit, 8-bit, Other (every other class: 16-bit, full
  precision, 2/3/5/6-bit and builds whose reference names no quantization).
  The classes come from AbstractCore's `quant_class` field.
- **Catalog / Hugging Face**: the switch left of the search box. In
  **Hugging Face** mode, type a name and press **Enter** (or **Search**): the
  gateway searches the Hugging Face Hub (answers are cached for 24 hours) and
  the results show as the same cards, with a **Hugging Face** badge: provider,
  artifact id, quantization when the result names one ("Not stated"
  otherwise, never guessed), size from the Hub, fit, and **Download** with
  the same progress bar. Capabilities of a Hub result are unknown until it is
  installed, so it offers no **Use as default** from here. When the Hub has
  nothing for the query the view says so; when the gateway host cannot reach
  the Hub it says "Hugging Face could not be searched right now" (the raw
  reason sits behind **Show details**), and results the Hub only partly
  answered carry a warning. The query is part of the address:
  `/console#catalog?hf=smollm`.
- **Provider**, **Capability** and **Status** (Downloaded, Not downloaded)
  chips, each with the number of builds it would show.
- **Fits this computer** hides the builds that do not fit (only Fits and Tight
  remain).
- A live count: "12 of 77 models · 31 artifacts shown". The row with the search
  box, the count and the filters in use stays under the header while you
  scroll; **Filters** brings the chips back into view. When nothing matches,
  **Clear filters** resets them.
- The filters are part of the address: `/console#catalog?quant=8bit&provider=mlx&fits=1`
  opens the tab with exactly that view, so a link reproduces it. The keys are
  `q`, `quant` (`4bit`, `8bit`, `other`), `provider`, `cap`, `status`
  (`downloaded`, `not_downloaded`), `fits=1` and `hf` (Hugging Face mode and
  its query).

Below the cards, **On this computer** is AbstractCore's own list of the models
the local engines hold (including models that are not in the catalog), with
their size and location, and **Delete** (with a confirmation that names the
model and any blocker, such as a model that is loaded right now).

The setup guide's **Default model** step shows the same catalog cards with
**Fits this computer** already on; **Open in the Models tab** carries the
filters over. An engine card's **Browse models** opens the tab filtered to that
engine's builds.

**Engines** (tab id `engines`):

- One card per engine with a status pill (Ready, Running, Installing, Needs
  your approval, Needs Apple tools, Not installed, Not for this computer),
  its version, base URL and model count, and one primary action for its
  state: **Install**, **Start**, **Stop**, **Continue with administrator
  password**, **Install tools** or **Try again**.
- **Install** opens a confirmation that shows what will run and the host it
  runs on, with a **Preview (dry run)** button. Ollama and LM Studio offer
  "Install" (just for you, no password) and, when that plan needs an
  administrator, "Install for all users (administrator)". What each install
  does, engine by engine: [engines.md](./engines.md).
- An install shows its progress on the card, one plain sentence first and the
  full log behind **Show details**. **Open download page** links to the
  vendor page.

Keys (when the tab is visible and you are not typing in a field): `/` search,
`f` fits-only on/off, `r` refresh; on a focused row `w` download, `d` delete,
`i` install, `o` open the download page, `c` cancel the row's job.

Who can do what:

- Every signed-in user can browse both tabs.
- Download, Delete, Install and Cancel are for **admins** only; the buttons are
  disabled for other users, and the gateway refuses those calls from them.
- Installing an engine also needs the gateway setting
  [`allow_engine_install`](./configuration.md#allow_engine_install). It lives in
  the runtime configuration and is on by default when the gateway listens on
  this machine only (`127.0.0.1`), and, whatever it listens on, for someone
  using the console on the gateway machine itself. A browser on another
  computer is refused until an admin turns the setting on. A dry run
  (**Preview**) is always allowed.
- Every action is recorded in the gateway's audit log, and each job card shows
  the equivalent command, for example
  `abstractgateway models download ollama qwen3:8b`.

If AbstractCore on the gateway host is missing or too old for these routes,
both tabs show a card with the installed version and the upgrade command; the
rest of the console keeps working.

### Apps tab

The Apps tab and the setup guide's Apps step show the same cards (the apps
themselves are described in [apps.md](./apps.md)). What a plain user sees on
each card: the app's mark, name and status pill (Not installed, Installed,
Running, Installing, Stopped unexpectedly, Keeps crashing), one line of
description (hover it for the whole sentence), and one row of buttons. The
button rows of the cards side by side are always at the same height.

| The app is | The action row |
|---|---|
| not installed | **Install** (installs Node.js first when the gateway needs it, then the app and, for Code when a ready-made download exists for this computer, its terminal app too; nothing opens by itself) |
| installing | a progress bar above the row (with one row per part: "Code in the browser", "Code in the terminal"), and **Cancel** (it stops both) |
| an install failed | the reason, **Show details**, and **Install** again |
| installed and running | **Open** (a new tab, already signed in) |
| installed but stopped, or crashed | **Open** (starts it, then opens it); a crash also shows the reason, with **Show details** |

Code has a terminal version too. Next to Code's Open: **Open in Terminal**
when the terminal version is installed and the browser is on the gateway
machine (a new terminal window opens there, signed in). The plain view never
installs the terminal version on its own: Code's Install installs both. When
the terminal version needs the Rust toolchain, or when the browser is on
another computer, the plain view shows no terminal button (the commands are
under Technical details, as is "Install terminal app" for a browser app that
is installed without it).

The last card is the **Assistant**, the desktop app: **Install** when it is
not on the gateway's computer, then **Open** (it starts in the menu bar of the
gateway's computer, or comes to the front when it already runs). From another
computer the card says "The Assistant runs on the gateway's computer: open it
there." with no button. See [apps.md](./apps.md#the-assistant-a-desktop-app).

A result box appears only after something you did ("Code opened in a new
Terminal window, signed in to this gateway.", "Flow Editor opened in a new
tab.") and closes itself after a few seconds. A failure stays, with the
gateway's reason and **Show details** (the full response or log).

The **Technical details** switch (bottom left of the console, and in the
guide) adds a secondary line under each card's buttons, and removes it again
when switched off:

- **Stop** (a running app), **Start** (start without opening), **Show log** /
  **Hide log** (the app's log, the log file's path, **Show more** up to 5000
  lines), **Update to X** when a newer version is published, and the version.
- For Code's terminal version: its version, **Update terminal app to X**, and
  the exact command to copy (one line, **Copy**): the command that opens it,
  `cargo install abstractcode` when it needs the Rust toolchain, or, for a
  browser on another computer, the command to run there and the one-time
  `abstractcode login` line.
- The app's local address, the `npx @abstractframework/<app>` line, and the
  log of a finished install.
- For an app started outside the gateway (the development stack, `npx`, a
  service): "Started outside the gateway on port 3001" instead of Stop,
  Start, Show log and Update; the card shows the Running pill and **Open**
  like any running app ([apps.md](./apps.md#apps-started-outside-the-gateway)).

Installing, starting, stopping and updating need an admin; other users see the
buttons disabled with the reason on hover.

## Terminal console (`abstractgateway-console`)

Install it from crates.io (Rust 1.87 or newer):

```bash
cargo install abstractgateway-console
```

Connect it to a running gateway. Pass the token through the environment rather
than on the command line:

```bash
ABSTRACTGATEWAY_AUTH_TOKEN=... abstractgateway-console --url http://127.0.0.1:8080
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
abstractgateway models loaded --url http://127.0.0.1:8080
abstractgateway models load   --url http://127.0.0.1:8080 --provider ollama --model qwen3:4b
abstractgateway models unload --url http://127.0.0.1:8080 --provider ollama --model qwen3:4b
```

The token comes from `--token` or `ABSTRACTGATEWAY_AUTH_TOKEN`, and the URL from
`--url` or `ABSTRACTGATEWAY_URL`. The command prints the gateway's JSON answer
and exits non-zero when the gateway reports a failure. `unload --force` unloads
a locked model; in-flight calls on the model are cancelled first.
