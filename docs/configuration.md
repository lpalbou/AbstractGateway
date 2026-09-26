# AbstractGateway — Configuration

AbstractGateway is configured in three places:

- **Runtime settings**, stored in the data dir and changed from the web
  console, the terminal console, the tray or the CLI (`abstractgateway
  network`, `abstractgateway apps config`, `abstractgateway config
  get|set|unset`): network exposure, reverse proxy, browser apps, engine
  installs, backlog folder, exec runner, process manager, stop kill switch.
- **AbstractCore configuration** for capability defaults, provider keys and
  other values AbstractCore owns (see [Two entry points, one store](#two-entry-points-one-store)).
- **Launch flags and environment variables** for deployment choices such as
  the data dir, auth mode, stores and limits.

This page is the reference for all three.

## Install extras (recommended)

The base install (`pip install abstractgateway`) is the remote-light server
profile: HTTP/SSE, durable stores, `AbstractRuntime`,
Runtime-owned provider/tool and multimodal support, AbstractAgent, AbstractFlow
compatibility (runs bundles produced by AbstractFlow; does not require the
`abstractflow` package), and AbstractMemory/LanceDB KG support. Local
sentence-transformer embeddings and hardware-local inference engines are
opt-in, so the base Linux install does not pull PyTorch/CUDA packages.

Remote embeddings are part of this base light profile. Configure
`embedding.text` for OpenAI, OpenRouter, Portkey, LM Studio, vLLM, another
OpenAI-compatible embeddings endpoint, or a remote AbstractCore server. The
`abstractgateway[embeddings]` extra is only for local HuggingFace/
sentence-transformer embeddings on the Gateway host.

Optional extras (see `pyproject.toml`):
- `abstractgateway[embeddings]`: local sentence-transformer embeddings for semantic KG queries
- `abstractgateway[apple]`: full native macOS Python profile with Apple-local engines and all non-NVIDIA framework capabilities; this is for native macOS, not Docker
- `abstractgateway[gpu]`: full native/container GPU profile with local GPU engines and all relevant framework capabilities; the NVIDIA Docker image uses this profile
- `abstractgateway[tray]`: the desktop menu bar / system tray icon shown by `abstractgateway serve` (pystray + Pillow; see [tray.md](./tray.md))
- `abstractgateway[docs]`: MkDocs site tooling
- `abstractgateway[dev]`: local dev/test deps

Default dependency floors (see `pyproject.toml`):
- `AbstractRuntime>=0.4.36`
- `abstractcore>=2.15.3`
- `abstractagent>=0.3.13`
- `AbstractMemory[lancedb]>=0.3.0`

Gateway's KG resolver targets AbstractMemory's TripleStore API. It does not use
the newer memory-agent API directly.

## Configuration helper

Gateway has a first-class configuration helper:

```bash
abstractgateway-config status
abstractgateway-config init --env-file .env
abstractgateway-config bootstrap-admin --print-token
abstractgateway-config claim-url [--open]
abstractgateway config status --json
```

`claim-url` (also `abstractgateway claim`) prints a one-time console sign-in
link for this machine; see [first-run.md](./first-run.md). `status --json`
also reports `data_dir_source`, `auth_mode`, `service`, `claim_pending`,
`first_run` and `serve` (schema `gateway_config_status_v1`, documented in
[first-run.md](./first-run.md#checking-the-setup-from-scripts)).

It reports Gateway auth/data/store/runtime defaults, Core-server handoff
configuration, memory-store selection, and package readiness. `init` writes a
private env file for server/operator deployments. Gateway Console (`/console`)
is the preferred place to configure provider connections, provider API keys,
endpoint base URLs, users, and Gateway/user defaults. Provider URLs and keys
belong to the Providers tab; the Multimodal Capabilities tab only chooses an
available provider and a discovered model.
`bootstrap-admin` is the non-interactive setup path used by Docker images:
when user auth is enabled, it ensures `default/admin` exists, stores only the
token hash in `auth/users.json`, and can write the raw bootstrap token to
`auth/bootstrap-admin-token` for first login.

## Core environment variables

### Paths + workflow source

- `ABSTRACTGATEWAY_DATA_DIR`: durable data directory. When unset: `./runtime`
  if it already exists in the working directory, else the per-user data folder
  (macOS `~/Library/Application Support/AbstractGateway`, Linux
  `$XDG_DATA_HOME/abstractgateway` or `~/.local/share/abstractgateway`,
  Windows `%LOCALAPPDATA%\AbstractGateway`). `serve --data-dir` sets it for
  one process. `abstractgateway-config status` prints the folder and why it
  was chosen.
  Evidence: `src/abstractgateway/host_paths.py`
- `ABSTRACTGATEWAY_FLOWS_DIR`: workflows directory. When unset, Gateway uses the
  packaged shipped bundle directory, which carries `basic-agent`,
  `coding-agent`, `deep-research`, `co-scientist`, and more
  ([shipped-workflows.md](./shipped-workflows.md)). If the shipped bundles are
  unavailable, Gateway fails clearly instead of starting with an empty default
  registry. Setting this replaces the shipped registry with your own directory.
  Evidence: `src/abstractgateway/config.py`
- `ABSTRACTGATEWAY_WORKFLOW_SOURCE`: `bundle` (the default and only
  supported source)  
  Evidence: `src/abstractgateway/service.py` (`create_default_gateway_service`)

### Authentication and user routing

**Default on loopback.** When no auth setting is present (none of
`ABSTRACTGATEWAY_AUTH_TOKEN[S]`, `ABSTRACTGATEWAY_USER_AUTH`,
`ABSTRACTGATEWAY_MULTI_USER`, `ABSTRACTGATEWAY_AUTH_MODE`,
`ABSTRACTGATEWAY_SECURITY`, `ABSTRACTGATEWAY_PROTECT_WRITE`), `serve` binds
`127.0.0.1` and enables user auth automatically. A non-loopback bind in that
state refuses to start. When any auth setting is present, `serve` keeps the
`0.0.0.0` default bind and your settings apply unchanged.

The normal browser-console/browser-app path uses Gateway user auth:

- `ABSTRACTGATEWAY_USER_AUTH=1` or `ABSTRACTGATEWAY_AUTH_MODE=users`: enable
  file-backed user principals and per-principal runtime routing
- `abstractgateway serve`: when user auth is enabled, ensures `default/admin`
  exists and writes the first-login token to
  `<ABSTRACTGATEWAY_DATA_DIR>/auth/bootstrap-admin-token` (mode `0600`). The
  token is printed on a loopback bind and hidden on other binds;
  `serve --print-token` / `--no-print-token` override that
  (`ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=1` is accepted as an alias of
  `--print-token`). Until
  the first-run guide is completed, a one-time sign-in link
  (`/console#claim=...`, 10 minutes, single use, loopback only) is printed
  instead
- `POST /api/gateway/session/claim`: redeems a one-time link code for an admin
  browser session; accepted only from a loopback peer without proxy headers.
  The response carries `claimed: true`, `first_run` (the guide state) and
  `claim: {created_by}`, which says who minted the link: `serve` (first run),
  `cli` (`claim-url` / `abstractgateway claim`) or `tray` (tray sign-in).
  `GET /api/gateway/host/first-run` / `POST` (admin) read and record the
  first-run guide state

Server/operator token mode uses a shared Gateway bearer token:

- `ABSTRACTGATEWAY_AUTH_TOKEN`: single Gateway admin token
- `ABSTRACTGATEWAY_AUTH_TOKENS`: comma-separated Gateway admin tokens

That shared bearer token maps to `local-admin` and is not accepted by browser
sign-in flows such as `/console` or AbstractFlow. User-auth mode resolves
Gateway user bearer tokens to principals and routes each principal to a separate
service/data plane:

- `ABSTRACTGATEWAY_USER_AUTH_AUTO=1`: compatibility mode that also enables
  user auth when the registry file already exists
- `ABSTRACTGATEWAY_USERS_FILE`: optional user registry path; default:
  `<ABSTRACTGATEWAY_DATA_DIR>/auth/users.json`
- `ABSTRACTGATEWAY_SESSIONS_FILE`: optional browser session registry path;
  default: `<ABSTRACTGATEWAY_DATA_DIR>/auth/sessions.json`
- `ABSTRACTGATEWAY_SESSION_TTL_S`: default browser session lifetime
- `ABSTRACTGATEWAY_REMEMBER_SESSION_TTL_S`: browser session lifetime when a
  browser app requests "remember me"
- `ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME`: keep the default
  `default/admin` admin principal on the Gateway's base data plane when its
  `runtime_id` is `default` or `admin` (default: enabled)
- `GET /api/gateway/me`: returns the resolved principal and routing mode
- `/api/gateway/admin/users`: admin-only user list/create/read/update/delete
- `/api/gateway/admin/runtime-reservations`: admin-only retained runtime
  list/transfer/purge lifecycle
- `/console`: built-in same-origin Gateway Console for session sign-in with
  Gateway user + token, account/runtime summary, admin user management, optional
  account email metadata, token rotation, retained runtime transfer/purge, and
  multimodal capability defaults selected from available providers

User records include `tenant_id`, `user_id`, roles/scopes, enabled state, and a
`runtime_id`. The registry stores password-grade bearer-token hashes only.
Generated or rotated user tokens are returned once from the admin response.
Gateway rejects duplicate `runtime_id` values within the same tenant when users
are created or updated, preserving `1 user = 1 runtime` for independent hosted
users. Deleting a user reserves its retained runtime id. Admins must explicitly
purge retained runtime data before the id can be reused by another user, or
transfer the retained runtime to an existing same-tenant user.

When user auth is active, `src/abstractgateway/service.py` keeps normal users
isolated in a per-principal service directory:

```text
<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/runtime
<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/flows
```

The bootstrap `default/admin` admin principal is a local-setup compatibility
exception by default: with `ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME=1`, it
uses the base Gateway data plane and bundle registry. That keeps the admin
connected to the default runtime and shipped `basic-agent` bundle while regular
users remain on `1 user = 1 runtime` routing.

Browser apps should exchange a Gateway user token for an opaque Gateway browser
session through `/api/gateway/session/login`; the raw bearer token should not be
kept in browser storage, and the login response body does not expose the session
id or CSRF token. Session-authenticated writes carry
`X-AbstractGateway-Session` plus `X-AbstractGateway-CSRF`, and
`/api/gateway/session/logout` revokes the session. Apps such as AbstractFlow,
AbstractCode, AbstractAssistant, and AbstractObserver should authenticate as the
current user/session in hosted mode. They should not share one app-server
Gateway token for all users.

<a id="network-exposure-localhost--local-network--internet"></a>
## Network exposure (localhost / local network / internet)

One setting decides who can reach the gateway. The console, the console TUI
(Connection screen), the tray and `abstractgateway network` all edit the same
runtime-config key (`network`); there is no environment variable for it.

| Mode | Bind | Requires |
|---|---|---|
| `localhost` ("Localhost only") | `127.0.0.1` | nothing: only this machine can connect |
| `lan` ("Local network") | `0.0.0.0` (IPv4) | user auth (accounts + console sign-in) |
| `internet` ("Internet…") | `0.0.0.0` (IPv4) | user auth **and** an explicit acknowledgement |

- **Applied at the next start.** A listening socket cannot move: after a change
  the status says `restart_required: true` with `configured` vs `effective`
  until the gateway restarts (`POST /api/gateway/network/restart`, the tray's
  *Restart AbstractGateway…*, `abstractgateway network restart`, or stop and
  start `serve`).
- **`serve --host/--port` win** over the setting and are reported as
  `effective.overridden_by_cli: true`. A restart replays the same command
  line, so it cannot apply the setting: the status says so
  (`restart.applies: false` + `restart.reason`) and the restart route refuses.
- **The login service lets the setting apply.** The LaunchAgent,
  systemd unit, XDG entry and Windows Run entry written by
  `abstractgateway service install|enable` start plain `serve`: no `--host`, no
  `--port`. Install/enable first **seed** the setting through the same change
  door as `network set` (same auth refusals; a refusal registers nothing):
  nothing stored yet → `localhost` on the chosen port; a stored mode/port →
  kept. `service install|enable --host H
  --port P` are written **into the setting** (`127.0.0.1` → `localhost`,
  `0.0.0.0` → `lan`, or the stored `internet`; a specific address is refused),
  never onto the command line. `--pin-command-line` is the technical escape
  hatch: `serve --host H --port P` on the command line, the setting
  untouched and overridden (`overridden_by_cli`, `service status` names it).
- **A registration that pins `--host/--port` reads "needs repair".** `service
  status` (and the tray's *Start at login — needs repair*) says
  "pinned to 127.0.0.1:N by the login item — run `abstractgateway service enable`
  again to let the Network setting apply". `service enable` (or the tray click)
  rewrites the registration in place, keeping a stored mode. The gateway
  running at that moment keeps its command line until it restarts:
  `abstractgateway service install` restarts it from the new registration, or
  log out and back in.
- **Auth is checked before anything is stored.** `lan`/`internet` are refused
  (HTTP 409, nothing written, `refused_reason` + `fix`) when the gateway was
  started with authentication switched off (`ABSTRACTGATEWAY_SECURITY=0` /
  `ABSTRACTGATEWAY_PROTECT_WRITE=0` in its launch environment), with read
  protection off (`ABSTRACTGATEWAY_PROTECT_READ=0`: unauthenticated reads would
  be answered as the admin, `reason_code: auth_disabled`), or with a posture
  without accounts (a shared token only, or `ABSTRACTGATEWAY_USER_AUTH=0`).
  The `fix` describes that state; a plain start (`abstractgateway serve`, or
  the login item `abstractgateway service enable` registers) has none of
  them: when no auth posture is configured at all (the first-run default),
  `serve` turns user auth on for the network mode and says so on stderr
  (`auth.source: network_setting`).
- **`internet` needs `acknowledge_internet: true`** (CLI
  `--acknowledge-internet`; the TUI and tray ask with a confirm). The gateway
  does not terminate TLS: put a TLS reverse proxy or a tunnel in front
  (Caddy, nginx, Cloudflare Tunnel, Tailscale Funnel, ngrok). Port forwarding
  and firewalls are yours to configure; the gateway changes neither.
- **Browser origins.** In a network mode from the setting, `serve` also allows
  the gateway's own discovered LAN origins (e.g. `http://192.168.1.23:8080`,
  `http://mymac.local:8080`) next to the loopback defaults, so the console can
  sign in from another machine. An address that appears later (new Wi-Fi)
  needs a restart. Your public origin (behind a proxy or tunnel) is a setting:
  see [Reverse proxy](#reverse-proxy-allowed-origins-and-trust-proxy) below.
- **A setting that cannot apply falls back loudly.** If the stored mode's auth
  requirement stops being met (the environment changed), `serve` binds
  `127.0.0.1`, prints `[ERROR] Network exposure 'lan' cannot be applied: … Fix: …`
  and the status carries `effective.blocked_reason`.

### Reverse proxy: allowed origins and trust proxy

Two settings a deployment behind a reverse proxy or a tunnel needs, stored in
the same `network` setting and changed through the same door
(`POST /api/gateway/network`, admin-only, audit-logged). **Both apply to the
next request: no restart.** The security middleware re-reads them per request
(one `stat()` of the settings file; parsed again only when it changed), so a
change from the console, the TUI or the CLI (another process) is live at once.

| Setting | Meaning | Default |
|---|---|---|
| `allowed_origins` | Browser origins whose pages may call the gateway, **added** to the always-allowed `http://localhost:*`, `http://127.0.0.1:*` (and, in a network mode, the gateway's own LAN origins). | none |
| `trust_proxy` | Take the client address from `X-Forwarded-For` (sign-in lockouts, audit log). Only when your own proxy sits in front of every request: otherwise any client chooses the address the gateway sees. | off |

**Validation** (one place, the gateway; every door shows its sentence
verbatim). An origin is `scheme://host[:port]`: `http` or `https`, no path, no
trailing slash, no query, no user info; IPv6 in brackets. It is stored the way
a browser sends it: scheme and host lowercased, the default port dropped
(`https://Gateway.Example.com:443` → `https://gateway.example.com`). `*` (every
origin), a leading `*.` label and a `:*` port are accepted only as typed and
are flagged with a warning. A list with any invalid entry is refused whole
(HTTP 400 `reason_code: invalid_origins`, `errors[{value, error}]`, nothing
written), e.g. `1 origin is not valid (nothing was saved): https://x.example/: no
trailing slash: an origin is scheme://host[:port] (write https://x.example)`.
An empty list clears the setting back to the default.

**The environment variables.** The two settings do not treat the launch
environment the same way:

- **Browser origins.** `ABSTRACTGATEWAY_ALLOWED_ORIGINS` in the environment a
  gateway was started with still decides (a deployment pin, the security
  carve-out in `env_registry.py`), and every surface says so: the payload
  carries `source: "env"` and `overridden_by_env: true` with
  `env_name`/`env_value` and a `note` ("This gateway was started with … in its
  environment: …"); saving is still allowed and answers
  `changed.allowed_origins.applies: "overridden_by_env"` ("Saved, but not in
  effect"). The saved list applies once the gateway starts without the
  variable. The origins `serve` itself exports for a network mode are never
  counted as an override.
- **Trust proxy.** The SAVED setting decides; `ABSTRACTGATEWAY_TRUST_PROXY` is
  only a fallback used when nothing is saved (`source: "env"`,
  `overridden_by_env: true` then). Once the switch is saved, the variable no
  longer applies to that data folder: the saved value applies to the
  next request whatever the environment says (`source: "setting"`, with a
  `note` that the variable is also set and the saved switch wins). The same
  rule is used for sign-in lockouts and the audit log's client address and for
  deciding whether a caller sits at the gateway computer. Once saved, change
  it with the switch (`abstractgateway network set --trust-proxy on|off`), not
  with the variable.

Status payload (`GET /api/gateway/network`, `reverse_proxy`):

```json
"reverse_proxy": {
  "allowed_origins": {"value": ["https://gateway.example.com"], "source": "setting", "overridden_by_env": false,
                      "effective": ["http://localhost:*", "http://127.0.0.1:*", "https://gateway.example.com"],
                      "builtin": ["http://localhost:*", "http://127.0.0.1:*"], "self_origins": [],
                      "applies": "live", "warnings": []},
  "trust_proxy": {"value": true, "source": "setting", "overridden_by_env": false, "effective": true,
                  "applies": "live", "warning": "Trust proxy is on: …"}
}
```

`source` is `setting` (stored), `env` (the start-time override) or `default`.
From the CLI (another process) the running gateway's environment is read from
its run record (`<data>/run/gateway-network.json`, `proxy_env`), never from the
CLI's own shell.

**Three ways, same semantics** (a headless server over SSH needs only the
last two):

| | Web console | Console TUI | CLI |
|---|---|---|---|
| Where | Network → *Advanced: reverse proxy* | Connection screen, below the addresses | `abstractgateway network …` |
| Add/replace origins | type an origin, *Add origin* (Enter); × on a chip removes it | *browser origins* line: comma-separated list, Enter saves, empty clears | `set --allowed-origins https://a,https://b` (`""` clears) |
| Trust proxy | *Trust the proxy's client address* switch | checkbox (Space) | `set --trust-proxy on\|off` |
| See values + source | pills: *Saved setting* / *Default* / *Set by the environment* | `[saved setting]` / `[default]` / `[environment override]` + the override line | `network show` (`--json` = the payload) |
| Refusal | the gateway's sentence under the input | notice `✗ reverse proxy refused: <sentence>` | `refused: <sentence>` on stderr, exit 1 |

The console and the TUI send `{allowed_origins}` / `{trust_proxy}` to
`POST /api/gateway/network`; the CLI writes the same store through the same
function (`network_exposure.apply_network_change`). The mode is untouched by a
reverse-proxy-only change (`mode` is optional).

### Addresses

`GET /api/gateway/network` lists every address a client can use, discovered on
each call: loopback; each up interface's IPv4/IPv6 (loopback, link-local and
down interfaces skipped; macOS names from `networksetup`, e.g. "Wi-Fi"; VPN
`utun`/CGNAT addresses labelled "VPN"); the Bonjour name `<LocalHostName>.local`
when it resolves. Discovery uses `psutil` when importable, else `ifconfig -a`
(macOS/BSD) or `ip -o addr show up` (Linux), else the hostname's own
resolution. Each row says whether the gateway listens there now
(`reachable`). The WAN address (`kind: public`) is looked up only on request
(`?lookup_public=1`, admin, `internet` mode only; one HTTPS GET to
`api.ipify.org`), never on a poll. `copy_hint` is the URL to copy first (the
LAN IPv4 when listening on the network, else loopback).

### API (`gateway_network_v1`)

- `GET /api/gateway/network[?lookup_public=1]`: any authenticated principal
  (`writable` says whether the caller may change it).
- `POST /api/gateway/network {mode?, port?, acknowledge_internet?, allowed_origins?, trust_proxy?}`:
  admin, any subset (at least one). 200
  `{ok, configured, effective, restart_required, restart, auth, reverse_proxy, changed, warnings, copy_hint}`
  where `changed{field: {from, to, applies: live|restart|overridden_by_env}}`;
  409 `{ok:false, reason_code: user_auth_required|auth_disabled|acknowledgement_required, refused_reason, fix?, warnings}`;
  400 invalid mode/port/`trust_proxy`, or `invalid_origins` with `errors[]`;
  422 unknown field or a non-boolean `trust_proxy`. Every attempt is one
  audit-log line (`audit_log.jsonl`) carrying `setting_change` (the fields
  changed, from/to, or the refusal).
- `POST /api/gateway/network/restart {force?}`: admin. 409 with
  `refused_reason` when a restart cannot apply the setting (CLI override, auth
  not met, nothing pending, process cannot relaunch itself).

A trimmed `GET` in `lan` mode, running and applied:

```json
{
  "schema": "gateway_network_v1",
  "configured": {"mode": "lan", "label": "Local network", "port": 8080, "bind_host": "0.0.0.0", "source": "stored"},
  "effective": {"mode": "lan", "bind_host": "0.0.0.0", "port": 8080, "overridden_by_cli": false,
                "host_source": "setting", "port_source": "setting", "running": true},
  "restart_required": false,
  "restart": {"available": true, "applies": true, "needed": false},
  "auth": {"user_auth": true, "token_auth": false, "ok_for_mode": true, "source": "env"},
  "modes": [{"id": "localhost", "allowed": true, "selected": false},
            {"id": "lan", "allowed": true, "selected": true},
            {"id": "internet", "allowed": true, "requires_acknowledgement": true}],
  "addresses": [
    {"kind": "loopback", "url": "http://127.0.0.1:8080", "reachable": true},
    {"kind": "lan", "url": "http://192.168.1.23:8080", "interface": "en0", "interface_label": "Wi-Fi", "reachable": true},
    {"kind": "hostname", "url": "http://mymac.local:8080", "reachable": true}
  ],
  "copy_hint": "http://192.168.1.23:8080",
  "warnings": ["Traffic is plain HTTP: …"]
}
```

### CLI

```bash
abstractgateway network status|show [--json] [--data-dir DIR]
abstractgateway network set [localhost|lan|internet] [--port N] [--acknowledge-internet]
                            [--allowed-origins ORIGIN[,ORIGIN...]] [--trust-proxy on|off]
abstractgateway network addresses [--copy] [--public] [--json]
abstractgateway network restart [--url URL] [--token T] [--force]
```

`status`, `set` and `addresses` work on the data dir directly (a running
gateway's bind and auth posture are read from `<data>/run/gateway-network.json`);
`restart` asks the running gateway. See [security.md](./security.md#network-exposure)
for what each mode changes for someone on your network.

## Two entry points, one store

AbstractCore (low level) and AbstractGateway (high level) are the two entry
points to the framework, and they share configuration. Where AbstractCore holds
a value, that value is the single source of truth: the Gateway reads and writes
it through AbstractCore, keeps no copy of it, and surfaces it alongside the
configuration the Gateway itself owns.

A fresh install starts with recommended defaults so generation works out of the
box — text on `lmstudio/qwen/qwen3.5-9b`, voice on `supertonic/supertonic-3`,
image on `mlx-gen/AbstractFramework/flux.2-klein-4b-8bit`. They appear in the
capability-defaults grid like any configured route and can be changed or
cleared from either entry point; a value supplied by an application or a run
always wins. The seed applies only when no AbstractCore configuration file
exists yet, so a store you already have is never modified.

**Which side owns what.**

| Domain | Authority | Where it is stored | Gateway surface |
| --- | --- | --- | --- |
| Capability route provider/model/base URL (text, image, video, voice, sound, music, 3D, embeddings) | AbstractCore | `capability_defaults.routes` in `abstractcore.json` | `GET/PUT/DELETE /api/gateway/config/capability-defaults[/{kind}/{modality}[/{task}]]`, console **Capability defaults** |
| Reasoning effort for text generation | AbstractCore | `reasoning` on the `output.text` route (stored as `input.text`) | the same routes and console panel |
| MTP default policy | AbstractCore | `options.speculation` on that text route | web/TUI **MTP** selector; application/run overrides remain independent |
| Plugin/provider route options (voice, profile, language) | AbstractCore | `options` on the route | the same routes and console panel |
| Provider API keys | AbstractCore | `api_keys` in `abstractcore.json` | console **Provider connections** (values are never returned) |
| Mail connection (IMAP/SMTP host, port, username, folder) | AbstractCore | `email` in `abstractcore.json` | the email bridge and inbox routes read it; `ABSTRACT_EMAIL_*` variables override it |
| Maintenance-triage LLM settings | AbstractCore | `maintenance` in `abstractcore.json` | the maintenance triage assistant; `ABSTRACT_TRIAGE_LLM_*` variables override it |
| Endpoint profiles (custom base URLs, per-profile keys, allowed models) | shared namespace | `provider_profiles` in `abstractcore.json` and `provider_endpoint_profiles` under the Gateway data dir | `/api/gateway/config/provider-endpoint-profiles` |
| Gateway auth, users, sessions, principals | Gateway | Gateway data dir | `/api/gateway/session/*`, `/api/gateway/users/*` |
| Bundles, workflow catalog, workspaces, run policy and retention | Gateway | Gateway data dir | the corresponding `/api/gateway/*` routes |
| Integrations (Agora, Telegram, process manager) | Gateway | Gateway data dir and environment | the corresponding `/api/gateway/*` routes |

Inside the Gateway, every read and write of an AbstractCore-owned value goes
through one module, `abstractgateway/core_config.py`. It is the only place that
talks to AbstractCore's configuration, which is what keeps "no Gateway copy"
true as the code grows.

Endpoint profiles are the one shared namespace: both sides can define
`endpoint:<id>` virtual providers, AbstractCore in its `provider_profiles`
section and the Gateway in its own store. A profile AbstractCore holds wins on
an id collision, and a Gateway profile resolves when AbstractCore has none — so
`abstractcore config set-default output.text --provider endpoint:<id>` and a
Gateway-defined profile of the same name always resolve to AbstractCore's
definition. Use distinct ids across the two unless you intend that.

### Capability defaults

The Gateway is a full CRUD surface over AbstractCore's per-modality
provider/model defaults (configure, surface, live-refresh) and keeps **zero**
local storage. Every read hits Core's manager and every write goes through
Core's setter, so configuring a default here configures Core's default, for text
and for every media modality: image, video, voice (TTS), voice input (STT),
sound, music, 3D.

**Where it is stored.** A JSON file under key `capability_defaults.routes`:
`~/.abstractcore/config/abstractcore.json` normally, or the Gateway-scoped
`<data_dir>/config/abstractcore.json` in hosted user-auth mode (the payload
reports both as `config_file` / `gateway_config_file` / `principal_config_file`).
`GET /api/gateway/config/capability-defaults` names the file it read.

| Route | What it defaults |
| --- | --- |
| `output.text` (stored as `input.text`) | text generation |
| `output.image[.text_to_image\|.image_to_image\|.image_upscale]` | image generation / edit / upscale |
| `output.video[.text_to_video\|.image_to_video]` | video generation |
| `output.voice` / `input.voice` | TTS **and voice cloning** / STT |
| `output.music` / `output.sound` | music / sound-effect generation |
| `output.scene3d[.text_to_scene3d\|.image_to_scene3d]` | 3D scene generation |
| `input.image` / `input.video` / `input.sound` / `input.music` | understanding (covered by `input.text` when that model is multimodal) |

The task→route mapping is stated once, in AbstractCore's capability-defaults
module, and every layer reads it from there. A `.task` suffix is only valid for
the tasks Core persists; `tts`, `stt`, `music_generation` and
`sound_generation` resolve at the modality cell.

CRUD: `GET /api/gateway/config/capability-defaults` (full grid — configured,
derived and unset rows, each naming its source),
`PUT`/`DELETE /api/gateway/config/capability-defaults/{kind}/{modality}` and
`.../{kind}/{modality}/{task}`. Every write re-applies the affected default to
the **live** runtime (`refresh_capability_defaults`), so the next run uses it
without a restart.

A `PUT` is a partial update: `provider`, `model`, `base_url`, `reasoning` and
`options` are all optional, a field you omit keeps its stored value, and `""`
clears a field. That is what lets the console edit a provider without discarding
a reasoning effort set through `abstractcore config set-default`, and the other
way round.

**The reasoning effort.** The text-generation route carries an optional
`reasoning` field beside its provider and model — the host's default reasoning
effort for reasoning-capable models. Set it in the console's capability-defaults
panel or through the route:

```bash
curl -X PUT "$GW/api/gateway/config/capability-defaults/output/text" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"lmstudio","model":"qwen3-30b","reasoning":"high"}'
```

It applies to any call that names no effort of its own. An explicit `thinking`
on a run, a Flow LLM/Agent node, or an entity's substrate wins over it,
`thinking=false` included; with no configured effort and no explicit value, no
reasoning parameter is sent at all.

**MTP defaults and overrides.** Fresh Core configurations seed native MTP at depth
2 for compatible models; existing stores are preserved. Gateway's web and terminal
capability-default editors change this Core-owned policy, not a separate Gateway
setting. Choose Off or a draft depth; clearing the policy does not reseed it.
Other route options are preserved by the dedicated selector. A default is a
policy, not proof that the selected backend or loaded model can execute it.

Flow, Assistant and Code default to inheritance. A run may supply `speculation`
on `/runs/start` or `/runs/schedule`, or `_runtime.speculation` in its input:
`false` disables MTP and a native-MTP object selects a depth. Explicit node/call
settings override inherited run settings. The sandbox selector uses execution
capabilities for the selected provider/model and reports the response's actual
MTP outcome. Selecting a depth never downloads a head or silently reloads a model.
Prepared models can change depth or switch Off without unloading; an unprepared
instance reports that provisioning/reloading is needed. Depth 2 is a starting
default, not a workload-independent speed guarantee.

**If the other entry point writes.** `abstractcore config set-default <route>
--provider … --model …` (and AbstractCore's console-TUI, which runs that
command) edits the same file with no way to notify a running Gateway. The host
therefore fingerprints the config files — `(path, mtime, size)`, one `stat`, no
parse — and re-publishes the defaults to the live runtime on the next
`start_run` when a file has moved. So both entry points are effective on the
next run, not at the next Gateway write or a restart. With a split AbstractCore
server (`ABSTRACTCORE_SERVER_BASE_URL`) there is no local file to watch, and the
write routes' push remains the freshness mechanism. Partial updates work the
same across that boundary: the AbstractCore server's own
`PUT /v1/config/capability-defaults/...` routes keep the fields a request omits
and clear the ones it sends empty, so the reasoning effort survives a
provider-only save whether AbstractCore runs in-process or as a server.

Cascade, per modality (highest wins): explicit request pins (a flow node's
`image_provider`, `tts_provider`, …) > flow defaults > this console default >
flow-scanned bootstrap (**text only**). A media node that names a provider is
never clobbered; a default only fills an absent/Auto one. See
`abstractgateway/provider_defaults.py` for the full contract.

Config beats env. The `output.image` default outranks `ABSTRACTVISION_BACKEND` /
`ABSTRACTCORE_VISION_BACKEND`, exactly as `output.voice` / `input.voice` outrank
`ABSTRACTVOICE_*`, and the voice contract's `active_model` reports the
configured route's model before any `ABSTRACTVOICE_*_MODEL` export. An
environment variable that loses to a configured value is logged once per
distinct (variable, config, env) triple, so a stale export stays visible.
Environment variables remain a labeled `#FALLBACK` for deployments that
configure nothing.

Voice-model environment variables such as `ABSTRACTGATEWAY_VOICE_TTS_MODEL` and
`ABSTRACTVOICE_OPENAI_TTS_MODEL` add entries to the **discovery catalog** — the
list of models a picker can offer. They do not select a default; the
`output.voice` route does.

### Model weights

Capability defaults say which model each route uses. These endpoints say
whether that model's weights are on the execution host, and fetch them when
they are not. They report the same four states as `abstractcore models status`
and both console-TUIs: `installed`, `not downloaded`, `unknown`, `remote`.

| Endpoint | What it does |
|---|---|
| `GET /api/gateway/models/availability` | The capability grid annotated with weight availability, plus the recommended fresh-install set — its raw counts (`total`, `installed`, `absent`, `would_download`) and `gaps`, the subset whose route has nothing else serving it. The text entry of `recommended` also carries AbstractCore's reasons: `catalog_id`, `basis` (`apple_silicon_tiers` or `portable_default`), `tier`, `fit_verdict`, `fits` and `warning` (a sentence when the model may not fit this computer). Read-only; never downloads. |
| `POST /api/gateway/models/download` | `{"provider": "...", "artifact": "..."}` or `{"recommended": true}`, with optional `"dry_run": true`. Returns a job id immediately. |
| `GET /api/gateway/models/download/{job}` | One job's progress: status, percent, byte counts and the provider tool's own recent output. |
| `GET /api/gateway/models/downloads` | Every download job this Gateway process knows about. |

The web console renders this as a **Weights** column on the capability-defaults
table, with a per-row download button and a fresh-install banner. In both
console-TUIs the verb is `w` on the Routes screen.

**The recommendation is advice for an empty route, not a standing debt.** The
banner speaks only about `gaps` — a recommended model that is absent *and*
whose route has nothing serving it. Route text generation at a model of your
own and the starter kit's LM Studio build stops being reported as missing:
nothing on this host needs it. A route whose *own* model is not downloaded is
still reported, on that row, in the Weights column. "Apply recommended" is a
standing action in the section head (`a` on the TUI Routes screen), available
whether or not the banner has anything to say.

**The artifact is not the model id.** A route stores the id the provider
*serves* (`qwen/qwen3.5-9b`); the download names the exact weights,
quantization included (`qwen/qwen3.5-9b@4bit`). The availability payload
carries `download_artifact` on any row where these differ — post that, not the
row's `model`.

**Single-flight.** A second request for an artifact already downloading joins
the running job instead of starting a second copy of the provider's tool; the
returned job's `joined` counter says so.

**Jobs run in AbstractCore's host job registry.** Downloads, deletes and
engine installs are jobs of one kind (`host_job_v1`), readable at
`GET /api/gateway/jobs/{id}` as well as through
`GET /api/gateway/models/download/{job}` (which keeps its `{ok, job}` envelope
and reports a queued job as `running`). AbstractCore keeps a snapshot of each
job on disk, so jobs started by `abstractcore models download` on the same
machine appear too. A job the gateway no longer knows returns 404; that is not
a lost download — the provider tool owns the bytes. Re-read
`/api/gateway/models/availability` to learn whether the weights landed.

**A default whose weights are missing does not stop the Gateway.** The host
loads, bundles register, and the failure surfaces when a run actually needs that
model — naming the capability route that configured the pair, how to change it,
and how to download it.

### Models and engines

The **Models** and **Engines** tabs of the web console, the terminal console
and the `abstractgateway models …` / `abstractgateway engines …` commands show
the same things AbstractCore shows (`abstractcore models …`, `abstractcore
engines …`): the host's hardware, the local inference engines, a model catalog
with a "fits this machine" verdict per download, the models already installed,
and the jobs that download, delete or install. The gateway does not detect
engines or size models itself; it serves AbstractCore's answers
so both entry points always agree.

| Endpoint | What it does |
|---|---|
| `GET /api/gateway/host/profile` | This host: OS, accelerator, RAM/VRAM, how much memory a model may use, free disk per model store. |
| `GET /api/gateway/engines?probe=1` | Ollama, LM Studio, MLX, llama.cpp, vLLM, Hugging Face: supported here, installed, version, running, and the exact install command. `install_allowed` says whether installs are enabled on this gateway. |
| `POST /api/gateway/engines/{id}/install` | `{"dry_run": true}` shows the command; `{"dry_run": false}` runs it on the gateway host as a job. Admin only, and only when `allow_engine_install` is on. |
| `GET /api/gateway/models/catalog?q=&engine=&fits=1&hub=1` | Downloadable models with presence and a fit verdict (`fits`, `tight`, `too_large`, `partial_offload`, `unknown`). |
| `GET /api/gateway/models/installed?provider=` | Every installed model per engine, with sizes and what would block a delete. |
| `POST /api/gateway/models/download` | Download one model as a job (see [Model weights](#model-weights)). Admin only. |
| `POST /api/gateway/models/delete` | `{"provider", "artifact", "dry_run", "force"}`: delete one model as a job. Admin only; refuses a loaded or shared model unless `force`. |
| `GET /api/gateway/jobs`, `GET /api/gateway/jobs/{id}`, `POST /api/gateway/jobs/{id}/cancel` | Download, delete and install jobs, newest first; cancel is admin only. |

Every job carries a `cli_equivalent` you can run by hand, for example
`abstractgateway models download ollama qwen3:8b` or
`abstractgateway engines install ollama --yes`. Payloads and refusals are
listed in [api.md](./api.md#models-and-engines).

<a id="allow_engine_install"></a>
#### `allow_engine_install`

Installing an engine runs its vendor installer (for example
`brew install ollama`) **on the machine that runs the gateway**, which for a
remote gateway is not the machine of the person clicking. So installs are
controlled by the runtime-config setting `allow_engine_install`:

| Gateway bound to | Default for someone at the gateway machine | Default for another computer |
|---|---|---|
| a loopback address (`127.0.0.1`, `::1`, `localhost`), which is what a bare `abstractgateway serve` and `abstractgateway service install` use | on | on |
| any other address (`0.0.0.0`, a LAN IP, a host name), or started without `abstractgateway serve` | on | off |

"Someone at the gateway machine" is a caller whose address is loopback or
one of this host's own interface addresses (a browser on the gateway machine
that uses its LAN address counts). For a browser app, the address is the
browser's own, relayed by the app's server on this computer, so a browser on
another computer never counts: see
[security.md](./security.md#callers-on-this-computer). The same rule gates app
installs (Apps page, tray). `install_policy` reports `caller_on_this_machine` and, when that rule
decided, `source: "default_same_machine"`.

An admin changes it with
`POST /api/gateway/admin/runtime-config {"allow_engine_install": true}`
(`false` turns it off, `null` returns to the default). The current value and
where it came from are in `GET /api/gateway/admin/runtime-config` and in
`install_policy` on `GET /api/gateway/engines`. There is no environment
variable for it. A dry run ("show the command") is always allowed, and every
install is admin-only and recorded in the audit log.

### Browser apps settings (`apps.*`)

The browser apps (Apps page: Flow, Code, Observer, Continuum, Entity) read five
runtime settings. Precedence is **stored > env > default**: a saved value
always wins; the `ABSTRACTGATEWAY_APPS_*` environment variable named in the
table is the fallback, and an env value that a saved one shadows is reported
as `env_shadowed`.

| Key | Label | Default | Value | Environment fallback |
|---|---|---|---|---|
| `apps.node` | Node.js for apps | `auto` | `auto` (Node.js 18+ on this computer, else the gateway's own) · `managed` · `system` · an absolute path to `node` | `ABSTRACTGATEWAY_APPS_NODE` |
| `apps.ports` | Ports for apps | (empty) | a port or `low-high`; empty = each app's usual port, else the next free one in 3100-3199 | `ABSTRACTGATEWAY_APPS_PORTS` |
| `apps.host` | Where apps listen | `127.0.0.1` | an IP or host name; `0.0.0.0` opens the apps to every network this computer is on | `ABSTRACTGATEWAY_APPS_HOST` |
| `apps.npm_registry` | npm registry | `https://registry.npmjs.org` | an http(s) URL (a mirror) | `ABSTRACTGATEWAY_APPS_NPM_REGISTRY` |
| `apps.pypi_url` | Node.js download index | `https://pypi.org/pypi` | an http(s) URL (a mirror) | `ABSTRACTGATEWAY_APPS_PYPI_URL` |

`GET /api/gateway/admin/runtime-config` returns them under `apps` as
`{name: {key, label, help, placeholder, default, env_name, value, source,
note?, env_shadowed?, invalid_stored?, invalid_env?}}` (the registry
`runtime_config.APPS_SETTINGS`: a new knob is one row, and the TUI renders
whatever the payload lists). Writes go through the generic door, admin-only
and audit-logged (`setting_change` on the request's audit line):
`POST /api/gateway/admin/runtime-config {"apps.host": "0.0.0.0"}` (or
`{"apps": {"host": "0.0.0.0"}}`); an empty value clears back to env/default.
Each value is validated before anything is written (400 with the reason). Read
at each use: a change applies at the next app start (`node`, `host`, `ports`)
or the next download (the two URLs).

Three ways, same semantics:

| | Web console | Console TUI | CLI |
|---|---|---|---|
| Where | Apps → *Advanced: apps settings* (one field per setting, with its source pill) | Runtimes → *Runtime knobs* → *Edit apps settings* | `abstractgateway apps config get [NAME] [--json]` |
| Change | type, *Save apps settings* (only changed fields are sent; empty = clear) | one line per setting (stored value prefilled; empty = clear) | `abstractgateway apps config set NAME VALUE` (`""` clears) |
| Refusal | the gateway's sentence (*Not saved*) | the form shows the gateway's sentence | `refused: <sentence>`, exit 2 |

The CLI works on the data dir directly (`--data-dir`, default: the `serve`
resolution), so a headless server needs no browser.

### Default agent workflow

`agents.default_workflow.<interface>` chooses the workflow that answers an
agent interface when a client picks "Gateway default" (AbstractCode's
workflow selector, the Assistant, the Telegram bridge, the backlog advisor).
The value is `[private:|catalog:]bundle[@version]:flow`: without a version the
latest published version runs; `private:` (the default) picks one of the
gateway's own workflows and `catalog:` one of the tenant catalog. The flow id
may itself contain `:` (the bundle part ends at the first `:`).

| Interface | When nothing is saved |
|---|---|
| `abstractcode.agent.v1` | the default entrypoint of the shipped `basic-agent` workflow (unavailable when it is not on the gateway) |
| `abstractassistant.agent.v1` | none: the Assistant runs its built-in orchestrator |
| any other interface a workflow declares | none until an admin saves one |

A saved value is checked when it is saved (it must exist on this gateway and
declare that interface; 400 with the reason otherwise) and again at every
run start: a value that no longer works is shown as unavailable with the
reason, and runs that ask for the default are refused (409) until it is
changed. It never falls back to another workflow on its own.

`GET /api/gateway/admin/runtime-config` returns

```json
"agents": {"default_workflow": {"abstractcode.agent.v1": {
  "key": "agents.default_workflow.abstractcode.agent.v1", "value": "coding-agent:coder", "source": "stored",
  "available": true, "reason": null, "default": "basic-agent:81795ea9",
  "resolved": {"bundle_id": "coding-agent", "bundle_version": "0.2.7", "flow_id": "coder",
               "registry_scope": "private", "workflow_id": "coding-agent@0.2.7:coder", "name": "coder"},
  "eligible": [{"value": "basic-agent:81795ea9", "workflow_id": "basic-agent@0.0.5:81795ea9", "name": "basic-agent", "...": "..."}]}},
  "index_source": "host"}
```

`source` is `stored` or `default` (there is no launch flag and no environment
variable for this setting).

| | Web console | Console TUI | CLI |
|---|---|---|---|
| Where | Workflows → *Default agent workflow* (one row per interface), or *Make agent default* on an entrypoint | Runtimes → *Runtime knobs* → *Edit default agent workflows*; Workflows marks the default with ★ | `abstractgateway config get agents.default_workflow.<interface>` |
| Change | choose in the list, *Save default agent workflows* | type the value (the choices are listed under the field; empty = the built-in default) | `abstractgateway config set agents.default_workflow.<interface> bundle[@version]:flow`, `config unset …` |

Writes are admin-only and audit-logged like every other setting. A write that
names a setting the gateway does not know is refused as a whole (400) and
saves nothing; two writers at the same time never lose each other's change.

### Stream replies by default

`agents.streaming_default` (`on`/`off`, default `off`) decides whether an
interactive run streams the model's reply live when the app that started it
does not say (`input_data._runtime.stream` absent; see
[API: live replies](api.md#4b-live-replies-token-deltas-on-the-same-stream)).
It applies to `POST /runs/start` only: scheduled runs, the Telegram and email
bridges and the entity loop never stream by default. A flow node that turns
streaming off for its LLM call always wins.

`GET /api/gateway/admin/runtime-config` returns

```json
"agents": {"streaming_default": {"key": "agents.streaming_default", "value": false, "source": "default",
                                 "default": false, "label": "Stream replies by default", "help": "…"}}
```

`source` is `stored` or `default`. Apps without admin rights read the
effective default from `GET /api/gateway/discovery/capabilities`
(`capabilities.streaming.default`).

| | Web console | Console TUI | CLI / API |
|---|---|---|---|
| Read | Workflows → *Stream replies by default* | Runtimes → *Runtime knobs* | `abstractgateway config get agents.streaming_default` |
| Change | the switch | the switch | `abstractgateway config set agents.streaming_default on`, `config unset agents.streaming_default`; `POST /api/gateway/admin/runtime-config {"agents": {"streaming_default": true}}` |

### Skills shelf

`skills.shelf` is the folder the gateway reads skills from (it holds
`skills/<name>/SKILL.md` and the trust files `validations.yaml`,
`advisories.yaml`, `guidance.yaml`). Leave it empty to use the gateway's own
copy of the curated shelf that ships with AbstractSkill: at each start the
gateway copies it into `<data dir>/skills/registry`, adding what is new and
refreshing what it wrote before, and never overwriting a file you edited
there. The report of that copy is in the gateway log, and
`POST /api/gateway/admin/skills/reseed` (or *Refresh the curated shelf* in the
console) runs it again on demand.

A saved folder that does not exist or holds no `skills/` folder is shown as
unavailable with the reason (the gateway does not silently use another
shelf). `GET /skills` and the settings say which shelf is in use
(`shelf_source`: `stored`, `env`, `seeded`, `checkout` or `none`).

| | Web console | Console TUI | CLI |
|---|---|---|---|
| Where | Apps → *Skills shelf* | Runtimes → *Runtime knobs* → *Edit skills shelf* | `abstractgateway config get skills.shelf` |
| Change | type the folder, *Save skills shelf*; *Refresh the curated shelf* | type the folder (empty = the gateway's own copy) | `abstractgateway config set skills.shelf /path/to/registry`, `config unset skills.shelf` |

### Backlog folder, exec runner and process manager (Continuum)

Continuum's Board, Backlog, Executions and Services pages read three runtime
settings. A fresh install needs none of them: the gateway keeps its own
backlog in `<data dir>/backlog/`, and creates the standard layout there the
first time the backlog is used (`docs/backlog/overview.md`,
`docs/backlog/template.md`, and the `planned/`, `proposed/`, `completed/`
folders; nothing existing is ever overwritten). Continuum then shows an empty
board with **Create your first item**.

| Key | Label | Default | Value |
|---|---|---|---|
| `triage_repo_root` | Backlog folder | `<data dir>/backlog` (created on first use) | a folder that contains `docs/backlog` (a project checkout), or the gateway's own folder |
| `backlog_exec_runner` | Backlog exec runner | off | `on` / `off`: run the items queued for execution on this machine |
| `process_manager` | Process manager | off | `on` / `off`: Continuum's Services page (process control also needs the backlog folder set to the framework checkout it manages) |

**Where a value comes from** (one resolution, `runtime_config.resolve_backlog_root`
and `resolve_exec_runner`; every consumer calls it: the backlog, report,
triage and process routes, the exec runner at each poll, the skills shelf):

1. the launch flag of the running gateway: `abstractgateway serve --backlog-root PATH`
   and `--exec-runner on|off` (for that run only; source `flag`);
2. the saved setting (source `stored`);
3. an environment value (`ABSTRACTGATEWAY_TRIAGE_REPO_ROOT`,
   `ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER`; source `env`;
   supported for compatibility, never needed; a saved value wins);
4. the default (source `default`).

`GET /api/gateway/admin/runtime-config` serves each as `{value, source, key,
label, help, cli, flag?}`; the backlog folder also carries `available`,
`reason` (why it is not usable, without the path), `default_path` and, under a
launch flag, the `stored_value` that applies once the gateway restarts
without it. Non-admins get the posture without server paths.
`GET /api/gateway/backlog/status` answers the same question for Continuum
(any signed-in user; paths for admins only).

Changing them, three doors with one validation (a folder must exist and
contain `docs/backlog`, or be the gateway's own folder, which is created; a
switch is `on` or `off`; a refusal is one plain sentence):

| | Web console | CLI | Continuum |
|---|---|---|---|
| Where | Apps → *Advanced: backlog settings (Continuum)* | `abstractgateway config get [KEY] [--json]` | Settings → *Gateway administration* |
| Change | edit, *Save backlog settings*; *Use the gateway's own folder* | `abstractgateway config set KEY VALUE`, `abstractgateway config unset KEY` | *Change…*, *Use the gateway's own folder*, *Enable* / *Disable* |

`config set` goes through the running gateway's door when one serves this
data dir on this machine (it applies at once and lands in the audit log);
otherwise it writes the settings store and the next start reads it. The same
`config get|set|unset` covers every runtime setting (`executor`,
`apps.<name>`, …).

When a saved folder disappears (a deleted or unmounted checkout), the backlog
routes answer `404` *Backlog folder not available on this gateway: the folder
does not exist (set by the saved setting)…* and Continuum shows the folder,
the reason and, for an admin, **Use the gateway's own folder** and **Choose a
folder…**.

Evidence: `src/abstractgateway/runtime_config.py` (`resolve_backlog_root`,
`resolve_exec_runner`, `validate_backlog_root`, `BACKLOG_SETTINGS`),
`src/abstractgateway/assets/backlog_skeleton/`, `src/abstractgateway/config_cli.py`,
`tests/test_gateway_backlog_root_settings.py`.

### Host state and model residency

Beyond weights on disk, these endpoints report and control what is loaded in
memory right now:

| Endpoint | What it does |
|---|---|
| `GET /api/gateway/host/state` | One-call host snapshot: memory, GPU, resident models (frozen `model_residency_row_v1` rows), session prompt caches, and byte totals. Sections degrade independently in-band (`degraded` + `reasons`); never a 500. |
| `GET /api/gateway/host/metrics/memory` | Host RAM/process/device memory snapshot; answers `supported: false` with a reason when the runtime facade has no snapshot. |
| `GET /api/gateway/host/metrics/gpu` | GPU utilization probe with the same `supported`/degraded style. |
| `GET /api/gateway/models/loaded` | Model residency listing: raw `models` records plus normalized `rows` (`row_schema = "model_residency_row_v1"`, including lock, modality, context-calibration, and host-identity fields). |
| `GET /api/gateway/models/context_estimate` | Context/KV memory estimate for a `provider`+`model` (optional `context_length` >= 1), with in-band `confidence`: `calibrated`, `estimated`, or `unknown`. |
| `POST /api/gateway/models/load` | Load (and by default pin) a model runtime. Admin only. |
| `POST /api/gateway/models/unload` | Unload a model runtime by `runtime_id` or task/provider/model selector. A locked model answers HTTP 409 unless the request carries `"force": true`. Admin only. |
| `POST /api/gateway/models/lock` | Lock a resident model against unload (same target selector as unload). Admin only. |
| `POST /api/gateway/models/unlock` | Release a model-residency lock. Admin only. |

Both consoles render this surface as a **Resources** view — a tab in the web
console, screen 8 in the console-TUI: memory and GPU meters, the resident-model
table (modality, tri-state residency, lock state, context facts), and session
prompt caches. Any authenticated user can browse it and request context
estimates; the warm-up, lock/unlock, unload (with a force confirmation when a
locked model answers 409), and cache-clear controls appear for admins.

The reads are available to any authenticated principal; the mutations (and
`POST /models/download` above) require an admin principal, and anonymous
requests are always rejected. For local development only,
`ABSTRACTGATEWAY_DEV_READ_NO_AUTH=1` (default off) allows unauthenticated
loopback reads as a non-admin read-only principal — see
[security.md](./security.md).

See [api.md](./api.md#host-state-and-model-residency) for payload shapes and
the `model_residency_row_v1` field list.

### Host control: pause, desktop tray, restart, update

The process's own controls, used by the desktop tray and the console. Reads
are user-level; every write is admin-only.

| Endpoint | What it does |
|---|---|
| `GET /api/gateway/host/runner` | `paused`, `paused_at`, `paused_by`, `reason`, `inflight_ticks` (runs still finishing their current step), `scope` (`"workflow runner"`), `runner_in_process`, `step_gate_supported`, restart/shutdown `capabilities`. |
| `POST /api/gateway/host/pause` / `resume` | Pause or resume execution process-wide (persisted in `<data_dir>/gateway_paused.json`). Body `{"reason": "..."}` optional. |
| `GET /api/gateway/host/metrics/live` | GPU + memory + paused/in-flight in one call, cached 1 s server-side — the tray's fast lane. |
| `GET /api/gateway/host/runs` | Recent runs across every data plane on this machine (`limit`, `window_hours`), newest first, with a readable `label` and the step count. Admin — it crosses tenants. Cached 5 s. Entity planes are skipped and named in `skipped_entity_planes`. |
| `GET /api/gateway/host/tray` | Whether the tray helper runs (`pid`, `ready`), and the decision (`reason`, `hint`) when it does not. |
| `POST /api/gateway/host/tray/show` | Retry the helper now (admin) — the escape hatch for one that crashed. There is no `hide`. |
| `POST /api/gateway/host/restart` / `shutdown` | Graceful restart (same command, same environment) or stop; `409` with the reason when this process cannot (`--reload`, not started by `abstractgateway serve`, an update is installing). |
| `GET /api/gateway/host/update` | How the gateway was installed (`install.kind`, `upgradable`, the command), the last update check, the upgrade job, `restart_pending`. |
| `POST /api/gateway/host/update/check` / `start` | Ask pypi.org for the latest release (offline is an in-band answer) / run the upgrade in the background. |

The tray icon has **no setting**: while the gateway serves a desktop that can
hold it, it is there. It is absent only for reasons that are facts about the
machine or the launch — no display, no `tray` extra, `serve --reload`, a
runner-only process, or `serve --no-tray` for this run — and `GET /host/tray`
names which. There is no `desktop_tray` setting; a
write to that key is refused with this explanation. `GET /api/health` carries `"paused": true` while paused (status
stays `healthy`). Full description: [tray.md](./tray.md).

### Runtime-scoped Core capability defaults

In hosted user-auth mode, `GET /api/gateway/config/capability-defaults` returns
the execution-host Core capability routes plus the Gateway/root baseline and
any defaults configured for the current Gateway principal. The bootstrap
`default/admin` principal edits the Gateway baseline when it uses the default
runtime. Normal user writes to
`PUT /api/gateway/config/capability-defaults/{kind}/{modality}` or
`PUT /api/gateway/config/capability-defaults/{kind}/{modality}/{task}` are stored under
that principal's Gateway data plane as a Core config file and override the
Gateway baseline only for that user:

```text
$ABSTRACTGATEWAY_DATA_DIR/config/abstractcore.json
$ABSTRACTGATEWAY_DATA_DIR/users/<tenant>/<runtime>/runtime/config/abstractcore.json
```

This lets operators set a Gateway default and lets hosted users choose
remote-provider defaults for their own runtime without mutating the operator's
global AbstractCore config or other users. The route schema, normalization,
task-specific generated-media suffixes, and file format come from AbstractCore
capability-default contracts. Capability defaults live only in the AbstractCore
config file; a `config/capability_defaults.json` file in the data dir is not
read (recreate such defaults with `abstractgateway-config set-default ...`). Provider API keys and raw secrets are
not returned by these routes. Use Gateway provider connections when a route
default needs an API key or custom base URL.

Gateway model discovery delegates to AbstractRuntime's AbstractCore discovery
facade. LLM and embedding default pickers can filter models with Core route keys
such as `capability_route=input.image,output.text` or
`capability_route=embedding.text`. Generated image/video/voice/sound/music
defaults continue to use their capability plugin catalogs so provider readiness,
download/setup state, and backend-specific metadata do not get written into the
raw Core model registry.

CLI examples:

```bash
# Gateway baseline Core default
abstractgateway-config set-default input.text \
  --provider endpoint:openai-prod \
  --model gpt-4.1

# One user's runtime Core override
abstractgateway-config set-default input.text \
  --scope user \
  --tenant default \
  --user alice \
  --provider endpoint:alice-openai \
  --model gpt-4.1

abstractgateway-config defaults --scope user --user alice
```

#### Modality rows and task rows

`output.image`, `output.video` and `output.scene3d` are the **parent** rows of
their `output.<modality>.<task>` siblings, not duplicates of them. The
parent answers every task of that modality that has no row of its own, so
setting it alone is the simple path (one image model for generate, edit and
upscale) and is what a fresh install seeds. A task row overrides it for that
task, wholesale — route rows are single coherent backend identities and are
never field-merged with their parent.

Resolution everywhere — execution, the Sandbox, and what `/capabilities`
advertises — is **task row first, modality row second**. A modality-level
question resolves through the canonical generation task
(`output.image.text_to_image`) before falling back to `output.image`, so the
backend Gateway advertises is always the backend it will execute.

`output.voice`, `output.sound` and `output.music` have no task rows; their
modality row is the primary key, not a fallback.

In the Multimodal Capabilities grid the task rows are indented beneath their
modality row, and a modality row that is unset while every task row beneath it
is configured shows `not needed` rather than `not configured` — nothing can
reach it in that state. It stays editable, because setting it is still the
one-value-for-everything path.

`input.text` is the canonical text LLM route. `output.text` is reported as a
read-only derived view of `input.text`, and CLI/API writes to `output.text` are
canonicalized to `input.text` for compatibility. `input.image` is a fallback
image-understanding route only: when the selected `input.text` model is known
from AbstractCore model capabilities to accept image input, the console marks
`input.image` as covered by `input.text` and disables separate editing.
`input.video` follows the same coverage model when the text model can handle
visual frames, but it remains overrideable so operators can choose a dedicated
video/VLM route. `input.voice` is the speech-to-text fallback route; if it is
not configured and the selected text model cannot accept audio natively,
Gateway/Core fail clearly instead of using a hidden installed STT backend.
`input.sound` is for non-speech audio understanding and is not used as STT.
`input.music` is the corresponding music-audio understanding route. `input.sound`
and `input.music` may be shown as covered by `input.text` only when the selected
text model is known to accept those native inputs, and both rows remain
overrideable.
Audio-language candidates such as `qwen3-omni-30b-a3b-instruct`,
`qwen3-omni-30b-a3b-captioner`, `qwen2.5-omni-7b`, and
`qwen2-audio-7b-instruct` are registry-known options when the configured
provider can serve them. Qwen3.6 text/image/video defaults should not be treated
as sound or music understanding models.

### Provider connections

Gateway Console and `POST /api/gateway/config/provider-endpoint-profiles` let
signed-in users define reusable provider connections through a guided setup
flow for `openai`, `anthropic`, `openrouter`, `portkey`, `lmstudio`, `ollama`,
or `openai-compatible`. A connection includes a stable id, display name,
description, optional base URL, optional API key, and optional advanced model
allowlist. The raw API key is write-only: responses include only `api_key_set`
and a short fingerprint. AbstractCore owns model capability metadata, so normal
setup does not ask users to classify models manually.

The console's **Test** action calls the selected provider through
`POST /api/gateway/config/provider-endpoint-profiles/discover-models` and
previews model discovery before saving. Leave the advanced model restriction
empty to keep live discovery active, or select one or more models to store a
fixed allowlist. The **Multimodal Capabilities** tab shows configured provider
connections and direct providers that are already usable from scoped
AbstractCore config or environment variables. It does not collect endpoint base
URLs or API keys. Reachable default local servers such as LM Studio and Ollama
also appear automatically when Gateway can discover models from their
configured/default endpoint.

Enabled profiles appear in `GET /api/gateway/discovery/providers` as virtual
provider ids such as `endpoint:office-vllm`. Direct configured providers such
as `openai` or `anthropic` also appear automatically when their required API
key is available from scoped Core config or process environment. Use those
provider ids in Flow nodes or Gateway capability defaults. At runtime the
Gateway host resolves virtual providers to the real provider family, base URL,
and API key for the transient AbstractRuntime call; direct providers use the
scoped Core config/environment already available to the execution host.
Workflow JSON and browser storage do not contain the raw secret. Normal users
can manage user-scoped profiles. Gateway-scoped profiles require an admin
principal.

The console **Sandbox** tab reuses this configuration. It tests the selected
multimodal capability default rather than an ad hoc provider/model pair. Text
chat uses the configured text route, and generated media tests use configured
routes such as `output.image.text_to_image`, `output.video.text_to_video`,
`output.voice`, `output.sound`, and `output.music`. Image edit, image upscale,
and image-to-video are configured separately in the Multimodal Capabilities tab
through `output.image.image_to_image`, `output.image.image_upscale`, and
`output.video.image_to_video`. The Sandbox renders generated images, videos,
voice, sound, and music artifacts inline when the route completes, while keeping artifact
links available for opening the raw content. Text chat can include uploaded
attachments such as images, audio, video, PDFs, Markdown, or text documents.
Uploaded attachments are stored as Gateway artifacts and then materialized by
Runtime into provider-ready media for AbstractCore, so vision-capable
OpenAI-compatible text routes receive image uploads as native multimodal
`image_url` content. Sandbox text turns also send bounded browser-local
grounding context, including local datetime, timezone, timezone offset, and
locale. Runtime may use that browser context for prompt grounding only; it keeps
server-derived context as provenance and never uses browser metadata for auth,
runtime routing, or credential selection. Country grounding is inferred from the
browser timezone when possible, with locale only as a fallback.

### Workspace policy (filesystem scope)

The gateway enforces a server-side workspace policy so thin clients cannot expand filesystem access by sending arbitrary paths.

Operator-controlled roots:
- `ABSTRACTGATEWAY_WORKSPACE_DIR`: base directory used for `/api/gateway/files/*` helpers and to clamp run-provided `workspace_root` / `workspace_allowed_paths`.
- `ABSTRACTGATEWAY_WORKSPACE_MOUNTS`: additional allowed roots, newline-separated `name=/abs/path`.

Client scope overrides (permissive; trusted machines only):
- `ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE=1` (or `ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE=1`) enables honoring client-provided `workspace_*` knobs, including `workspace_access_mode=all_except_ignored`.

Discoverability:
- `GET /api/gateway/workspace/policy` returns `{policy: {...}}` including whether client overrides are enabled (mount names only; no absolute paths).

Built-in deny list. These folders of the gateway's user account are never
listed nor served by the workspace browser (`GET /runs/{run_id}/workspace/…`),
for anyone:

`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`,
`~/Library/Keychains`, `~/.abstractgateway`, `~/.abstractcode`,
`~/.abstractassistant`, `~/.abstractcontinuum`, `~/.abstractcore`, and the
gateway's data folder (except a run's own conversation folder inside it).

The same folders are denied to every run's file tools: the gateway gives each
run (and the runs it starts, scheduled runs included) the folders as
`workspace_builtin_deny_prefixes`, plus one exception,
`workspace_builtin_allow`, for the run's own conversation folder inside the
data folder. Everything under a denied folder is refused; nothing inside the
data folder is listed one by one, and these entries are enforced without being
written into the model's prompt (so the prompt stays the same from turn to
turn however much the data folder holds). A client cannot send these two
entries (they are dropped); the operator's own `workspace_ignored_paths` are
kept as sent. For runs this is a default an admin may turn off:
`abstractgateway config set workspace_builtin_deny off` (or `POST
/api/gateway/admin/runtime-config {"workspace_builtin_deny": false}`); the
workspace browser keeps hiding them.
`GET /api/gateway/admin/runtime-config` reports the list as `builtin_deny
{value: [paths], enabled, source}`. The file tools honour the deny list; shell
commands a run is allowed to execute are not confined by it.

A run cannot use a folder inside the gateway's data folder as its
`workspace_root`, except the conversation folder the gateway made for the same
user and session (or one of that user's per-run folders). This check applies
at every door that takes a client's inputs: `POST /runs/start`, `POST
/runs/schedule` and entity summons.

Every run the gateway starts, whatever started it (the HTTP routes, the
Telegram, email and agora bridges, entity summons, scheduled runs, which
inherit it from their schedule), works in a folder (its conversation's
gateway-made folder when it named none) and gets the built-in deny rule above.
Entity visits (the entity chat and its own-time loop) use the entity's own
tools, which never leave `<entity home>/workspace` and the operator's mounts.

Evidence: `src/abstractgateway/routes/gateway.py` (`_workspace_root`, `_workspace_mounts`, `_sanitize_run_workspace_policy`, `_apply_builtin_tool_deny`, `_browse_workspace_root`, `start_run`), `src/abstractgateway/workspace_browse.py`.

### Durability backend

- `ABSTRACTGATEWAY_STORE_BACKEND`: `file` (default) or `sqlite`  
  Evidence: `src/abstractgateway/service.py`
- `ABSTRACTGATEWAY_DB_PATH`: SQLite DB file path (optional; default: `<DATA_DIR>/gateway.sqlite3`)  
  Evidence: `src/abstractgateway/stores.py` (`build_sqlite_stores`)
  Note: for safety, when `ABSTRACTGATEWAY_STORE_BACKEND=sqlite`, the DB path must be **under** `ABSTRACTGATEWAY_DATA_DIR`.
  The gateway fails fast if `ABSTRACTGATEWAY_DB_PATH` points elsewhere (prevents cross-wiring UAT/prod durable state).

### KG memory store

Gateway selects an AbstractMemory TripleStore through a small resolver; it does
not implement memory stores itself.

- `ABSTRACTGATEWAY_MEMORY_STORE_BACKEND`: `lancedb` (default), `memory`, or `sqlite` when the installed AbstractMemory build exposes `SQLiteTripleStore`
- `ABSTRACTGATEWAY_MEMORY_STORE_PATH`: optional explicit store path
- `ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR=1`: fail fast when the selected backend cannot satisfy semantic/vector recall

Backend behavior:

- `lancedb`: persistent and vector-capable; semantic `query_text` requires the execution-host
  `embedding.text` capability route.
- `sqlite`: persistent and structured-query only when `SQLiteTripleStore` is available; semantic `query_text` fails clearly.
- `memory`: process-local test/dev backend; non-durable.

The same resolver is used for bundle `memory_kg_*` nodes and
`POST /api/gateway/kg/query`. Capability discovery reports memory backend,
persistence, vector support, and embedder status. A missing on-disk store is not
an unavailable state by itself: when AbstractMemory is installed and the backend
resolves, fresh stores are authoring-ready and structured queries simply return
no matches until assertions are written.

### Runner tuning (advanced)

These map to `GatewayHostConfig` and `GatewayRunnerConfig`:
- `ABSTRACTGATEWAY_RUNNER`: `1` (default) / `0` to disable runner in-process  
  Evidence: `src/abstractgateway/config.py`, `src/abstractgateway/cli.py`
- `ABSTRACTGATEWAY_POLL_S` (default `0.25`)
- `ABSTRACTGATEWAY_COMMAND_BATCH_LIMIT` (default `200`)
- `ABSTRACTGATEWAY_TICK_MAX_STEPS` (default `100`)
- `ABSTRACTGATEWAY_TICK_WORKERS` (default `4`)
- `ABSTRACTGATEWAY_RUN_SCAN_LIMIT` (default `200`)

Evidence: `src/abstractgateway/config.py`, `src/abstractgateway/runner.py`.

### Stop and the kill switch

A `cancel` command (the Stop button) cancels the run tree and also stops the
model call that is executing: the runtime hands the call a cancel event and the
provider stops within one token (MLX) or one stream chunk (any streaming
provider). The stopped call is recorded as an `llm_call` step with status
`cancelled` and `cancelled_by: command`.

If a model call of the cancelled tree is still executing after the kill-switch
deadline (a provider lane that cannot observe the event, e.g. a non-streaming
HTTP request), the gateway kills that inference in process, never the gateway
process: other runs, sessions and the HTTP API keep working. The runtime
injects an `EffectKilled` exception into the one thread executing the call (it
unwinds within one token of a Python-level decode loop); the step is recorded
`cancelled` with `killed_by: kill_switch`. The gateway logs an ERROR line
(`STOP KILL SWITCH FIRED … killed_by=kill_switch action=kill_inference`), writes
an `abstract.status` record "Stop forced at N s: inference killed" on every run
of the tree (the web UI shows it), and ends the runs CANCELLED with that reason.
A thread blocked inside ONE native call for more than 5 s after the kill
(`KILL_GRACE_S`) is reported as "could not be interrupted" in the log and the
ledger; the pending kill fires when that call returns. Tools are never
escalated: a tool still running is named in the log and its result is never
fed to another model call.

| Knob (runtime config key / env) | Default | Meaning |
|---|---|---|
| `stop_kill_switch_s` / `ABSTRACTGATEWAY_STOP_KILL_SWITCH_S` | `10` | seconds after the cancel is applied; `0` disables (logged at ERROR on every Stop) |

It is read at every Stop (runtime config via `POST /api/gateway/admin/runtime-config`
supersedes env, env supersedes the default). Evidence: `src/abstractgateway/stop_kill_switch.py`.

## LLM/tool defaults (bundle mode)

Only needed when the loaded bundle(s) contain LLM/tool/agent nodes.

- `input.text` capability route
  Default text route for LLM execution and Gateway LLM helper endpoints. Configure it through
  `abstractgateway-config set-default input.text ...` or
  `abstractcore config set-default input.text ...`.
  If no pair is configured, helpers return a clear configuration error instead of falling back to a
  hardcoded model.
  Evidence: `src/abstractgateway/provider_defaults.py`, `src/abstractgateway/hosts/bundle_host.py`
- `ABSTRACTGATEWAY_TOOL_MODE`:
  - `approval` (default): execute safe tools locally; require explicit approval for dangerous/unknown tools
  - `passthrough`: require explicit approval for *all* tools (then execute in-process on resume)
  - `delegated`: do not execute tools; tool calls yield a durable `JOB` wait for external executors
  - `local` (or `local_all`): execute all tools inside the gateway process (dev only; higher risk)
  Evidence: `src/abstractgateway/hosts/bundle_host.py` (tool executor selection)

### Embeddings

The gateway exposes an embeddings API when the execution host has an explicit `embedding.text`
capability default. Remote/provider-backed embeddings work with the base
remote-light install; local HuggingFace/sentence-transformer embeddings require
`abstractgateway[embeddings]`.

Configure it through the same capability-default control plane used by Flow:

```bash
abstractgateway-config set-default embedding.text \
  --provider lmstudio \
  --model text-embedding-nomic-embed-text-v1.5 \
  --base-url http://127.0.0.1:1234/v1
```

In embedded deployments Gateway uses the local Core embedding manager. In split deployments it
delegates to the remote AbstractCore `/v1/embeddings` route so provider `base_url` is evaluated
from the Core host.

Evidence: `src/abstractgateway/embeddings_config.py`

### Prompt cache controls (provider-dependent)

Gateway prompt-cache endpoints are available when the AbstractCore integration
for the active provider/model exposes them. Remote providers usually provide
server-managed cache hints; local in-process providers can expose stronger
control-plane operations when installed in a custom runtime image.
Provider-level endpoints remain available for operators, and session-level
endpoints provide a deterministic gateway-owned namespace/key lifecycle for thin
apps without pretending unsupported providers have local KV state.

- `GET /api/gateway/prompt_cache/capabilities`
- `GET /api/gateway/prompt_cache/stats`
- `POST /api/gateway/prompt_cache/set`
- `POST /api/gateway/prompt_cache/update`
- `POST /api/gateway/prompt_cache/fork`
- `POST /api/gateway/prompt_cache/clear`
- `POST /api/gateway/prompt_cache/prepare_modules`
- `POST /api/gateway/blocs/upsert_text`
- `GET /api/gateway/blocs/record`
- `GET /api/gateway/blocs`
- `POST /api/gateway/blocs/delete`
- `GET /api/gateway/blocs/kv/manifest`
- `GET /api/gateway/blocs/kv/list`
- `POST /api/gateway/blocs/kv/ensure`
- `POST /api/gateway/blocs/kv/load`
- `POST /api/gateway/blocs/kv/delete`
- `POST /api/gateway/blocs/kv/prune`
- `GET /api/gateway/prompt_cache/saved`
- `POST /api/gateway/prompt_cache/save`
- `POST /api/gateway/prompt_cache/load`
- `GET /api/gateway/sessions/{session_id}/prompt_cache/status`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/prepare`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/rebuild`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/clear`
- `GET /api/gateway/sessions/prompt_cache`
- `POST /api/gateway/sessions/{session_id}/prompt_cache/clear_all` (admin)

Session lifecycle responses distinguish `unsupported`, `keyed`, and
`local_control_plane` modes. Keyed providers receive a stable `runtime_hint`;
local-control-plane providers can prepare, clear, and rebuild when their
AbstractCore provider exposes those operations.

Treat the prompt-cache surfaces separately:

- `/prompt_cache/*`: provider/model prompt-cache controls
- `/sessions/{session_id}/prompt_cache/*`: gateway-owned volatile session
  lifecycle, derived from the session/bundle/provider identity
- `GET /sessions/prompt_cache` + `/sessions/{session_id}/prompt_cache/clear_all`:
  enumeration of the caches the runtime actually minted — the recommended lane
  for observing and reclaiming session cache state, because it cannot miss
  caches whose keys the gateway never derived
- `/blocs/*`: durable exact-reuse bloc/KV contract that returns `prompt_cache_binding`

The `saved` / `save` / `load` aliases are Runtime-backed host-local admin
operations. Local runtimes write under `<DATA_DIR>/prompt_cache_exports`; remote
and hybrid runtimes report `prompt_cache_local_only`.

### Multimodal provider/plugin controls

The base install already includes the Gateway HTTP/SSE server and the Runtime
multimodal integration layer. Direct Gateway routes for voice/audio, image/video,
and music become available when the corresponding lower-layer capability
packages are installed on the gateway host (or when Gateway is configured to
proxy to a remote AbstractCore server).

Local heavy engines remain explicit opt-ins in the provider packages; Gateway
does not implicitly install them.

- `input.text` capability route: default text model for bundle LLM nodes
- `OPENAI_BASE_URL` / `OPENAI_API_KEY`: generic OpenAI-compatible text endpoint for AbstractCore providers
  - Apple/MLX Docker deployments should point the lightweight Gateway container
    at host-native inference, for example
    `http://model-runner.docker.internal/engines/v1`,
    `http://host.docker.internal:1234/v1`, or another `/v1` endpoint.
- `LMSTUDIO_BASE_URL` / `OLLAMA_BASE_URL`: named local endpoint providers for
  LM Studio and Ollama model discovery/routing from inside the Gateway container.
- `ABSTRACTGATEWAY_VISION_BACKEND` / `ABSTRACTGATEWAY_VISION_BASE_URL` / `ABSTRACTGATEWAY_VISION_API_KEY` / `ABSTRACTGATEWAY_VISION_MODEL_ID`: Gateway-scoped image backend settings. The `ABSTRACTVISION_*` names are also accepted by the lower package.
- `ABSTRACTGATEWAY_VOICE_TTS_ENGINE` / `ABSTRACTGATEWAY_VOICE_STT_ENGINE`: Gateway-scoped voice engine settings. The `ABSTRACTVOICE_*` names are also accepted by the lower package.
- `ABSTRACTGATEWAY_VOICE_TTS_MODEL` / `ABSTRACTGATEWAY_VOICE_STT_MODEL`: Gateway-scoped TTS/STT model defaults.
- `ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL` / `ABSTRACTGATEWAY_VOICE_REMOTE_API_KEY`: remote voice endpoint used by AbstractVoice.
- `GET /api/gateway/discovery/capabilities`: reports installed packages plus AbstractCore capability plugins for `voice`, `audio`, `vision`, and `music`; also returns `capabilities.contracts.version=1` with thin-client feature gates for AbstractFlow, AbstractAssistant, AbstractCode, shared run input/history endpoints, artifact search/import/export, direct voice/audio/image/video/music endpoints, workflow-backed image/video generation, and provider/session prompt-cache controls
- `GET /api/gateway/voice/voices`: proxies AbstractCore `/v1/audio/voices` when `ABSTRACTCORE_SERVER_BASE_URL` is configured; otherwise returns static Gateway/env voice descriptors.
- `GET /api/gateway/audio/speech/models`: proxies AbstractCore `/v1/audio/speech/models` when configured.
- `GET /api/gateway/audio/transcriptions/models`: proxies AbstractCore `/v1/audio/transcriptions/models` when configured.
- `GET /api/gateway/audio/music/providers`: proxies AbstractCore `/v1/audio/music/providers` when configured.
- `GET /api/gateway/audio/music/models`: proxies AbstractCore `/v1/audio/music/models` when configured.
- `GET /api/gateway/vision/provider_models`: proxies AbstractCore `/v1/vision/provider_models` when configured.
- `GET /api/gateway/vision/models`: reports locally known/cached AbstractVision model ids when the in-process capability path is available.
- `GET /api/gateway/vision/adapters`: lists installed compatible vision adapters for a provider/model/task combination through Runtime's discovery facade.
- `POST /api/gateway/runs/{run_id}/images/generate`: creates a durable Runtime child run for text-to-image and returns an artifact-backed image result. Optional `size`/`width`/`height`, batch `count` / `n`, `seeds`, and ordered `lora_adapters` values are passed through only when the client supplies them. Batch responses also return `image_artifacts` alongside the compatibility `image_artifact`.
- `POST /api/gateway/runs/{run_id}/images/edit`: creates a durable Runtime child run for image-to-image edits and optional mask-guided edits. Optional `size`/`width`/`height`, batch `count` / `n`, `seeds`, and ordered `lora_adapters` values are passed through only when the client supplies them. Batch responses also return `image_artifacts`.
- `POST /api/gateway/runs/{run_id}/images/upscale`: creates a durable Runtime child run for image upscaling from a run-visible `image_artifact`. Optional `resolution` accepts a shortest-edge integer or a scale factor such as `2x`; `scale`, `softness`, `seed`, `quantize`, and `vae_tiling` values are passed through only when the client supplies them.
- `POST /api/gateway/runs/{run_id}/videos/generate`: creates a durable Runtime child run for text-to-video and returns an artifact-backed video result. Optional batch `count` / `n`, `seeds`, ordered `lora_adapters`, and `flow_shift` values are passed through only when the client supplies them. Batch responses also return `video_artifacts`.
- `POST /api/gateway/runs/{run_id}/videos/from_image`: creates a durable Runtime child run for image-to-video and returns an artifact-backed video result. Optional batch `count` / `n`, `seeds`, ordered `lora_adapters`, and `flow_shift` values are passed through only when the client supplies them. Batch responses also return `video_artifacts`.
- `POST /api/gateway/runs/{run_id}/music/generate`: creates a durable Runtime child run and returns an artifact-backed music result for thin clients.

Direct image, image-edit, image-upscale, text-to-video, and image-to-video child runs advertise
`event_name=abstract.progress`. Thin clients should stream the returned
`child_run_id` ledger and render progress when the backend reports it; image
backends that do not expose step progress still emit at least a start record and
then the final artifact.

Core catalog proxy settings:

- `ABSTRACTCORE_SERVER_BASE_URL`: explicit Core server base URL for catalog proxying.
- `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN` / `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY`
  (or Core's `ABSTRACTCORE_AUTH_TOKEN` / `ABSTRACTCORE_SERVER_API_KEY`): Core server auth token.
  This is separate from Gateway auth.
- `ABSTRACTGATEWAY_CORE_CATALOG_TIMEOUT_S`: catalog proxy timeout (default `3.0` seconds).

## CLI flags

`abstractgateway --help` shows all subcommands (serve/runner/migrate/triage/…).

Most-used:
- `abstractgateway serve [--host H] [--port P] [--data-dir DIR] [--no-runner] [--reload] [--no-tray]`
  (`--no-tray`: no menu bar / tray icon for this run, for a test or scratch
  gateway next to your usual one)
  (host/port default to the [network exposure](#network-exposure-localhost--local-network--internet)
  setting; with none stored, `--host` defaults to `127.0.0.1` when no auth is
  configured, else `0.0.0.0`, and `--port` to `8080`. Explicit flags override the setting.)
  Evidence: `src/abstractgateway/cli.py`
- `abstractgateway network status|set|addresses|restart`: who can reach the
  gateway and the URLs to copy ([network exposure](#network-exposure-localhost--local-network--internet))
- `abstractgateway claim [--open] [--port P | --url URL] [--json]`: one-time
  console sign-in link ([first-run.md](./first-run.md))
- `abstractgateway service install|uninstall|enable|disable|status [--port P] [--host H] [--pin-command-line] [--data-dir DIR] [--dry-run] [--json]`:
  start the gateway at login (LaunchAgent, systemd user unit or XDG autostart
  entry, Windows Run entry; `enable`/`disable` are the tray's switch;
  [first-run.md](./first-run.md#4-start-the-gateway-at-login-optional)). The
  registration runs plain `serve`; `--host/--port` are written into the
  [network exposure](#network-exposure-localhost--local-network--internet)
  setting, or onto the command line with `--pin-command-line`
- `abstractgateway runner` (worker only)
- `abstractgateway config status --json`
- `abstractgateway config get [KEY] [--json]`, `config set KEY VALUE`, `config unset KEY`:
  runtime settings from a terminal ([backlog folder, exec runner, process
  manager](#backlog-folder-exec-runner-and-process-manager-continuum), and every other key)
- `abstractgateway serve --backlog-root PATH --exec-runner on|off`: the backlog
  folder and the exec runner for this run (they win over the saved settings until the gateway stops)
- `abstractgateway migrate --from=file --to=sqlite --data-dir <DIR> --db-path <FILE>`
- `abstractgateway models loaded|load|unload --url <URL> [--provider P --model M] [--force]`
  (model residency on a running gateway; see [console.md](./console.md#model-residency-from-a-shell))

## Related docs

- First run: [first-run.md](./first-run.md)
- Getting started: [getting-started.md](./getting-started.md)
- FAQ: [faq.md](./faq.md)
- Security configuration: [security.md](./security.md)
- Deployment: [deployment.md](./deployment.md)
- API overview: [api.md](./api.md)
- Operator tooling env vars: [maintenance.md](./maintenance.md)
