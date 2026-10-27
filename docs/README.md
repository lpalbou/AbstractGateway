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
| [getting-started.md](./getting-started.md) | explicit setup: bundles, starting and scheduling runs, split API and runner (opt-in), file vs SQLite stores |
| [architecture.md](./architecture.md) | components, diagrams, the replay-first durable contract, worker pools and off-loop service builds, automations, triggers, events and attention (alerts with their channels, push), live replies, the workspace guard, the desktop hand-over, deployment shapes (split mode) |
| [api.md](./api.md) | the client contract with curl examples: the gateway default workflow (`@default`), live replies, a run's workspace folder, durable commands, the automation routes, trigger connections, the well-known feed catalogue, sample inputs and the signed webhook ingress, your event stream (`/me/events`, its vocabulary, published alerts with `recipient` and `channels`, the one-notification-per-machine rule), After the run (`/runs/{run_id}/after`), push devices and the push forwarder setting, discovery, media, models, host state, `/about`; map of every route family |
| [configuration.md](./configuration.md) | every setting: runtime settings, network exposure (detected addresses, Tailscale), apps, default agent workflows, stream replies, skills shelf, backlog, workspace policy, capability defaults, environment variables, split mode (API and runner in two processes), CLI flags (worker pools, run index, GC tuning, push relay) and the load test |
| [faq.md](./faq.md) | recurring questions and limits |
| [troubleshooting.md](./troubleshooting.md) | symptoms, causes and fixes: sign-in, network modes, runs, automations and mail steps, installs, downloads, `503 service_starting`, a slow gateway, split mode, watch and iPhone alerts, tray, login service |

## Topic guides

| Page | Read it for |
|---|---|
| [email.md](./email.md) | per-user email: connecting your own mailbox and further mailboxes, each Read only or Read and write (password or OAuth2), recipient policy and send limits, automations on new mail, email notifications, account recovery by email, the administrator switch, migration from the retired environment variables |
| [automations.md](./automations.md) | automations: a workflow run on a trigger as a durable controller run; every route with its shapes, the error envelope, tool approval, typed waits and how to answer them, attention and `/seen`, wakes an agent schedules (`schedule_run`), discussions, run lists, legacy schedules, operations (one writer process, restart recovery, boot warm-up, the acceptance script) and the limits of v1 |
| [events-examples.md](./events-examples.md) | events and triggers, three end-to-end examples with the exact commands: watch a mailbox and tell me when something important arrives, a digest of a news feed, run a flow when an external system calls my webhook (signed curl); following your event stream |
| [push.md](./push.md) | alerts on the Apple Watch and iPhone while the app is closed: nothing to configure for people, the Push forwarder setting and the `--push-relay` flag, alerts that choose their channels, running your own relay with your own Apple key, what the relay and Apple see, the fixed messages |
| [openai-api.md](./openai-api.md) | the OpenAI-compatible API at `/v1`: connecting apps and SDKs, what is supported, Protected/Open and Who can connect, errors, the request log, moving from `/core/v1` |
| [console.md](./console.md) | the web console at `/console` (the grouped sidebar and every tab, including Accounts, My email (mailboxes, notifications, watch and iPhone devices, the push forwarder), Workflows and Triggers (well-known feeds, Handled by); responsive layout on phones and tablets) and the `abstractgateway-console` terminal app (its screens and keys) |
| [apps.md](./apps.md) | installing, starting and opening the browser apps (Flow, Code, Observer, Continuum, Entity, Note), Code's terminal app and the desktop Assistant |
| [engines.md](./engines.md) | installing local engines (Ollama, LM Studio, MLX, llama.cpp, vLLM, Hugging Face): what each Install does, when a password or the Apple tools are needed |
| [model-downloads.md](./model-downloads.md) | download jobs: progress, stalls, cancel, end reasons, parent jobs, the event stream |
| [tray.md](./tray.md) | the desktop tray icon: sessions, automation and approval notifications, apps, models, pause, start at login, network, restart and update |
| [security.md](./security.md) | user accounts, who sees which account and entity, sessions, origins, network exposure, callers on this computer, the Assistant's sign-in, workspace scope and the built-in deny list, limits, audit log |
| [deployment.md](./deployment.md) | Docker images, Compose, provider variables, single machine without Docker |
| [shipped-workflows.md](./shipped-workflows.md) | the workflows a fresh install serves (coding agent, deep research, co-scientist, …) and managing the registry |
| [deep-research.md](./deep-research.md) | the shipped `deep-research` workflow contract |
| [entities.md](./entities.md) | summoned entities: who can see one, homes, lifecycle, summoning, replay |
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
