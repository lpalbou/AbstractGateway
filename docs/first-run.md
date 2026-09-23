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
Gateway auth: user auth enabled automatically (bound to loopback 127.0.0.1, no auth configured). ...
Gateway admin token file: .../auth/bootstrap-admin-token
First run: open http://127.0.0.1:8080/console#claim=agclaim_...
           (one-time link, valid 10 minutes, works from this machine only; ...)
```

Open the `First run` link in a browser on the same machine. The console signs
you in as the admin and opens the **first-run guide**.

The admin token is not printed. It is kept in
`<data dir>/auth/bootstrap-admin-token` (file mode `0600`). Set
`ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=1` if you want it printed.

## 2. The first-run guide

The guide has five steps. Every step is optional:

| Step | What it shows |
|---|---|
| Welcome | This machine (memory, GPU), the data folder and why it was chosen, the sign-in mode, whether the gateway starts at login |
| Local engines | Local model engines on this machine. Engine detection (`GET /api/gateway/engines`) comes with the next AbstractCore release; until then the step links to the Ollama and LM Studio downloads |
| Default model | The text model currently configured, **Use recommended defaults** (the same action as the Multimodal tab's *Apply recommended*), and a **Download** button for each recommended model that is not on this machine, with progress |
| Apps | The `npx @abstractframework/{flow,code,observer,continuum,entity}` commands with copy buttons |
| Done | How to reopen the console, the login-service status, and the CLI equivalents |

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
  CSRF cookie).

## 4. Start the gateway at login (optional)

```bash
abstractgateway service install      # install and start
abstractgateway service status       # installed? loaded?
abstractgateway service uninstall    # stop and remove (your data is kept)
```

| OS | What is installed | Logs |
|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/ai.abstractframework.gateway.plist` (`RunAtLoad`, restarted if it crashes), loaded with `launchctl bootstrap gui/<uid>` | `~/Library/Logs/AbstractGateway/` |
| Linux | systemd user unit `~/.config/systemd/user/abstractgateway.service` (`Restart=on-failure`), enabled with `systemctl --user enable --now` | `journalctl --user -u abstractgateway.service` |
| Windows (experimental) | Startup-folder shortcut `AbstractGateway.lnk` that starts the gateway with `pythonw.exe` (no console window) | `<data dir>\logs\gateway.log` |

Details:

- The service runs the absolute path of the `abstractgateway` you installed
  and sets `PATH` itself (including `~/.local/bin`, `~/.lmstudio/bin`,
  `/opt/homebrew/bin`, `/usr/local/bin`), because service managers do not read
  your shell profile.
- The port is `--port` when given, else the port of a previous install, else
  the first free port from 8080 upwards. The choice is stored in
  `<data dir>/service.json`.
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

The automatic setup applies to loopback binds only. `serve --host 0.0.0.0` (or
any non-loopback address) still refuses to start until you configure auth
explicitly (`ABSTRACTGATEWAY_USER_AUTH=1` or `ABSTRACTGATEWAY_AUTH_TOKEN`); see
[security.md](./security.md). When any auth setting is present, `serve` keeps
its `0.0.0.0` default bind.

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
