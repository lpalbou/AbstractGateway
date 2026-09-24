# AbstractGateway — First run

This page covers the shortest path from `pip install abstractgateway` (or
`uv tool install abstractgateway`) to a signed-in console on your own machine:
no environment variables, no token to copy. It also covers starting the gateway
at login. For server deployments, see [deployment.md](./deployment.md); for
every setting, see [configuration.md](./configuration.md).

## 1. Start the gateway

```bash
abstractgateway serve
```

With no auth configured, `serve`:

- binds **127.0.0.1** (port 8080; use `--port` to pick another);
- turns on **user auth** and creates the admin user `default/admin`;
- keeps its data in the per-user data folder for your OS (see
  [Where the data lives](#where-the-data-lives));
- prints a short banner:

```text
Gateway data dir: /Users/you/Library/Application Support/AbstractGateway (os_default: ...)
Gateway auth: user auth enabled automatically (bound to loopback 127.0.0.1, no auth posture in this gateway's environment): ...
Gateway admin token file: .../auth/bootstrap-admin-token
First run: open http://127.0.0.1:8080/console#claim=agclaim_...
           (one-time link, valid 10 minutes, works from this machine only; ...)
```

Open the `First run` link in a browser on the same machine. The console signs
you in as the admin and opens the **first-run guide**.

The admin token is printed just above the link, and kept in
`<data dir>/auth/bootstrap-admin-token` (file mode `0600`). Start with
`abstractgateway serve --no-print-token` to keep it out of the output.

## 2. The first-run guide

The guide has five steps. Every step is optional:

| Step | What it shows |
|---|---|
| Welcome | This machine (memory, GPU), the data folder and why it was chosen, the sign-in mode, whether the gateway starts at login |
| Local engines | The engines found on this machine (Ollama, LM Studio, MLX, llama.cpp, ...), whether each is installed and running, and an **Install** button. The confirmation shows the exact command before anything runs |
| Default model | The text model currently configured, **Use recommended defaults** (the same action as the Multimodal tab's *Apply recommended*), a **Download** button for each recommended model that is missing, and the Models tab's catalog cards with **Fits this computer** on (one card per model, every 4-bit and 8-bit build, filters, **Open in the Models tab**). A downloaded text model's **Use as default** makes it the default text model |
| Apps | "Apps that work with this gateway": one card per browser app (Observer, Continuum, Code, Entity, Flow Editor), then the desktop **Assistant**: name, status, one line of description and one row of buttons at the same height on every card: **Install** when it is not installed (on Code it installs the terminal app too, when a ready-made download exists for this computer; nothing opens by itself), then **Open** (Open also starts a stopped app), plus **Open in Terminal** on Code when its terminal app is installed. The Assistant opens on the gateway's computer only. Stop, Show log, Update, versions and commands appear with **Technical details** (see [console.md](./console.md#apps-tab)) |
| Done | How to reopen the console, the login-service status, one line saying the console also exists as a terminal app, and (with **Technical details**) the CLI equivalents and the terminal console's install and open commands |

**What "recommended" means on this computer.** AbstractCore picks the
recommended text model; the gateway shows that pick and holds no list of its
own. On a Mac with Apple silicon the pick is an MLX build chosen by the
computer's memory:

| Memory | Recommended text model |
|---|---|
| less than 24 GB | `mlx-community/Qwen3.5-9B-MLX-4bit` (Qwen3.5 9B) |
| 24 GB up to, but not including, 128 GB | `mlx-community/Qwen3.8-27B-4bit` (Qwen3.8 27B) |
| 128 GB or more | `mlx-community/Qwen3.8-Flash-Next-4bit` (Qwen3.8 Flash-Next) |

LM Studio and Ollama builds stay in the catalog and can be downloaded, but on
a Mac they are not the recommendation. Other computers keep the LM Studio
build `qwen/qwen3.5-9b@4bit`. When AbstractCore's memory estimate says the
recommended model may not fit, the **Chat and text** card says so with the
estimate; the recommendation does not quietly switch to another model.

A typical path from a fresh install to a working local model:

1. **Local engines.** If no engine is installed, click **Install** on Ollama
   or LM Studio. The confirmation shows what will run and that it runs on
   this machine (on a Mac: download Ollama's signed app and place it in
   Applications; see [engines.md](./engines.md)). The install runs in the background with progress; when it
   finishes the row shows the version.
2. **Default model.** The catalog opens on models that fit this machine's
   memory, one card per model with its 4-bit and 8-bit builds. Click
   **Download** on the build you want; its progress bar shows on the row.
3. When the download finishes, the row reads *Downloaded*. Click **Use as
   default**: the gateway's text route now uses it.

The same steps are available later in the **Engines** and **Models** tabs (see
[console.md](./console.md#models-and-engines-tabs)) and from the command line
(`abstractgateway engines install ollama`, `abstractgateway models download
ollama qwen3:8b`). Installing an engine needs an admin and the
[`allow_engine_install`](./configuration.md#allow_engine_install) setting, which
is on by default for a gateway that listens on this machine only, and for
someone at the gateway machine whatever it listens on.

The guide opens by itself once per data folder. **Finish** or **Skip setup**
records that it ran (`POST /api/gateway/host/first-run`); clicking outside the
dialog closes it without recording anything. The **Setup** button (⚑, top
right, admins only) reopens it at any time.

## 3. Get a new sign-in link

A link works once and expires after 10 minutes. To get another one:

```bash
abstractgateway claim            # prints the link
abstractgateway claim --open     # prints it and opens your browser
abstractgateway-config claim-url # same command, from the config helper
```

The command finds the running gateway's port from the data folder (the gateway
writes `<data dir>/run/gateway-serve.json` while it runs). Use `--port` or
`--url` to target another gateway, `--data-dir` for another data folder, and
`--json` for machine-readable output. It exits with code `2` when the running
gateway does not use user auth (a static `ABSTRACTGATEWAY_AUTH_TOKEN`
deployment), because that gateway would refuse the link.

How the link is protected:

- only someone who can write the data folder can mint one (the CLI writes it
  under `<data dir>/auth/claims/`, stored as a SHA-256 digest only);
- `POST /api/gateway/session/claim` redeems it **only** from a loopback socket
  peer, and refuses any request carrying proxy headers (`X-Forwarded-For`,
  `X-Forwarded-Host`, `X-Real-IP`, `Forwarded`);
- the console removes the code from the address bar before sending it;
- the result is the same browser session as a normal sign-in (session cookie +
  CSRF cookie). The response's `claim.created_by` says who minted the link
  (`serve`, `cli` or `tray`), so the console can treat a tray sign-in
  differently from a first run.

## 4. Start the gateway at login (optional)

```bash
abstractgateway service install      # install and start
abstractgateway service status       # on | off | broken | other, and why
abstractgateway service enable       # start THIS gateway at the next login (starts nothing now)
abstractgateway service disable      # stop starting it at login (the running gateway keeps running)
abstractgateway service uninstall    # stop and remove (your data is kept)
```

The desktop tray's **Start AbstractGateway at login** item is the same switch
(`enable`/`disable`, shared module `abstractgateway.autostart`). `status`
reports `broken` when a registration exists that would not start — the
program it points at is gone (a moved or reinstalled gateway), the file is
unreadable, the unit is not enabled, or launchd / Task Manager / the desktop
switched it off — and `other` when it belongs to another data folder. It also
reports `broken` with **needs repair** when a registration pins
`--host/--port` on its command line, so the Network setting cannot apply:
"pinned to 127.0.0.1:N by the login item — run `abstractgateway service
enable` again to let the Network setting apply". `service enable` rewrites it.

| OS | What is installed | Logs |
|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/ai.abstractframework.gateway.plist` (`RunAtLoad`, restarted if it crashes), loaded with `launchctl bootstrap gui/<uid>` | `~/Library/Logs/AbstractGateway/` |
| Linux | systemd user unit `~/.config/systemd/user/abstractgateway.service` (`Restart=on-failure`), enabled with `systemctl --user enable --now` | `journalctl --user -u abstractgateway.service` |
| Linux without a systemd user manager | XDG autostart entry `~/.config/autostart/abstractgateway.desktop` (starts at graphical login) | `<data dir>/logs/gateway.log` |
| Windows (experimental) | per-user Run entry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\AbstractGateway` that starts the gateway with `pythonw.exe` (no console window, no admin) | `<data dir>\logs\gateway.log` |

Details:

- The service runs the absolute path of the `abstractgateway` you installed
  and sets `PATH` itself (including `~/.local/bin`, `~/.lmstudio/bin`,
  `/opt/homebrew/bin`, `/usr/local/bin`), because service managers do not read
  your shell profile.
- The login item runs plain `abstractgateway serve`: the host and port come
  from the [Network setting](./configuration.md#network-exposure-localhost--local-network--internet)
  (`abstractgateway network set localhost|lan|internet [--port N]`, the tray's
  Network menu, the console) at every start. `install`/`enable` store it
  first: a stored mode and port are kept; otherwise the mode is `localhost`
  (127.0.0.1) and the port is `--port` when given, else the running
  gateway's (`enable`), else a previous install's, else the first free port
  from 8080 upwards. `--host 127.0.0.1|0.0.0.0` and `--port` are written into
  that setting and printed. `--pin-command-line` puts `--host/--port` on the
  command line instead (for technical setups; the Network setting then does
  not apply). The registration is recorded in `<data dir>/service.json`.
- After starting, `install` waits up to 60 seconds for `/api/health`
  (`--wait-s`, `--no-wait`) and, on a first run, prints a sign-in link
  (`--no-claim` to skip).
- `--dry-run` prints the files and commands without changing anything.
  `--no-start` registers the service without starting it now.
- On Linux, a user unit runs while you are logged in. To keep it running after
  logout or start it at boot, run `loginctl enable-linger "$USER"` once.

## Where the data lives

When `ABSTRACTGATEWAY_DATA_DIR` is not set, the gateway uses, in order:

1. `./runtime`, if it already exists in the working directory (repository
   checkouts and the AbstractFramework workspace scripts);
2. the per-user data folder for your OS:
   - macOS: `~/Library/Application Support/AbstractGateway`
   - Linux: `$XDG_DATA_HOME/abstractgateway` (default `~/.local/share/abstractgateway`)
   - Windows: `%LOCALAPPDATA%\AbstractGateway`

`abstractgateway-config status` prints the folder and the reason it was
chosen. `serve --data-dir <dir>` or `ABSTRACTGATEWAY_DATA_DIR` choose it
explicitly.

## Exposing the gateway beyond this machine

Choose who can reach the gateway with the network setting:

```bash
abstractgateway network set lan                            # this machine + your local network
abstractgateway network set internet --acknowledge-internet
abstractgateway network restart                            # apply it now
```

The console's **Network** tab and the tray's **Network** menu change the same
setting. User accounts stay on in every mode; the gateway does not terminate
TLS, so put a reverse proxy or a tunnel in front for `internet`. See
[configuration.md](./configuration.md#network-exposure-localhost--local-network--internet)
and [security.md](./security.md#network-exposure).

An explicit `serve --host 0.0.0.0` (or any non-loopback address) refuses to
start unless auth is configured (`ABSTRACTGATEWAY_USER_AUTH=1` or
`ABSTRACTGATEWAY_AUTH_TOKEN`).

## Checking the setup from scripts

`abstractgateway-config status --json` includes these keys (schema
`gateway_config_status_v1`):

| Key | Meaning |
|---|---|
| `data_dir`, `data_dir_source`, `data_dir_reason` | The data folder, and `env`, `legacy_cwd_runtime` or `os_default` |
| `auth_mode`, `auth` | `users`, `token`, `users+token`, `open` or `loopback_auto` (nothing configured: `serve` on loopback enables user auth) |
| `service` | The login service: `installed`, `mechanism`, `unit_path`, `port`, `url` |
| `claim_pending`, `claims` | Whether an unused, unexpired sign-in link exists |
| `first_run` | Whether the first-run guide was completed |
| `serve` | The running gateway for this data folder (`url`, `port`, `pid`, `alive`, its `auth`), or `null` |

`GET /api/gateway/host/state` carries the same facts in its `gateway` block.

## Related docs

- [getting-started.md](./getting-started.md): runs, bundles, stores
- [configuration.md](./configuration.md): every environment variable and CLI flag
- [console.md](./console.md): the web console and the terminal console
- [security.md](./security.md): auth, origins, limits
- [tray.md](./tray.md): the desktop tray icon
- [troubleshooting.md](./troubleshooting.md): sign-in links, login service, network modes
