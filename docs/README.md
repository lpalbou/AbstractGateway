# AbstractGateway docs

Start here if you’re new to the project.

## AbstractFramework ecosystem

AbstractGateway is one component in the larger AbstractFramework ecosystem:

- **AbstractRuntime** (required): durable runs + stores
- **AbstractCore** (optional, via `abstractruntime[abstractcore]`): LLM/tool execution wiring used by many bundles
- Higher-level UIs (optional): AbstractFlow / AbstractObserver / AbstractCode / thin clients

Related repos:
- AbstractFramework: https://github.com/lpalbou/AbstractFramework
- AbstractCore: https://github.com/lpalbou/abstractcore
- AbstractRuntime: https://github.com/lpalbou/abstractruntime

## Docs map

- Quickstart + stores (file/SQLite): [getting-started.md](./getting-started.md)
- FAQ / troubleshooting: [faq.md](./faq.md)
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

## Project docs

- Package README: [../README.md](../README.md)
- Changelog: [../CHANGELOG.md](../CHANGELOG.md) (compat: `CHANGELOD.md`)
- Contributing: [../CONTRIBUTING.md](../CONTRIBUTING.md)
- Security policy (vulnerability reporting): [../SECURITY.md](../SECURITY.md)
- Acknowledgments: [../ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md) (compat: `ACKNOWLEDMENTS.md`)
