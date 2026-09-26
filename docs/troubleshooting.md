# AbstractGateway — Troubleshooting

Each entry starts from a symptom you can see, then gives the likely causes,
how to confirm them, the fix, and where the full explanation lives. For
conceptual questions, see [faq.md](./faq.md).

Two commands answer most questions:

```bash
abstractgateway-config status          # data dir, auth mode, login service, running gateway
curl -sS http://127.0.0.1:8080/api/health
```

## Starting and signing in

### `serve` says "Refusing to start: no sign-in would protect this gateway"

**Cause.** `--host` points beyond this computer (for example `0.0.0.0`) and
the gateway was started with neither user accounts nor a token.

**Fix.** Choose the exposure with the network setting instead of `--host`;
user accounts are turned on for you:

```bash
abstractgateway network set lan        # or: internet --acknowledge-internet
abstractgateway serve
```

Or keep the gateway on this computer: `abstractgateway serve` (loopback) or
`--host 127.0.0.1`. See
[configuration.md](./configuration.md#network-exposure-localhost--local-network--internet).

### `serve` refuses a weak token

**Cause.** A shared `ABSTRACTGATEWAY_AUTH_TOKEN` shorter than 15 characters
or easy to guess, on a non-loopback bind or with public wildcard origins.

**Fix.** Use a long random token, or use user accounts
(`ABSTRACTGATEWAY_USER_AUTH=1`). See [security.md](./security.md).

### The gateway does not start: "this gateway needs abstractruntime>=…"

The installed AbstractRuntime lacks live token deltas or the built-in tool
deny rules the gateway relies on; the message names what is missing. Upgrade
it in the gateway's Python, then start again:

```bash
pip install -U "abstractruntime>=0.5.0"
```

### The one-time sign-in link does not work

**Checks and fixes.**

- A link works **once** and for **10 minutes**. Mint a new one with
  `abstractgateway claim --open`.
- It only works from a browser **on the gateway machine** (a loopback
  connection without proxy headers). From another computer, sign in with a
  user id and token instead.
- `abstractgateway claim` exits with code `2` when the running gateway uses a
  shared token without user accounts: that gateway cannot redeem links. Sign
  in with the token, or start the gateway with user accounts.

See [first-run.md](./first-run.md#3-get-a-new-sign-in-link).

### I lost the admin token

It is kept in `<data dir>/auth/bootstrap-admin-token` (mode `0600`).
`abstractgateway-config status` prints the data dir. For a loopback gateway
you can also sign in with `abstractgateway claim --open` and rotate tokens
from the console's **Users & Entities** tab.

### `401`, `403`, `429` or `413` from `/api/gateway/*`

| Status | Likely cause | Fix |
|---|---|---|
| `401` | missing or invalid `Authorization: Bearer <token>` or session | sign in again; check the token file |
| `401` with `user_accounts_off_admin_only` | a non-admin account while user accounts are off | turn user accounts on, or sign in as an admin ([security.md](./security.md)) |
| `403` (origin not allowed) | the browser page's origin is not in the allowlist | add it with `abstractgateway network set --allowed-origins https://…` |
| `403` on an admin route | the signed-in principal is not an admin | use an admin account |
| `429` | repeated failed sign-ins from the same client address (lockout) | wait for the backoff; check `trust_proxy` behind a proxy |
| `413` | the body exceeds `ABSTRACTGATEWAY_MAX_BODY_BYTES` (or the attachment / bundle limit) | send less, or raise the limit ([security.md](./security.md#limits-abuse-resistance)) |

## Network access

### A change of network mode "needs a restart"

A listening socket cannot move. After `abstractgateway network set …`, the
status reads `restart_required: true` until the gateway restarts:
`abstractgateway network restart`, the tray's **Restart AbstractGateway…**, or
stop and start `serve`.

If `network status` says the restart **cannot** apply the setting, the
running gateway was started with `--host/--port` (they win over the
setting), or its login item pins them: run `abstractgateway service enable`
once, then restart.

### `lan` or `internet` is refused (HTTP 409)

The `reason_code` says why:

- `user_auth_required`: the gateway runs without user accounts (a shared
  token only, or `ABSTRACTGATEWAY_USER_AUTH=0`).
- `auth_disabled`: it was started with authentication or read protection off
  (`ABSTRACTGATEWAY_SECURITY=0`, `ABSTRACTGATEWAY_PROTECT_WRITE=0`,
  `ABSTRACTGATEWAY_PROTECT_READ=0`).
- `acknowledgement_required`: `internet` needs
  `--acknowledge-internet` (or the confirmation in the console or tray).

Start the gateway without those variables (a plain `abstractgateway serve`)
and set the mode again. See [security.md](./security.md#network-exposure).

### Another computer cannot open the console

- Check `abstractgateway network status`: the mode must be `lan` or
  `internet`, applied (no restart pending).
- Use an address from `abstractgateway network addresses`; the machine's
  firewall must allow the port.
- The console accepts the gateway's own LAN origins discovered at start. An
  address that appeared later (another Wi-Fi network) needs a restart.
- Behind a reverse proxy or tunnel, add its public origin with
  `abstractgateway network set --allowed-origins https://your.host`.

## Workflows and runs

### `GET /api/gateway/bundles` returns no bundles

- `ABSTRACTGATEWAY_FLOWS_DIR` points at an empty directory. Unset it to serve
  the shipped bundles ([shipped-workflows.md](./shipped-workflows.md)), or
  upload a bundle with `POST /api/gateway/bundles/upload`.
- A bundle can be present but not served: the `skipped` array names it with
  the reason (for example a `min_runtime` floor or a compile error).

### A run stays RUNNING and nothing happens

- `GET /api/health` reports the runner (`runner.runners[].status`). With
  `serve --no-runner`, start `abstractgateway runner` on the same data dir.
- `StartRunResponse.runner_warning` is set when no runner is ticking the data
  dir.
- The gateway may be **paused** (`"paused": true` on `/api/health`, a banner
  in the console): resume it from the tray, the console, or
  `POST /api/gateway/host/resume`.

### A run start answers 409 naming `agents.default_workflow.<interface>`

The run asked for the gateway default (`flow_id: "@default"`), and the saved
default for that interface cannot run: its workflow was removed or
deprecated, or no longer declares the interface. The message names the
setting and where its value comes from. Choose another workflow in the
console (Workflows → *Default agent workflow*), or run
`abstractgateway config unset agents.default_workflow.<interface>` to return
to the built-in default. A 400 means the request itself is wrong: `interface`
is missing, or `bundle_id`/`bundle_version` were sent with `@default`. See
[configuration.md](./configuration.md#default-agent-workflow).

### Replies arrive only at the end (no live text)

- The run did not ask for streaming: send `input_data._runtime.stream: true`,
  or turn on `agents.streaming_default` (it applies to interactive
  `POST /runs/start` only, never to schedules, bridges or the entity loop).
- The call could not stream: its `llm.delta_end` says `reason: "unavailable"`
  with a `detail` (for example `structured_output`, `node_stream_off`,
  `provider_cannot_stream`); the answer is complete either way.
- `GET /api/gateway/discovery/capabilities` must show
  `capabilities.streaming.deltas: true`.

See [api.md](./api.md#4b-live-replies-token-deltas-on-the-same-stream).

### The skills list is empty

`GET /api/gateway/skills` says why in `warnings` and which shelf it read in
`shelf_source`. A saved `skills.shelf` that does not exist or holds no
`skills/` folder is reported as unavailable; unset it to use the gateway's own
copy, or refresh that copy with *Refresh the curated shelf* (console, Apps) or
`POST /api/gateway/admin/skills/reseed`. See
[configuration.md](./configuration.md#skills-shelf).

### "LLM nodes but no default provider/model is configured"

Configure the text route, for example:

```bash
abstractgateway-config set-default input.text \
  --provider lmstudio --model qwen/qwen3.5-9b --base-url http://127.0.0.1:1234/v1
```

Or pick a default model in the console (**Multimodal**, or **Use as default**
on a downloaded model in **Models**). See
[configuration.md](./configuration.md#capability-defaults).

### A run fails because a model's weights are missing

The error names the capability route that selected the model. Download it
from the console's **Models** tab or with
`abstractgateway models download <provider> <artifact>`, or choose another
default. See [model-downloads.md](./model-downloads.md).

### "LLM/tool execution requires AbstractCore integration", "Visual Agent nodes require AbstractAgent", or `memory_kg_*` nodes ask for AbstractMemory

These packages are part of the base install. Check the environment the
gateway runs in:

```bash
pip show abstractgateway AbstractRuntime abstractcore abstractagent AbstractMemory
```

For KG memory, keep the default `lancedb` backend; `sqlite` works only when
the installed AbstractMemory exposes `SQLiteTripleStore`.

### `/voice/tts` or `/audio/transcribe` answer "capability unavailable"

Configure the voice routes (`output.voice`, `input.voice`) in the console's
**Multimodal** tab, or the Gateway-scoped voice variables for a remote
backend (`ABSTRACTGATEWAY_VOICE_TTS_ENGINE`,
`ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL`, …). Local voice engines need the
`apple` or `gpu` extra. See [configuration.md](./configuration.md).

### Catalog routes return only `gateway_static` defaults

The request reached a gateway without the capability packages you expected,
often another `abstractgateway serve` still running from another
environment on the same port. Stop it and start the one from your current
environment.

## Engines, models and apps

### Install buttons are disabled or answer `403`

Installs run on the gateway machine, so they follow the
[`allow_engine_install`](./configuration.md#allow_engine_install) setting: on
by default for a loopback gateway and for someone at the gateway machine;
off by default for a browser on another computer. An admin can turn it on.
Installs also require an admin account.

### An engine install stops in `needs_admin` or `needs_tools`

This is expected when a step needs an administrator password or the Apple
command-line tools. Use **Continue with administrator password** or
**Install tools** in the console, or
`abstractgateway engines continue <job-id>`. On a headless machine, run the
command the job shows and press **Re-check**. See [engines.md](./engines.md).

### A download says "Stalled" or ends "failed"

- `stalled`: no bytes for 15 seconds; the job keeps trying and resumes by
  itself.
- `failed`: `ended_reason` says what happened (a dropped connection, a Hub
  error, a full disk, a gateway restart) and what a new download reuses.
  Start the download again.
- A parent job id (`grp_…`) answers `404` after a gateway restart; its
  children stay readable with `GET /api/gateway/jobs`.

See [model-downloads.md](./model-downloads.md).

### An app does not install or start

| Reason in the card or API | Fix |
|---|---|
| `network_unavailable` | the npm registry (or PyPI, for Node.js) is unreachable; installed apps keep working offline |
| `no_free_port` | free a port in the app's usual range or set `apps.ports` |
| `crash_loop` | open **Show log** (Technical details) or `abstractgateway apps logs <app>` |
| `installs_not_allowed` | see "Install buttons are disabled" above |
| `app_loopback_only` | the app listens on `127.0.0.1`; open it from the gateway machine |
| `started_outside_gateway` | the app was started elsewhere (dev stack, `npx`); stop it there |

See [apps.md](./apps.md).

### The backlog folder is "not available on this gateway"

The saved backlog folder no longer exists (a deleted or unmounted checkout).
Choose **Use the gateway's own folder** in Continuum or the console, or run
`abstractgateway config set triage_repo_root /path/to/checkout`. See
[configuration.md](./configuration.md#backlog-folder-exec-runner-and-process-manager-continuum).

## Desktop

### There is no tray icon

`serve` prints `Desktop tray: started (pid …)` or the reason it did not:

| Reason | Fix |
|---|---|
| `missing_dependency` | `pip install "abstractgateway[tray]"` (Linux also needs the GTK/AppIndicator bindings) |
| `headless` | no display (SSH, container, service); expected |
| `dev_reload` | start without `--reload` |
| `runner_only` | the tray belongs to the process that serves the console |
| `no_tray_flag` | the gateway was started with `serve --no-tray`; start it without the flag |

GNOME needs the AppIndicator extension. If the helper started and then
disappeared, read `<data dir>/logs/tray.log`; `GET /api/gateway/host/tray`
reports its exit code, and `POST /api/gateway/host/tray/show` (admin) starts
it again. See [tray.md](./tray.md).

### The Assistant opened from the console is not signed in

- It was already running: a running Assistant receives no sign-in code. Quit
  it and open it again from the console or the tray.
- More than two minutes passed before it started, or the code was already
  used: open it again from the console.

See [apps.md](./apps.md).

### "Start at login" reads "needs repair" (`service status`: `broken`)

The registration points at a program that no longer exists (a moved or
reinstalled gateway), is unreadable or disabled, or pins `--host/--port` so
the Network setting cannot apply. Run:

```bash
abstractgateway service enable     # rewrite the registration for this gateway
abstractgateway service status
```

`other` means the login item belongs to another data folder. See
[first-run.md](./first-run.md#4-start-the-gateway-at-login-optional).

## Related docs

- [faq.md](./faq.md): conceptual questions and limits
- [first-run.md](./first-run.md), [getting-started.md](./getting-started.md)
- [configuration.md](./configuration.md), [security.md](./security.md)
