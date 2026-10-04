# AbstractGateway — Security guide

AbstractGateway secures the **gateway API surface** (`/api/gateway/*`) using an ASGI middleware:
`GatewaySecurityMiddleware` in `src/abstractgateway/security/gateway_security.py`.

Notes:
- `/api/health` is intentionally not protected.
- `/api/triage/action/*` uses signed action tokens and is not under `/api/gateway` (see `src/abstractgateway/routes/triage.py`).
- Vulnerability reporting policy: see [../SECURITY.md](../SECURITY.md).

## Default behavior

Security is on by default for `/api/gateway/*`:

- A plain `abstractgateway serve` with no auth posture in its environment binds
  `127.0.0.1` (or the stored network mode), turns **user accounts** on and
  creates the admin account `default/admin`
  ([first-run.md](./first-run.md)).
- Choosing `lan` or `internet` with the [network setting](#network-exposure)
  keeps user accounts on.
- An explicit non-loopback `--host` with neither user accounts nor a token
  refuses to start, and so does a weak shared token on a non-loopback bind.

Evidence: startup self-checks in `src/abstractgateway/cli.py`,
`src/abstractgateway/first_run.py`.

Explicit browser-console/browser-app setup:

```bash
export ABSTRACTGATEWAY_USER_AUTH=1
export ABSTRACTGATEWAY_DATA_DIR="$PWD/runtime/gateway"
abstractgateway serve --host 127.0.0.1 --port 8080

# Use this with Gateway user admin.
cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token"
```

API clients can send a Gateway user token:

```text
Authorization: Bearer <token>
```

### Tenant and user isolation

In token mode, `ABSTRACTGATEWAY_AUTH_TOKEN` is a gateway-level control-plane
token and maps to the `local-admin` principal. Treat that token as
full authority for the Gateway instance.

Hosted user-auth mode is enabled with `ABSTRACTGATEWAY_USER_AUTH=1` or
`ABSTRACTGATEWAY_AUTH_MODE=users`. In that mode, Gateway bearer tokens resolve
to concrete principals with `tenant_id`, `user_id`, roles/scopes, and a token
fingerprint. `GET /api/gateway/me` returns the resolved principal and routing
mode. The presence of an `auth/users.json` registry file is readiness state; it
does not silently enable hosted user auth unless `ABSTRACTGATEWAY_USER_AUTH_AUTO=1`
is set for compatibility. Admin principals can manage users through:

- `GET /api/gateway/admin/users?kind=human|entity|all` (default `all`)
- `POST /api/gateway/admin/users`
- `GET /api/gateway/admin/users/{user_id}?tenant_id=...`
- `PATCH /api/gateway/admin/users/{user_id}?tenant_id=...` (`enabled` is the Active switch:
  false = signed out and unable to sign in; an admin cannot deactivate their own account,
  `409 cannot_deactivate_self`, nor the last active admin, `409 last_admin`; `email` is the
  user's email address)
- `DELETE /api/gateway/admin/users/{user_id}?tenant_id=...`
- `GET /api/gateway/admin/runtime-reservations`
- `POST /api/gateway/admin/runtime-reservations/{runtime_id}/transfer`
- `POST /api/gateway/admin/runtime-reservations/{runtime_id}/purge`

Every user row carries a first-class `principal_kind` field (`"human"` or
`"entity"`); clients must read it (or the `kind` filter) instead of
re-deriving kind from the `roles` convention. Census asymmetry is deliberate:
`GET /api/gateway/entities` is the ENTITY census (homes on disk), while
`?kind=entity` here is the entity-PRINCIPAL census — homes created before
principal minting have no user row, so the two lists can legitimately differ
and neither may be derived from the other.

Entity principals (minted at entity creation) are shaped by the entities
lane, not the users lane: `PATCH` refuses `token`/`rotate_token`/`roles`/
`runtime_id` and `DELETE` refuses outright (HTTP 403 naming the lane). A
rotation would mint a live entity bearer that by design must not exist, and
a delete would remove the name-collision guard protecting the entity's
identity. `enabled` (the door-side disable), `email`, and `scopes` stay
editable. The guard lives in `GatewayUserRegistry` itself, so the config CLI
refuses the same writes.

Gateway stores user token hashes in `<ABSTRACTGATEWAY_DATA_DIR>/auth/users.json`
by default. Generated or rotated bearer tokens are returned once from the admin
create/update response and are never stored in plaintext.

Browser apps should exchange user bearer tokens for Gateway browser sessions
instead of storing bearer tokens. `POST /api/gateway/session/login` accepts a
Gateway user id and user token, validates them against the registry, and sets an
opaque signed session id plus a CSRF token as cookies. The JSON response body
does not expose those values. Gateway stores session records in
`<ABSTRACTGATEWAY_DATA_DIR>/auth/sessions.json` by default.
The session cookie is HTTP-only; the CSRF cookie is readable by the hosting app
so it can send the CSRF header. Both cookies use path `/` and `SameSite=Lax`.
Plain HTTP local-dev responses do not set `Secure`; HTTPS responses, including
requests forwarded with `X-Forwarded-Proto: https`, do set `Secure`.
Non-remembered sessions omit `Max-Age`; remembered sessions include one.
Session-authenticated mutating requests must send:

```text
X-AbstractGateway-Session: <session id>
X-AbstractGateway-CSRF: <csrf token>
```

`POST /api/gateway/session/logout` revokes the session. Disabling, deleting, or
rotating the Gateway user invalidates existing browser sessions for that user.

**Who can sign in depends on whether user accounts are on.** With user
accounts on, every registry account (admin or not) can sign in, and each one
works in its own runtime (below). With user accounts off the gateway runs one
runtime, the operator's, so every signed-in person would share the operator's
runtime, capability defaults, endpoint profiles and workflows. In that mode
only accounts with the `admin` role can hold a browser session:

- `POST /api/gateway/session/login` answers `401` for a non-admin account, with
  `reason_code: "user_accounts_off_admin_only"` and a message naming the two
  ways out: the gateway operator turns user accounts on, or the person signs
  in with an admin account.
- A session that already exists for a non-admin account (for example one
  created while user accounts were on) is refused and removed at its next
  use. Every session path applies the same rule (`principal_barred_from_shared_runtime`
  in `security/sessions.py`), including the browser-app sign-in handover.
- `POST /api/gateway/admin/users` answers `409` (same `reason_code`) instead of
  creating a non-admin account that could never sign in, and
  `PATCH /api/gateway/admin/users/{user_id}` refuses to remove the `admin` role
  from an admin account in this mode.

Admin accounts sign in in both modes. The rule reads the same setting the
service routing reads, so the two cannot disagree.

**The last admin account is protected.** `DELETE /api/gateway/admin/users/{user_id}`,
and a `PATCH` that disables it or removes its `admin` role, answer `409` with
`reason_code: "last_admin"` when the target is the only enabled admin account
left (entity principals never count). Create or enable another admin first.

When user auth is active, the Gateway service composition root routes each
principal to an isolated service/data plane under:

```text
<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/runtime
<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/flows
```

Gateway rejects duplicate `runtime_id` values within the same tenant during
user creation and update. This keeps the default multi-user invariant at
`1 user = 1 runtime`. Deleting a user removes the credential but reserves the
retained runtime id for that principal, so another same-tenant user cannot be
assigned to retained data by accident. Reusing the same runtime id in a
different tenant remains valid.

Admins can intentionally resolve retained runtime reservations through
admin-only lifecycle routes. Transfer assigns a retained runtime to an existing
same-tenant user and reserves that user's previous runtime id. Purge requires an
exact `confirm_runtime_id`, deletes the retained runtime root under
`<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant_id>/<runtime_id>/`, then releases the
runtime id for reuse. Regular users cannot list, transfer, or purge retained
runtime reservations.

Clients must not send authoritative `user_id`, `tenant_id`, `runtime_id`, or
workspace-root values. Runtime fields such as `actor_id` and `session_id`, and
references such as `run_id`, `artifact_id`, and memory `owner_id`, remain
correlation and lookup fields; they do not authorize access by themselves.

In hosted multi-user mode, the request path resolves a principal and routes it
to its own service. Gateway also applies a central
route-family authorization table for operator/admin surfaces. Admin-only route
families include user management, audit, process control, backlog/triage/report
operations, the deprecated `/email/*` aliases (the calling admin's own mailbox),
the per-user email switch, model residency mutations
(`POST /models/load|unload|lock|unlock|download`), session-wide prompt-cache
clearing (`POST /sessions/{session_id}/prompt_cache/clear_all`), server
workspace file helpers, server-workspace artifact import/export, and global
prompt-cache/bloc mutation routes. Host and residency reads —
`GET /models/loaded`, `GET /models/context_estimate`, `GET /host/state`,
`GET /host/metrics/*`, and `GET /sessions/prompt_cache` — are visibility every
authenticated client needs and serve any authenticated principal; anonymous
requests remain rejected. Regular users remain able to use
their own runtime data plane for run, ledger, artifact upload, discovery, and
runtime-scoped Core capability-default routes.

The route table is intentionally conservative around server filesystem access:
browser-local files should use `/api/gateway/attachments/upload`; server
workspace reads/imports/exports require an admin principal until a stronger
per-user workspace grant model exists. The one user-level exception is a run's
own folder: the person who started a run can list and preview that run's
workspace (`GET /runs/{run_id}/workspace`, `/files`, `/content`), confined to
it and with the built-in deny list applied
([api.md](./api.md#a-runs-workspace-folder-browse-and-preview)).

Capability discovery follows the same policy. Regular users can still discover
ordinary run, ledger, artifact, upload, provider/model catalog, KG, and
runtime-scoped defaults surfaces, but admin-only workspace artifact
import/export and provider prompt-cache controls are advertised as unavailable
with machine-readable `admin_required` metadata. Session-level prompt-cache
keys remain available for users; the private hash includes the current
principal scope, so two users using the same session id/provider/model tuple do
not collide in a shared provider control plane.

Hosted provider secrets are supported through Gateway provider connections.
Connections are stored under the relevant Gateway data plane, expose only
non-secret metadata and a virtual provider id such as `endpoint:office-vllm`,
and inject the raw key only into the transient Runtime provider call. Normal
users can manage user-scoped connections; Gateway-scoped connections require an
admin principal. The current capability-default cascade uses execution-host
Core defaults, then the Gateway/root Core config baseline, then the user's
runtime Core config override under that user's Gateway data plane. A stronger encrypted vault, audit model, and
bridge/delegated-tool propagation policy remain future hardening work.

### Who sees which account

An admin sees every user and every entity (`GET /api/gateway/admin/accounts`,
admin-only: other accounts get 403). Anyone else sees only their own account
and the entities they created (`GET /api/gateway/me/accounts`, and
`GET /api/gateway/me/accounts/{id}/activity` for their own activity or an own
entity's). The console's Accounts page follows the same rule.

- `POST /entities` records the creator (`created_by: {tenant_id, user_id}`) in
  the new entity's manifest. Entities created before this field existed have
  no creator and are visible to admins only; no creator is guessed and no
  manifest is rewritten.
- Every entity route checks visibility first. An entity you may not see
  answers exactly like a missing one (404, same message), so names cannot be
  probed.
- Entity names belong to the whole gateway: creating an entity under a name
  another account already holds (an entity in any runtime, or a user account)
  answers 409 "That name is taken".
- Seeing an entity is not managing it: admin-only entity writes (state, tool
  policy, prompt, substrate, …) stay admin-only for the entities you created,
  and only an admin can suspend an entity or rotate a user's token.
- A gateway without user accounts is one shared world: every entity is visible.

An entity has no token to rotate (its credential is discarded when it is
created) and cannot be deleted (its name is kept for life); an admin suspends
it instead. Details: [api.md](./api.md#who-sees-which-account).

### Per-user email

Each user's mailbox ([email.md](email.md)) lives in that user's own data plane,
resolved from the authenticated principal on every route; no path or body id
selects another user's account, and entities have none.

- **Credentials** (password or OAuth tokens) are sealed with AES-256-GCM
  (`<plane>/email/account/email/secret.enc`, 0600 in a 0700 folder); the key is
  in the OS keychain, or a 0600 key file next to it on hosts without one. They
  are used in memory at the moment of a connection and never enter run
  variables, ledgers, events, logs, the audit log or API responses. The
  encryption protects copies of the data folder, backups and file-reading tools;
  code running as the gateway's OS user can reach the key.
- **TLS** is verified on every IMAP, SMTP and OAuth connection (certificate
  chain and host name); there is no plaintext mode. A private CA file is an
  administrator setting.
- **OAuth endpoints** come from the provider preset for Google and Microsoft;
  endpoint and scope overrides are refused (`403 email_oauth_override_refused`),
  for administrators too. Only an administrator may use `provider: "custom"`,
  which brings its own client. The gateway's and the built-in OAuth client are
  sent only to their provider's own endpoints.
- **Turned off by an administrator** means no connection at all: the user's
  Connect, Test and OAuth sign-in are refused before any connection.
- **Administrators** can turn email on or off per user and see its state,
  address and last error — never messages, recipient policies or credentials.
- **Sending** always passes the user's recipient policy (allowlist or denylist)
  and send limits; an agent's send to anyone but the user waits for approval.
- **Inbound mail** is untrusted data: it reaches a model inside a fixed frame and
  cannot change a run's recipients or tools. The mailbox is only read.
- **Availability** is the administrator's: `email`, `email_agent_tools` (off by default) and
  `email_recovery` are gateway-wide defaults with per-user overrides; agents get email tools only
  when available, connected and switched on by the user — checked when toolsets are built and
  again on every tool call (the runtime's own send actions only need the account).
- **Recovery by email** trusts the mailbox: whoever controls a user's mailbox can sign in as that
  user. It is on by default for users with email; administrators can turn it off gateway-wide
  (`email_recovery`).
- **Recovery codes** are single use, expire after 10 minutes, are stored as
  keyed hashes, are rate-limited per account and client address, and the request
  answer never reveals whether an account exists or has email. Issue and use are
  audited without the code.
- **Audit**: typed events (`email.connected`, `email.tested`, `email.disconnected`,
  `email.capability_changed`, `email.cursor_reset`, `email.message_unprocessable`,
  `email.notification_sent|failed`, `email.recovery_code_issued|used|refused`)
  in `<data_dir>/audit_log.jsonl`.

A user-supplied IMAP/SMTP host is a connection the gateway makes on that
user's behalf; deployments that must restrict outbound destinations should do
so at the network layer.

### Workflow registry ownership

Writing a workflow registry requires owning it. Under hosted user auth the
`/api/gateway/bundles` routes resolve to the calling principal's own bundle
directory, which that user may change freely. The gateway's own directory is
the shared set every user can see and run, so changing it requires an admin
principal.

One check covers every route that writes a registry — `POST /bundles/upload`,
`POST /bundles/reload`,
`POST /bundles/{bundle_id}/deprecate`, `POST /bundles/{bundle_id}/undeprecate`
and `POST /visualflows/{flow_id}/publish` — so a shared workflow cannot be
replaced through one route while another is restricted. The check runs before
the route looks the bundle up, so a non-admin gets `403` for a bundle that
does not exist as well. Since a non-admin account cannot be signed in while user accounts are off
(above), this check is the second, independent line of defence. `POST /visualflows/{flow_id}/publish` accepts a caller-supplied
`bundle_id`, `bundle_version` and `overwrite`, and installs into the same
registry as `upload`; it is gated on the same rule. Non-admin requests against
the shared registry return `403`. Read routes are unchanged.

Workflows are archived, never deleted: `DELETE /bundles/{bundle_id}` answers
`410` for everyone. `POST /bundles/{bundle_id}/archive` and `/unarchive`
(optional body `{"bundle_version": "..."}`) hide a bundle from lists and refuse
its new runs while the file and every past run stay; an admin archives shared
bundles, a user their own, and bundles that ship with the gateway (including
`basic-agent`) answer `409`. `PATCH /bundles/{bundle_id}` `{"description"}`
follows the same rule (the owner, an admin for shared bundles, `409` for
shipped ones) and is audited as `workflow.description`.

### Workflow availability

`PUT /api/gateway/admin/workflows/{bundle_id}/availability` with
`{"available": false}` (admin only; `403` otherwise) hides a shared workflow
from non-admins. One rule applies everywhere: `GET /bundles` (the list every
app picker reads), `GET /bundles/{bundle_id}`, its flows and download, run start,
scheduling and automation creation (`403` with "This workflow isn't available to
users on this gateway. Ask an admin."). An app's default workflow keeps running
for everyone. The setting is stored in
`<data_dir>/config/workflow_availability.json`; archives in
`<data_dir>/config/workflow_archive.json` (shared bundles) and
`<data_dir>/users/<tenant>/<user>/config/workflow_archive.json` (a user's own).

### Shared workflow catalog

Do not share workflows by pointing multiple users at another user's private
bundle directory. Private `/api/gateway/bundles` routes stay scoped to the
current principal's runtime. Shared/default workflows belong in the Gateway
workflow catalog:

- catalog versions are immutable by `scope + tenant + bundle_id +
  bundle_version + sha256`;
- admins move explicit default pointers instead of overwriting existing
  versions;
- catalog ACLs are checked at run start against the authenticated principal's
  tenant, roles, and user id;
- catalog runs execute in the requesting user's runtime by default;
- catalog run policy is Gateway-issued and HMAC-signed before it is handed to
  Runtime state; client-supplied `_runtime.workflow_policy` values are stripped;
- private bundle inspection routes reject catalog-internal bundle ids, so
  catalog flow/schema inspection remains ACL-aware;
- deprecate/block/tombstone changes block new starts without deleting stored
  bundle bytes.

Catalog mutation routes are admin-only under
`/api/gateway/admin/workflow-catalog/*`. User-visible catalog discovery is
available at `GET /api/gateway/workflow-catalog`.

## Origin allowlist (browser/origin defense)

If the request includes an `Origin` header, the middleware allows it only when
it matches the allowlist (glob-style patterns, fnmatch). The allowlist is
`http://localhost:*` and `http://127.0.0.1:*`, the pages served at the
addresses the gateway detects (`http://<interface address, Bonjour or Tailscale
name>:<listening port>` and `https://<Tailscale name>`, refreshed every 60 s;
see [configuration.md](./configuration.md#detected-addresses-are-accepted-origins)),
plus the **`allowed_origins` setting** (console: Network → *Advanced*; TUI:
Connection screen; CLI:
`abstractgateway network set --allowed-origins https://gateway.example.com`).
The setting is read per request: a change applies to the next request, no
restart. Each origin is validated (`scheme://host[:port]`, no path, no trailing
slash); `*` and wildcard patterns are accepted only as typed and are flagged.
See [configuration.md](./configuration.md#reverse-proxy-allowed-origins-and-trust-proxy).

One more case is allowed without a list entry: an https page asking its own
address. The Origin is `https://` plus the request's `Host`, and the request
itself arrived over TLS: either native TLS, or `X-Forwarded-Proto: https`
from a proxy on the gateway machine (the only peer whose forwarded headers
are believed). This is `tailscale serve` or a local nginx that keeps the
browser's `Host`. DNS rebinding cannot produce it. The browser writes the
Origin, a rebinding page is plain `http://`, and an `https://` rebinding page
would need a certificate for its own name from the proxy on this machine.

A gateway started with `ABSTRACTGATEWAY_ALLOWED_ORIGINS` in its environment
uses that list instead (a deployment pin): every surface says "This gateway was
started with ABSTRACTGATEWAY_ALLOWED_ORIGINS in its environment" and reports
`overridden_by_env: true`; the saved setting applies once it starts without it.

Evidence: `GatewayAuthPolicy.allowed_origins`, `_effective_allowed_origins()`,
`_origin_allowed()` and `_https_same_origin()` in `src/abstractgateway/security/gateway_security.py`;
`live_reverse_proxy()` in `src/abstractgateway/network_exposure.py`.

Important nuance:
- FastAPI’s CORS middleware in `src/abstractgateway/app.py` is permissive, but **origin enforcement for gateway endpoints is done by this security middleware**.
- In a network exposure mode from the settings store, `serve` adds the
  gateway's own discovered LAN origins (IP literals and `<name>.local`). A
  foreign origin, including a DNS-rebinding name that resolves to your LAN IP,
  is still refused (403) unless it is in `allowed_origins`.

## Network exposure

The network exposure setting ([configuration.md](./configuration.md#network-exposure-localhost--local-network--internet))
chooses `localhost`, `lan` or `internet`. What changes for someone else on
your network:

- **`localhost`** (default for a first run): the gateway listens on
  `127.0.0.1` only. Nobody else can open a connection; every local process of
  every local user still can, which is why user auth stays on.
- **`lan`**: the gateway listens on every IPv4 interface. Anyone on the same
  network (and anyone on a VPN such as Tailscale whose address is listed) can
  reach the sign-in page and the API. The gate is authentication: `lan` is
  refused unless user auth will be on at the next start; unauthenticated
  requests answer 401, failed credentials are locked out per client address
  with a growing wait, and a browser page from a foreign origin is refused
  (403). `lan` and `internet` are also refused when the gateway was started
  with read protection off (`ABSTRACTGATEWAY_PROTECT_READ=0`: unauthenticated
  reads would be answered as the admin). What `lan` does NOT give you:
  - **encryption**: it is plain HTTP. Passwords, bearer tokens and the
    session cookie cross the network in clear; the session cookie is
    `HttpOnly; SameSite=Lax` but not `Secure` over HTTP. Anyone who can sniff
    the network (shared Wi-Fi, a compromised router) can capture and replay a
    session. Use `lan` on networks you trust, or use a TLS proxy / VPN.
  - **a smaller attack surface**: every admin route is reachable to whoever
    holds an admin credential. Give each person their own account, keep the
    admin token off other machines, and prefer non-admin accounts for daily use.
  - **exposure of the browser apps**: apps always listen on `127.0.0.1` and
    are reached through the gateway at `/apps/<app>/`, which requires a
    gateway session for that app on every request (a signed-out page load
    goes to the console, anything else gets 401), relays only the app's own
    cookies (never the console's session or an `Authorization` header), and
    tells the app the browser's real address (`X-Forwarded-For`, written by
    the gateway). The sign-in handover (`/apps/handover/{code}`) only works on
    the host it was minted for. An app version that does not announce it can
    be served this way is never served under `/apps/` and stays reachable
    from the gateway machine only.
  - **engine and app installs for remote admins**: `allow_engine_install`
    defaults to off on a non-loopback bind for callers on other computers.
    Someone at the gateway machine itself can still install (see
    [Callers on this computer](#callers-on-this-computer)).
- **`internet`**: the same bind plus an explicit acknowledgement. The gateway
  does **not** terminate TLS and does not configure your router or firewall.
  Put a TLS reverse proxy (Caddy, nginx, Traefik) or a tunnel (Cloudflare
  Tunnel, Tailscale Funnel, ngrok) in front and expose that; add the public
  `https://` origin under *Reverse proxy* (`allowed_origins`), turn on *Trust
  the proxy's client address* (`trust_proxy`) only when your own proxy is in
  front of every request, and rate-limit at the proxy. Forwarding the raw port
  means plain HTTP on the internet: do not.

The mode is applied at the next start and `serve --host/--port` override it;
`GET /api/gateway/network` always says what is configured, what is running,
and why they differ.

## Callers on this computer

Some defaults belong to the person sitting at the gateway computer and not to
the rest of the network: installing engines and apps
([`allow_engine_install`](./configuration.md#allow_engine_install)), opening a
run's folder in the file manager (`open_supported` on
`GET /runs/{run_id}/workspace`), and the `caller_is_this_machine` fact the
workspace routes report. One rule decides them all
(`src/abstractgateway/security/same_machine.py`):

- The caller's address is the socket peer. `serve` runs uvicorn with
  `forwarded_allow_ips` pinned to `127.0.0.1` and `::1` (a
  `FORWARDED_ALLOW_IPS` value in the environment is ignored with a warning),
  so `X-Forwarded-For` is believed only from a peer on this computer.
- The browser apps' servers (the AbstractCode web server and the AbstractUIC
  app server) run on this computer and relay requests with the browser's real
  address in `X-Forwarded-For` (overwritten, never appended) and their marker
  header `X-AbstractFramework-App-Proxy: <app id>`. A browser on another
  computer that opens an app is therefore not local; a browser on this
  computer is.
- The caller is local when that address is loopback or one of this host's own
  interface addresses. A remote computer cannot use this host's own address
  as the source of an established TCP connection.

Fail-safes:

- A request with the app-proxy marker but no `X-Forwarded-For` is never local
  (a proxy that dropped the header would make every browser look local).
- While the gateway trusts a reverse proxy (`trust_proxy`), a request relayed
  by an app server is never local: the reverse proxy hides the browser's
  address. For this rule the saved `trust_proxy` setting decides first; the
  `ABSTRACTGATEWAY_TRUST_PROXY` launch environment counts only when nothing
  is saved.
- A forwarded header (`X-Forwarded-For`, `Forwarded`, `X-Forwarded-Host`,
  `X-Real-IP`) sent by a peer that is not on this computer is never local,
  and neither is a request that carries a proxy header other than
  `X-Forwarded-For` (a proxy this rule cannot read).

Native clients on this computer (the web console, the terminal console, the
Assistant, the tray) call the gateway directly, so their loopback peer
decides.

### Desktop Assistant sign-in

The Assistant opened from the console or the tray is signed in through a
one-time code the gateway writes into a file only your account can read
(`<data dir>/handover/<random>.json`, mode 0600) and names on the Assistant's
command line (`--gateway-handover-file`); the code itself is never on a command
line or in the environment. The Assistant trades it at
`POST /api/gateway/apps/desktop-handover`, which answers only a direct caller
on this computer (no proxy header and no app-server session header; 403
otherwise), once, within two minutes (410 after). The resulting session is a
remembered session (30 days) of the user who clicked Open. See
[apps.md](./apps.md) and
[architecture.md](./architecture.md#desktop-assistant-hand-over).

## Workspaces: three levels

Thin clients (browser apps, bridges, the Assistant) start runs whose file tools
(`list_files`, `read_file`, `write_file`, …) touch the gateway's computer. A
workspace is a directory an agent may work in. Which workspaces a run may use is
decided by the gateway, never by the client, at three levels that share one
shape: a **posture**, a **default mode** and **rows**, each row Read-only
(`ro`), Read & write (`rw`) or Refused (`deny`).

- **Deny everything, allow listed workspaces** (`allowed_only`): nothing is
  reachable except the listed workspaces. A refused row carves a sub-directory
  out of a listed one.
- **Allow everything, refuse listed workspaces** (`any_except_denied`): every
  directory is reachable at the default mode, except the listed workspaces,
  which are refused or carry their own mode.

The levels:

1. **Gateway (admin) = the eligible set.** `GET`/`PUT
   /api/gateway/workspace/policy`. Each row's mode is a CAP. The built-in
   refusals (the gateway's data folder, credential folders such as `~/.ssh`)
   always apply. A fresh gateway allows everything, read & write.
2. **Account = the account's default subset** (people and entities alike):
   `GET`/`PUT /api/gateway/workspace/policy/{account}`. The account picks its
   own posture within the eligible set and its own rows, each inside the set
   and at most at its cap. Not configured = the gateway policy as is. A person
   sets their own; an entity's is set by an admin or the entity's creator.
3. **Session = one conversation's subset**, and **run = a one-off payload**
   (Flow's run window, Observer's launch, an automation): the same shape,
   checked against the gateway's eligible set (not the account default), stored
   by the gateway on the conversation (`GET`/`PUT
   /api/gateway/sessions/{id}/workspaces`) or carried in the start body
   (`workspace`).

A run gets the first that applies: run > session > account > gateway. For every
path its mode is the lower of the gateway's cap and that level's rule (refused
< read-only < read & write). **Nesting: the most specific row wins** (the
longest real-path prefix), refused rows included. Refusing `/Users/me` while
allowing `/Users/me/projects` (read & write) is valid at every level: the
child is reachable and the rest of `/Users/me` is refused. A refused row inside
an allowed one refuses that subtree. Caps still bind: a row below the gateway
never exceeds the gateway's cap at its path (the gateway's most specific row
there), so a child of a read-only gateway row stays read-only, and a child of a
refused gateway row is eligible only where the gateway lists it. The built-in
refusals are absolute: a read-only or read & write row inside one is refused
at every level with `'<path>' is inside the built-in refused workspace
'<built-in>'.` (the one exception: a conversation folder of the account's own
data plane, below). The effective set, its line, the dry run, the run's file
tools, the server file routes, the workspace browser and the command sandbox
all apply this one rule. `GET
/api/gateway/workspace/effective/{account}[?session=]` returns the result and
one line that every surface shows verbatim, for example
`Deny everything, allow listed workspaces · /Users/me/Pictures (rw) · /Users/me/Documents (ro)`,
next to the gateway's own line (`gateway_summary`), for example
`Allow everything, refuse listed workspaces (rw) · /secrets (refused) · /archive (ro)`.
A write outside the eligible set or above a cap is refused (400
`workspace_refused`, one sentence, the offending path); the API is in
[api.md](./api.md#workspaces).

Each conversation also keeps its own **private workspace** in its account's
data plane (protected by the built-in refusals, so another account's agents
never read it). It is always read & write for that run and never listed. A run
that names no workspace works there: a relative path such as `out.txt` is
written to `<data dir>/workspaces/session-…/out.txt` (a run without a
conversation gets its own `<data dir>/workspaces/<run>` folder). The run's
allowed workspaces are listed to the agent with their paths and modes, in the
workspace context of every tool-using call:

```text
Default working directory: "<data dir>/workspaces/session-…"
Allowed workspaces:
  "/Users/me/Pictures" (read & write)
  "/Users/me/Documents" (read-only)
```

Under "Allow everything, refuse listed workspaces" a last line gives the mode
of everything else, for example `Everything else: (read-only)`. Refused
workspaces are never listed as allowed. There is no "Shared workspace" line any
more (a stale `workspace_shared_path` sent by a client is dropped).

Enforcement reads only the effective set:

- **Run starts** (`POST /runs/start`, `/runs/schedule`, entity summons, the
  automation definitions): a one-off `workspace` outside the eligible set or
  above a cap is refused; a `workspace_root` (for example the folder an app was
  launched from) is accepted only when the run reaches it; a legacy
  `workspace_allowed_paths` list may only narrow; a client that sends
  `workspace_access_mode: "all_except_ignored"` is refused. Refusals are 400s
  with a sentence; nothing is silently dropped. A launch folder gets no special
  trust: when the run does not reach it, a client asks the person to add the
  workspace.
- **Every run's tool sandbox**, whatever started it (HTTP, the Telegram, email
  and agora bridges, schedules, automations, entities), is bound by the host
  (`run_workspace_guard.apply_workspace_policy`), which resolves the level
  again and CLAMPS a forwarded one-off (rows outside the set dropped, modes
  lowered), never widens, and never silently: each clamped row is recorded on
  the run with its sentence (`_gateway_workspace.clamped`, shown by
  `GET /runs/{id}/workspace`):
  - "Deny everything, allow listed workspaces" at either level →
    `workspace_or_allowed` with the reachable workspaces.
  - "Allow everything, refuse listed workspaces" at both levels →
    `all_except_ignored`. Only the gateway sets this mode, never a client.
  - Refused rows → `workspace_ignored_paths`. AbstractRuntime resolves a path
    by the longest prefix among the run's own folder, the allowed paths and the
    refused paths (a tie is refused), so a refused parent with an allowed child
    reaches the runtime exactly as the gateway computed it.
  - Read-only workspaces → `workspace_read_only_paths`.
  - A read-only default → every directory is read-only except the run's own
    folder and the read & write workspaces (`workspace_writable_paths`,
    AbstractRuntime; a client's own value is dropped, and child runs inherit
    the parent's exactly). Writes into a read-only workspace are refused with a
    sentence; reads work.
  - The level and its line are recorded on the run (`_gateway_workspace.{level,
    summary}`), so the ledger and replay show what the run could use.
- **A workspace inside the gateway's data folder** (for example another
  conversation folder of the same account) counts only for the account whose
  own data plane holds it. The host lifts the built-in data-folder deny for
  that path, for that account only, never for another account's plane.
- **The run workspace browser** serves a launch folder only while the run still
  reaches it. **The server file routes** (`/files/*`, admin) use the given root
  or the first read & write workspace as their base and the other reachable
  workspaces as mounts; exports into a workspace that is not read & write are
  refused.

Every change is recorded in the audit log as `workspace_policy_changed`
`{scope: gateway|account|session|migration, actor, changed, account?, session_id?}`.

**Every entity has a gateway account.** Entities get theirs at creation; homes
created before entity accounts existed get one at serve start, once, minted
like a creation (roles `entity`, token discarded) and audited as
`entity_account_created` (reason `migration`). A record of the same name that
is not an entity account is never adopted.

**Migrations** run once, at serve start or at the first read, and never reach
beyond the new ceiling:

- From round 9 (shared workspace + "accounts narrow only"), `_migrated.workspace_policy_v2`:
  the gateway posture becomes "Allow everything, refuse listed workspaces"
  (operator decision), keeping its default mode; the old shared workspace
  becomes a listed read & write row; existing rows are kept with their modes as
  caps; each account entry becomes a configured account layer under the same
  posture with its own read-only/refused rows and lowered default.
- From the older model (whitelist/blacklist access modes, per-user allow/deny
  lists, launch-folder trust, "Any folder (old clients)"),
  `_migrated.workspace_policy_v1` first, then v2: the old workspace root (only
  when one was configured) becomes the listed read & write row; the old extra
  workspaces and every account's allowed folders become read & write rows; an
  account that did not have a folder another account had gets a refused row for
  it; old refused folders become refused rows (gateway's or the account's);
  launch folders trusted under the old model are not added.

Missing folders are dropped and listed in the settings store under
`_migrated.workspace_policy_v1` / `_v2`, next to the old blocks. The old runtime-config
keys (`workspace_root`, `workspace_mounts`, `workspace_allowed_paths`,
`workspace_blocked_paths`, `workspace_default_mode`, `trust_client_launch_folder`,
`client_workspace_scope_overrides`, `user_workspace_policies`) are refused on
write; the `ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE` /
`ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE` variables are no longer read.

### Built-in deny list

Whatever the workspace policy allows, the gateway's data folder and the
credential and configuration folders of the gateway's user account (`~/.ssh`,
`~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`, `~/Library/Keychains`,
`~/.abstractgateway`, `~/.abstractcode`, `~/.abstractassistant`,
`~/.abstractcontinuum`, `~/.abstractcore`) are:

- never listed or served by the workspace browser, for anyone, even when a
  run's folder contains them;
- denied to every run's file tools, as whole-folder rules
  (`workspace_builtin_deny_prefixes`) with the run's own folder inside the
  data folder as the one exception (`workspace_builtin_allow`). Clients cannot
  send these two entries (they are dropped), and the rules are enforced
  without being written into the model's prompt. An admin can turn the run
  side off with the `workspace_builtin_deny` setting; the browser keeps hiding
  the folders.

Evidence:
- Policy model (three levels), effective set, migrations: `src/abstractgateway/workspace_policy.py`; the session level: `src/abstractgateway/session_workspaces.py`; entity accounts: `src/abstractgateway/entity_accounts.py`
- Every run start: `src/abstractgateway/run_workspace_guard.py` (called from `WorkflowBundleGatewayHost.start_run`)
- Client scope clamping: `src/abstractgateway/routes/gateway.py` (`_sanitize_run_workspace_policy`, `_files_scope`, `_browse_workspace_root`)
- Browse and preview: `src/abstractgateway/workspace_browse.py`
- Runtime tool scoping: `abstractruntime/integrations/abstractcore/workspace_scoped_tools.py`
- Tests: `tests/test_gateway_workspace_policy_r11.py`, `tests/test_r11w1_levels.py`, `tests/test_r11w1_real_boot_migration.py`, `tests/test_gateway_workspace_policy_enforcement.py`, `tests/test_r12w2_nesting.py` (the nesting rule)

Canonical public server paths use `rel/path` for the base workspace and
`mount_alias/rel/path` for the other folders. When two folders share the same
basename, Gateway emits deterministic digest-suffixed mount aliases so the
public path string stays stable across discovery, import/export, and Runtime
execution.

## Command sandbox

Every tool that starts a process (`execute_command`, `shell_exec`,
`execute_python`, the local helpers: AbstractRuntime's `SANDBOXED_TOOL_NAMES`) runs inside an
**operating-system sandbox** built from the run's effective workspaces. These
are the same keys the file tools read, so the two cannot disagree: the run's
private workspace read & write, the allowed workspaces with their modes, the
refused workspaces, the built-in refusals and the posture's default mode. The
command string is never parsed. `cd`, `$(…)`, symlinks, scripts and
interpreters are all confined by the kernel:

- **macOS**: `/usr/bin/sandbox-exec` with a generated profile.
- **Linux**: bubblewrap (`bwrap`) when installed; Landlock (kernel 5.13 or
  later) for "Deny everything, allow listed workspaces" when bubblewrap is
  missing.
- **Anything else** (Windows, Linux without either): the command is
  **refused** with one sentence and the run continues.

Every command starts from the gateway's **scrubbed environment**. This is the
same scrub the gateway applies to the apps it starts: nothing named
`ABSTRACTGATEWAY_*` / `ABSTRACTCORE_*`, and no `*_TOKEN`, `*_SECRET`,
`*_API_KEY`, `*_PASSWORD` or `*_KEY`. Each command also gets a private
`TMPDIR` inside the run's workspace. The gateway sets this host policy once
per process at boot (`command_sandbox.configure_at_boot`, from
`start_gateway_runner`; AbstractCore's `configure_host`). From then on a
spawning tool call that does not carry a run's sandbox is refused.

**`abstractgateway serve --unsandboxed-commands`** (also on the split
`abstractgateway runner`) re-enables commands on a host with **no** sandbox.
They then run with the gateway's own file access, still with the scrubbed
environment. The flag is off by default and has no environment variable. Where
a sandbox exists it changes nothing. With `--reload` it is ignored (the app
runs in uvicorn's reloader child). It is audited at boot as
`command_sandbox_configured`
`{actor: "system:serve"|"system:runner", source, kind, state, line, unsandboxed_commands_allowed, env_keys}`.

**State, not a control.** `GET /api/gateway/workspace/policy` and `GET
/api/gateway/discovery/tools` carry `command_sandbox` `{state:
sandboxed|partial|unsandboxed|refused, kind, line, sentence,
unsandboxed_commands_allowed, configured, flag}`. The console shows `line`
under the Accounts head, with `sentence` as its tooltip, and the terminal
console shows it on its Workspaces page:

- `Commands sandboxed: macOS sandbox-exec` (or `Linux bubblewrap`)
- `Commands refused: no sandbox on this host`
- `Unsandboxed commands allowed (flag)`

In `/discovery/tools`, each process-spawning tool row carries `sandboxed:
true|false` and `sandbox` (for example "Sandboxed to this run's workspaces"),
so the apps' tool cards can show the state.

**Evidence per command.** The runtime stamps every spawning call with the
paths it enforced (the hidden `_sandbox` argument, recorded with the tool call
in the run ledger). The tool result carries `sandbox: {kind, label, posture,
default_mode, private_workspace, tmpdir, allowed: [{path, mode}], refused: [...],
builtin_refused: <count>}` and the line `Sandbox: macOS sandbox-exec`, or
`Sandbox: none — commands refused on this host`.

Evidence: `src/abstractgateway/command_sandbox.py`, `src/abstractgateway/cli.py`
(`--unsandboxed-commands`), AbstractCore `abstractcore/tools/sandbox.py`,
AbstractRuntime `workspace_scoped_tools.py` (`sandbox_stamp`). Tests:
`tests/test_r12w2_command_sandbox.py`, `tests/test_r12w2_nesting.py`.

## Common security env vars

All are loaded by `load_gateway_auth_policy_from_env()` (see `src/abstractgateway/security/gateway_security.py`).

### Enable/disable

- `ABSTRACTGATEWAY_SECURITY=1|0` (default: enabled)

### Tokens

- `ABSTRACTGATEWAY_AUTH_TOKEN` (single shared secret)
- `ABSTRACTGATEWAY_AUTH_TOKENS` (comma-separated list)
- `ABSTRACTGATEWAY_USER_AUTH=1` or `ABSTRACTGATEWAY_AUTH_MODE=users`: enable
  file-backed user principals and per-principal service routing
- `ABSTRACTGATEWAY_USER_AUTH_AUTO=1`: compatibility mode that also enables
  user auth when the registry file exists
- `ABSTRACTGATEWAY_USERS_FILE`: optional user registry path; defaults to
  `<ABSTRACTGATEWAY_DATA_DIR>/auth/users.json`
- `ABSTRACTGATEWAY_SESSIONS_FILE`: optional browser session registry path;
  defaults to `<ABSTRACTGATEWAY_DATA_DIR>/auth/sessions.json`
- `ABSTRACTGATEWAY_SESSION_TTL_S`: default browser session lifetime in seconds
  (default: 8 hours; bounded)
- `ABSTRACTGATEWAY_REMEMBER_SESSION_TTL_S`: browser session lifetime when an
  app requests "remember me" (default: 30 days; bounded)

### Protect reads vs writes

- `ABSTRACTGATEWAY_PROTECT_WRITE=1|0` (default: `1`)
- `ABSTRACTGATEWAY_PROTECT_READ=1|0` (default: `1`)
- `ABSTRACTGATEWAY_DEV_READ_NO_AUTH=1|0`  
  Dev escape hatch: allow unauthenticated reads **from loopback only**.

### Limits (abuse resistance)

- `ABSTRACTGATEWAY_MAX_BODY_BYTES` (default: `10MB`)  
  Applies to every mutating request. Oversized requests are **rejected** with
  `413` naming both sizes — bodies are never truncated. The default is sized for
  authored documents (a VisualFlow save is a whole workflow, not a small API
  payload), not just for abuse resistance.
- `ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES` (default: `25MB`)
- `ABSTRACTGATEWAY_MAX_BUNDLE_BYTES` (default: `75MB`)
- `ABSTRACTGATEWAY_MAX_CONCURRENCY` (default: `64`)
- `ABSTRACTGATEWAY_MAX_SSE` (default: `32`)

### Auth lockout (brute-force safety net)

- `ABSTRACTGATEWAY_LOCKOUT_AFTER` (default: `5`)
- `ABSTRACTGATEWAY_LOCKOUT_BASE_S` (default: `1.0`)
- `ABSTRACTGATEWAY_LOCKOUT_MAX_S` (default: `60.0`)

### Audit log (write requests)

- `ABSTRACTGATEWAY_AUDIT_LOG=1|0` (default: enabled for writes)
- `ABSTRACTGATEWAY_AUDIT_LOG_MAX_BYTES` (default: `50MB`)
- `ABSTRACTGATEWAY_AUDIT_LOG_ROTATIONS` (default: `10`)
- `ABSTRACTGATEWAY_AUDIT_LOG_HEADERS` (comma-separated allowlist; default: `x-client-id,x-client-version,x-forwarded-for`)

### Reverse proxies

- `X-Forwarded-For` from a proxy on the gateway machine (loopback peer, such
  as `tailscale serve`) is always used for IP attribution (audit log) and
  lockout tracking. The `trust_proxy` setting (console: Network → *Advanced* →
  *Trust proxies on other machines*; TUI: Connection screen checkbox; CLI:
  `abstractgateway network set --trust-proxy on|off`) extends that to a proxy
  on another machine. Read per request:
  it applies to the next request. Only when your own proxy sits in front of
  every request; otherwise any client chooses the address the gateway sees.
  The ephemeral tray token never honours it (raw socket peer only).
- Trust proxy follows one rule everywhere (IP attribution, lockouts, the
  same-machine rule and the network status): the saved setting first, and the
  `ABSTRACTGATEWAY_TRUST_PROXY` environment variable only while nothing is
  saved. Once the switch has been saved, the variable no longer applies to
  that data folder; use `abstractgateway network set --trust-proxy on|off`
  (see [Callers on this computer](#callers-on-this-computer)).

## Production checklist (minimal)

- Run behind TLS (reverse proxy) and bind `--host 127.0.0.1` (proxy in front) or lock down your network if binding `0.0.0.0`.
- Use a strong random token and list exact origins in `allowed_origins` (avoid public wildcards).
- Keep `ABSTRACTGATEWAY_SECURITY=1`.

## Related docs

- Configuration overview: [configuration.md](./configuration.md)
- API overview: [api.md](./api.md)
- FAQ: [faq.md](./faq.md)

## OpenAI API

The OpenAI-compatible API at `/v1` is stopped by default. In **Protected** mode a caller's API key is their own gateway token, resolved by the security middleware; the gateway forwards authenticated requests to Core with its own internal credential, so a caller's token never reaches Core, and refused keys count toward the per-address lockout. **Open** mode serves direct local, LAN and VPN clients without a key, never through a proxy or in Internet mode, and Core keeps stored cloud-provider credentials for authenticated callers. **Who can connect** filters on the client address (the socket peer, or `X-Forwarded-For` from a proxy on this machine or a trusted proxy); **Anywhere** requires Internet mode with its acknowledgement and Protected. Request fields that would re-route a provider or carry a credential (`base_url`, `api_key`, `provider`, `headers`, ...) are refused with 400, so a caller cannot make the gateway connect to an address of its choosing. Browser pages without a key are limited to the accepted origins. Every request is one audit-log line (no prompts or replies). See [openai-api.md](./openai-api.md).
