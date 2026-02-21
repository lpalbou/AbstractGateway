# AbstractGateway — Security guide

AbstractGateway secures the **gateway API surface** (`/api/gateway/*`) using an ASGI middleware:
`GatewaySecurityMiddleware` in `src/abstractgateway/security/gateway_security.py`.

Notes:
- `/api/health` is intentionally not protected.
- `/api/triage/action/*` uses signed action tokens and is not under `/api/gateway` (see `src/abstractgateway/routes/triage.py`).
- Vulnerability reporting policy: see [../SECURITY.md](../SECURITY.md).

## Default behavior (token required)

By default, `abstractgateway serve` refuses to start if write endpoints are protected and no auth token is configured.  
Evidence: startup self-check in `src/abstractgateway/cli.py` (`load_gateway_auth_policy_from_env`).

Recommended dev setup:

```bash
export ABSTRACTGATEWAY_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Clients must send:

```text
Authorization: Bearer <token>
```

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
