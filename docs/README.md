# AbstractGateway docs

Start here if you’re new to the project.

## Docs map

- Quickstart + stores (file/SQLite): [getting-started.md](./getting-started.md)
- Architecture (durable contract + components): [architecture.md](./architecture.md)
- Configuration (env vars + install extras): [configuration.md](./configuration.md)
- API overview (client contract + OpenAPI): [api.md](./api.md)
- Security guide (auth/origin/limits/audit log): [security.md](./security.md)
- Operator tooling (triage/backlog/process manager): [maintenance.md](./maintenance.md)

## API docs (generated)

When the HTTP server is running (`abstractgateway serve`):
- Health: `GET /api/health`
- OpenAPI JSON: `GET /openapi.json`
- Interactive Swagger UI: `GET /docs`
