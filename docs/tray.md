# AbstractGateway — Desktop tray icon

`abstractgateway serve` can show a small icon in the macOS menu bar, the
Windows system tray or a Linux panel. It exists so that anyone running a
gateway on their own computer — including people who never open a terminal —
can see what it is doing and act on it in one click:

- **Open Console** — the web console (`/console`). One door: every other
  console entry point in the menu is a deep link to a tab of it.
- **Workflows** — the last 24 hours of runs, newest first: a state badge
  (🟢 running, 🟡 waiting, ✅ completed, ❌ failed, ⚪️ cancelled), the step
  count and how long each took, under a one-line tally. Rows are information;
  **Open Runs in Console** at the bottom is the way in. Root runs only — a
  deep-research run spawns dozens of children and this is a glance. The list
  is **host-wide** (`GET /api/gateway/host/runs`), not per-principal: memory,
  GPU and loaded models on this menu describe the machine, and the run list
  has to describe the same machine. Catalog-published workflows are shown
  under the name you know them by (their run id encodes scope and tenant in
  base64) and are not mistaken for the gateway's own bookkeeping runs. Entity planes are the one exception and
  the payload names them — reaching one goes through the entity registry,
  which opens homes and wires embedders, and that must not ride a poll.
- **Pause / Resume Workflows** — the one high-level control over all of that:
  stop new workflow steps from running to free the machine or to look at what
  is going on (the gateway keeps answering; work queues until you resume).
- **Loaded Models** — every model currently in memory with its size; click
  one to unload it and free the memory.
- **Show Activity Window** — two live graphs (memory, GPU) and the model list
  in a small window (needs `tkinter`; see below).
- **Check for Updates / Restart / Quit / About / Help.**

The icon itself is a live gauge: the outer ring fills with system memory in
use (blue), the inner ring with GPU load (amber). A green dot in the centre
means a workflow step is executing right now; pause bars mean paused; a red
ring with `!` means the gateway is not answering.

Everything works offline except *Documentation*, *Report a Problem* and
*Check for Updates*, which open a website or ask pypi.org. Menu items name what
they open and nothing else — a "(needs internet)" suffix on every second line
is noise the reader steps over, and the browser says so the one time it
matters. (The human docs live online: the wheel ships `llms.txt` for
docs-grounded Q&A, not a browsable copy of this site.)

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
time). When it is missing, the item is simply absent — the console's Resources
tab shows the same graphs, and a second menu entry pointing at the same browser
page is one choice too many.

## When the icon appears

At `abstractgateway serve` time the gateway decides, and says why on stderr:

**While the gateway runs, the icon is there.** There is no setting to turn it
off and no *Hide* item in its menu: the icon is how someone who never opens a
terminal reaches their gateway, and a switch whose only effect is to remove
that entry point is a way to lose the product. Every reason it can be absent
is a fact about the machine, not a preference:

| Situation | Outcome |
|---|---|
| `abstractgateway[tray]` not installed | not started; the install hint is printed |
| No display (SSH session, container, Windows service, macOS daemon, CI) | not started, silently |
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

Pause reaches inside a tick: an AbstractRuntime that ships
`Runtime.tick(step_gate=…)` (the release after 0.4.31) consults the gateway's
gate at every step boundary. With an older runtime the pause takes effect at
tick boundaries (up to `tick_max_steps` steps later); `GET /host/runner`
reports `step_gate_supported` and the menu says so.

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

`FORWARDED_ALLOW_IPS=*` (uvicorn's proxy setting) would let any client rewrite
its peer address; the gateway warns at boot when it sees that with a tray
running. Use a concrete proxy IP.

## Troubleshooting

- **"Desktop tray: not started (missing_dependency …)"** — install the extra
  (and, on Linux, the GTK/AppIndicator bindings).
- **The helper starts then disappears** — read `<data_dir>/logs/tray.log`;
  `GET /api/gateway/host/tray` reports `exit_code` and the readiness failure.
  Two crashes in a row stop automatic restarts until the setting is toggled
  or `POST /api/gateway/host/tray/show` is used.
- **The icon says "Not responding"** — the gateway is restarting, stopped or
  wedged. *Force Quit* in that state sends the gateway process SIGTERM and,
  after five seconds, kills it.
- **GNOME shows no icon** — install the AppIndicator extension; the gateway
  reports `headless: no system tray on this desktop` when it can tell.

## Licensing note

`pystray` is LGPL-3.0 and `python-xlib` (Linux) is LGPL-2.1. AbstractGateway
imports them dynamically as ordinary dependencies, which is compatible with
its MIT license. A frozen single-file build (PyInstaller and the like) would
have to honour the LGPL relinking terms.
