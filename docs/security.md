# AbstractGateway — Security guide

AbstractGateway secures the **gateway API surface** (`/api/gateway/*`) using an ASGI middleware:
`GatewaySecurityMiddleware` in `src/abstractgateway/security/gateway_security.py`.

Notes:
- `/api/health` is intentionally not protected.
- `/api/triage/action/*` uses signed action tokens and is not under `/api/gateway` (see `src/abstractgateway/routes/triage.py`).

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
