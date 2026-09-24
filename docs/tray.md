# AbstractGateway — Desktop tray icon

`abstractgateway serve` can show a small icon in the macOS menu bar, the
Windows system tray or a Linux panel. It exists so that anyone running a
gateway on their own computer — including people who never open a terminal —
can see what it is doing and act on it in one click:

- **Open Console** — the web console (`/console`), **already signed in**:
  the tray mints a one-time sign-in link locally (the same code as
  `abstractgateway claim`; see *Security*) so an expired browser session never
  ends on a token prompt. One door: every other console entry point in the menu
  is a deep link to a tab of it.
- **Apps** — the six AbstractFramework apps, one short line each: **Open X**
  (running, whoever started it, or installed: started first), **Install X…**
  (installs through the gateway, with progress notifications: the same
  install as the console's Install button, so Code's terminal app comes with
  it; nothing opens by itself, the menu then offers **Open X**), **Launch
  Assistant** (the desktop app), otherwise the app's name, greyed. See *Apps*
  below.
- **Workflows** — the last 24 hours of runs, newest first: a state badge
  (🟢 running, 🟡 waiting, ✅ completed, ❌ failed, ⚪️ cancelled), the step
  count and how long each took, under a one-line tally. Rows are information;
  **Open Runs in Console** at the bottom is the way in. Root runs only — a
  deep-research run spawns dozens of children and this is a glance. The list
  is **host-wide** (`GET /api/gateway/host/runs`), not per-principal: memory,
  GPU and loaded models on this menu describe the machine, and the run list
  has to describe the same machine. Catalog-published workflows are shown
  under the name you know them by (their run id encodes scope and tenant in
  base64) and are not mistaken for the gateway's own bookkeeping runs. Summoned
  entities' data planes are not listed (the payload names them in
  `skipped_entity_planes`).
- **Pause / Resume Workflows** — the one high-level control over all of that:
  stop new workflow steps from running to free the machine or to look at what
  is going on (the gateway keeps answering; work queues until you resume).
- **Models** — what is in memory, what can be loaded, and the way to either:
  eject a loaded model, or preload an installed one. See *Models* below.
- **Show Activity Window** — two live graphs (memory, GPU) and the model list
  in a small window (needs `tkinter`; see below).
- **Start AbstractGateway at login** — a check item that shows whether this
  gateway WOULD start at your next login, and switches it. See *Start at login*.
- **Check for Updates / Restart / Quit / About / Help.**

The menu as shipped (macOS, a machine with 151 installed models; `[…]` is a
greyed information line, `▸` a submenu, `☐/☑` the check item):

```text
[AbstractGateway — Running]
[Ready · 1 model loaded · 249 MB]
[http://127.0.0.1:8080 · localhost only]
Open Console
Apps ▸              Open Observer | Open Continuum | Install Code… | … | Launch Assistant | Manage Apps in Console…
Copy Address ▸      http://127.0.0.1:8080 | http://192.168.1.23:8080 (Wi-Fi) | http://mymac.local:8080
Workflows ▸
Pause Workflows
[Memory   82.8 GB of 128 GB (65%)]
[GPU   0% busy]
Models ▸
    [Loaded: 1 · 249 MB · 45.2 GB free]
    ✓ Qwen1.5-0.5B-Chat-4bit · 249 MB · MLX ▸
        Eject — frees 249 MB
    Load a Model ▸
        [Your defaults]
        ☑ Text: Qwen1.5-0.5B-Chat-4bit · 260 MB · MLX · loaded
        MLX (11) ▸         recognised models first, then A–Z
        LM Studio (13) ▸
        Ollama (1) ▸
        Hugging Face (126) ▸   Recognised models (3) ▸ | A – F (30) ▸ | F – Q (30) ▸ | …
        Download Models in Console…
    Manage Models in Console…
Check for Updates…
Restart AbstractGateway…
☐ Start AbstractGateway at login
Network ▸           [Now: localhost only · 127.0.0.1:8080] | ● Localhost only | ○ Local network | ○ Internet…
Help ▸
Quit AbstractGateway…
```

The icon itself is a live gauge: the outer ring fills with system memory in
use (blue), the inner ring with GPU load (amber). A green dot in the centre
means a workflow step is executing right now; pause bars mean paused; a red
ring with `!` means the gateway is not answering.

Everything works offline except *Documentation*, *Report a Problem* and
*Check for Updates*, which open a website or ask pypi.org. The documentation
is online; the console's docs assistant answers from the `llms.txt` shipped
with the gateway.

## Install

The tray is an optional extra so servers never pull GUI libraries:

```bash
pip install "abstractgateway[tray]"      # pystray + Pillow
```

| Platform | What else is needed | Notes |
|---|---|---|
| macOS | nothing (pystray installs the `pyobjc` Cocoa bindings) | Retina-crisp icon; native alerts. |
| Windows | nothing | The icon may start in the tray *overflow* (the `^` chevron); drag it out to pin it. |
| Linux | the GTK/AppIndicator bindings: `sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1` (Debian/Ubuntu) | GNOME needs the *AppIndicator and KStatusNotifierItem* extension; KDE, XFCE, Cinnamon work out of the box. Pure Wayland sessions without a status-notifier host have no tray. |

The Activity window uses `tkinter` from the Python standard library. Some
Python builds ship without it (Homebrew: `brew install python-tk`; Debian:
`sudo apt install python3-tk`; pyenv builds need the Tk headers at build
time). When it is missing, the item is absent; the console's **Resources** tab
shows the same graphs.

## When the icon appears

At `abstractgateway serve` time the gateway decides, and says why on stderr:

**While the gateway runs, the icon is there.** There is no setting to turn it
off and no *Hide* item in its menu, because the icon is how people who never
open a terminal reach their gateway. It is absent only for one of these
reasons:

| Situation | Outcome |
|---|---|
| `abstractgateway[tray]` not installed | not started; the install hint is printed |
| No display (SSH session, container, Windows service, macOS daemon, CI) | not started (reason `headless`) |
| `serve --reload` (development) | not started (the app runs in uvicorn's reloader child) |
| A runner-only process (`abstractgateway runner`) | not started; the tray belongs to the process that serves the console |
| Otherwise | started; `Desktop tray: started (pid …)` |

The console's **Resources → Gateway** card reports which of these applies, as
plain text. If the helper itself crashed, `POST /api/gateway/host/tray/show`
(admin) retries it without restarting the gateway.

Everything the tray shows is also in the console's **Resources** tab: the
Gateway card (pause/resume, update, restart, quit), the memory and GPU meters,
and the model table with unload buttons. A paused gateway shows a banner on
every console tab with a *Resume* button.

## Models

**Models ▸** leads with what is in memory — a header (`Loaded: N · size ·
free memory`), then one row per loaded model. Each loaded row is a submenu
whose one action is **Eject — frees N GB**: a click on a check-marked row
never unloads by surprise. Eject asks first; when work is running on the
gateway it says so, because eject stops the calls running on that model first
and the next request that needs it loads it again (the gateway's eject
semantics). A locked model ("kept in memory") asks again before it goes.

**Load a Model ▸** preloads an installed model (`POST
/api/gateway/models/load`; the gateway pins it resident):

1. **Your defaults** — the models your capability routes name (Text, Image,
   Voice, Speech to text, Music), loaded under that route's task. A default
   that is not downloaded is shown greyed with its status, never offered as a
   click that fails.
2. **One submenu per engine** — MLX, LM Studio, Ollama, Hugging Face — with
   every model the engines hold on this machine (`GET /models/installed`),
   size on disk included. Models the catalog recognises come first, then A–Z.
   A list longer than 30 is split into ranges (`A – F (30) ▸`); nothing is
   ever left out. A model larger than the free memory says so, and loading it
   asks first. Embedding models are listed greyed ("load on use"): the
   residency API has no embedding task.

Every load shows *Loading X* while it runs (a notification and a greyed row),
then *Loaded X after N s* — or a dialog with the gateway's full reason when it
fails.

Installed models are grouped by engine rather than by capability: the engine
is always known and decides how a model loads, while many installed artifacts
carry no capability metadata. Capabilities appear where they are known: the
routes you configured, under **Your defaults**.

The model lists refresh every 5 minutes and after each load or eject; the
Activity window and the console's Models tab show the same data live.

## Apps

The **Apps** submenu lists Observer, Continuum, Code, Entity, Flow (the
stack order and ports of `scripts/start-local.sh`: 3001-3005) and Assistant,
one short line each and never a reason: the full reason lives in the
console's Apps tab. Presence is detected, never imported — the gateway does
not depend on its apps:

| App | Detected by | Line |
|---|---|---|
| Observer, Continuum, Code, Entity, Flow | the gateway (`GET /api/gateway/apps`): its own installs, and apps started outside it (the dev stack, `npx`, a service) found on their usual port | **Open X** when running, whoever started it (a one-time signed-in handover); **Open X** when installed and stopped (starts it, then opens it); **Install X…** when it can be installed (the browser app and, for Code when a ready-made download exists for this computer, its terminal app, as one job; a notification when it is done, then **Open X**); otherwise **X**, greyed |
| the same, installed globally | the app's command on PATH (`abstractobserver`, `abstractflow-editor`, `abstractcode-web`, `abstractcontinuum`, `abstractentity`) or the package under `npm root -g` | **Open X** — started by the tray with the gateway URL passed in and stopped when the tray exits; sign in inside the app (install it here instead for the one-click sign-in) |
| Assistant | the same detection as the console's Assistant card (`apps_desktop.detect_assistant`): `AbstractAssistant.app` in /Applications or ~/Applications, the `abstractassistant` command (this Python's scripts folder, or PATH), or the package in this Python (`importlib.util.find_spec`, without importing it) | **Launch Assistant** when found (also while it runs); **Install Assistant…** when the gateway can install it into its own Python (the console's Install); otherwise **Assistant**, greyed |
| Code's terminal version (the only app with one today) | the gateway's presence check, reported as `interfaces[kind="tui"]` on the app row: its own copy in `<data dir>/apps/bin/`, `abstractcode` on PATH, or `~/.cargo/bin` | **Open Code in Terminal** — a new terminal window on this machine, signed in through a one-time code (`POST /api/gateway/apps/code/launch-tui`, the same route as the console's button). Shown only when the gateway reports it installed; greyed when the gateway would refuse (the console says why) |

When an app cannot be installed from here, ONE line near the bottom says so:
"Installs are off for this gateway · Console → Apps" (the gateway's install
setting refuses this caller), or "Installs unavailable now · Console → Apps"
(for example the npm registry is unreachable). The tray is on the gateway
machine, so with the default setting its installs are allowed whatever
address the gateway listens on. **Manage Apps in Console…** at the bottom
opens the console's Apps tab (updates, logs, stop, and the full reasons).

The tray talks to its gateway over loopback (`http://127.0.0.1:<port>`) for
every network mode; the network address in the menu's header and in
**Copy Address** is for other devices.

A folder called `abstractassistant` in the gateway's working directory (a
source checkout) resolves as a *namespace package*; it is not an install and
is ignored. Apps started by the tray get an environment with every token,
secret, password and key removed, like the apps the gateway runs itself.

## Start at login

**Start AbstractGateway at login** is checked only when THIS gateway (its data
folder) would really start at your next login. Toggling it registers or
removes the same per-user login item as the CLI and the installers:

| OS | Mechanism (per user, no admin) | "On" means |
|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/ai.abstractframework.gateway.plist` (`RunAtLoad`) | the plist parses, the program it starts exists, and `launchctl print-disabled` does not list it as disabled |
| Linux (systemd) | user unit `~/.config/systemd/user/abstractgateway.service` | the program exists and `systemctl --user is-enabled` says `enabled` |
| Linux (no systemd user manager) | XDG autostart entry `~/.config/autostart/abstractgateway.desktop` (graphical login) | the program exists and the entry is not switched off (`X-GNOME-Autostart-enabled=false`, `Hidden=true`) |
| Windows (experimental) | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\AbstractGateway` → `pythonw -m abstractgateway.os_service launch …` | the program exists and Task Manager's *Startup apps* has not disabled it |

The item reads **— needs repair** when a registration exists but would not
start (the gateway was moved or reinstalled elsewhere, the file is unreadable,
the unit is disabled); the line under it says why, and a click repairs it. It
reads **(another gateway is registered)** when the login item belongs to
another data folder; a click asks before replacing it. A registration that
pins `--host/--port` on its command line also reads **— needs repair**
("pinned to 127.0.0.1:N by the login item …"): it starts, but the **Network**
choice cannot apply to it. The click rewrites it to plain `serve` and keeps
the stored network mode.

Turning it on registers for the **next** login: it never starts a second copy
of the gateway that is already running. Turning it off only unregisters: the
gateway keeps running now. The same switch from a terminal:

```bash
abstractgateway service status      # on | off | broken | other, and why
abstractgateway service enable      # this gateway at next login; the Network setting binds it
abstractgateway service disable     # the running gateway keeps running
abstractgateway service install     # enable + start now + wait for health (installers)
abstractgateway service uninstall   # disable + stop it
```

## Network and addresses

The line under the status header is the address to share and the network
mode, e.g. `http://127.0.0.1:8080 · localhost only` (`· restart required`
while a change waits for a restart). **Network ▸** shows what runs now
(`Now: local network · 0.0.0.0:8080`) and three choices — **Localhost only**,
**Local network**, **Internet…** — whose mark is what is *set*. Choosing one
saves it (`POST /api/gateway/network`); when it needs a restart, **Restart to
apply** appears (and the headers say *restart required*). **Internet…** first
shows what exposing the gateway means, with the gateway's own warnings, and
posts only after you acknowledge it. A refusal (for example sign-in not set up
for that mode) opens a dialog with the reason and the fix. **Copy Address ▸**
lists every address the gateway answers on (loopback, each network interface,
the machine's `.local` name) and copies the one you click.

A gateway without the network settings route says so in the Network submenu,
and Copy Address still offers the address the tray talks to.

## Menus without submenus

pystray draws submenus and check marks on macOS, Windows and Linux
(AppIndicator). Some Linux panels drop submenus: set `"flat_menu": true` in
`<data_dir>/tray/prefs.json` and restart the gateway — every row is kept,
prefixed with its path (`Models › Load a Model › MLX › …`). pystray's plain
X11 backend (`xorg`) has no menu at all: clicking the icon opens the console,
and a notification at start names the CLI equivalents (`abstractgateway
service …`, `apps …`, `models …`).

## Pause

Pausing is process-wide and **persists across restarts**: a laptop paused to
get its GPU back does not silently resume after a reboot or an update. While
paused:

- no new workflow step starts — runs, schedules and bridge-started work are
  accepted and wait;
- a step already inside an LLM or tool call finishes first (the menu says
  "Finishing N runs at the next step");
- the console, the API and connected apps keep answering; cancelling a run
  still works;
- summoned entities' own-time loops are **not** affected (they are separate
  processes with their own lifecycle controls);
- `GET /api/health` carries `"paused": true` while `status` stays
  `"healthy"` — a supervisor must never recycle a paused gateway.

Pause reaches inside a tick: AbstractRuntime's `Runtime.tick(step_gate=…)`
consults the gateway's gate at every step boundary. Where the runtime does
not offer that gate, the pause takes effect at tick boundaries (up to
`tick_max_steps` steps later); `GET /host/runner` reports
`step_gate_supported` and the menu says so.

In the split layout (`serve --no-runner` + `abstractgateway runner`) the
pause is written to `<data_dir>/gateway_paused.json` and the runner process
picks it up within about two seconds.

## Restart and update

**Restart** asks uvicorn for its normal graceful shutdown (runner drain,
entity close), then relaunches the same command in the same environment
(`python -m abstractgateway …`). On macOS and Linux the process keeps its PID
and terminal; on Windows a new process is spawned on the same console. Restart
is refused (HTTP 409, greyed out in the tray) under `serve --reload`, when the
server was not started by `abstractgateway serve`, or while an update is
being installed.

**Check for Updates** asks pypi.org for the latest release (5 s timeout, one
check per hour). Offline is a normal answer, not an error. The one-click
**Update** is offered only when the gateway can reproduce its own install:

| Install | Upgrade command used | One-click? |
|---|---|---|
| `pip` in a virtual environment | `python -m pip install --upgrade "abstractgateway[<your extras>]"` | yes |
| `uv venv` / `uv pip` | `uv pip install --python … --upgrade …` | yes |
| `pipx` | `pipx upgrade abstractgateway` | yes |
| `uv tool` | `uv tool upgrade abstractgateway` | yes |
| editable checkout (`pip install -e .`) | — update with `git pull` | no |
| Docker image | — pull the newer image | no |
| system Python (PEP 668 "externally managed") | — use pipx or a venv | no |

The installed extras (`apple`, `gpu`, `embeddings`) are detected and kept.
The upgrade runs in the background (log tail on the console's Gateway card);
when it finishes the tray offers **Restart to finish the update**. Restarting
is what makes the new version run — until then the process keeps serving the
old code.

## Security

The tray talks to the gateway over loopback HTTP with a **per-process
ephemeral admin token** handed over on the helper's stdin — never on the
command line, never in the environment, never on disk. The token is accepted
only from a loopback socket peer (`127.0.0.0/8`, `::1`) and dies with the
process. Audit-log entries made through it carry
`source: loopback-ephemeral:desktop-tray`.

**Signed-in links.** *Open Console* does not ask the gateway to sign anyone
in — there is no such endpoint, by design. The tray writes a one-time claim
code under `<data_dir>/auth/claims/` (only someone who can write the data
folder can), valid 2 minutes, and opens `/console#claim=<code>`; the console
redeems it once, from a loopback browser only. A gateway running with a static
token (no user accounts) cannot redeem claims, so the plain URL opens and a
notification says why. *Copy Console Link* copies the plain URL: a sign-in
link does not belong on a clipboard. App links use the gateway's one-time
handover (`POST /api/gateway/apps/{id}/open`, 2 minutes, single use).

`FORWARDED_ALLOW_IPS=*` (uvicorn's proxy setting) would let any client rewrite
its peer address; the gateway warns at boot when it sees that with a tray
running. Use a concrete proxy IP.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md#there-is-no-tray-icon) for a
missing icon. Other cases:

- **The helper starts then disappears**: read `<data_dir>/logs/tray.log`;
  `GET /api/gateway/host/tray` reports `exit_code` and the readiness failure.
  After two crashes in a row it is not restarted automatically;
  `POST /api/gateway/host/tray/show` (admin) starts it again.
- **The icon says "Not responding"**: the gateway is restarting, stopped or
  unresponsive. *Force Quit* in that state sends the gateway process SIGTERM
  and, after five seconds, kills it.

## Licensing note

`pystray` is LGPL-3.0 and `python-xlib` (Linux) is LGPL-2.1. AbstractGateway
imports them dynamically as ordinary dependencies, which is compatible with
its MIT license. A frozen single-file build (PyInstaller and the like) would
have to honour the LGPL relinking terms.
