# AbstractGateway docs

Start here if you’re new to the project.

## AbstractFramework ecosystem

AbstractGateway is one component in the larger AbstractFramework ecosystem:

- **AbstractRuntime** (required): durable runs + stores
- **AbstractRuntime + transitive capability packages** (required by the default server install): Runtime owns the LLM/tool/media integration boundary; Gateway uses its discovery/run facades for prompt-cache controls, generated image/voice/audio capabilities, and KG-backed bundle execution
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
- Apple Silicon local Gateway + Flow quickstart: [apple-local-gateway-flow.md](./apple-local-gateway-flow.md)
- Deployment (Docker/GHCR/Compose): [deployment.md](./deployment.md)
- API overview (client contract + OpenAPI): [api.md](./api.md)
- Security guide (auth/origin/limits/audit log): [security.md](./security.md)
- Operator tooling (triage/backlog/process manager): [maintenance.md](./maintenance.md)

## API docs (generated)

Published static docs site: https://www.lpalbou.info/AbstractGateway/

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
