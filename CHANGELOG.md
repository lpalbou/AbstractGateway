# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.2] - 2026-09-24

This release requires AbstractCore 2.15.1 and AbstractRuntime 0.4.34
(installed automatically). The terminal console is unchanged
(`abstractgateway-console` 0.8.0).

### Added

- The **Assistant** (AbstractAssistant, the desktop menu-bar app) is an app
  card (`kind: "desktop"`, id `assistant`, after the five browser apps):
  presence detection shared with the tray (`apps_desktop.detect_assistant`:
  the command next to the gateway's Python or on PATH, the package without
  importing it, `AbstractAssistant.app` in /Applications or ~/Applications;
  version from the package or the app's Info.plist; Running from the process
  list). **Install** installs `abstractassistant` into the gateway's own Python
  as a job, with every `abstract*` package pinned by a constraints file.
  **Open** (`POST /apps/assistant/launch`) starts it on the gateway's
  computer with a scrubbed environment and nothing on its command line, or
  brings a running one to the front; from another computer 409
  `not_on_gateway_machine`, and the card says the Assistant runs on the
  gateway's computer. `abstractgateway apps install|launch assistant` work
  too. The tray offers **Install Assistant…** when it is missing.

### Changed

- Apps cards: ONE button, **Install** (it was "Install and open", plus a
  separate "Install for Terminal" on Code). Install only installs; the card
  then shows **Open**, and **Open in Terminal** beside it when the terminal
  app is installed. For an app with a terminal version that has a
  ready-made download for this computer (Code), Install installs the browser
  app AND the terminal app as one job: the job's new `parts` are the two rows
  the progress shows ("Code in the browser", "Code in the terminal"), Cancel
  stops both, and a failed terminal part keeps the browser app and says so.
  Without a ready-made download, Install installs the browser app only.
  `POST /apps/{id}/install` gains `with_terminal` (default true); rows gain
  `kind` and `install_parts`. Installing the terminal app alone is now a
  Technical details action ("Install terminal app").
- Tray: **Install X…** runs the same install (both parts) and no longer opens
  the app by itself; a notification says it is installed and the menu offers
  **Open X**.
- Guide step 4: "Apps that work with this gateway" (it lists a desktop app too).
- Dependency floors: `abstractcore>=2.15.1` (also in the `embeddings`
  extra) and `AbstractRuntime>=0.4.34` (also in the `apple` and `gpu`
  extras), for the download end reasons and the facade's
  `host_job_cancel(job_id, by=, user=)`.

### Fixed

- Downloads in the console are no longer cancelled by a stray click. The card re-rendered every half
  second, which closed the "Files" list each time and moved "Cancel download" into the place the
  file rows had been; the next click on the list cancelled the download ("Cancelled / Download
  cancelled. Download it again any time."). The list now stays open across updates, and Cancel asks
  first ("Stop this download?", with "Keep downloading" in the Cancel button's own place).
- A failed download shows its plain reason (a dropped connection, a Hub error, a restart) and a
  cancelled one says who cancelled it and when; `POST /models/download/{id}/cancel` accepts
  `{"via": "console"}` and records the admin who asked.
- The model catalog's fit tooltip compares the need with the usable memory (the verdict's own
  numbers), not the raw ceiling.

### Tests

- `tests/test_gateway_apps_install_and_assistant.py`: the combined install
  (both parts, web only without a prebuilt terminal app, a failed terminal
  part, cancel covering both, `with_terminal: false`), the Assistant's
  detection matrix and running check, install job (constraints, failure,
  cancel, installs off), launch (bundle, script, running, same-machine
  refusal, a launch that exits), routes, the tray's shared detection and
  menu lines, and the console card pins. Updated: apps routes and terminal
  tests (six rows), console first-run (one Install, the Assistant card). The
  test suite's conftest hides the real machine from the Assistant detection.
- `tests/test_gateway_console_download_cancel.py`: the keyed file list
  survives redraws, Cancel asks first and posts `{"via": "console"}` only on
  "Stop download", the end reason on the tile, the group card and the
  catalog, the usable-memory tooltip. `test_gateway_model_download_progress`:
  who cancelled, and why a download ended (never a bare "Cancelled").
- The test suite hides a terminal app installed on the developer's machine
  (`abstractcode` on PATH) from the apps manager, so the combined Install
  behaves in a test as on a clean CI runner; the two browser-install tests
  of `test_gateway_apps_manager` pass `with_terminal=False`. A single-flight
  download test waits for its second job before counting calls.

## [0.4.1] - 2026-09-24

This release requires AbstractCore 2.15.0 and AbstractRuntime 0.4.33
(installed automatically). The `v0.4.0` tag exists but 0.4.0 was never
published: its release run stopped at the test job (Linux-only test
failures, fixed below), so 0.4.1 is the first release of this wave. The
terminal console ships as `abstractgateway-console` 0.8.0 (see
`console-tui/CHANGELOG.md`).

### Added
- **A fresh gateway has a working backlog; the backlog folder and the exec
  runner are settings and launch flags (mission II, 2026-09-24).** The
  operator: "fix continuum for a new fresh install ... i don't like
  environment variables and these should be handled with proper settings and
  --param_name." Continuum's Board on a fresh gateway said "Backlog browsing is
  not configured ... set ABSTRACTGATEWAY_TRIAGE_REPO_ROOT". Now:
  - without a setting the gateway uses its own folder `<data dir>/backlog/`
    and creates the standard layout there on first use (`docs/backlog/`
    `overview.md`, `template.md`, `planned/`, `proposed/`, `completed/`,
    shipped as package data; nothing existing is overwritten);
  - ONE resolution (`runtime_config.resolve_backlog_root` /
    `resolve_exec_runner`): `serve --backlog-root PATH` / `--exec-runner
    on|off` > the saved setting (`triage_repo_root`, `backlog_exec_runner`) >
    a legacy environment value (reported as `source: env`, never needed) >
    the default. Every consumer calls it: the backlog, report, triage and
    process routes, the exec runner (at each poll), the skills shelf;
  - `GET /api/gateway/backlog/status` (available, source, reason; paths for
    admins) and the runtime-config rows carry label/help/cli, `available`,
    a path-free `reason`, `default_path`, and `stored_value` under a flag;
  - `abstractgateway config get [KEY]`, `config set KEY VALUE`, `config unset
    KEY`: every runtime setting from a terminal, through the running
    gateway's door when one serves the data dir (applies at once, audited),
    else the settings store;
  - the console's Apps tab gains *Advanced: backlog settings (Continuum)*
    (folder, exec runner, process manager, with source pills and *Use the
    gateway's own folder*);
  - one validation for every door: a folder must exist and contain
    `docs/backlog`, or be the gateway's own folder; switches are on/off (a
    typo refuses instead of storing "off").
- **The Entity card offers "Create your first entity" (mission JJ,
  2026-09-24).** The operator: with no entity yet, Open landed on an empty
  list with nothing to do. The apps payload gains `content_summary` on every
  row: `{"entities_count": n}` for Entity (the entry count of
  `GET /api/gateway/entities`, from the same registry, `null` when unknown or
  when an Entity started outside the gateway talks to another gateway),
  `null` for the other apps. At exactly 0 the card's one primary button reads
  "Create your first entity" and opens the app on its creation form; 1 or
  more, or unknown, keeps "Open". Same card on the guide's Apps step.
- **`POST /api/gateway/apps/{id}/open` accepts `path`** (e.g. `/#new`): where
  the browser lands inside the app after the signed-in handover. The path is
  bound to the one-time code and must be a path inside the app: `//host`, a
  full address, a backslash, whitespace or a control character is refused
  with 400 `invalid_app_path` and no link is made. docs/apps.md.
- **Console: the create-user modal follows the user-accounts mode.** With
  user accounts off (`/me` → `auth.user_auth_enabled: false`) it offers the
  admin role only and says "User accounts are off on this gateway: only admin
  accounts can sign in. Turn user accounts on to add members."; a gateway
  whose `/me` lacks the field is named in the modal; a 409
  (`user_accounts_off_admin_only`) is shown in the server's own words.
- **Reverse proxy settings instead of environment variables (mission Z).**
  Operator: "i explicitly told you i don't like env vars. most should be
  something one can configure from the consoles (wui+tui)". Browser origins
  (`allowed_origins`) and trust proxy (`trust_proxy`) are stored in the
  network setting and changed through the same door as the mode:
  `POST /api/gateway/network {allowed_origins?, trust_proxy?}` (`mode` is now
  optional), `abstractgateway network set --allowed-origins https://a,https://b
  --trust-proxy on|off`, `abstractgateway network show`, the console's Network →
  *Advanced: reverse proxy* (chips, validation, switch, "Saved · applies now")
  and the console TUI's Connection screen (origins edit line, trust checkbox).
  Both apply to the NEXT REQUEST: the security middleware reads them per
  request (one `stat()`, re-parsed on change), no restart. `GET /network`
  carries `reverse_proxy{allowed_origins, trust_proxy}` with `value`,
  `source: setting|env|default`, `overridden_by_env`, `effective`, `applies`;
  a POST answers `changed{field: {from, to, applies: live|restart|overridden_by_env}}`.
  Origins are validated (`scheme://host[:port]`, no path, no trailing slash,
  default port dropped, `*`/patterns flagged); a list with a bad entry is
  refused whole (400 `invalid_origins`, `errors[]`), and every door shows the
  same sentence. `ABSTRACTGATEWAY_ALLOWED_ORIGINS` / `ABSTRACTGATEWAY_TRUST_PROXY`
  still pin the values when set at start, and every surface says so in words.
  The running gateway's environment is recorded in the run record
  (`proxy_env`) so the CLI reports it, not its own shell's.
- **Browser apps settings `apps.*` (mission Z).** The five
  `ABSTRACTGATEWAY_APPS_*` knobs are runtime-config keys (`apps.node`,
  `apps.ports`, `apps.host`, `apps.npm_registry`, `apps.pypi_url`; registry
  `runtime_config.APPS_SETTINGS` with label/help), stored > env > default,
  written through `POST /api/gateway/admin/runtime-config {"apps.host": …}`,
  `abstractgateway apps config get|set`, the Apps page (*Advanced: apps
  settings*) and the TUI (Runtimes → *Runtime knobs* → *Edit apps settings*).
  `AppsManager` reads them at each use (`resolve_apps_setting`; an unknown
  name raises). `env_registry` rows name the replacing setting
  (`superseded_by`).
- **Setting changes on the audit line.** `POST /network` and
  `POST /admin/runtime-config` attach `setting_change` (fields from/to, or the
  refusal) to the security middleware's audit-log line for the request.

- **Apps that also run in a terminal: "Open in Terminal" (mission Y).** Each
  row of `GET /api/gateway/apps` gains `interfaces[]`: the browser app (kind
  `web`, mirroring the row) and, for Code, its terminal version (kind `tui`:
  `installed`, `version`, `install_available`, `install_method`
  `release_binary`|`cargo`, `install_blocked_reason`, `launch_available`,
  `launch_blocked_reason`, `launch_mode` `terminal`|`copy`, `command`,
  `install_command`, `signin_command`). The overview also carries
  `console_tui` (the gateway console's own terminal app, crates.io only).
  Presence only: the binary in `<data>/apps/bin/`, on PATH or in
  `~/.cargo/bin`, accepted when its `--help` names it (PyPI's unrelated
  `abstractcode` script is not it). `POST /apps/{id}/install-tui` (admin)
  installs Code's prebuilt binary from its GitHub release, checked against
  the release's `SHA256SUMS` and GitHub's per-file digest, smoke-tested with
  `--version` before it replaces anything; with no binary for the computer the
  row says "Needs the Rust toolchain" and gives `cargo install abstractcode`
  (409 `toolchain_required` + `command` from the route, never a fake button).
  `POST /apps/{id}/launch-tui` (admin) opens a new terminal window on the
  gateway machine (macOS Terminal, Linux terminal emulators, Windows `cmd`)
  only when the request comes from that machine (loopback socket peer,
  loopback `Host`, no proxy headers); otherwise 409 `not_on_gateway_machine`
  with the command to copy. The window's launcher script holds a one-time
  code (2 minutes, single use, deletes itself) that `tui_signin.py` trades at
  `POST /apps/tui-handover` (outside `/api/gateway`, loopback peers only) for
  a per-launch bearer token acting as the caller, accepted from loopback only,
  kept in the gateway's memory only, and handed to the app through its
  environment (never argv, a file or the browser). The ephemeral loopback
  token registry can now bind a token to a principal
  (`register_ephemeral_loopback_token(..., principal=)`). Console: the Apps
  step and Apps tab cards show "Also runs in your terminal" with "Open in
  Terminal", the command to copy (remote browser), "Install for Terminal", or
  "Needs the Rust toolchain" + the command; commands are one mono line with an
  ellipsis and Copy. The Done step says once that the console also exists as a
  terminal app, with `cargo install abstractgateway-console`. Tray: "Open Code
  in Terminal" when the gateway reports it installed. Terminal parity:
  `abstractgateway apps install-tui <id>` (same job and checksum rules; a
  refusal prints the reason and the cargo command), `abstractgateway apps
  tui-command <id>` (`POST /apps/{id}/tui-command`, admin, gateway machine
  only: the same one-use launcher without a window; prints the one-time
  sign-in line and the plain command), and a `terminal:` line per app in
  `abstractgateway apps list`. docs/apps.md "Terminal versions", docs/tray.md.
- **The guide's "Recommended for this computer" text card follows
  AbstractCore's per-computer pick (mission W1).** On a Mac the recommended
  text model is an MLX build chosen by memory (below 24 GiB Qwen3.5 9B,
  24 GiB to below 128 GiB Qwen3.8 27B, 128 GiB and above Qwen3.8
  Flash-Next); other computers keep LM Studio `qwen/qwen3.5-9b@4bit`. The
  Gateway holds no list of its own: the card, **Download all**, **Use
  recommended defaults** and the tray's "Your defaults" all read AbstractCore
  (`recommended_text_model()` through the runtime facade). The text entry of
  `/models/availability` `recommended` carries `catalog_id`, `basis`, `tier`,
  `fit_verdict`, `fits` and `warning`, and the card shows the warning when
  AbstractCore's estimate says the model may not fit. Catalog artifacts carry
  `quant_class` and `options` (docs/api.md, "Catalog artifacts").
- **Console: the model catalog is one card per model, with a filter bar
  (mission X2).** The Models tab (and the setup guide's Default model step)
  no longer shows one long table: each model is a card (name, organisation,
  parameters, licence, capabilities, a Starter badge) with its builds as
  compact rows (provider, artifact id shortened with a tooltip and copied on
  click, quantization, download size, weights and fit pills, one action:
  Download, then the shared progress bar with bytes, speed, time left and
  Cancel, then Use as default). The recommended build is first and marked.
  The filter bar: search, **4-bit / 8-bit / Other** from AbstractCore's
  `quant_class`, provider, capability, Downloaded / Not downloaded and **Fits
  this computer**, with a live count ("12 of 77 models · 31 artifacts
  shown"); its top row stays under the header while the cards scroll. The
  8-bit builds (MLX `-8bit`, Ollama `-q8_0`, GGUF `Q8_0`; LM Studio `@8bit` ids could not be verified upstream and are not listed)
  are listed next to the 4-bit ones, every build, never capped. The filters
  live in the address (`#catalog?quant=8bit&provider=mlx&fits=1`), so a link
  reproduces the view. A gateway whose AbstractCore does not send
  `quant_class` says so and keeps the quantization filter off (the class is
  never guessed in the browser). The guide's step shows the same cards with
  Fits this computer on and **Open in the Models tab**; an engine's **Browse
  models** opens the tab filtered to that engine. AbstractCore's installed
  list (with Delete) stays below the cards as **On this computer**. A
  **Catalog / Hugging Face** switch next to the search box brings back the
  old table's Hugging Face search: Enter asks the catalog API's Hub search and
  the results are the same cards (quantization "Not stated" when the result
  names none, size from the Hub, fit, Download with the same progress), the
  query lives in the address (`#catalog?hf=smollm`), and an empty answer or
  an unreachable Hub is said in plain words.
- **Console: every backend contract that landed after the redesign is wired
  (mission L2).** Downloads: "Download all" is ONE card for the parent job
  (`grp_…`: overall bar, bytes, speed, ETA, one row per model with its own
  bar and Cancel, plus "Cancel all"); the console follows
  `GET /models/downloads/stream` (SSE) and falls back to polling whenever the
  stream is not open; a stalled download reads "Stalled · no data for 18 s ·
  still trying" in the warning tone. Engines: Ollama / LM Studio offer the two
  real install locations from the gateway's own dry-run plans ("Install" =
  just for you, no password; "Install for all users (administrator)" only
  when that plan needs an administrator); Start/Stop, Cancel and the
  `continue` actions show progress on the button and a result on the card
  ("Re-check", "Install tools", "Continue with administrator password").
  Apps: "Show log" (the tail, how much is shown, "Show more" up to the
  route's 5000 lines, then the log file's path), "Update to <version>", a
  Node.js row with its own Install, and results on the card. A new
  **Network** tab (also on the guide's Done step): the three modes of
  `gateway_network_v1` as a segmented control with a plain explanation each,
  the refusal reason and its fix, the Internet warnings before the
  acknowledgement, "Restart to apply" / "Restart now" only when a restart can
  apply it (else the reason), and every address with its own Copy; the header
  shows the primary address with a copy button. Refusals from the apps and
  network routes (`{message}` / `{refused_reason}`) are shown in words, never
  as "HTTP 409", with the whole response behind "Show details".
- **Console: tray links land where they point.** `#claim=<code>&tab=<tab>`
  keeps the tab after the code is stripped, and a claim no longer re-opens the
  first-run guide once first run is completed (a `tray` link, per
  `claim.created_by`, never opens it); the "Setup guide" button always does.
  The guide is keyboard-complete: each step starts at its title, Tab walks
  its controls, Escape closes it and focus returns to what opened it.
- **`POST /api/gateway/session/claim` says who minted the link.** The
  response gains `claim: {created_by}`, one of `serve` (first run), `cli`
  (`abstractgateway-config claim-url` / `abstractgateway claim`) or `tray`
  (the tray's sign-in). It is `null` for a link minted before the field
  existed. The console can now tell a tray sign-in from a first run. The
  change only adds a field.
- **Downloads keep working after an MLX model is loaded, and installed rows
  carry `kind`/`tasks`** (through AbstractCore). Loading an MLX model no
  longer puts the gateway process into Hugging Face offline mode, which made
  every later download job fail with `OfflineModeIsEnabled`. Download jobs
  never inherit an offline flag written in-process after start, and their log
  says so. `GET /api/gateway/models/installed` rows gain `kind`, `tasks` and
  `tasks_source`, read from local files only. **Restart the gateway to pick
  this up.**
- **Network exposure: localhost only / local network / internet (mission R).**
  One runtime-config setting (`network`: `exposure` + `port`), edited through
  `GET/POST /api/gateway/network` (contract `gateway_network_v1`),
  `abstractgateway network status|set|addresses|restart`, and the console TUI's
  Connection screen (↑/↓ + Enter picks the mode, `c` copies the highlighted
  URL, a restart banner when the running bind differs). `serve` reads it when
  `--host/--port` are not given (explicit flags win and are reported as
  `overridden_by_cli`). `lan`/`internet` bind `0.0.0.0` and are refused (409,
  nothing stored, the exact fix) unless user auth will be on at the next start;
  `internet` also needs `acknowledge_internet: true` and returns the TLS /
  port-forwarding warnings. Changes apply at the next start
  (`restart_required` + `configured` vs `effective`);
  `POST /api/gateway/network/restart` (admin) restarts through the existing
  host-control relaunch and refuses when a restart cannot apply the setting.
  Addresses are discovered per call (psutil, else `ifconfig`/`ip`), labelled
  ("Wi-Fi"), with the Bonjour `<name>.local` when it resolves and the WAN
  address only on an explicit admin request. In a network mode `serve` allows
  the gateway's own LAN origins when `ABSTRACTGATEWAY_ALLOWED_ORIGINS` is
  unset, so the console signs in from another machine.
- **The tray icon is a control centre (mission Q).** The menu is now built as
  data (`tray/menu_model.py`: snapshot + extras → nodes; rendered on pystray,
  unit-tested without a display) and gains:
  - **Start AbstractGateway at login** — a check item showing whether THIS
    gateway would really start at the next login, toggled from the menu. One
    shared module, `abstractgateway.autostart`, used by the tray, the new
    `abstractgateway service enable|disable` (and `status`, which now reports
    `on | off | broken | other` with the reason) and the installers. "On" is
    read back, never assumed: a LaunchAgent / unit / Run value pointing at a
    program that no longer exists, an unreadable file, a unit that is not
    enabled, a `launchctl disable`, a Task Manager "disabled" switch or an XDG
    entry switched off reads **broken** ("— needs repair"; a click repairs it);
    a registration for another data folder reads **other**. Enabling registers
    for the next login without starting a second gateway; disabling only
    unregisters (the running gateway, possibly the tray's parent, keeps
    running).
  - **Apps ▸** — Observer, Flow, Code, Continuum, Entity and Assistant,
    detected by presence only (no import): the gateway-managed install
    (`/api/gateway/apps`), the app's own command on PATH or its package under
    `npm root -g` (the terminal `abstractcode` is not Code), and for the
    Assistant an `AbstractAssistant.app` bundle, the `abstractassistant`
    command or `find_spec` in this Python (a namespace-only match — a folder in
    the working directory — does not count). Each app shows what it can do now:
    **Open X** (one-time signed-in handover), **Start X**, **Install X…**
    (install + start + open signed in, with progress), **Start X (global
    install)**, **Launch Assistant**; launched processes get a token-free
    environment. **Manage Apps in Console…** opens the Apps tab.
  - **Models ▸** replaces "Loaded Models": what is in memory (each loaded model
    a submenu whose action is **Eject — frees N GB**; eject warns when work is
    running), then **Load a Model ▸**: your configured defaults first, then every
    installed model grouped by engine (recognised catalog models first, then
    A–Z, lists over 30 split into ranges, nothing dropped), sizes on disk, a
    "more than free memory" hint with a confirmation, embeddings greyed ("load
    on use"). Loads and ejects show progress and the result, failures the
    gateway's full reason.
  - **Open Console signs you in**: the tray (and the Activity window) mint a
    one-time claim link locally, as `abstractgateway claim` does, so an expired
    8-hour session never ends on a token prompt; plain URL + a notification
    saying why when the gateway runs with a static token. No sign-in endpoint
    was added.
  - **Network ▸** and **Copy Address ▸** (consuming `GET/POST
    /api/gateway/network`): the primary address and mode under the status
    header, radio items Localhost only / Local network / Internet… (Internet
    asks for an explicit acknowledgement showing the gateway's warnings),
    "Restart to apply" while a change is pending, refusals shown with their
    reason and fix; every address the gateway answers on, one click to copy.
  - Explicit degrade: `"flat_menu": true` in `<data>/tray/prefs.json` renders
    the same rows without submenus (path-prefixed) for panels that drop them;
    pystray's menu-less X11 backend gets a start notification naming the CLI
    equivalents.
- **Linux login item without systemd.** Where no systemd user manager answers,
  `service install|enable` writes an XDG autostart entry
  (`~/.config/autostart/abstractgateway.desktop`) instead of failing.
- **The console looks and behaves like a finished product (mission L).**
  The first-run guide is a full-page flow instead of a 760 px modal: a step
  rail on the left (done / current / upcoming), a content area that uses the
  window, and a sticky footer with Skip / Back / Next; Escape closes it
  without marking it done. Local engines are cards, not a table: one card per
  engine with a plain status pill (Ready, Running, Installing, Needs your
  approval, Needs Apple tools, Not installed, Not for this computer), one
  primary action per state from the `gateway_engines_v2` row (Install, Start,
  Stop, Continue with administrator password, Install tools, Try again), an
  inline progress bar, the job's plain sentence first and the full log behind
  "Show details". Downloads (wizard starter models, Multimodal "Weights") show
  a real bar with bytes, percent, speed, time left and per-file rows, a
  distinct "Stalled" state, Cancel, and a "Download all" for the recommended
  set; a reload re-attaches to running downloads. The Apps step and a new
  **Apps** tab install, start, open (signed in) and stop the browser apps
  through the gateway's apps service, with "Node.js will be installed for
  you" instead of terminal commands. Every table fits its box: when a table
  no longer fits, each row becomes a card (label/value grid) instead of a
  horizontal scrollbar; ids, model names, paths and URLs are ellipsized with
  the full value on hover and click-to-copy, never character-wrapped
  (ADR-0026). CLI lines, route ids and commands sit behind a "Technical
  details" switch (sidebar and guide). New module `console_ui.py`.
- **The console header runs the abstractuic kit's real widgets.** The
  top-right cluster is the kit's `AfTopBarActions` (assistant, appearance,
  setup guide, signed-in identity, Connect/Disconnect pill) and Appearance is
  the kit's `AfAppearanceDialog` (its `ThemeSelect`, font and header size),
  mounted as React islands from the kit's own bundle
  (`ui-kit/scripts/build_islands.mjs`), vendored into the generated
  `console_islands.py` with the kit's component CSS by
  `python -m abstractgateway.console_islands_sync`, and drift-pinned by
  `tests/test_gateway_console_islands_sync.py` (kit-source hash, byte compare
  of the built bundle, API probe). The static cluster stays as the fallback.
- **Model downloads show real progress from the first second, and "Use
  recommended defaults" is one job.** A download job now carries `state`
  (queued, resolving, downloading, verifying, installing, done, failed,
  cancelled, stalled), bytes done and total, percent, speed, time left, the
  file arriving now and one row per file, and a plain sentence such as
  "Downloading model.safetensors (2 of 5) · 1.2 GB of 4.8 GB · 38 MB/s ·
  1 min left", for Hugging Face / mlx-gen, Ollama, LM Studio and Supertonic
  alike. A download that receives no bytes for 15 s says it is stalled and
  recovers by itself. `POST /models/download {"recommended": true}` also
  returns a parent job (`grp_...`) whose bytes, percent, speed and time left
  add up the three models. New routes: `POST /models/download/{job}/cancel`
  (admin; stops the transfer within about a second, or every child of a
  parent) and `GET /models/downloads/stream` (Server-Sent Events of the same
  jobs; polling still works). See [docs/model-downloads.md](docs/model-downloads.md).
  Requires the matching AbstractCore. The `message` of a running download is
  now that sentence; the engine tool's own last line moved to `detail`.
- **The gateway installs and runs the browser apps.** Flow Editor, Code,
  Observer, Continuum and Entity can be installed, started, stopped, updated
  and opened from the gateway, with no terminal: when the machine has no
  Node.js 18+, the gateway installs Node.js 24 in its own data folder (no
  admin rights, sha256-checked), then downloads the app from the npm registry
  (sha512-checked), installs its dependencies, starts it on a free port and
  opens it already signed in to this gateway through a one-time link (the
  token never reaches the page or the URL). Every step is a job with percent,
  bytes and a plain message, and the full log when it fails. Started apps are
  children of the gateway: they stop with it, restart after a crash (at most
  3 times a minute), and start again with the gateway until you stop them.
  Routes under `/api/gateway/apps` (list, runtime/install, install, update,
  launch, stop, open, logs, jobs), the `/apps/handover/{code}` link, and
  `abstractgateway apps list|install|launch|stop|update|open|logs|runtime|jobs`.
  See [docs/apps.md](docs/apps.md).
- **Engine installs finish without a terminal: user-level first, tools and
  administrator only through the operating system's own dialogs.** llama.cpp
  installs upstream's prebuilt Metal wheel (0.3.28; CPU 0.3.35 on Linux and
  Windows x64) instead of building the PyPI sdist; a source build happens only
  when no wheel fits, and only after the job has checked for the Apple
  command-line tools (`needs_tools`, one-click `xcode-select --install`, the
  job resumes by itself). Ollama and LM Studio on macOS download the vendor's
  signed app (Ollama: SHA-256 from its GitHub release; both: Developer ID team
  check), place it in `/Applications` when the account can write it or else
  in `~/Applications`, and start the server. A step that genuinely needs an
  administrator stops in `needs_admin` with the reason and the exact command;
  nothing elevated runs until someone presses Continue, and then only through
  macOS's password dialog (`osascript … with administrator privileges`) or
  `pkexec` on a Linux desktop. vLLM on a Mac and MLX off Apple silicon are
  refusals with the reason and no Install button. Jobs report state, percent,
  bytes, a plain-language message (heartbeat every 3 s) and the full log.
  New routes in `routes/engines.py`: `GET /engines` (contract
  `gateway_engines_v2`), `POST /engines/{id}/install {location}`,
  `GET /engines/jobs[/{id}]`, `POST /engines/jobs/{id}/continue|cancel`,
  `POST /engines/{id}/start|stop`; CLI `abstractgateway engines
  continue|cancel|start|stop`. See [docs/engines.md](docs/engines.md).

### Changed
- **Console: calm app cards with one action row (mission GG).** On the Apps
  tab and the setup guide's Apps step each card is now icon + name + status
  pill / one line of description (the whole sentence on hover) / one action
  row. The action rows of a grid row sit at the same height (each card is five
  subgrid rows: head, description, body, actions, technical), and the row
  holds the primary action for the state (**Install and open**, **Open**;
  **Open** also starts a stopped or crashed app, there is no separate Start)
  plus the terminal action next to it (**Open in Terminal**, or a quiet
  **Install for Terminal**). Stop, Start, Show log, Update, the version, the
  address, the npx line and the terminal commands (Rust toolchain case,
  another computer) are rendered only with the **Technical details** switch on,
  as one secondary line of text buttons under the action row; switching it off
  removes them from the page. The "is installed and running. Open it with the
  Open button." box is gone: result boxes ("Code opened in a new Terminal
  window, signed in to this gateway.", "Flow Editor opened in a new tab.")
  close themselves after 6 seconds; errors stay, with **Show details**. The
  terminal version is no longer its own block ("Also runs in your terminal",
  glyph, pill, sentence). The Done step's note about the console's terminal
  app is one quiet line; its install and open commands show with Technical
  details. The Engines cards and the guide's recommended-model tiles use the
  same five rows, so their action rows line up too (a "Learn more"-only row
  sat 16 px lower than a row of buttons); a model download's **Cancel** moved
  into the tile's action row.
- **No user-facing text tells anyone to set an environment variable** (mission
  Z, operator rule). Network-mode warnings point at the controls ("Add your
  public https origin under Reverse proxy below"); auth refusals describe the
  state the gateway was started in ("This gateway was started with accounts
  (user auth) off …"); `serve`'s hardening hints, the missing-auth refusal,
  the loopback auth line, `claim`'s refusal and the apps hints name settings
  and commands. Inventory: `untracked/missionZ/env_instructions.md`.

- **The login service lets the Network setting apply (mission T).** Every
  registration (LaunchAgent, systemd user unit, XDG autostart entry, Windows Run
  entry) now starts plain `serve` — no `--host`, no `--port` — so a mode chosen
  in the tray, the console or `abstractgateway network set` takes effect at the
  next start. Before, the pinned `serve --host 127.0.0.1 --port N` overrode it
  forever (`restart.applies: false`). `service install|enable` store the bind
  in the setting first (nothing stored → `localhost` on the chosen port: the
  same bind as before; a stored mode is kept; `--host 127.0.0.1|0.0.0.0` and
  `--port` are written into the setting, printed; a refused mode registers
  nothing). New `--pin-command-line` keeps the old command line on purpose
  (recorded as `pinned` in `service.json`, reported as an override).
  `service status` / `autostart_status` report a registration that still pins
  `--host/--port` as `broken` + `needs_repair` ("pinned to 127.0.0.1:N by the
  login item — run `abstractgateway service enable` again to let the Network
  setting apply"), which the tray shows as *needs repair*; `service enable` or
  the tray click rewrites it in place. New status fields: `bind_source`,
  `pinned_command_line`, `network_setting`, `repairs`, `needs_repair`. The
  tray's switch no longer passes its own URL's host/port (that would have
  written 127.0.0.1 into the setting). **Installed machines: run
  `abstractgateway service enable` once** (then `service install`, or log out
  and in, to restart the gateway from the new registration).
- **Windows login item is a per-user `HKCU\…\Run` value** (was a Startup-folder
  shortcut). A Run value can be read back and verified (a `.lnk` cannot without
  COM) and Task Manager's switch for it is readable; install/uninstall remove
  an older `AbstractGateway.lnk`, which would start a second gateway. Still
  experimental (not validated on a Windows VM).
- `service uninstall` on Linux only calls `systemctl --user disable` when the
  unit file exists (it used to fail on a machine with nothing installed).

### Fixed
- **A leftover browser app from a gateway that died is stopped again on
  Linux.** The reaper recognises the app by its install path in the process
  command line, read with `ps`; Linux `ps` cut that line at 80 columns when
  not writing to a terminal, so the path never matched, the leftover was not
  stopped and kept its port ("No free port for the app"). It now reads the
  whole line (`ps -ww`).
- **`abstractgateway --version`** prints `abstractgateway <version>` (the
  flag did not exist).
- **No false "PyTorch was imported" GGUF warning.** Every command printed
  "GGUF GPU offload could NOT be reserved ... PyTorch was imported before this
  gateway started" on a light install (no llama-cpp-python) and on any host
  that is not Apple silicon, where Metal offload cannot exist. The reservation
  and its warning now run only on Apple silicon with llama-cpp-python
  installed; `config`, `--version` and `--help` skip it.
- **The saved backlog folder was ignored by the backlog routes (mission II).**
  Every backlog/report/process route read ONLY the environment, so the
  `triage_repo_root` setting saved from Continuum or the console changed
  nothing, and the exec runner did the same. A saved folder that disappears
  now answers `404 Backlog folder not available on this gateway: <reason>`
  (no server path in the sentence) instead of "not configured".
- **`abstractgateway backlog-exec-runner` no longer writes
  `os.environ["ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"]`** in its own process: the
  folder (`--repo-root`, else the setting) is passed to the runner as an
  argument. Its old fallback to the current directory is replaced by the
  setting's default (the gateway's own folder).
- **Process control stays off on the gateway's default backlog folder** (it
  is not a checkout); it needs `triage_repo_root` set to the framework
  checkout it manages, as before.
- **Apps started outside the gateway are listed and open signed in (mission
  HH, 2026-09-24).** The operator's report: with the whole dev stack running
  (`scripts/start-local.sh --build`: all five apps on 127.0.0.1:3001-3005),
  the tray said "Observer — can't install here: …" for every app. The apps
  manager only knew apps it installed itself. It now asks the usual loopback
  ports (3001-3005, then 3000 and 3007) for the start page and recognises
  each app by its title (no app has an identity route that answers without a
  gateway sign-in); the version is read from the `package.json` of the
  listening program. Such an app is `installed`, `running`,
  `source: "external"`, `managed: false`, with `url`, `port`, `version` and
  one action, `open`, which goes through the usual one-time handover (the
  app's server reads the same cookies whoever started it; the gateway URL in
  them is the gateway's own). Stop answers 409 `started_outside_gateway`;
  launch does not start a second copy. Probing is loopback only, 0.5 s per
  port in parallel, cached 5 s, and never probes a port an app of this
  gateway holds.
- **One port table, the stack map.** The gateway's usual app ports now match
  `scripts/start-local.sh`: Observer 3001, Continuum 3002, Code 3003, Entity
  3004, Flow 3005 (`apps_manager.STACK_PORTS`, used by the managed launch, the
  external probe and the tray; it was Code 3002, Flow 3003, Continuum 3004,
  Entity 3007). Apps are listed in that order.
- **The person at the gateway machine may install by default, whatever the
  bind.** With no saved `allow_engine_install`, installs of apps, Node.js,
  terminal apps and engines are allowed for a caller on the gateway machine
  itself: the socket peer is loopback or one of this host's own interface
  addresses, and the request has no proxy header
  (`security/same_machine.py`). A LAN-bound gateway used to refuse the
  operator at their own keyboard. Remote callers still need the setting; a
  saved OFF is off for everyone. `install_policy` gains
  `caller_on_this_machine` and the source `default_same_machine`.
- **Tray Apps submenu: one short line per app, no reasons.** Running
  (managed or external) or installed → "Open X"; installable → "Install X…";
  otherwise the name, greyed. When an install is blocked, one line near the
  bottom: "Installs are off for this gateway · Console → Apps". The console's
  Apps card shows an external app with the Running pill and Open, and under
  Technical details "Started outside the gateway on port N" instead of Stop.
- **Tray base URL.** Confirmed loopback (`http://127.0.0.1:<port>`) for every
  network mode (wildcard and loopback binds); the LAN address in the header
  is the network status, not the tray's connection. Pinned by a test.

- **Engine installer downloads go to the per-OS user cache (mission FF).**
  Without an explicit cache directory, `engines_install` wrote to a
  hard-coded `~/.cache/abstractgateway/engines`, ignoring `XDG_CACHE_HOME`.
  The new `host_paths.user_cache_dir()` (next to `user_data_dir()`) answers
  `~/Library/Caches/AbstractGateway` on macOS, `$XDG_CACHE_HOME/abstractgateway`
  (default `~/.cache/abstractgateway`; a relative value is ignored per the XDG
  spec) on Linux and `%LOCALAPPDATA%\AbstractGateway\Cache` on Windows.
- **The administrator copy of an engine app is chowned to the process owner
  from the password database**, not to an inherited `USER` value (which can
  name someone else, e.g. under `sudo`). The `USER` read is gone.
- **Every environment read is declared.** `env_registry` now carries the
  desktop-session reads (`DISPLAY`, `WAYLAND_DISPLAY`) and the per-OS
  directory reads (`XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`,
  `LOCALAPPDATA`, `APPDATA`) as reads of the operating system, not settings
  (class `deployment`, owner `desktop-session`, silent in the boot scanner).
- **Console: cancelling a model download uses the long-call budget.** The
  download panel's Cancel (`POST /models/download/{id}/cancel`) went through
  the 60 s default; like the download start it now passes `slow: true`.
- **The consoles say why MTP did not run, in words.** The web console's sandbox and
  default-route test lines, and the terminal console's test line, now show the response's
  `speculation.message` (and the discovery capabilities' `message`) instead of only the
  slug. For example: "MTP not used: MTP acceleration off: companion
  mlx-community/Qwen3.5-9B-MTP-4bit … is not downloaded; download it with `abstractcore
  models download mlx …`". Model downloads now fetch an MLX model's MTP companion in
  the same job. That comes from AbstractCore: see its CHANGELOG, mission CC.
- **`lan`/`internet` are refused when read protection is off** (mission AA
  finding). `ABSTRACTGATEWAY_PROTECT_READ=0` makes the middleware answer every
  unauthenticated read as the admin; the network modes now refuse it
  (`reason_code: auth_disabled`) and `serve` falls back to loopback with the
  reason. `auth_mode_summary` reports `read_protected`.
- `abstractgateway network … --data-dir DIR` now uses DIR (it was accepted
  and ignored); `network` no longer reserves the GGUF GPU offload (no model
  is ever loaded by it).

- **Security: with user accounts off, a non-admin account can no longer sign
  in and change the operator's settings (mission BB).** With user accounts
  off the gateway runs one runtime, the operator's; `POST /session/login`
  nevertheless accepted any registry account, so a non-admin (role `user`,
  created by the admin) could sign in and then change the gateway-wide
  capability defaults and add endpoint profiles to the admin's own list (his
  bearer token was already refused). Now only admin accounts can hold a
  browser session in that mode: login answers `401`
  (`reason_code: "user_accounts_off_admin_only"`, a message naming the two
  ways out: the operator turns user accounts on, or sign in with an admin
  account), and a session that already exists for a non-admin account is
  refused and removed at its next use. The rule lives in one place
  (`security/sessions.py::principal_barred_from_shared_runtime`) and every
  session path applies it, including the browser-app sign-in handover. Admin
  accounts sign in in both modes, and the login answer now reports the real
  mode instead of always claiming user accounts are on.
- **Users: no account that could never sign in, and never zero admins.**
  With user accounts off, `POST /admin/users` answers `409` instead of
  creating a non-admin account, and a `PATCH` that removes the `admin` role is
  refused the same way. In both modes, deleting, disabling or demoting the
  last enabled admin account answers `409` (`reason_code: "last_admin"`).
- **Workflows: `POST /bundles/{id}/deprecate` and `/undeprecate` apply the
  shared-registry ownership check** that every other registry write already
  applied (they answered `404` for a missing bundle, i.e. authorization had
  passed, where `/bundles/reload` answered `403`).
- **The route authorization contract test tells the truth.** Its comment
  credited a `_principal_requires_isolation` function that never existed; it
  now describes the real mechanism and pins both modes against the live
  route table: with user accounts off a non-admin session is refused on every
  write route, and with them on it gets `403` on every admin family and never
  changes the admin's gateway-wide view.
- **An engine Install no longer surfaces a raw build failure.** Before, the
  llama.cpp Install ran `uv pip install llama-cpp-python` (a CMake source build
  that fails without Xcode's tools, shown as a 43-line log and "uv exited 1"),
  and the Ollama Install on macOS ran the vendor shell script, which needs
  `sudo` for `/usr/local/bin` and cannot ask for it from a job.
- **The admin token is printed again at first launch.** 0.3.0 hid it behind
  `ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=1`, so a first launch showed only the
  one-time console link and no credential for the browser apps. A loopback
  `serve` now prints the token (as 0.2.30 did) together with the link; a
  non-loopback bind hides it. The switch is a launch flag,
  `abstractgateway serve --print-token` / `--no-print-token`; the environment
  variable is kept only as an alias of `--print-token`.
- **A restart from the tray or console no longer carries in-process Hugging
  Face offline flags into the new gateway.** `host_control.relaunch_process`
  used `os.execv`, which hands the new process the current `os.environ`. An
  `HF_HUB_OFFLINE=1` written in-process during the run (the old MLX / Hugging
  Face provider writes, a third-party library) therefore became the new
  process's start-up environment, which AbstractCore records as the
  operator's choice and which makes every explicit download refuse by name.
  The relaunch (POSIX `execve` and the Windows spawn) now resets
  `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` and `HF_DATASETS_OFFLINE` to the
  values the gateway started with (a value counts only if both the gateway's
  and AbstractCore's start-up snapshots saw it) and logs each variable it
  dropped or restored. Every other variable passes through unchanged.

### Tests
- **The suite passes on a headless Linux runner (CI).** The local engines'
  default addresses (`LMSTUDIO_BASE_URL`, `OLLAMA_BASE_URL`, `OLLAMA_HOST`)
  point at a closed loopback port for every test and the whole session: off
  Apple silicon a gateway with no provider builds an LM Studio client that
  lists models at localhost:1234. The engine-install fakes answer
  `xcode-select -p` with a folder that exists; the apps tests use valid port
  ranges and build the terminal command as on a Mac instead of needing a
  desktop session.
- The conftest's network guard now also refuses 127.0.0.1:3000-3007 (the
  operator's browser apps), and an autouse fixture empties the external-app
  probe's port list: a test names its own scratch ports (mission HH).
- **Subprocess guard (mission FF, 2026-09-24).** A child process has its own
  sockets, so a real engine CLI slipped past the network guard. The conftest
  now refuses launching `lms`, `ollama`, `open` or `xdg-open` (through
  `subprocess`, asyncio subprocesses or `os.system`, including `sh -c` and
  `env …` forms), fails the test and lists the launch under "subprocess
  guard". Opt-outs: the `fake_cli` fixture registers a stand-in script (only
  that file may run); `@pytest.mark.desktop("reason")` (mandatory reason,
  skipped unless `pytest --allow-desktop`) and `network` tests may launch the
  real ones. First catch: the fresh-install catalog test ran the operator's
  real `~/.lmstudio/bin/lms ls --json` through the refusal's weights probe; it
  now uses a fake `lms`. Self-tests in `tests/test_conftest_subprocess_guard.py`.
- **Tests never touch your home or the network (mission DD, 2026-09-24).**
  `tests/conftest.py` now also isolates `HOME`, `HF_HOME`/`HF_HUB_CACHE` and
  the AbstractCore config directory (at import and per test), clears exported
  `ABSTRACT*`/`HF_*` path settings and `XDG_*`, and installs a socket guard that
  refuses non-loopback destinations and the live local services on loopback
  (8080, 1234, 11434, 18850). Findings fixed: the live API-parity checks in
  `test_gateway_offline_parity.py` queried the operator's gateway on :8080 by
  default (now opt-in by URL and marked `network`); the fresh-install catalog
  test queried the live LM Studio on :1234 (now a loopback fake); three tests
  resolved the stand-in `core.test` server over real DNS (now stubbed).
  `@pytest.mark.network("reason")` (skipped unless `pytest --allow-network`)
  and `@pytest.mark.real_home("reason")` are the opt-outs; a bare marker is a
  collection error. See
  [CONTRIBUTING.md](CONTRIBUTING.md#tests-never-touch-your-home-or-the-network).

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
