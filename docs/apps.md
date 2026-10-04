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
| A newer version is published | **Update to x.y.z** beside **Open** (an admin; its tooltip: "Install the newest Flow Editor (0.8.0); a running app restarts on it") |
| Failed | the reason, **Show details**, and **Install** again |

Stop, Show log, versions, addresses and commands are under
**Technical details**.

## Updates

Every app row says when a newer version is published: the browser apps from
the npm registry (`dist-tags.latest`), the Assistant from PyPI
(`<pypi_url>/abstractassistant/json`, `info.version`). One cache serves the
npm registry, PyPI and GitHub (the terminal apps' releases): an answer is
reused for 10 minutes, a failure for 1 minute, then the registry is asked
again, so a version published while the gateway runs shows up within 10
minutes without a restart (**Check again** asks the gateway at once, still
through that cache). `update_available` is true when that version is newer
than the installed one (semantic versions, prereleases before their release).

- **An app the gateway installed** (browser apps, the Assistant) gets **Update
  to x.y.z**: one click starts the update as a job, the same one as Install
  for that version. A running browser app restarts on the new version. The
  Assistant: see [The Assistant](#the-assistant-a-desktop-app).
- **An app started outside the gateway** shows "Latest 0.8.0 · Started outside
  the gateway — update it where it was installed" and has no update action:
  its process belongs to whoever started it. An Assistant installed from a
  source checkout likewise shows "Latest x.y.z · Installed from a source
  checkout — update it there".
- The button's label and tooltip come from the gateway (`update_label`,
  `update_tip` on the row), so the web console and the terminal console show
  the same words; in the terminal console `u` updates the selected app, with
  the tooltip as its confirmation.
- When the installer upgrades the framework with no gateway running, it leaves
  the apps' versions in `<data dir>/apps-upgrade.pending` and the gateway
  brings each installed app UP to that version at its next start; an app you
  already updated past it from the Apps page is kept as it is.

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
it answers, and opens it in a new tab, already signed in, at
`<the gateway's address>/apps/<app>/` (see
[Apps are served through the gateway](#apps-are-served-through-the-gateway)).

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
- Apps always listen on `127.0.0.1`: browsers reach them through the gateway
  (next section), never directly. Ports: the app's usual port when it is
  free, else the first free port in 3100-3199. The usual ports are the
  framework's stack port map (`scripts/start-local.sh`): Observer 3001,
  Continuum 3002, Code 3003, Entity 3004, Flow 3005.

## Apps are served through the gateway

Every browser app opens at **`/apps/<app>/` on the gateway's own address**:
`http://127.0.0.1:8080/apps/observer/` on the gateway machine,
`https://gateway.example.com/apps/flow/` behind a reverse proxy, the same
path through a tunnel. One address and one port serve the console, the API
and every app, so a remote or headless gateway needs nothing more than the
one address it already has.

The gateway relays each request to the app's own server on `127.0.0.1`
(app_proxy.py):

- **Signed in, per app.** Every request under `/apps/<app>/` needs a valid
  gateway session for that app: the app's own session cookie, set by the
  one-time handover below with `Path=/apps/<app>/`. Without one, opening a
  page sends the browser to the console (`/console#apps?open=<app>&path=…`),
  which signs you in and opens the app on the page you asked for; any other
  request gets 401 `app_sign_in_required`. A WebSocket without one is
  refused. A request or WebSocket whose `Origin` is not the gateway's own
  address (another site, or another port on the same host) is refused too
  (403 `cross_origin`).
- **Public assets.** What a browser fetches without cookies answers without
  a session: the web app manifest (`manifest.webmanifest`, `manifest.json`,
  `site.webmanifest`), `favicon*`, `icon*`, `apple-touch-icon*` and
  `icons/<file>` at the app's root, GET/HEAD only. They are relayed with no
  cookie and returned only when the app answers 200/304 with a manifest or
  image type, with no `Set-Cookie`. Pages, scripts, `sw.js` and everything
  under `api/` stay signed-in only.
- **Streaming.** Responses are relayed as they arrive (live updates, server-sent
  events), and WebSockets frame by frame.
- **What the app receives.** The path with `/apps/<app>` removed, plus
  `X-Forwarded-Prefix: /apps/<app>`, `X-Forwarded-For: <the browser's
  address>` (written by the gateway, never passed through from the
  browser), `X-Forwarded-Proto` and `X-Forwarded-Host`, always both. A
  request whose `Host` is not a host name or address the gateway can pass on
  (browsers accept some, such as `a_b.example.com`, that it cannot) is refused
  with 400 `invalid_host` and never reaches the app. It receives only its
  own cookies (`<app prefix>_*`): never the console's session cookie, never an
  `Authorization` header. It can set only its own cookies in return.
- **Only apps that say they can.** An app announces that it can be served
  this way with a header on every response:
  `X-AbstractFramework-App: <app>; mount=1` (the abstractuic app-server kit
  does it). The row's `mounted` and `app_path` (`/apps/<app>/`) say whether it
  does. An older app version does not: it would read every visitor as a
  browser on the gateway machine (the gateway is its local peer), so the
  gateway never serves it under `/apps/<app>/` (409 `not_mountable`), and it
  opens by its own port from the gateway machine only (409
  `app_loopback_only` from anywhere else, with the hint to update it).

How an app uses these headers (base path, the browser's address, cookie
paths, the identity header) is the app-server kit's contract:
[abstractuic `app-server`](https://github.com/lpalbou/AbstractUIC/tree/main/app-server).

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
one-time sign-in link below, and `/apps/<app>/` when it announces it can be
served there): the app's server reads the same sign-in cookies whoever
started it. The gateway does not stop, update or show the
log of an app it did not start; the console's **Technical details** says
"Started outside the gateway on port 3001" instead (when a newer version is
published, the card says "Latest x.y.z · Started outside the gateway — update
it where it was installed"), and `POST /apps/{id}/stop`
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
(`POST /api/gateway/apps/{id}/open`, sending the browser's `origin`) and
opens it in a new tab. The link (`/apps/handover/<code>`) works once, for two
minutes, and only on the address it was made for. The gateway creates a
browser session for the person who clicked, puts it in the app's own sign-in
cookies (`Path=/apps/<app>/`), and answers with a **relative** redirect to
`/apps/<app>/`, so it lands on whatever address the browser used (a LAN
address, a reverse proxy, a tunnel). The app's server finds the session and
the app opens connected. The gateway token never appears in the page, the
link or the browser's storage.

The answer also carries `app_path` (`/apps/<app>/…`) and `app_url` (the
`origin` you sent, or the address the request came in on, plus `app_path`).
`origin` must be exactly `scheme://host[:port]` (400 `invalid_origin`
otherwise).

An older app version that cannot be served through the gateway keeps the
direct link to its own port (`app_path: null`, cookies at `Path=/`), which
works from the gateway machine only; from another computer the gateway says
so (409 `app_loopback_only`) instead of producing a broken link.

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
 "installed": true, "version": "0.7.0", "source": "gateway", "path": "/home/me/.local/bin/abstractcode",
 "latest_version": "0.7.0", "update_available": false,
 "install_available": false, "install_method": "release_binary", "install_blocked_reason": null,
 "install_command": "cargo install abstractcode", "download_page": "https://github.com/lpalbou/abstractcode/releases",
 "launch_available": true, "launch_blocked_reason": null, "launch_mode": "terminal",
 "command": "abstractcode --gateway http://127.0.0.1:8080",
 "signin_command": null, "active_job": null}
```

- **One folder, shared with the installer.** Terminal apps go where uv put
  the gateway's own commands (`uv tool dir --bin`, usually `~/.local/bin`, on
  Windows `%USERPROFILE%\.local\bin`), which the installer puts on `PATH`;
  the gateway reads it from the receipt uv writes into its environment
  (`uv-receipt.toml`). The installer builds `abstractcode` and the terminal
  console into the same folder (`cargo install --root` its parent), so
  `abstractcode` runs by name whichever of the two installed it. A gateway
  that is not a uv tool install (a development checkout, pip) uses
  `<data dir>/apps/bin/`, off `PATH` (`command` then carries the full path).
  A receipt uv did not write (no `[tool]` table, no `entrypoints` list, a
  relative `install-path`) is ignored and `<data dir>/apps/bin/` is used.
- **Found by presence.** The gateway never imports or runs an app to detect
  it: it looks for the binary in that folder, in `<data dir>/apps/bin/`
  (where gateways before 0.7.1 put it; the next install or update moves it
  and removes the old copy), on `PATH` and in `~/.cargo/bin`, and accepts it
  only when its `--help` names the program (PyPI's unrelated Python package
  `abstractcode` installs a script with the same name). `source` says where
  it was found (`gateway`: one of the gateway's folders, `path`: elsewhere).
- **Install (`install_method`).** `release_binary`: Code publishes prebuilt
  binaries for macOS (Apple silicon and Intel), Linux (x86_64 and arm64,
  glibc) and Windows (x86_64) on its GitHub release, with a `SHA256SUMS` file.
  The card's Install (and `install-tui` on its own) downloads the archive for
  this computer, checks it
  against `SHA256SUMS` and against the sha256 digest GitHub reports for the
  file (both must agree), unpacks the single binary into the folder above,
  makes it executable and runs `--version` before it replaces anything.
  It replaces only a copy of the app itself (its own earlier install or the
  installer's build). When another program already holds the name there, for
  example the older Python `abstractcode` from PyPI, the install stops with
  `foreign_binary`, names the file and leaves it: run
  `uv tool uninstall abstractcode` (or remove the file), then install again.
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
  CSRF) and forwards `/api/*` to the gateway, which is how live updates
  (server-sent events) reach the page;
- Observer reveals local folders, Flow keeps its connection file, and
  Continuum proxies the agora hub.

The gateway serves them all on its own address at `/apps/<app>/` (above). It
gives each server its port and gateway URL as launch flags (`--port`,
`--host`, `--gateway-url`), which win over the environment and over any
settings file the app keeps. An installed version older than the flags
(Observer 0.1.14, Code 0.5.0 or Entity 0.2.2 and earlier), or a global
install of unknown version started from the tray, also gets the legacy
environment (`PORT`, `HOST`, `<APP>_GATEWAY_URL`), so it still listens on the
port the gateway chose. The bind address is always `127.0.0.1`.

## Settings

Four settings control the apps: `apps.node` (*Node.js for apps*),
`apps.ports` (*Ports for apps*), `apps.npm_registry` (*npm registry*) and
`apps.pypi_url` (*Node.js download index*). `apps.host` (*Where apps
listen*) is **deprecated**: apps always listen on `127.0.0.1` and open
through the gateway. It accepts only a loopback address; an older saved
`0.0.0.0` (or `ABSTRACTGATEWAY_APPS_HOST`) is ignored with one warning in the
gateway's log, and clearing it removes the warning. Change them from the Apps page (the toolbar gear, *Apps settings*), the
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
- Installing needs the npm registry (and PyPI for Node.js and the Assistant). When it cannot be
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
  `AbstractAssistant.app` in `/Applications` or `~/Applications`.
- **One Assistant: the installed package.** When the package is installed (its
  command, else this Python running it), the card describes, opens and watches
  that one; the app bundle is used only when no package is installed. The
  version is that Assistant's (the package's, or the app's `Info.plist` for
  the bundle). It is **Running** when a process of that Assistant runs (its
  command, `python -m abstractassistant…`, or — for the bundle — the app's own
  program). When the OTHER one runs (for example an older
  `AbstractAssistant.app` next to the installed package), the card is not
  "Running": it says so in one sentence — "Another Assistant is running:
  /Applications/AbstractAssistant.app 0.5.0 — quit it to use 0.13.0" — and
  **Open** still starts the installed one. The tray's "Launch Assistant" uses
  the same detection (`apps_desktop.detect_assistant`) and shows the same
  sentence under it, so the tray and the console always agree.
- **Install** installs `abstractassistant` into the gateway's own Python as a
  job (`uv pip install --python <gateway python> abstractassistant`, or pip
  when there is no uv), with every `abstract*` package the gateway runs
  pinned to its current version (`name==version` requirements in the same
  command), so installing the Assistant never changes the gateway. The same
  rule as the other installs decides who may install (see [Who may install](#who-may-install)).
- **Open** starts it on the gateway's computer: its command (or this Python
  running it), or `open -a AbstractAssistant.app` when only the app exists, as
  a separate process with none
  of the gateway's tokens, secrets or keys in its environment. A running
  Assistant is not started twice: the app is brought to the front (or, when
  it was started from its command, the card says its icon is in the menu
  bar). An Assistant opened from here (the console or the menu bar icon) is
  connected to this gateway and signed in as you automatically: the gateway
  starts it with `--gateway-url <address> --gateway-handover-file <file>`,
  where the file (readable by you only, in `<data dir>/handover/`) holds a
  one-time code valid for two minutes and the id of the user who clicked
  Open (`{schema: "abstractgateway.desktop_handover.v1", code, base_url,
  expires_at, user_id}`). The Assistant reads and deletes the
  file, trades the code for a remembered sign-in and keeps it. No code or
  token is ever on its command line or in its environment. An Assistant that
  is already running cannot receive a code: if it is not signed in, quit it
  and open it again from here.
- **Update to x.y.z** (when PyPI has a newer `abstractassistant`) runs the
  install job with `abstractassistant==<latest>` and the same pins (the
  Assistant itself is never pinned to its old version). An Assistant this
  gateway opened is quit and opened again on the new version, signed in as
  the admin who clicked Update ("a running app restarts on it"). An Assistant
  the gateway did not open (started from its command or a script such as
  `scripts/start-local.sh`, or the other one of the two above) is left alone:
  the tooltip, the job's result and the card say "Quit it and open it again
  to run 0.14.0" until that process ends. When PyPI cannot be reached,
  **Technical details** say "PyPI is not reachable: …" and there is no
  Update. The gateway remembers which Assistant it opened only while it runs.
  The update must land: when the installer finishes but the requested version
  is not the one installed afterwards, the job fails with "Asked for 0.14.0,
  but 0.13.0 is still installed — not updated." (the installer output under
  **Show details**) and nothing is restarted.
- **Installed from a source checkout** (an editable install: the package's
  `direct_url.json` says `dir_info.editable: true`, as `scripts/build.sh`
  installs it): the card shows its version and, when PyPI has a newer one,
  "Latest x.y.z · Installed from a source checkout — update it there", with
  no Update (installing a PyPI wheel would replace the checkout);
  `POST /apps/assistant/update` refuses with the same sentence.
- **From another computer** the card says "The Assistant runs on the gateway's
  computer: open it there." with no button: it is a desktop app for that
  computer's screen.
- **Technical details** show its version, where it was found and the launch
  command (or the install command when it is not installed).

## HTTP API

All routes are under `/api/gateway/apps` and need a signed-in principal, except `desktop-handover`.

| Method and path | Who | What |
|---|---|---|
| `GET /apps?latest=true` | any user | Node.js status, one row per app (`kind` `web` with `interfaces[]`, see "Terminal versions", and `install_parts`; then the Assistant, `kind` `desktop` with `desktop {location, found_by, launch_command, install_command, launch_available, launch_blocked, launch_blocked_reason, other_running, restart_note, started_by_gateway, source_checkout, latest_error}`); every row has `latest_version`, `update_available`, `update_label` and `update_tip` (see [Updates](#updates)) and `console_tui` (the gateway console's terminal app). `gateway_url` is where the app servers reach the gateway (on its machine); `browser_gateway_url` is the address the caller uses (e.g. `https://<host>.ts.net` behind `tailscale serve`), the one to show in any command or link. `latest=false` skips the npm registry, PyPI and GitHub release lookups (cached 10 minutes). |
| `POST /apps/runtime/install` | admin | Install Node.js (a job), or `job: null` when one is already usable. |
| `POST /apps/{id}/install` `{"version"?, "launch"?, "with_terminal"?}` | admin | ONE job: Node.js if needed, download, check, dependencies, then the terminal app when the row's `install_parts` has `"tui"` (`with_terminal: false` skips it); the job's `parts` are its child rows. Starts nothing unless `launch: true`. For `assistant`: installs `abstractassistant` into the gateway's Python (every `abstract*` package is pinned to its current version in the same command). |
| `POST /apps/{id}/update` `{"version"?}` | admin | A job: install the latest (or given) version; a running app is restarted on it. For `assistant`: `abstractassistant==<version>` from PyPI with the gateway's pins; the Assistant this gateway opened reopens signed in as the caller, any other running Assistant is left alone ("Quit it and open it again to run x.y.z"). A row started outside the gateway has no `update` action. |
| `POST /apps/{id}/launch` | admin | Start the app (waits until it answers) and mark it enabled. For `assistant`: open it on the gateway's computer, signed in as the caller, `{ok, app, already_running, signed_in_by_gateway, message}`; from another computer 409 `not_on_gateway_machine`, and nothing starts. |
| `POST /apps/desktop-handover` `{"code"}` | no sign-in; this computer only | The Assistant trades its one-time code for a remembered sign-in: `{base_url, session_id, csrf_token, user_id, expires_at}`. Answered only for a direct caller on this computer (no proxy headers, no app-server session header): 403 otherwise; 410 for a used or expired code. |
| `POST /apps/{id}/stop` | admin | Stop the app and mark it disabled. 409 `started_outside_gateway` for an app the gateway did not start. |
| `POST /apps/{id}/open` `{"remember"?, "path"?, "origin"?}` | any user | A one-time `open_url` (relative to the gateway) that opens the running app signed in, at `path` inside the app when given (e.g. `/#new`): `{open_url, mounted, app_path, app_url, expires_in_s}`. `app_path` is `/apps/<id>/…` for an app served through the gateway (else `null`); `app_url` is `origin` (default: this request's address) + `app_path`. 400 `invalid_app_path` / `invalid_origin`. 409 `desktop_app` for the Assistant (use `/launch`). |
| `GET /apps/{id}/logs?tail=200` | admin | The end of the app's log. |
| `GET /apps/jobs`, `GET /apps/jobs/{job}` | any user | Jobs: `state` (queued, running, succeeded, failed, cancelled), `percent`, `bytes_done`, `bytes_total`, `message`, `steps`, `parts` (the child rows of an Install that covers two parts), `details` (full log on failure). |
| `POST /apps/jobs/{job}/cancel` | admin | Cancel a job. |
| `POST /apps/{id}/install-tui` | admin | A job: download, check and place the app's prebuilt terminal version (or update the gateway's copy). 409 `toolchain_required` with `command` when only a source build exists. |
| `POST /apps/{id}/launch-tui` | admin, on the gateway machine | Open the terminal version in a new terminal window, signed in as the caller: `{ok, app_id, interface: "tui", terminal, version, message, expires_in_s}`. From another computer (non-loopback peer or `Host`, or any proxy header): 409 `not_on_gateway_machine` with `command` and `signin_command`, and nothing opens. |
| `POST /apps/{id}/tui-command` | admin, on the gateway machine | The same one-use launcher as launch-tui without opening a window: `{ok, app_id, interface, version, signin_command, command, expires_in_s}`. From another computer: 409 `not_on_gateway_machine` with `command` and `signin_command`. |
| `POST /apps/tui-handover` `{"code"}` (outside `/api/gateway`) | the launcher script, loopback only | Trade a launcher's one-time code for `{token, token_env, url_env, gateway_url, gateway_flag, user}`. 403 `loopback_only`, 410 `handover_expired`. |

The apps themselves, outside `/api/gateway`:

| Method and path | Who | What |
|---|---|---|
| `GET /apps/handover/<code>` | the one-time code | Sets the app's sign-in cookies and redirects: relative `303` to `/apps/<id>/…` (cookies `Path=/apps/<id>/`), or to the app's own port for an older app. 410 used or expired, 400 another address. |
| any `/apps/<id>/…` (HTTP, SSE, WebSocket) | the app's gateway session | The app, relayed (see [Apps are served through the gateway](#apps-are-served-through-the-gateway)). Without a session: `303` to `/console#apps?open=<id>&path=…` for a page load, else 401 `app_sign_in_required`. 400 `invalid_host`, 404 `unknown_app`, 403 `cross_origin`, 409 `not_running` / `not_mountable`, 502 `app_not_answering`. `/apps/<id>` redirects to `/apps/<id>/`. |

Errors are `{"ok": false, "reason", "message", "hint"?, "details"?}` with
404 (`unknown_app`, also for a terminal route on a browser-only app), 409
(`not_installed`, `node_missing`, `not_running`, `app_loopback_only`,
`toolchain_required`, `foreign_binary`, `not_on_gateway_machine`, `no_terminal`,
`started_outside_gateway`), 403
(`installs_not_allowed`), 502 (`integrity_mismatch`), 503
(`network_unavailable`, `no_free_port`) or 500 (`launch_failed`, with the
app's output in `details`). The terminal errors also carry `command` (and,
where it helps, `install_command` or `signin_command`) so the console can
offer the command to copy instead.
