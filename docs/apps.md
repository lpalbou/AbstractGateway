# Browser apps

The five browser apps (Observer, Continuum, Code, Entity and Flow Editor) can be
installed, started, stopped and updated from the gateway: from the console's
Apps page, from the HTTP API below, or with `abstractgateway apps`. Nobody has
to open a terminal or install Node.js by hand.

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
3. **Start.** With "Install and open", the gateway starts the app on a free
   port, waits until it answers, and opens it in a new tab, already signed in.

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
- Apps listen on `127.0.0.1` only. Ports: the app's usual port when it is
  free, else the first free port in 3100-3199. The usual ports are the
  framework's stack port map (`scripts/start-local.sh`): Observer 3001,
  Continuum 3002, Code 3003, Entity 3004, Flow 3005.

## Apps started outside the gateway

An app can also run without the gateway having started it: the framework's
development stack (`scripts/start-local.sh`), `npx @abstractframework/observer`,
a global npm install, a service. The gateway finds such an app by asking the
usual ports on this machine (3001-3005, then 3000 and 3007, which older
launch scripts used) for their start page and reading its title
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
on the app's start page, as before.

### An app with nothing in it yet

Each app row carries `content_summary`: a small fact about what the app holds
on this gateway, or `null` for apps that report nothing. Today only Entity
has one, `{"entities_count": n}`: the number of entries
`GET /api/gateway/entities` lists (counted from the same entity registry,
without reading any entity), `null` when it cannot be known, including for an
Entity started outside the gateway whose page names another gateway. When the
count is exactly 0, the Entity card's button reads **Create your first
entity** and opens the app on `/#new` through the handover above; with one
entity or more, or an unknown count, it reads **Open**. The guide's Apps step
shows the same card.

## Terminal versions

Some apps also run in a terminal. Today that is **Code** (`abstractcode`, a
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
  "Install for Terminal" downloads the archive for this computer, checks it
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

## Why the gateway runs each app's own server

The gateway could in principle serve the apps' built files itself. It does
not, because each app's server does real work:

- it holds the sign-in (`/api/connection/gateway`, HttpOnly session cookies,
  CSRF) and forwards `/api/*` to the gateway on the same origin, which is how
  live updates (server-sent events) reach the page;
- four of the five apps load their files from absolute `/assets/...` paths,
  and Code and Observer register a service worker at the site root, so they
  cannot live under a sub-path of the gateway;
- Observer reveals local folders, Flow keeps its connection file, and
  Continuum proxies the agora hub.

Serving the files alone would break sign-in and live updates in every app.

## Settings

Settings, changed from the Apps page (*Advanced: apps settings*), the TUI
(Runtimes → *Runtime knobs* → *Edit apps settings*) or the terminal
(`abstractgateway apps config get|set NAME VALUE`). A saved value applies at the
next app start or download. See
[configuration.md](./configuration.md#browser-apps-settings-apps).

| Setting (console label) | Default | Effect | CLI | Legacy env (fallback) |
|---|---|---|---|---|
| `apps.node` (*Node.js for apps*) | `auto` | `auto`: Node.js on the machine, else the gateway's own. `managed`: always the gateway's own. `system`: never install Node.js. A path: use that `node`. | `apps config set node auto` | `ABSTRACTGATEWAY_APPS_NODE` |
| `apps.ports` (*Ports for apps*) | (empty) | A port or range, e.g. `3100-3199`. When set, apps only use ports in it. | `apps config set ports 3100-3199` | `ABSTRACTGATEWAY_APPS_PORTS` |
| `apps.host` (*Where apps listen*) | `127.0.0.1` | Where the apps listen. Other values expose them to the network: use your own access control. | `apps config set host 0.0.0.0` | `ABSTRACTGATEWAY_APPS_HOST` |
| `apps.npm_registry` (*npm registry*) | `https://registry.npmjs.org` | npm registry (mirror) for app downloads and their dependencies. | `apps config set npm_registry URL` | `ABSTRACTGATEWAY_APPS_NPM_REGISTRY` |
| `apps.pypi_url` (*Node.js download index*) | `https://pypi.org/pypi` | Where the Node.js build is looked up. | `apps config set pypi_url URL` | `ABSTRACTGATEWAY_APPS_PYPI_URL` |

Console: Apps → *Advanced: apps settings* (one field per setting, with where its
value comes from). TUI: Runtimes → *Runtime knobs* → *Edit apps settings*. CLI:
`abstractgateway apps config get [NAME] [--json]` / `set NAME VALUE` (`""`
clears), on the data dir directly. All three validate the same way and show the
gateway's sentence on a refusal.

The legacy environment variables are only a fallback: a saved value always wins
(stored > env > default). A value that comes from the environment the gateway
was started with is reported as `source: env` ("From the environment" on the
page); an env value a saved one shadows is reported as `env_shadowed`.

Installing Node.js or an app runs software on the gateway host, so it follows
the host's **allow engine install** setting: on by default when the gateway
listens on this machine only (loopback), off otherwise. Install, update,
start and stop need an admin. Any signed-in user can list the apps and open a
running one (as themselves).

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
abstractgateway apps install code --launch
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

## HTTP API

All routes are under `/api/gateway/apps` and need a signed-in principal.

| Method and path | Who | What |
|---|---|---|
| `GET /apps?latest=true` | any user | Node.js status, one row per app (with `interfaces[]`, see "Terminal versions") and `console_tui` (the gateway console's terminal app). `latest=false` skips the npm registry and GitHub release lookups (cached 10 minutes). |
| `POST /apps/runtime/install` | admin | Install Node.js (a job), or `job: null` when one is already usable. |
| `POST /apps/{id}/install` `{"version"?, "launch"?}` | admin | A job: Node.js if needed, download, check, dependencies, and with `launch: true` start the app. |
| `POST /apps/{id}/update` `{"version"?}` | admin | A job: install the latest (or given) version; a running app is restarted on it. |
| `POST /apps/{id}/launch` | admin | Start the app (waits until it answers) and mark it enabled. |
| `POST /apps/{id}/stop` | admin | Stop the app and mark it disabled. 409 `started_outside_gateway` for an app the gateway did not start. |
| `POST /apps/{id}/open` `{"remember"?, "path"?}` | any user | A one-time `open_url` (relative to the gateway) that opens the running app signed in, at `path` inside the app when given (e.g. `/#new`). 400 `invalid_app_path` for anything that is not a path inside the app. |
| `GET /apps/{id}/logs?tail=200` | admin | The end of the app's log. |
| `GET /apps/jobs`, `GET /apps/jobs/{job}` | any user | Jobs: `state` (queued, running, succeeded, failed, cancelled), `percent`, `bytes_done`, `bytes_total`, `message`, `steps`, `details` (full log on failure). |
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
