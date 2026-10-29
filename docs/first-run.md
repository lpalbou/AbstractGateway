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

## 1b. Set up your gateway: the admin password

A fresh gateway (a new data folder) has an admin account but nobody holding a
password for it. Open the console at the gateway's address, from this machine
or any address the gateway accepts (a LAN address, Tailscale, a reverse
proxy, Tailscale Funnel): instead of the sign-in form it shows **Set up your
gateway**:

| Field | What it is |
|---|---|
| Your name | The admin's display name in the console (default `admin`). You can sign in with it or with the account id `admin` |
| Admin password, Confirm | At least 10 characters with at least 5 different ones, not the account name and not a well-known password. A few words make a good one |
| Setup code | The six words printed when the gateway was installed (see below) |
| Remember this browser | Keeps the session for 30 days instead of until the browser closes |

**Set up and sign in** sets the password, signs this browser in as the admin
and opens the first-run guide (section 2). From then on the same address shows
the sign-in form, and the onboarding never comes back for this data folder.

**The setup code.** Until the admin password is set, whoever reaches the
console first could set it, so the form always asks for the six-word setup
code, from any browser. (The gateway cannot tell a browser on its own machine
from a proxy that relays strangers without saying so, such as a minimal nginx
`proxy_pass` or `tailscale funnel --tcp`.) Someone at the gateway's machine can
skip the form with the one-time link of section 3, which needs no code. Print
the code on the gateway's machine:

```bash
abstractgateway claim --code
moose bicycle honey camel ankle agent
```

`abstractgateway service install` and `abstractgateway service enable` print it
too, and so does the AbstractFramework installer's summary. Case and
separators do not matter (`Moose-Bicycle-...` works). The code stops working
once the password is set; `abstractgateway claim --code` then says the gateway
is already set up.

**What closes the onboarding.** Setting the admin password here, or ANY admin
sign-in (a one-time link from section 3, the admin token, a code sent by
email), an admin setting the admin's password from the Accounts page, or an
admin creating another account. When
it closes, unused one-time links are deleted. A data folder that already had
accounts when the gateway was upgraded to 0.18.0 is never "fresh": it keeps
the sign-in form.

**Limits.** 5 wrong setup codes from one address, or 20 from all addresses
together, lock the form for 15 minutes (the second lock never applies to a
browser on the gateway's own machine). Every claim is logged with the
client address (a warning log line, the audit log, and
`<data dir>/auth/onboarding.json`, mode `0600`).

## Signing in

The sign-in form asks for **Account** and **Password**. **Sign in with a
token instead** switches to the token (the admin token from section 1, or a
token an admin gave you); tokens stay for apps and automation. A password is
set:

- for the admin, by the onboarding above;
- for anyone, by an admin: **Accounts** > **Create user** (optional
  **Password** field), or the key button on an account's row (set, change or
  remove);
- by each person for themselves: the key button on their own row (it asks the
  current password once one is set).

Setting, changing or removing a password signs that account's other browsers
out. Ten wrong passwords from one address lock that address for 15 minutes;
five wrong passwords in a row for one account from one address lock that
address out of that account for 30 seconds, doubling up to 15 minutes; every
20 for one account from all addresses lock it for 15 minutes, doubling up to
24 hours, except for addresses that signed it in before. Every lock ends, and
the account's token keeps working. See
[security.md](./security.md#sign-in-passwords-tokens-and-the-first-run-window).

## 2. The first-run guide

The guide has six steps. Every step is optional:

| Step | What it shows |
|---|---|
| Welcome | This machine (memory, GPU), the data folder and why it was chosen, the sign-in mode, whether the gateway starts at login |
| Local engines | The engines found on this machine (Ollama, LM Studio, MLX, llama.cpp, ...), whether each is installed and running, and an **Install** button. The confirmation shows the exact command before anything runs |
| Cloud providers | "Paste a provider's API key to use its models; the key stays on this gateway." One card per cloud provider AbstractCore supports natively (today OpenAI, Anthropic, OpenRouter and Portkey, read from AbstractCore's registry: `GET /api/gateway/discovery/providers`, `local_provider: false`), then **OVH AI Endpoints** (an OpenAI-compatible service with its base URL preset) and **Other OpenAI-compatible endpoint** (Name, Base URL, API key: Mistral, Groq, Together, Fireworks, DeepSeek, xAI, Google… through their /v1 URLs; AbstractCore's `openai-compatible` provider). Each card has its mark, its connection state (key fingerprint only), an **API key** field and ONE action: **Connect** (**Reconnect** when it already has a connection). Connect checks the key first (`POST …/provider-endpoint-profiles/discover-models`, which stores nothing) and, only when it works, saves it as a NEW provider connection (`openai-key`, `openai-key-2`, …, `create_only`), never replacing one you have: the line reads "Connected · 48 models", or the reason (the key was refused, the provider is limiting requests, it could not be reached) with nothing stored. The key is never shown again. The same cards are the Providers tab's **Cloud providers** section. A step of its own rather than part of Local engines: the engines step is about software on this computer, and a key saved here is offered by the next step |
| Default models | Two views, **Simple** first. **Simple**: one **Use recommended** button (AbstractCore's recommended models for this computer, where you have not chosen your own; the missing ones download in the background with a progress row per model), then the capabilities as coloured badges in two groups, **Input** (what it understands: Text, Image, Voice, Video, Files) and **Output** (what it makes: Text, Image, Video, Voice, Music, 3D, Embeddings, Rankers). Each badge says its state in readable names, never a model id: *Not set* (with the recommended model's name when there is one), *Recommended* (the pick is in use), *Your choice*, or *Covered* (read by the text model). A badge opens a guided choice: the model recommended for this computer first (**Use it**, **Use it and download**), then **Other models that fit** (the catalog filtered to that capability, fits-this-computer on) and **All engines and models…** (the Multimodal route editor), and for text **Or use an API key** (any cloud provider of the Cloud providers step, with the same check: the key is checked first, then kept as a NEW provider connection, never replacing one you have). **Advanced**: one table row per capability with the engine and model ids, **Use recommended**, **Download** and **Choose…** per row, and **Use recommended for all**; below it the recommended downloads (**Download all**), a card per capability this computer cannot run, and the catalog cards. Setting a model clears the route's previous server address and reasoning effort (they belonged to the previous model). Admins only, like the whole guide |
| Apps | Shown once a default text model exists (until then the step says **Choose a default text model first** with a button back to the model step). "Apps that work with this gateway": one card per browser app (Observer, Continuum, Code, Entity, Flow Editor), then the desktop **Assistant**: name, status, one line of description and one row of buttons at the same height on every card: **Install** when it is not installed (on Code it installs the terminal app too, when a ready-made download exists for this computer; nothing opens by itself), then **Open** (Open also starts a stopped app), plus **Open in Terminal** on Code when its terminal app is installed. The Assistant opens on the gateway's computer only. Stop, Show log, Update, versions and commands appear with **Technical details** (see [console.md](./console.md#apps-tab)) |
| Done | How to reopen the console, the login-service status, one line saying the console also exists as a terminal app, and (with **Technical details**) the CLI equivalents and the terminal console's install and open commands |

**What "recommended" means on this computer.** AbstractCore picks the
recommended model for each capability (`recommended_models(host)`, see
AbstractCore's `docs/recommended-models.md`); the gateway shows those picks and
holds no list of its own.

The rule per memory band. "Memory" is the GPU's memory on an NVIDIA computer
and the unified memory on a Mac. The **Today** column is what AbstractCore
answers now; the **Target** column is the operator's table (2026-10-28), which
AbstractCore adopts once each pick is measured on real hardware (root backlog
1072); until then the guide shows the **Today** pick.

| Capability | Band | Today (AbstractCore) | Target |
|---|---|---|---|
| Text | Mac, below 24 GB | Qwen3.5 9B, MLX 4-bit | the 8–14B class, or a lower quant of Qwen3.8 27B that fits whole |
| Text | Mac, 24 to 128 GB | Qwen3.8 27B, MLX 4-bit (128 GB and up: Qwen3.8 Flash-Next) | Qwen3.8 27B 4-bit on MLX (24–64 GB) |
| Text | NVIDIA, 24 to 64 GB | LM Studio `qwen/qwen3.5-9b@q4_k_m` | Qwen3.8 27B 4-bit: `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` (16.5 GB) on llama.cpp |
| Text | NVIDIA, 16 GB | LM Studio `qwen/qwen3.5-9b@q4_k_m` | Qwen3.8 27B `UD-Q3_K_XL` (13.1 GB, fits whole), or the 8–14B class |
| Image (text to image, image to image) | 12 GB and up | FLUX.2 klein 4B (MLX-Gen 8-bit on a Mac, Diffusers on NVIDIA) | FLUX.2 klein 4B |
| Voice out | every computer | Supertonic 3 | Supertonic 3; Qwen3-TTS when 8 GB are free and voice cloning is wanted |
| Voice in | every computer | Whisper large-v3 (faster-whisper; an Apple silicon Mac with MLX: MLX Whisper) | Whisper large-v3-turbo from 6 GB, else Whisper small |
| Video | Mac, where it fits (not on small-memory Macs) | Wan2.2 TI2V 5B, MLX-Gen 8-bit | Wan 2.2 TI2V 5B from 16 GB, 14B from 32 GB, MiniMax H3 from 64 GB |
| Video | NVIDIA | none | as above, on Diffusers (`Wan-AI/Wan2.2-TI2V-5B-Diffusers`, `Wan-AI/Wan2.2-T2V-A14B-Diffusers`) |
| Music | every computer | none in the guide: AbstractCore's music recommendation is opt-in (ACE-Step 1.5 XL turbo, set it with Choose…) | (no target given) |
| 3D, Embeddings, Rankers | every computer | none: Choose… | (no target given) |

The Apple text tiers in detail: on a Mac with Apple silicon the pick is an MLX build chosen by the
computer's memory:

| Memory | Recommended text model |
|---|---|
| less than 24 GB | `mlx-community/Qwen3.5-9B-MLX-4bit` (Qwen3.5 9B) |
| 24 GB up to, but not including, 128 GB | `mlx-community/Qwen3.8-27B-4bit` (Qwen3.8 27B) |
| 128 GB or more | `mlx-community/Qwen3.8-Flash-Next-4bit` (Qwen3.8 Flash-Next) |

LM Studio and Ollama builds stay in the catalog and can be downloaded, but on
a Mac they are not the recommendation. Other computers get the LM Studio
build `qwen/qwen3.5-9b@q4_k_m`. On a computer with an NVIDIA GPU, the
recommended image model is FLUX.2 [klein] 4B through Diffusers
(`black-forest-labs/FLUX.2-klein-4B`; it fits a 16 GB card, and an 8 GB card
is reported as not available); video generation is not recommended there. When AbstractCore's memory estimate says the
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
   default**: the gateway's text route uses it from then on.

The same steps are available later in the **Providers** (Local providers) and **Models** tabs (see
[console.md](./console.md#models-and-local-providers)) and from the command line
(`abstractgateway engines install ollama`, `abstractgateway models download
ollama qwen3:8b`). Installing an engine needs an admin and the
[`allow_engine_install`](./configuration.md#allow_engine_install) setting, which
is on by default for a gateway that listens on this machine only, and for
someone at the gateway machine whatever it listens on.

The guide opens by itself once per data folder. **Finish** or **Skip setup**
records that it ran (`POST /api/gateway/host/first-run`); clicking outside the
dialog closes it without recording anything. **Setup**, at the bottom of the
sidebar (admins only), reopens it at any time and keeps your current choices
unless you replace them.

### On a headless server

On a machine without a browser (for example a Linux server you reach over SSH),
run the same guide in the terminal console on that machine:

```bash
abstractgateway console          # finds the console, the gateway and your admin token
```

The installer builds the console next to `abstractgateway`; without it,
`cargo install abstractgateway-console`, then
`abstractgateway-console --url http://127.0.0.1:8080 --token <token>` where
`<token>` is the admin token `abstractgateway serve` prints when it starts.

It opens the setup guide for an admin whose first run is not completed, with
the same steps through the same gateway routes, and records **Finish** or
**Skip setup** on the gateway. See
[console.md](./console.md#terminal-console-abstractgateway-console).

## 3. Get a new sign-in link

A link works once and expires after 10 minutes. To get another one:

```bash
abstractgateway claim            # prints the link
abstractgateway claim --open     # prints it and opens your browser
abstractgateway-config claim-url # same command, from the config helper
```

The command finds the gateway from the data folder: the running gateway's
address (the gateway writes `<data dir>/run/gateway-serve.json` while it runs),
else the installed login service's, else the port saved in the Network setting,
else `http://127.0.0.1:8080`. `abstractgateway models loaded|load|unload` find
it the same way. Use `--port` or
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

`service uninstall` returns only once the gateway and every process it started have exited: the entity
loops, model downloads, managed processes, apps and the tray. Some of these run in their own session, so the
service manager would not stop them. The command waits up to 20 s for them, then stops what is left
(SIGTERM, then SIGKILL), and lists what it stopped. If a process survives, the command fails and names it.
On Linux without a systemd user manager (XDG autostart), it stops the gateway itself.

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
- On Linux, a user unit runs while you are logged in. When lingering is off
  (`loginctl show-user "$USER" -p Linger` answers `Linger=no`, the default),
  `service install`, `service enable` and `service status` say so in one line:
  "On a server, this service stops when you log out and does not start at boot
  until you run once: sudo loginctl enable-linger <user>". The consoles' Start at
  login switch shows the same sentence. The gateway never runs it for you (it
  needs an administrator); `install.sh --server` does.

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

## Several gateways on one machine (instances)

A second gateway for the same user on the same machine is a named INSTANCE. One name drives
everything, and nothing is picked by hand:

```bash
abstractgateway --instance fresh service install --port 8443   # or: install.sh --server --instance fresh --port 8443
abstractgateway --instance fresh claim --code                  # its setup code
abstractgateway --instance fresh service status
```

| | Default instance | Instance `fresh` |
|---|---|---|
| Data folder | `~/.local/share/abstractgateway` (macOS: `~/Library/Application Support/AbstractGateway`) | the same with `-fresh` |
| AbstractCore config (model routes, provider connections) | AbstractCore's default | `<data folder>/core-config` |
| Gateway port (unless `--port` or the Network setting) | 8080 | 8090 + its slot |
| App ports (Code, Observer, ...) | 3001-3006, then 3100-3199 | 3001+200×slot..., then 3100+200×slot..3199+200×slot |
| Login service | `abstractgateway.service`, `ai.abstractframework.gateway` | `abstractgateway-fresh.service`, `ai.abstractframework.gateway.fresh` |
| Console | "AbstractGateway Console" | "AbstractGateway Console · fresh", and the name in the sidebar |

The SLOT is assigned once per instance (the smallest free one, never two instances on one slot) and
recorded in `~/.config/abstractgateway/instances.json`, so the app port blocks of two instances never
collide. `ABSTRACTGATEWAY_INSTANCE=fresh` is the same as `--instance fresh` (the login service runs
`abstractgateway --instance fresh serve`). A gateway lists and opens only its own apps: a running app
another instance serves is never shown as this one's. `GET /api/gateway/about` carries `instance`.

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
