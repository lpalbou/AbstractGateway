# AbstractGateway docs

AbstractGateway is the durable run gateway and control plane of
AbstractFramework: it starts and replays AbstractRuntime runs over HTTP/SSE,
and lets you manage users, providers, models, local engines, browser apps and
network access from a web console, a terminal console, the CLI or a desktop
tray icon.

New here? Start with [first-run.md](./first-run.md) on your own machine, or
[getting-started.md](./getting-started.md) for an explicit setup.

## AbstractFramework ecosystem

- **AbstractRuntime** (required): durable runs, workflow registry, stores.
- **AbstractCore**, **AbstractAgent**, **AbstractMemory** (installed with the
  gateway): providers, tools and media capabilities through Runtime's
  facades, agent nodes, and KG memory.
- Apps that use the gateway (optional): AbstractFlow, AbstractCode,
  AbstractObserver, AbstractContinuum, AbstractEntity, AbstractAssistant.

Related repos:
[AbstractFramework](https://github.com/lpalbou/AbstractFramework) ·
[AbstractCore](https://github.com/lpalbou/abstractcore) ·
[AbstractRuntime](https://github.com/lpalbou/abstractruntime)

## Core docs

| Page | Read it for |
|---|---|
| [first-run.md](./first-run.md) | the zero-configuration start on your own machine: one-time sign-in link, first-run guide, start at login |
| [getting-started.md](./getting-started.md) | explicit setup: bundles, starting and scheduling runs, split API/runner, file vs SQLite stores |
| [architecture.md](./architecture.md) | components, diagrams, the replay-first durable contract, live replies, the workspace guard, the desktop hand-over, deployment shapes |
| [api.md](./api.md) | the client contract with curl examples: the gateway default workflow (`@default`), live replies, a run's workspace folder, discovery, media, models, host state, `/about`; map of every route family |
| [configuration.md](./configuration.md) | every setting: runtime settings, network exposure, apps, default agent workflows, stream replies, skills shelf, backlog, workspace policy, capability defaults, environment variables, CLI flags |
| [faq.md](./faq.md) | recurring questions and limits |
| [troubleshooting.md](./troubleshooting.md) | symptoms, causes and fixes: sign-in, network modes, runs, installs, downloads, tray, login service |

## Topic guides

| Page | Read it for |
|---|---|
| [console.md](./console.md) | the web console at `/console` (every tab) and the `abstractgateway-console` terminal app |
| [apps.md](./apps.md) | installing, starting and opening the browser apps (Flow, Code, Observer, Continuum, Entity), Code's terminal app and the desktop Assistant |
| [engines.md](./engines.md) | installing local engines (Ollama, LM Studio, MLX, llama.cpp, vLLM, Hugging Face): what each Install does, when a password or the Apple tools are needed |
| [model-downloads.md](./model-downloads.md) | download jobs: progress, stalls, cancel, end reasons, parent jobs, the event stream |
| [tray.md](./tray.md) | the desktop tray icon: apps, models, pause, start at login, network, restart and update |
| [security.md](./security.md) | user accounts, sessions, origins, network exposure, callers on this computer, the Assistant's sign-in, workspace scope and the built-in deny list, limits, audit log |
| [deployment.md](./deployment.md) | Docker images, Compose, provider variables, single machine without Docker |
| [shipped-workflows.md](./shipped-workflows.md) | the workflows a fresh install serves (coding agent, deep research, co-scientist, …) and managing the registry |
| [deep-research.md](./deep-research.md) | the shipped `deep-research` workflow contract |
| [entities.md](./entities.md) | summoned entities: homes, lifecycle, summoning, replay |
| [apple-local-gateway-flow.md](./apple-local-gateway-flow.md) | an Apple Silicon local Gateway + Flow setup with local engines |
| [maintenance.md](./maintenance.md) | operator tooling: reports, triage, backlog, exec runner, process manager, bridges (high trust) |

## API reference (generated)

Published docs site: https://www.lpalbou.info/AbstractGateway/

When the server is running (`abstractgateway serve`):

- Health: `GET /api/health`
- OpenAPI JSON: `GET /openapi.json`
- Interactive Swagger UI: `GET /docs`

## Project docs

- Package README: [../README.md](../README.md)
- Changelog: [../CHANGELOG.md](../CHANGELOG.md)
- Contributing: [../CONTRIBUTING.md](../CONTRIBUTING.md)
- Code of conduct: [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)
- Security policy (vulnerability reporting): [../SECURITY.md](../SECURITY.md)
- Acknowledgments: [../ACKNOWLEDGMENTS.md](../ACKNOWLEDGMENTS.md)
- License: [../LICENSE](../LICENSE)
