# Backlog Item 001: Swagger UI bearer auth docs

## Summary
Add OpenAPI security metadata so Swagger UI shows an Authorize button and injects the
gateway bearer token for `/api/gateway/*` calls.

## Reason
The gateway enforces auth in custom middleware, so OpenAPI does not currently declare
Bearer auth. This hides the Swagger UI Authorize flow and makes "Try it out" awkward.

## Strategy
- Option A: Override OpenAPI schema (docs-only) and tag `/api/gateway/*` operations.
- Option B: Add a FastAPI `HTTPBearer` dependency on the router.
- Decision: Option A. It keeps auth enforcement centralized in middleware and avoids
  duplicating auth logic in route dependencies while still improving the docs UX.

## Scope
### In scope
- Add an OpenAPI bearer security scheme to the app schema.
- Mark `/api/gateway/*` operations as secured in OpenAPI.
- Add a test that validates `/openapi.json` metadata.
- Update API docs to mention the Swagger Authorize flow.

### Out of scope
- Changing runtime auth behavior or policy.
- Adding new auth methods (OAuth, cookies, etc.).
- UI changes beyond OpenAPI metadata.

## Dependencies
- FastAPI OpenAPI generation (`fastapi.openapi.utils.get_openapi`).

## Expected outcomes
- Swagger UI displays an Authorize button for bearer token input.
- OpenAPI schema advertises bearer auth and secured gateway routes.
- Tests confirm the OpenAPI metadata.

## Report
### Outcome
- Added OpenAPI bearer auth metadata for `/api/gateway/*` so Swagger UI exposes Authorize.
- Documented the Authorize flow in the API and getting-started docs.
- Added OpenAPI contract tests to lock the behavior in.

### Implementation details
- Overrode `app.openapi` to inject a `gatewayBearerAuth` scheme and apply it to gateway paths.
- Added a focused OpenAPI test that validates scheme presence and per-operation security.

### Tests
- `pytest` (failed: `ModuleNotFoundError: No module named 'abstractgateway'` during collection).
- `PYTHONPATH=src pytest` (pass; 143 items, 3 skipped; warnings about unknown pytest marks and some logging errors from `abstractcore`/colorama when streams close).
