# Offline-first Gateway connectivity contract

## Problem

AbstractFramework must remain usable without internet access. Browser clients were
using capability/provider discovery as a connection check, and the API docs pages
depended on public Swagger/ReDoc CDNs. That made local Gateway and Flow appear
unavailable even when the Gateway process itself was running.

## Direction

- Thin clients should validate Gateway reachability/authentication through a
  cheap protected endpoint: `GET /api/gateway/ping`.
- Capability, provider, model, voice, vision, and embeddings discovery are
  separate best-effort probes. They may return partial results, empty lists, or
  errors for unavailable optional backends, but they must not decide whether a
  client can connect to Gateway.
- Gateway and local framework docs must not require external assets. Docs pages
  should render with inline/local assets and always expose a direct
  `/openapi.json` link.

## Follow-up

- Add a versioned client capability contract field that tells clients whether
  `/api/gateway/ping` is supported.
- Consider a richer offline docs viewer with local packaged Swagger UI assets,
  but do not reintroduce CDN dependencies.
