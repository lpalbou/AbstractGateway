# Apps

The five browser apps (Observer, Continuum, Code, Entity and Flow Editor) can be
installed, started, stopped and updated from the gateway: from the console's
Apps page, from the HTTP API below, or with `abstractgateway apps`. Nobody has
to open a terminal or install Node.js by hand. The Apps page also lists the
desktop app, the **Assistant** (see [The Assistant](#the-assistant-a-desktop-app)).

On a card, a plain user sees one button for the state the app is in:

| State | Buttons |
|---|---|
| Not installed | **Install** |
| Installing | a progress bar (one row per part, e.g. "Code in the browser" and "Code in the terminal") and **Cancel** |
| Installed (running or not) | **Open**, and **Open in Terminal** right beside it when the app's terminal version is installed |
| Failed | the reason, **Show details**, and **Install** again |

Stop, Show log, Update, versions, addresses and commands are under
**Technical details**.

## What happens when you click Install

1. **Node.js.** The apps are small Node.js servers. If the gateway finds
   Node.js 18 or newer on the machine, it uses it. Otherwise it installs
   Node.js 24 for you in its own data folder (`<data dir>/runtime/node/`,
   about 56 MB to download, no administrator rights). This is the Node.js
   build published on PyPI as `nodejs-wheel-binaries`, the same one the
   installer's `--with-apps` option gets with `uv tool install nodejs-wheel`.
   The download is checked against PyPI's sha256 checksum.
2. **The app.** The gateway downloads `@abstractframework/<name>` from the npm
   registry into `<data dir>/apps/<app>/<version>/` and checks it against the
   registry's sha512 integrity hash. Apps that need other npm packages get them
   through npm (with its cache in `<data dir>/apps/npm-cache/`); Code needs
   none and is unpacked directly.
3. **The terminal app, too.** When the app also runs in a terminal and a
   ready-made download exists for this computer (Code, see
   [Terminal versions](#terminal-versions)), the same Install installs it
   right after the browser app: ONE job whose `parts` are the two rows the
   card shows (`install` then `install-tui`, each `waiting`, `running`,
   `done`, `failed`, `cancelled` or `skipped`). Cancel stops both. If the
   terminal part fails, the browser app stays installed and the job says so
   ("Code is installed for the browser, but its terminal app did not install:
   …"); with Technical details on, the card offers "Install terminal app" to
   try that part again. Without a ready-made download for this computer,
   Install installs the browser app only and the terminal app's command stays
   under Technical details. The row's `install_parts` (`["web"]` or
   `["web", "tui"]`) says in advance what Install covers.

Install only installs: nothing starts and no tab opens. The card then shows
**Open**, which starts the app on a free port when it is stopped, waits until
it answers, and opens it in a new tab, already signed in.

Every step is a job with a percentage, downloaded bytes and a plain message.
When a step fails, the job says why in one sentence and carries the full log
in its `details` field (`<data dir>/apps/jobs/<job>.log`).

## Running apps

- An app started from the gateway is a child process of the gateway. It stops
  when the gateway stops (even if the gateway is killed: the app watches the
  pipe the gateway holds open and exits when it closes).
- An app you started stays **enabled**: the next time the gateway starts, it
  starts the app again, on the same port when that port is free. Stop the app
  to turn this off (the console's **Stop** is under **Technical details**, or
  `abstractgateway apps stop <app>`). Nothing is registered with launchd, systemd or the login
  items; the gateway itself starts the apps.
- If an app exits unexpectedly, the gateway restarts it (after 1, 2 then 4
  seconds). After more than 3 crashes in a minute it stops trying and shows
  "crash_loop" with the app's log.
- Each app writes its output to `<data dir>/logs/apps/<app>.log`.
- Apps listen on `127.0.0.1` (the `apps.host` setting changes this). Ports: the app's usual port when it is
  free, else the first free port in 3100-3199. The usual ports are the
  framework's stack port map (`scripts/start-local.sh`): Observer 3001,
  Continuum 3002, Code 3003, Entity 3004, Flow 3005.

## Apps started outside the gateway

An app can also run without the gateway having started it: the framework's
development stack (`scripts/start-local.sh`), `npx @abstractframework/observer`,
a global npm install, a service. The gateway finds such an app by asking the
usual ports on this machine (3001-3005, then 3000 and 3007) for their start
page and reading its title
("AbstractObserver", "AbstractContinuum", "AbstractCode", "AbstractEntity",
"AbstractFlow"). None of the apps has an address that says who it is without
a gateway sign-in, and the start page is plain HTML, so this costs one short
local request per port (half a second at most, all ports at once, remembered
for 5 seconds). The version is read from the `package.json` of the program
listening on the port, when this computer shows which program that is.

Such an app is listed as installed and running, with `source: "external"`,
`managed: false`, its `url`, `port` and `version`, and one action: **Open**.
Opening it works exactly like opening an app the gateway started (the
one-time sign-in link below): the app's server reads the same sign-in
cookies whoever started it. The gateway does not stop, update or show the
log of an app it did not start; the console's **Technical details** says
"Started outside the gateway on port 3001" instead, and `POST /apps/{id}/stop`
answers 409 `started_outside_gateway`. Starting the gateway's own copy while
an outside one runs does nothing (the app is already running).

## Who may install

Installing an app (or Node.js, or a terminal version) runs software on the
gateway machine, so it follows the gateway's `allow_engine_install` setting.
With no saved choice, installs are allowed for someone at the gateway machine
itself, whatever address the gateway listens on (a browser or the tray on that
machine, including one that uses its network address), and for everyone when
the gateway listens on this machine only. A browser on another computer needs
an admin to turn the setting on. How "at the gateway machine" is decided is
in [configuration.md](./configuration.md#allow_engine_install).

## Opening an app signed in

The console asks the gateway for a one-time link
(`POST /api/gateway/apps/{id}/open`) and opens it in a new tab. The link
(`/apps/handover/<code>`) works once, for two minutes, and only on the address
it was made for. The gateway creates a browser session for the person who
clicked, puts it in the app's own sign-in cookies, and redirects to the app.
Browsers keep cookies per machine name, not per port, so the app's server finds
the session and the app opens connected. The gateway token never appears in
the page, the link or the browser's storage.

A browser on another computer cannot reach an app that listens on
`127.0.0.1`; the gateway says so instead of producing a broken link.

### Landing somewhere inside the app

`POST /api/gateway/apps/{id}/open` accepts an optional `path`: where inside
the app the browser lands after the handover, for example `/#new`, Entity's
creation form. The path is bound to the one-time code when the link is made
(the link itself carries nothing), and it must stay inside the app: it starts
with a single `/`, and a second leading slash (`//host`), a full address, a
backslash, a space or a control character is refused with 400
`invalid_app_path` before any link is made. Without `path` the browser lands
on the app's start page.

### An app with nothing in it yet

Each app row carries `content_summary`: a small fact about what the app holds
on this gateway, or `null` for apps that report nothing. Entity reports `{"entities_count": n}`: the number of entries
`GET /api/gateway/entities` lists (counted from the same entity registry,
without reading any entity), `null` when it cannot be known, including for an
Entity started outside the gateway whose page names another gateway. When the
count is exactly 0, the Entity card's button reads **Create your first
entity** and opens the app on `/#new` through the handover above; with one
entity or more, or an unknown count, it reads **Open**. The guide's Apps step
shows the same card.

## Terminal versions

Some apps also run in a terminal: **Code** (`abstractcode`, a
Rust terminal app from the abstractcode repository); Flow Editor, Observer,
Continuum and Entity are browser apps only. The gateway's own console also
has a terminal twin, `abstractgateway-console`, which the console's Done step
mentions once.

Every app row therefore lists its interfaces: `interfaces[0]` is the browser
app (it mirrors the row's own fields), and apps with a terminal version have a
second entry of kind `"tui"`:

```json
{"kind": "tui", "name": "Code in the terminal", "binary": "abstractcode",
 "installed": true, "version": "0.5.1", "source": "gateway", "path": "<data dir>/apps/bin/abstractcode",
 "latest_version": "0.5.1", "update_available": false,
 "install_available": false, "install_method": "release_binary", "install_blocked_reason": null,
 "install_command": "cargo install abstractcode", "download_page": "https://github.com/lpalbou/abstractcode/releases",
 "launch_available": true, "launch_blocked_reason": null, "launch_mode": "terminal",
 "command": "<data dir>/apps/bin/abstractcode --gateway http://127.0.0.1:8080",
 "signin_command": null, "active_job": null}
```

- **Found by presence.** The gateway never imports or runs an app to detect
  it: it looks for the binary in `<data dir>/apps/bin/`, on `PATH` and in
  `~/.cargo/bin`, and accepts it only when its `--help` names the program
  (PyPI's unrelated Python package `abstractcode` installs a script with the
  same name). `source` says where it was found (`gateway` or `path`).
- **Install (`install_method`).** `release_binary`: Code publishes prebuilt
  binaries for macOS (Apple silicon and Intel), Linux (x86_64 and arm64,
  glibc) and Windows (x86_64) on its GitHub release, with a `SHA256SUMS` file.
  The card's Install (and `install-tui` on its own) downloads the archive for
  this computer, checks it
  against `SHA256SUMS` and against the sha256 digest GitHub reports for the
  file (both must agree), unpacks the single binary into `<data dir>/apps/bin/`,
  makes it executable and runs `--version` before it replaces anything.
  About 3 MB, no administrator rights, no terminal. `cargo`: there is no
  prebuilt binary for this computer (musl Linux, Windows on ARM, other CPUs),
  or the program is published as source only (the gateway console, crates.io).
  Then `install_available` is `false`, `install_blocked_reason` starts with
  "Needs the Rust toolchain" and `install_command` is the exact command to
  copy; the console shows no Install button.
- **Open (`launch_mode`).** `terminal`: the caller is an admin on the gateway
  machine itself, and "Open in Terminal" opens a new terminal window there
  (macOS Terminal; on Linux the first of `x-terminal-emulator`,
  `gnome-terminal`, `konsole`, `xfce4-terminal`, `kitty`, `alacritty`, `xterm`
  when a desktop session exists; Windows `cmd`). `copy`: the browser is on
  another computer (or the caller is not an admin); with **Technical
  details** on, the card shows `command` (this gateway's address as the
  browser reaches it) and `signin_command` (`abstractcode login --gateway
  <url> --token <your token>`) to copy. A terminal is never opened for a
  remote browser. How the card presents each case: [console.md](./console.md#apps-tab).

### Signed in, without a token anywhere it could leak

The terminal window runs a small launcher script
(`<data dir>/apps/terminal/open-code-<random>.command`, mode 0700) that holds
a **one-time code** (two minutes, single use) and deletes itself as its first
line. It runs `tui_signin.py` from the gateway package (standard library only,
`python -I`), which trades the code at `POST /apps/tui-handover` for a bearer
token and then becomes the terminal app with the token in the app's
**environment** (`ABSTRACTCODE_GATEWAY_TOKEN`, which Code prefers over its
saved login). The token

- is new for each launch and is not the admin token;
- acts as the person who clicked (their identity and role, never more);
- is accepted only from this machine (a loopback socket peer);
- lives in the gateway's memory only, so it stops working when the gateway
  restarts (open the app again from the console);
- is never in a command line, a file, the page, or a URL.

### From a terminal

Everything above works without the console:

```bash
abstractgateway apps list                 # each app; Code also gets a "terminal:" line (installed, where, what to run)
abstractgateway apps install-tui code     # the same job: release binary, SHA256SUMS + GitHub digest, --version check
abstractgateway apps tui-command code     # the one-time sign-in line for THIS machine (2 minutes, works once) + the plain command
```

`install-tui` prints the job's progress; when there is no prebuilt binary for
the computer it prints the reason and `cargo install abstractcode` and exits 2.
`tui-command` makes the same one-use, self-deleting launcher as "Open in
Terminal" (it holds a single-use code, never a token) but opens no window: run
the printed line in a terminal on the gateway machine. From another computer
it prints the command and the `abstractcode login` line instead.

`/apps/tui-handover` sits outside `/api/gateway` like the browser handover:
the code in its body is its only credential, it answers only loopback socket
peers without proxy headers, and a browser handover code does not work there
(nor the reverse).

## How the apps are served

Each app runs its own small server, started by the gateway, rather than being
served as static files by the gateway. The app's server does real work:

- it holds the sign-in (`/api/connection/gateway`, HttpOnly session cookies,
  CSRF) and forwards `/api/*` to the gateway on the same origin, which is how
  live updates (server-sent events) reach the page;
- four of the five apps load their files from absolute `/assets/...` paths,
  and Code and Observer register a service worker at the site root, so they
  cannot live under a sub-path of the gateway;
- Observer reveals local folders, Flow keeps its connection file, and
  Continuum proxies the agora hub.

This is why each app has its own port. The gateway gives each server its
port, bind address and gateway URL: Continuum as launch flags (`--port`,
`--host`, `--gateway-url`), which win over its saved settings file; the
other four apps in the environment (`PORT`, `HOST`, `<APP>_GATEWAY_URL`).

## Settings

Five settings control the apps: `apps.node` (*Node.js for apps*),
`apps.ports` (*Ports for apps*), `apps.host` (*Where apps listen*),
`apps.npm_registry` (*npm registry*) and `apps.pypi_url` (*Node.js download
index*). Change them from the Apps page (*Advanced: apps settings*), the
terminal console (Runtimes → *Runtime knobs* → *Edit apps settings*) or the
CLI:

```bash
abstractgateway apps config get [NAME] [--json]
abstractgateway apps config set ports 3100-3199     # "" clears back to the default
```

A saved value applies at the next app start or download. Values, defaults and
the environment-variable fallbacks are listed in
[configuration.md](./configuration.md#browser-apps-settings-apps).

Install, update, start and stop need an admin, and installs follow
[Who may install](#who-may-install). Any signed-in user can list the apps and
open a running one (as themselves).

## Without internet

- Apps already installed keep working offline.
- Installing needs the npm registry (and PyPI for Node.js). When it cannot be
  reached, the Apps page says so on each app and the Install button is off;
  a job that loses the network fails with "The npm registry
  (registry.npmjs.org) is not reachable…". The gateway does not ship app
  copies: the five packages are about 6 MB, but four of them need npm
  dependencies (Flow alone installs to about 130 MB), which would have to
  ship too.

## Command line

The same actions, through the running gateway:

```bash
abstractgateway apps list                 # Node.js, every app, installed/latest, URL
abstractgateway apps install code            # the browser app and, where available, its terminal app
abstractgateway apps install code --launch   # ... and start it
abstractgateway apps install assistant       # the desktop Assistant, into the gateway's Python
abstractgateway apps launch assistant        # open it on this computer
abstractgateway apps launch observer
abstractgateway apps open observer        # prints a one-time signed-in link
abstractgateway apps logs observer --tail 50
abstractgateway apps update flow
abstractgateway apps stop observer
abstractgateway apps runtime              # install Node.js only
abstractgateway apps install-tui code     # Code's terminal version (see "Terminal versions")
abstractgateway apps tui-command code     # one-time signed-in launch line for this machine
abstractgateway apps jobs [JOB_ID]
```

`--url`, `--token` and `--data-dir` work as for `abstractgateway models`: by
default the command finds the gateway running for this data dir and uses its
admin token on a loopback URL.

## The Assistant (a desktop app)

AbstractAssistant (PyPI `abstractassistant`) is the framework's desktop
companion: a menu-bar app with a chat palette and hands-free voice
conversations. It is a Python app, not a browser app, so its card
(`kind: "desktop"`, id `assistant`, after the five browser apps) has no address
or port. The full `abstractframework` install already includes it, in the same
Python environment as the gateway; a gateway-only install may not.

- **Found by presence** (nothing is imported or started to find it): the
  `abstractassistant` command next to the gateway's own Python (or on `PATH`),
  the installed package (`importlib.util.find_spec`, without importing it; a
  folder that merely has the package's name does not count), and on macOS
  `AbstractAssistant.app` in `/Applications` or `~/Applications`. The version
  comes from the package, else from the app's `Info.plist`. It is **Running**
  when a process on this computer is the Assistant (its command, `python -m
  abstractassistant…`, or the app's own program). The tray's "Launch
  Assistant" uses the same detection (`apps_desktop.detect_assistant`), so
  the tray and the console always agree.
- **Install** installs `abstractassistant` into the gateway's own Python as a
  job (`uv pip install --python <gateway python> abstractassistant`, or pip
  when there is no uv), with every `abstract*` package the gateway runs
  pinned to its current version (`name==version` requirements in the same
  command), so installing the Assistant never changes the gateway. The same
  rule as the other installs decides who may install (see [Who may install](#who-may-install)).
- **Open** starts it on the gateway's computer: `open -a AbstractAssistant.app`
  when the app exists, otherwise its command, as a separate process with none
  of the gateway's tokens, secrets or keys in its environment. A running
  Assistant is not started twice: the app is brought to the front (or, when
  it was started from its command, the card says its icon is in the menu
  bar). The Assistant keeps its own connection settings (its Settings window,
  Connection): the gateway passes it no address and no token, and it has no
  one-time sign-in handover.
- **From another computer** the card says "The Assistant runs on the gateway's
  computer: open it there." with no button: it is a desktop app for that
  computer's screen.
- **Technical details** show its version, where it was found and the launch
  command (or the install command when it is not installed).

## HTTP API

All routes are under `/api/gateway/apps` and need a signed-in principal.

| Method and path | Who | What |
|---|---|---|
| `GET /apps?latest=true` | any user | Node.js status, one row per app (`kind` `web` with `interfaces[]`, see "Terminal versions", and `install_parts`; then the Assistant, `kind` `desktop` with `desktop {location, found_by, launch_command, install_command, launch_available, launch_blocked, launch_blocked_reason}`) and `console_tui` (the gateway console's terminal app). `latest=false` skips the npm registry and GitHub release lookups (cached 10 minutes). |
| `POST /apps/runtime/install` | admin | Install Node.js (a job), or `job: null` when one is already usable. |
| `POST /apps/{id}/install` `{"version"?, "launch"?, "with_terminal"?}` | admin | ONE job: Node.js if needed, download, check, dependencies, then the terminal app when the row's `install_parts` has `"tui"` (`with_terminal: false` skips it); the job's `parts` are its child rows. Starts nothing unless `launch: true`. For `assistant`: installs `abstractassistant` into the gateway's Python (every `abstract*` package is pinned to its current version in the same command). |
| `POST /apps/{id}/update` `{"version"?}` | admin | A job: install the latest (or given) version; a running app is restarted on it. |
| `POST /apps/{id}/launch` | admin | Start the app (waits until it answers) and mark it enabled. For `assistant`: open it on the gateway's computer, `{ok, app, already_running, message}`; from another computer 409 `not_on_gateway_machine`, and nothing starts. |
| `POST /apps/{id}/stop` | admin | Stop the app and mark it disabled. 409 `started_outside_gateway` for an app the gateway did not start. |
| `POST /apps/{id}/open` `{"remember"?, "path"?}` | any user | A one-time `open_url` (relative to the gateway) that opens the running app signed in, at `path` inside the app when given (e.g. `/#new`). 400 `invalid_app_path` for anything that is not a path inside the app. 409 `desktop_app` for the Assistant (use `/launch`). |
| `GET /apps/{id}/logs?tail=200` | admin | The end of the app's log. |
| `GET /apps/jobs`, `GET /apps/jobs/{job}` | any user | Jobs: `state` (queued, running, succeeded, failed, cancelled), `percent`, `bytes_done`, `bytes_total`, `message`, `steps`, `parts` (the child rows of an Install that covers two parts), `details` (full log on failure). |
| `POST /apps/jobs/{job}/cancel` | admin | Cancel a job. |
| `POST /apps/{id}/install-tui` | admin | A job: download, check and place the app's prebuilt terminal version (or update the gateway's copy). 409 `toolchain_required` with `command` when only a source build exists. |
| `POST /apps/{id}/launch-tui` | admin, on the gateway machine | Open the terminal version in a new terminal window, signed in as the caller: `{ok, app_id, interface: "tui", terminal, version, message, expires_in_s}`. From another computer (non-loopback peer or `Host`, or any proxy header): 409 `not_on_gateway_machine` with `command` and `signin_command`, and nothing opens. |
| `POST /apps/{id}/tui-command` | admin, on the gateway machine | The same one-use launcher as launch-tui without opening a window: `{ok, app_id, interface, version, signin_command, command, expires_in_s}`. From another computer: 409 `not_on_gateway_machine` with `command` and `signin_command`. |
| `POST /apps/tui-handover` `{"code"}` (outside `/api/gateway`) | the launcher script, loopback only | Trade a launcher's one-time code for `{token, token_env, url_env, gateway_url, gateway_flag, user}`. 403 `loopback_only`, 410 `handover_expired`. |

Errors are `{"ok": false, "reason", "message", "hint"?, "details"?}` with
404 (`unknown_app`, also for a terminal route on a browser-only app), 409
(`not_installed`, `node_missing`, `not_running`, `app_loopback_only`,
`toolchain_required`, `not_on_gateway_machine`, `no_terminal`,
`started_outside_gateway`), 403
(`installs_not_allowed`), 502 (`integrity_mismatch`), 503
(`network_unavailable`, `no_free_port`) or 500 (`launch_failed`, with the
app's output in `details`). The terminal errors also carry `command` (and,
where it helps, `install_command` or `signin_command`) so the console can
offer the command to copy instead.
