# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.4] - 2026-09-25

Requires AbstractCore 2.15.3 and AbstractRuntime 0.4.36 (installed
automatically). The terminal console is unchanged (`abstractgateway-console`
0.8.0).

### Fixed

- **Ejecting a model frees its memory.** Ejecting a model from the console or
  tray now frees its memory from the whole gateway process (weights,
  prompt/KV caches, MLX cache); the memory figures show what the process
  really holds, and the Models menu no longer reports "No models loaded"
  while memory is still held. Gateways started with older versions must be
  restarted once to reclaim memory already held.
- The console's accelerator memory meter counts the MLX memory this gateway
  process holds (live buffers plus MLX's cache) when it is larger than the
  system-wide figure, which on macOS does not see MLX memory, and says which
  of the two it shows.
- With nothing listed in memory but memory still held, the console's Models
  table, the tray menu and the Activity window say "Gateway still holds N GB
  (no model listed)" and offer the two ways out: eject the held model, or
  restart the gateway. A model kept in memory by another part of the gateway
  shows as **resident via other holders**; ejecting it frees every holder.
- When the Models and Engines tabs cannot load, their card names AbstractCore
  2.15.3 as the version to install.

## [0.4.3] - 2026-09-25

Requires AbstractCore 2.15.2 and AbstractRuntime 0.4.35 (installed
automatically). The terminal console is unchanged (`abstractgateway-console`
0.8.0).

### Fixed

- **Installing the Assistant works on a new Mac.** Installing the Assistant
  (and engines) into the gateway's Python failed with "File not found:
  …/Library/Application" whenever the data folder's path contained a space,
  as the macOS `Application Support` folder does. The gateway now pins its
  own packages to their current versions in the install command itself.
- Continuum, when the gateway (or the tray, for a global install) starts it,
  gets its port, bind address and gateway URL as launch flags (`--port`,
  `--host`, `--gateway-url`) instead of environment variables. Continuum
  0.3.1's settings file (`~/.abstractcontinuum/settings.json`) takes
  precedence over the environment, so a saved port, host or gateway URL
  could otherwise have replaced the ones the gateway chose. The other four
  apps still receive them in the environment.
- The web console page no longer carries the source code's maintainer
  comments (design notes, review references, dates); it is about 11% smaller
  (1.25 MB to 1.12 MB). The artifact search box shows `YYYY-MM-DD` as its
  date example.
- When the Models and Engines tabs cannot load, their card names AbstractCore
  2.15.1 as the version to install (it said 2.14.0, which lacks the cancel
  attribution the gateway uses).
- The documentation site no longer publishes the backlog: planning notes
  under `docs/backlog/` stay in the repository and are left out of the site
  build. The one API page that cited a backlog item links to it on GitHub.

### Changed

- Dependency floors: `abstractcore>=2.15.2` (also in the `embeddings` extra)
  and `AbstractRuntime>=0.4.35` (also in the `apple` and `gpu` extras).
  AbstractCore 2.15.2's default MLX model is a repository that exists on
  Hugging Face, so a first MLX download no longer fails.

## [0.4.2] - 2026-09-24

Requires AbstractCore 2.15.1 and AbstractRuntime 0.4.34 (installed
automatically). The terminal console is unchanged (`abstractgateway-console`
0.8.0).

### Added

- **The Assistant as an app card.** AbstractAssistant, the desktop menu-bar
  app, appears after the five browser apps (`kind: "desktop"`, id
  `assistant`). **Install** installs `abstractassistant` into the gateway's own
  Python as a job, with every `abstract*` package kept at its current version.
  **Open** (`POST /apps/assistant/launch`) starts it on the gateway's computer,
  or brings a running one to the front; from another computer the route
  answers 409 `not_on_gateway_machine`. `abstractgateway apps install|launch
  assistant` and the tray's **Install Assistant…** do the same. See
  [docs/apps.md](docs/apps.md#the-assistant-a-desktop-app).

### Changed

- **One Install button per app.** Install only installs; the card then shows
  **Open**, and **Open in Terminal** beside it when the terminal app is
  installed. For Code, when a prebuilt terminal app exists for the computer,
  Install installs the browser app and the terminal app as one job with two
  progress rows (`parts`); Cancel stops both, and a failed terminal part keeps
  the browser app. `POST /apps/{id}/install` accepts `with_terminal` (default
  `true`); app rows carry `kind` and `install_parts`. "Install terminal app"
  alone is under **Technical details**.
- **Tray:** **Install X…** runs the same install and no longer opens the app;
  a notification says when it is installed and the menu offers **Open X**.
- The first-run guide's Apps step is titled "Apps that work with this gateway".
- Dependency floors: `abstractcore>=2.15.1` (also in the `embeddings` extra)
  and `AbstractRuntime>=0.4.34` (also in the `apple` and `gpu` extras).

### Fixed

- The download card in the console keeps its file list open while progress
  updates, and **Cancel** asks for confirmation before stopping a download.
- A failed download shows why it ended (`ended_reason`: a dropped connection,
  a Hub error, a restart), and a cancelled one says who cancelled it and when.
  `POST /models/download/{id}/cancel` accepts `{"via": "console"}` and records
  the admin who asked. See [docs/model-downloads.md](docs/model-downloads.md).
- The model catalog's fit tooltip compares the model's needs with the usable
  memory used by the verdict.

## [0.4.1] - 2026-09-24

Requires AbstractCore 2.15.0 and AbstractRuntime 0.4.33 (installed
automatically). The terminal console ships as `abstractgateway-console` 0.8.0
(see `console-tui/CHANGELOG.md`). The `v0.4.0` tag was not published to PyPI;
0.4.1 is the first release with the changes below.

### Upgrade notes

- **Login service:** run `abstractgateway service enable` once on machines
  where the gateway starts at login, then restart it (`abstractgateway service
  install`, or log out and back in). Registrations now start plain
  `abstractgateway serve` so the Network setting applies; `service status`
  reports older registrations as `broken` / *needs repair*.
- **Settings instead of environment variables:** browser origins, trust proxy,
  the apps settings, the backlog folder and the exec runner are runtime
  settings. The matching environment variables still work as a start-time
  fallback (and, for origins and trust proxy, as a pin), and every surface says
  when one is in effect.
- **Default app ports** follow the framework stack map: Observer 3001,
  Continuum 3002, Code 3003, Entity 3004, Flow 3005.
- **User accounts off:** only admin accounts can sign in to the console and
  the browser apps; existing non-admin sessions are ended at their next use.

### Added

- **Network exposure** (`localhost`, `lan`, `internet`): one setting, changed
  from the console's new **Network** tab, the terminal console, the tray or
  `abstractgateway network status|show|set|addresses|restart`
  (`GET/POST /api/gateway/network`, contract `gateway_network_v1`). `lan` and
  `internet` require user accounts; `internet` also requires an explicit
  acknowledgement. Changes apply at the next start, and `serve --host/--port`
  override the setting. See
  [docs/configuration.md](docs/configuration.md#network-exposure-localhost--local-network--internet).
- **Reverse proxy settings:** `allowed_origins` and `trust_proxy`, changed
  from the console (Network → *Advanced: reverse proxy*), the terminal console
  or `abstractgateway network set --allowed-origins … --trust-proxy on|off`,
  applied to the next request without a restart.
- **Browser apps managed by the gateway:** Flow Editor, Code, Observer,
  Continuum and Entity can be installed, started, stopped, updated and opened
  signed in from the console's **Apps** tab, the first-run guide, the tray, the
  API (`/api/gateway/apps`) and `abstractgateway apps …`. The gateway installs
  Node.js when the machine has none, checks every download, supervises the
  apps, and detects apps started outside it. `POST /apps/{id}/open` accepts a
  `path` inside the app. See [docs/apps.md](docs/apps.md).
- **Apps settings** `apps.node`, `apps.ports`, `apps.host`,
  `apps.npm_registry`, `apps.pypi_url`: from the Apps tab, the terminal console
  or `abstractgateway apps config get|set`.
- **Code in the terminal:** "Open in Terminal" opens Code's terminal app on
  the gateway machine, signed in through a one-time code; `abstractgateway
  apps install-tui|tui-command code` do the same from a shell.
- **Engine installs without a terminal:** Ollama and LM Studio install from
  the vendors' signed apps on macOS, llama.cpp from prebuilt wheels, and a
  step that needs the Apple command-line tools or an administrator password
  pauses (`needs_tools`, `needs_admin`) until you continue through the
  operating system's own dialog. New `/api/gateway/engines/*` routes and
  `abstractgateway engines continue|cancel|start|stop`. See
  [docs/engines.md](docs/engines.md).
- **Model downloads with real progress:** bytes, speed, time left and per-file
  rows for Hugging Face, MLX, Ollama, LM Studio and Supertonic; a `stalled`
  state; "Use recommended defaults" as one parent job;
  `POST /models/download/{id}/cancel` and `GET /models/downloads/stream`
  (Server-Sent Events). See [docs/model-downloads.md](docs/model-downloads.md).
- **Model catalog as cards:** one card per model with all its builds, filters
  (search, 4-bit / 8-bit / other, provider, capability, status, fits this
  computer), Hugging Face search, and filters kept in the address.
- **Recommended text model per computer:** on a Mac, AbstractCore picks an MLX
  build by memory; the guide, "Use recommended defaults" and the tray follow
  that pick.
- **Backlog folder and exec runner settings:** a fresh gateway keeps its own
  backlog in `<data dir>/backlog/`; choose another folder with `abstractgateway
  config set triage_repo_root PATH`, the console or Continuum, or for one run
  with `serve --backlog-root PATH` and `--exec-runner on|off`.
  `abstractgateway config get|set|unset` change any runtime setting from a
  terminal. `GET /api/gateway/backlog/status` reports the folder.
- **Tray control centre:** start at login, apps, models (eject, load), network
  mode and addresses, and a console link that signs you in. See
  [docs/tray.md](docs/tray.md).
- **Login service:** `abstractgateway service enable|disable|status` (states
  `on`, `off`, `broken`, `other`), an XDG autostart entry on Linux without a
  systemd user manager, and `--pin-command-line` to keep `--host/--port` on the
  command line.
- **Console:** a full-page first-run guide, engine cards, app cards with one
  action row, a **Technical details** switch, and the header widgets of the
  AbstractFramework UI kit. The create-user dialog follows the user-accounts
  mode.
- `POST /api/gateway/session/claim` reports who minted the link
  (`claim.created_by`: `serve`, `cli` or `tray`).
- `GET /api/gateway/models/installed` rows carry `kind`, `tasks` and
  `tasks_source`.
- `abstractgateway --version`.

### Changed

- Windows login item: a per-user `HKCU\…\Run` value replaces the Startup
  folder shortcut (the shortcut is removed on install).
- Someone at the gateway machine may install engines and apps by default,
  whatever address the gateway listens on; remote callers still need
  `allow_engine_install`.
- Messages that used to ask for an environment variable now name the setting
  or command to use.
- `serve` prints the admin token again on a loopback bind; `serve
  --print-token` / `--no-print-token` control it.

### Fixed

- The saved backlog folder is used by every backlog, report, triage and
  process route and by the exec runner; a folder that disappears answers
  `404` with the reason.
- Model downloads keep working after an MLX model is loaded, and a restart
  from the tray or console no longer carries in-process Hugging Face offline
  flags into the new process.
- A leftover browser app from a gateway that died is stopped on Linux.
- No false "PyTorch was imported" GGUF warning on hosts without Apple silicon
  or llama-cpp-python.
- Engine installer downloads use the per-OS user cache directory.
- `abstractgateway network … --data-dir DIR` uses `DIR`.
- `service uninstall` on Linux works when no unit file exists.
- The consoles explain in words why MTP did not run.

### Security

- With user accounts off, a non-admin account can no longer sign in and change
  the operator's settings (`401 user_accounts_off_admin_only`); creating such
  an account answers `409`.
- The last enabled admin account cannot be deleted, disabled or demoted
  (`409 last_admin`).
- `POST /bundles/{id}/deprecate` and `/undeprecate` apply the shared-registry
  ownership check.
- `lan` and `internet` are refused when the gateway was started with read
  protection off (`ABSTRACTGATEWAY_PROTECT_READ=0`).

## [0.3.0] - 2026-09-23

This release requires AbstractRuntime 0.4.33 and AbstractCore 2.14.0
(installed automatically).

### Added
- **Models and Engines tabs in the web console.** Browse models that fit this
  machine, download or delete them, and see and install local engines
  (Ollama, LM Studio, MLX, llama.cpp, Hugging Face). These are AbstractCore's
  own screens, embedded in the gateway, so the gateway and
  `abstractcore serve` show the same data and the same actions. Download,
  delete and install are admin-only; an install first shows the exact command
  it will run on the gateway host. See [docs/console.md](docs/console.md).
- **The first-run guide uses them.** The engines step lists the real engines
  on this machine with an install button. The model step lists models that
  fit, downloads one, and sets an installed model as the default text model.
- **Routes** (same bodies and payloads as AbstractCore's `/acore/*`):
  `GET /api/gateway/host/profile`, `GET /api/gateway/engines`,
  `GET /api/gateway/engines/{id}`, `POST /api/gateway/engines/{id}/install`,
  `GET /api/gateway/models/catalog`, `GET /api/gateway/models/installed`,
  `POST /api/gateway/models/delete`, `GET /api/gateway/jobs`,
  `GET /api/gateway/jobs/{id}` and `POST /api/gateway/jobs/{id}/cancel`.
  Every POST is admin-only and in the audit log. An AbstractCore older than
  2.14.0 answers 501 with the upgrade command instead of failing.
  See [docs/api.md](docs/api.md#models-and-engines).
- **Commands:** `abstractgateway models list|catalog|search|download|delete|jobs|cancel`
  and `abstractgateway engines status|install|open`, with the same arguments
  and exit codes as `abstractcore models|engines` (0 ok, 1 error, 2 refused).
  They call the running gateway; `--local` runs them in-process instead.
  Job cards in the consoles show these commands.
- **`allow_engine_install`** (runtime config): engine installs from the
  console or API run on the gateway host, so they are on by default only for
  a gateway bound to loopback. Dry runs are always allowed. See
  [docs/configuration.md](docs/configuration.md#allow_engine_install).
- `abstractgateway claim` and `abstractgateway-config claim-url` accept
  `--base-url` as another name for `--url` (the bootstrap installers use it).
- **console-tui (crate `abstractgateway-console` 0.7.0, versioned separately):**
  the terminal console gains screens 9 **Models** and 0 **Engines**, which are
  AbstractCore's shared screens from the `abstractcore-console` crate mounted
  over the gateway's `/api/gateway/models/*`, `/engines/*`, `/host/profile`
  and `/jobs/*` routes. See
  [console-tui/CHANGELOG.md](console-tui/CHANGELOG.md).
- **Zero-configuration first run.** With no auth configured, `abstractgateway serve`
  binds `127.0.0.1`, enables user auth, creates `default/admin`, and prints a
  one-time console sign-in link instead of a token. See
  [docs/first-run.md](docs/first-run.md).
- **One-time sign-in links:** `abstractgateway claim [--open]` and
  `abstractgateway-config claim-url [--open]` mint a single-use, 10-minute link
  (`/console#claim=<code>`); `POST /api/gateway/session/claim` redeems it for an
  admin browser session from a loopback peer only.
- **First-run guide in the web console** (host summary, local engines, default
  model with recommended downloads, apps, CLI equivalents), opened once per
  data folder and reachable later from the **Setup** button.
  `GET /api/gateway/host/first-run` and `POST` (admin) hold its state.
- **`abstractgateway service install|uninstall|status`**: start the gateway at
  login as a macOS LaunchAgent, a Linux systemd user unit, or (experimental) a
  Windows Startup shortcut, with `--dry-run`, free-port selection and a
  persisted port.
- `serve --data-dir`.
- `abstractgateway-config status --json` gains `schema`
  (`gateway_config_status_v1`), `data_dir_source`, `data_dir_reason`,
  `auth_mode`, `auth`, `service`, `claim_pending`, `claims`, `first_run` and
  `serve`; `GET /api/gateway/host/state` gains a `gateway` block with the same
  facts.

### Changed
- **Model downloads run in AbstractCore's job registry.** `POST /models/download`
  and `GET /models/download/{job}` keep their `{ok, job}` envelope and
  behaviour (a queued job reads `running`, a duplicate request joins the
  running job), and the job is also readable at `GET /api/gateway/jobs/{id}`.
  The job now carries AbstractCore's fields as well (`schema`, `job_id`,
  `kind`, `log_tail`, `command`, `cli_equivalent`); `started_at` is an
  ISO-8601 time instead of a Unix timestamp. Jobs started by the
  `abstractcore` CLI on the same machine appear in the job list.
- **Default data folder.** When `ABSTRACTGATEWAY_DATA_DIR` is unset, the gateway
  uses `./runtime` only if it already exists in the working directory, and
  otherwise the per-user data folder (macOS
  `~/Library/Application Support/AbstractGateway`, Linux
  `$XDG_DATA_HOME/abstractgateway`, Windows `%LOCALAPPDATA%\AbstractGateway`).
  The `triage-reports`, `triage-apply`, `backlog-exec-runner` and `data list`
  commands use the same default as `serve` (they previously defaulted to
  `./runtime/gateway`). Set `ABSTRACTGATEWAY_DATA_DIR` to keep any other layout.
- **`serve --host` default.** `127.0.0.1` when no auth setting is present;
  `0.0.0.0` (unchanged) when any auth setting is present.
- **The bootstrap admin token is no longer printed** on loopback starts; it
  stays in `<data dir>/auth/bootstrap-admin-token`. Set
  `ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=1` to print it.
- **Windows:** the runner's singleton lock uses `msvcrt.locking`, so two
  gateways on one data folder no longer both run workflows.

## [0.2.30] - 2026-09-23

This release requires AbstractRuntime 0.4.32, AbstractAgent 0.3.13 and
AbstractMemory 0.3.0 (installed automatically). It also folds in the
`[0.2.29]` changes below, which were never published separately.

### Added
- **Stop kill switch.** A `cancel` command (the Stop button) cancels the run
  tree and stops the model call that is executing. If a call of the cancelled
  tree is still running after `stop_kill_switch_s` seconds (runtime config key,
  or `ABSTRACTGATEWAY_STOP_KILL_SWITCH_S`; default `10`, `0` disables), the
  gateway kills that inference in process. The gateway process, other runs and
  the HTTP API keep working. Stopped calls are recorded as `cancelled` ledger
  steps with `cancelled_by` / `killed_by`. See
  [docs/configuration.md](docs/configuration.md#stop-and-the-kill-switch).
- **`abstractgateway models loaded|load|unload`.** List, warm and eject models
  on a running gateway from a shell, through the same routes the consoles use
  (`--url`, `--token`, `--provider`, `--model`, `--force` for a locked model).
- **MTP (speculative decoding) controls.** `speculation` is accepted on
  `/runs/start`, `/runs/schedule`, `/sandbox/generate` and as
  `_runtime.speculation` (`false` = Off, a native-MTP object selects a depth).
  The web console and the console TUI edit the Core-owned default
  (`options.speculation` on the text route) with an MTP selector.
- **Desktop tray icon for `abstractgateway serve`** (install the `tray` extra):
  open the console, pause/resume workflows, unload models, and watch memory,
  GPU and recent runs. See [docs/tray.md](docs/tray.md).
- **Pause / resume execution** (`POST /api/gateway/host/pause|resume`, admin;
  `GET /host/runner`). A paused runner still applies commands, so Stop works.
- **Restart and self-update** from the tray or console (`POST /host/restart`,
  `GET /host/update`, `POST /host/update/check|start`), aware of pip, uv, pipx,
  editable and Docker installs.
- **Host views:** `GET /host/metrics/live` (GPU, memory and execution state in
  one call) and `GET /host/runs` (recent runs across every data plane, admin).
- **Workflows tab in both consoles** listing every registered workflow with its
  versions and entrypoints, plus import, export
  (`GET /api/gateway/bundles/{bundle_id}/download`) and delete. Versions that
  cannot be served are listed in `skipped` with the reason.
- **Out-of-the-box workflows.** A fresh install serves `basic-agent`,
  `coding-agent` (`coder` entrypoint), `deep-research`, `co-scientist`,
  `docs-qa`, and the `react-agent` / `codeact-agent` / `memact-agent` native
  loops. See [docs/shipped-workflows.md](docs/shipped-workflows.md).
- **Durable session replay.** `use_session_history` seeds a run's
  `context.messages` from the session's prior turns (with a message cap), and
  `GET` history-bundle / session-bloc endpoints serve replayable transcripts.
- **Summoned entities.** Persistent entities with their own homes, identity,
  memory and lifecycle: `abstractgateway entity create|list|inspect|verify|chat`
  and `/api/gateway/entities/*` (summon with a queue, chat, visits,
  sleep/wake/pause, diary, skills, voice, task inbox, tool policy). See
  [docs/entities.md](docs/entities.md).
- **Run-level skills selection** and skills/MCP inventories for launch surfaces.
- **One seam for AbstractCore-owned configuration** (`core_config.py`); the
  text reasoning effort is editable from the Gateway.
- `inject_guidance` runner command, durable `emit_event` delivery
  (`payload.durable: true`), and a declared environment-variable registry.

### Changed
- **One gateway-owned workspace per session**, not per run (HTTP API and
  Telegram bridge). The system prompt stays byte-stable across turns, so
  prompt caches are reused.
- **Skills selection never widens an explicit tool ceiling.** If a run passes
  `_runtime.allowed_tools`, include `read_skill` yourself when you want the
  skill tool available.
- **Cancellation, turn grounding and agent loops follow AbstractRuntime 0.4.32
  and AbstractAgent 0.3.13:** cancelled ledger steps have status `cancelled`;
  stored user turns may start with a `<runtime_metadata>` grounding envelope;
  tool loops append messages marked `_af_synthetic`. Clients that render
  transcripts should handle all three.
- The fresh-install capability seed belongs to the install (it is not re-applied
  on every boot), and the capability-defaults read reports its provenance.
- `dp-*` workflow ids are renamed `deep-*`.
- The tray menu has a Workflows section; the `desktop_tray` setting was removed
  (the icon is present whenever `serve` runs on a desktop).

### Fixed
- The ledger stream's `event: done` follows the run's terminal save instead of
  an idle timer, and the runner wakes on events instead of polling, so a
  no-tool chat turn finishes as soon as its answer is saved.
- The shipped `basic-agent` bundle (0.0.5) no longer waits 3 s after answering.
- Workflow publish, promote, upload and reload no longer block health checks.
- `POST /prompt_cache/prepare_modules` forwards `thinking`.
- Runs of catalog-published workflows are listed normally.
- An event-entry flow no longer gets a second derived listener (no duplicate
  messages or tool calls).
- A non-object client `context` is kept as sent.
- A configured `ABSTRACTGATEWAY_BACKLOG_CODEX_BIN` counts as an available
  executor.
- Idle file-store deployments no longer burn CPU, and valid credentials are no
  longer caught by the auth lockout.
- Gateway writes of Core-owned configuration keep the fields they did not name.

### Security
- Writes to the shared workflow registry (upload, delete, reload, deprecate,
  publish) require an admin principal; per-user registries are unchanged.
- `POST /models/download` and `POST /config/capability-defaults/apply-recommended`
  require an admin principal.

## [0.2.29] - 2026-08-27

Never published separately; these changes ship in 0.2.30.

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
