# AbstractGateway — Security guide

AbstractGateway secures the **gateway API surface** (`/api/gateway/*`) using an ASGI middleware:
`GatewaySecurityMiddleware` in `src/abstractgateway/security/gateway_security.py`.

Notes:
- `/api/health` is intentionally not protected.
- `/api/triage/action/*` uses signed action tokens and is not under `/api/gateway` (see `src/abstractgateway/routes/triage.py`).
- Vulnerability reporting policy: see [../SECURITY.md](../SECURITY.md).

## Default behavior

By default, `abstractgateway serve` refuses to start if write endpoints are
protected and neither a legacy server/operator token nor Gateway user auth is
configured.
Evidence: startup self-check in `src/abstractgateway/cli.py` (`load_gateway_auth_policy_from_env`).

Recommended browser-console/browser-app setup:

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

In local/single-user mode, `ABSTRACTGATEWAY_AUTH_TOKEN` remains a gateway-level
control-plane token and maps to the `local-admin` principal. Treat that token as
full authority for the Gateway instance.

Hosted user-auth mode is enabled with `ABSTRACTGATEWAY_USER_AUTH=1` or
`ABSTRACTGATEWAY_AUTH_MODE=users`. In that mode, Gateway bearer tokens resolve
to concrete principals with `tenant_id`, `user_id`, roles/scopes, and a token
fingerprint. `GET /api/gateway/me` returns the resolved principal and routing
mode. The presence of an `auth/users.json` registry file is readiness state; it
does not silently enable hosted user auth unless `ABSTRACTGATEWAY_USER_AUTH_AUTO=1`
is set for compatibility. Admin principals can manage users through:

- `GET /api/gateway/admin/users`
- `POST /api/gateway/admin/users`
- `GET /api/gateway/admin/users/{user_id}?tenant_id=...`
- `PATCH /api/gateway/admin/users/{user_id}?tenant_id=...`
- `DELETE /api/gateway/admin/users/{user_id}?tenant_id=...`
- `GET /api/gateway/admin/runtime-reservations`
- `POST /api/gateway/admin/runtime-reservations/{runtime_id}/transfer`
- `POST /api/gateway/admin/runtime-reservations/{runtime_id}/purge`

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

Hosted multi-user mode is an incremental surface: the core request path now has
principal auth and per-principal services. Gateway also applies a central
route-family authorization table for operator/admin surfaces. Admin-only route
families include user management, audit, process control, backlog/triage/report
operations, email bridge routes, host metrics, model residency list/load/unload,
server workspace file helpers, server-workspace artifact import/export, and
global prompt-cache/bloc mutation routes. Regular users remain able to use
their own runtime data plane for run, ledger, artifact upload, discovery, and
runtime-scoped Core capability-default routes.

The route table is intentionally conservative around server filesystem access:
browser-local files should use `/api/gateway/attachments/upload`; server
workspace reads/imports/exports require an admin principal until a stronger
per-user workspace grant model exists.

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

If the request includes an `Origin` header, the middleware enforces `ABSTRACTGATEWAY_ALLOWED_ORIGINS` using glob-style patterns (fnmatch).

Examples:

```bash
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*"
# or (example) an ngrok domain:
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="https://*.ngrok-free.app"
```

Evidence: `GatewayAuthPolicy.allowed_origins` and `_origin_allowed()` in `src/abstractgateway/security/gateway_security.py`.

Important nuance:
- FastAPI’s CORS middleware in `src/abstractgateway/app.py` is permissive, but **origin enforcement for gateway endpoints is done by this security middleware**.

## Workspace filesystem scope (blacklist/whitelist)

AbstractGateway supports “thin clients” (browser UIs, bridges) that can trigger **filesystem-ish tools** (e.g. `list_files`, `read_file`, `write_file`). To avoid a thin client expanding server filesystem access, the gateway enforces a **workspace policy**.

Key point: the **main configuration** for filesystem allowlisting/denylisting is set when you **launch the gateway** (operator-controlled env vars). Thin clients can only request broader scopes when the gateway is started in a permissive mode.

### Default (safe): everything outside the run workspace is blocked

- When a run is started via `POST /api/gateway/runs/start` and `workspace_root` is missing (or rejected), the gateway creates a **per-run workspace** under:
  - `<ABSTRACTGATEWAY_DATA_DIR>/workspaces/<uuid>`
- AbstractRuntime applies workspace scoping to filesystem-ish tool arguments. The default is:
  - `workspace_access_mode=workspace_only`
  - absolute paths must stay under `workspace_root`

This means that by default, **all absolute paths are effectively “blacklisted”** except the run’s `workspace_root`.

Evidence:
- Run default workspace injection: `src/abstractgateway/routes/gateway.py` (`start_run`)
- Client scope clamping: `src/abstractgateway/routes/gateway.py` (`_sanitize_run_workspace_policy`, `_client_workspace_scope_overrides_enabled`)
- Runtime tool scoping: `abstractruntime/integrations/abstractcore/workspace_scoped_tools.py`
- Tests: `tests/test_gateway_workspace_policy_enforcement.py`

### Operator-controlled allowlist roots (recommended)

- `ABSTRACTGATEWAY_WORKSPACE_DIR`: base directory used to resolve relative workspace paths and as the default root for `/files/*` helpers.
- `ABSTRACTGATEWAY_WORKSPACE_MOUNTS`: additional allowed roots (newline-separated `name=/abs/path`).

Thin clients can discover the server policy via:
- `GET /api/gateway/workspace/policy`  
  Note: it returns **mount names only** (no absolute paths).

### Permissive mode: allow thin clients to choose scope (trusted machines only)

To honor client-provided workspace knobs (`workspace_root`, `workspace_access_mode`, `workspace_allowed_paths`, `workspace_ignored_paths`) beyond the operator roots, enable one of:

- `ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE=1`
- `ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE=1`

In this mode, a client can request:
- `workspace_access_mode=all_except_ignored` (“full access” unless explicitly blocked)

Do **not** enable this when serving untrusted browser origins: a compromised thin client can request access to arbitrary server paths.

### Important limitation (still true in all modes)

`execute_command` is **not** an OS sandbox: even if the runtime sets the default working directory under `workspace_root`, the command itself can reference absolute paths or `cd ..`.

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

- `ABSTRACTGATEWAY_MAX_BODY_BYTES` (default: `256000`)
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

- `ABSTRACTGATEWAY_TRUST_PROXY=1|0`  
  If enabled, `X-Forwarded-For` is used for IP attribution and lockout tracking.

## Production checklist (minimal)

- Run behind TLS (reverse proxy) and bind `--host 127.0.0.1` (proxy in front) or lock down your network if binding `0.0.0.0`.
- Use a strong random token and set exact `ABSTRACTGATEWAY_ALLOWED_ORIGINS` (avoid public wildcards).
- Keep `ABSTRACTGATEWAY_SECURITY=1`.

## Related docs

- Configuration overview: [configuration.md](./configuration.md)
- API overview: [api.md](./api.md)
- FAQ: [faq.md](./faq.md)
