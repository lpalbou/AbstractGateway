# Proposed: Gateway Install Profiles And Configuration Entrypoint

## Metadata
- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

AbstractGateway is the deployment composition root for durable agents and thin clients. It owns
its own HTTP/SSE access, auth, CORS/origin policy, workflow bundles, durable stores, runner
lifecycle, artifacts, workspace policy, tool approval policy, provider/model runtime defaults,
embeddings, and capability readiness reporting.

Gateway should compose AbstractRuntime, AbstractCore, AbstractAgent, AbstractVision, AbstractVoice,
AbstractMemory, and AbstractSemantics. It should not absorb their internal responsibilities.

## Current Code Reality

- Gateway directly imports and hosts AbstractRuntime stores, commands, runner, bundles, and ledger.
- Bundle execution uses AbstractCore integration when workflows need LLMs, tools, media, or
  generated outputs.
- Gateway falls back to AbstractCore config defaults when flow/provider pins are missing.
- Direct TTS/STT/image routes lazily use Voice/Vision/Core capability paths.
- Gateway security middleware already separates client-facing Gateway auth/origin policy from
  provider credentials and Core internals.
- Pending changes make the bare Gateway package depend on `AbstractRuntime[multimodal]`,
  `abstractvision`, and `abstractvoice`.
- Pending Docker changes add a remote-light server image and a `server-nvidia` image.
- AbstractCore `2.13.12` is now released on PyPI and its wheel metadata exposes the canonical
  profile aliases `apple`, `gpu`, `all-apple`, and `all-gpu`.
- AbstractCore `2.13.12` also includes the Voice/Vision catalog-capable plugin floors:
  `abstractvoice>=0.9.2` and `abstractvision>=0.3.3`.
- AbstractRuntime `0.4.8` is the Gateway-aligned Runtime baseline candidate. It raises its
  AbstractCore extras to `abstractcore>=2.13.12`, keeps Runtime free of Apple/GPU profile extras,
  and removes direct Runtime reads of `ABSTRACTGATEWAY_*` environment variables.
- Gateway package metadata and docs still reference older floors in several places:
  `abstractcore>=2.13.10`, `abstractvision>=0.3.1`, and `abstractvoice>=0.9.0`.

## Problem

Gateway has two legitimate install personas:

- runner/control-plane library: durable host and CLI with minimal dependencies;
- deployable server: remote providers, multimodal capability plugins, tools, media, agent nodes,
  and thin-client endpoints.

Putting the full remote-light server dependency set into bare `pip install abstractgateway` removes
the first persona. A `runner = []` extra cannot restore it because Python extras only add
dependencies.

There are also pending consistency risks:

- docs/compose examples moved toward port `8000`, while the CLI default remains `8080`;
- `server-nvidia` is published with best-effort workflow settings but docs risk presenting it as
  equivalent to the light image;
- profile naming currently uses `server-nvidia` but not the accepted `gpu` / `all-gpu` vocabulary;
- discovery can still confuse "package installed" with "backend configured and ready";
- memory KG workflows currently need the AbstractMemory 0.0.x `lancedb` extra, but Gateway has no explicit
  memory extra.

## Proposed Direction

Keep Gateway as the composition root, but split the install profiles deliberately:

- `abstractgateway`: minimal durable host/runner/CLI with `AbstractRuntime>=0.4.8`.
- `abstractgateway[http]`: FastAPI/Uvicorn HTTP surface.
- `abstractgateway[multimodal]`: Runtime/Core multimodal integration plus light Vision/Voice
  plugin packages.
- `abstractgateway[server]`: recommended Docker/server profile: HTTP, Runtime multimodal,
  AbstractCore remote/media/tools/tokens/compression/vision/voice/audio, AbstractAgent,
  AbstractFlow bundle compatibility, AbstractVision, AbstractVoice, and Gateway endpoints.
- `abstractgateway[memory]`: Gateway KG memory support with `AbstractMemory[lancedb]>=0.2.4`.
- `abstractgateway[apple]`: full native macOS Python deployment profile, cascading through
  `abstractcore[all-apple]`, Runtime/Agent pass-through profiles, and package-owned Apple-relevant
  capability extras.
- `abstractgateway[gpu]`: full native GPU Python deployment profile, cascading through
  `abstractcore[all-gpu]`, Runtime/Agent pass-through profiles, and package-owned GPU-relevant
  capability extras.
- `abstractgateway[all-apple]`: explicit aggregate spelling for the same native macOS deployment
  intent as `apple`.
- `abstractgateway[all-gpu]`: explicit aggregate spelling for the same native GPU deployment
  intent as `gpu`.
- `abstractgateway[server-nvidia]`: explicit NVIDIA Docker image profile. It is separate from the
  generic Python `gpu` profile because container dependency and CUDA runtime constraints are a
  Docker deployment concern.

If maintainers prefer bare `pip install abstractgateway` to be the remote-light server profile, then
rename the runner/control-plane install story rather than pretending `runner = []` is meaningful.

Required dependency floors after Core and Runtime release:

- `AbstractRuntime>=0.4.8` for minimal Gateway.
- `AbstractRuntime[multimodal]>=0.4.8` for server/multimodal Gateway.
- `abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]>=2.13.12` for
  remote-light server deployments.
- `abstractcore[all-apple]>=2.13.12` and `abstractcore[all-gpu]>=2.13.12` for full native Gateway
  hardware profiles; lower-level `abstractcore[apple]` and `abstractcore[gpu]` remain Core-only
  local LLM aliases.
- `abstractvision>=0.3.3` and `abstractvoice>=0.9.2` whenever Gateway depends on those packages
  directly.

## Configuration Entrypoint

Add `abstractgateway-config` as a first-class CLI or subcommand.

It should manage:

- Gateway auth token(s);
- `ABSTRACTGATEWAY_DATA_DIR`;
- store backend and DB path;
- workflow source and bundles directory;
- prompt-cache defaults;
- attachment/session-media byte limits;
- allowed origins;
- runner mode;
- default provider/model for runs;
- tool approval mode;
- workspace root/mounts/policy;
- embedding provider/model;
- memory KG status;
- capability readiness status.

It may call or guide `abstractcore-config` for provider API keys, provider base URLs, Core server
settings, and Core media defaults. Gateway config should not duplicate Core provider internals.

Recommended environment/config precedence:

1. Explicit request/run values when Gateway policy permits them.
2. Gateway deployment env/config for Gateway-owned concerns:
   `ABSTRACTGATEWAY_AUTH_TOKEN(S)`, `ABSTRACTGATEWAY_ALLOWED_ORIGINS`,
   `ABSTRACTGATEWAY_DATA_DIR`, `ABSTRACTGATEWAY_STORE_BACKEND`, `ABSTRACTGATEWAY_DB_PATH`,
   `ABSTRACTGATEWAY_FLOWS_DIR`, `ABSTRACTGATEWAY_RUNNER`, `ABSTRACTGATEWAY_TOOL_MODE`,
   workspace env vars, Gateway provider/model defaults, embedding provider/model, memory backend,
   and runner tuning.
3. AbstractCore config/env for provider credentials, provider base URLs, Core default
   provider/model, Core server auth, Core server allowlists, media/fetch/local-file policy, and
   Core media defaults.
4. Capability-package env for backend-specific settings:
   `ABSTRACTVISION_*`, `ABSTRACTVOICE_*`, and future `ABSTRACTMUSIC_*`.
5. Package defaults, with explicit `#FALLBACK` warnings when defaults are used because higher
   layers omitted a value.

Gateway should write run-level defaults into Runtime as JSON-safe state, such as
`_runtime.provider`, `_runtime.model`, `_runtime.audio_policy`, `_runtime.prompt_cache`,
`_runtime.max_attachment_bytes`, and workspace policy fields. Runtime and Agent nodes may read those
values. Gateway should not force lower packages to read `ABSTRACTGATEWAY_*` directly.

Runtime `0.4.7` makes this boundary stricter. Gateway-owned env vars must be translated by Gateway:

- `ABSTRACTGATEWAY_PROMPT_CACHE` should become `_runtime.prompt_cache` on runs/sessions where Gateway
  wants automatic prompt-cache keys.
- `ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES` should become `_runtime.max_attachment_bytes`, or
  `TOOL_CALLS.payload.max_attachment_bytes` when Gateway needs a per-effect limit.
- `ABSTRACTGATEWAY_FLOWS_DIR` should be passed as an explicit workflow bundle registry path, or
  translated to the shared `ABSTRACTFRAMEWORK_WORKFLOWS_DIR` before Runtime registry construction.

Core-server topology must be explicit:

- In-process Core: only Gateway's HTTP auth/CORS boundary is exposed to clients.
- Standalone Core server: Gateway must be configured with Core server URL and Core server auth
  token/header explicitly.
- Gateway auth tokens must not be reused implicitly as Core server auth or upstream provider keys.

## Security Boundary

Gateway owns the public deployment envelope. That means the token(s) thin clients use, browser
allowed origins, request/body/SSE limits, runner exposure, reverse proxy assumptions, audit posture,
workspace policy, and tool approval policy are Gateway configuration.

That does not make Gateway auth a master key for lower packages:

- Gateway auth protects Gateway routes only.
- AbstractCore server auth protects the standalone Core server only.
- Provider API keys and provider base URLs remain AbstractCore or capability-package configuration.
- Direct Gateway image/TTS/STT routes are Gateway-owned HTTP/artifact contracts, but backend
  selection and provider secrets remain Core/Vision/Voice concerns.

When Gateway embeds Core in-process, Core server CORS/auth settings are not exposed to thin clients.
When Gateway calls or launches a standalone Core server, Gateway should pass explicit Core settings
or client headers, such as the Core server URL and Core server `Authorization` token. It should not
silently map `ABSTRACTGATEWAY_AUTH_TOKEN` or `ABSTRACTGATEWAY_ALLOWED_ORIGINS` into Core settings.
Sharing a generated service token between Gateway and Core may be offered only as an explicit
deployment choice, not the default conceptual model.

Capability packages normally receive outbound configuration only: provider credentials, base URLs,
model ids, timeouts, cache paths, devices, and backend flags. CORS and bearer auth belong to the
package only if that package intentionally exposes its own production server.

## Capability Readiness Contract

Gateway discovery should distinguish:

- `installed`: import/package present;
- `registered`: plugin/backend registered;
- `configured`: required env/config present;
- `ready`: a cheap non-generating preflight passes, or no preflight is needed;
- `route_available`: Gateway HTTP route exists;
- `available`: route and backend are usable now.

This matters for thin clients. They should not enable image/audio/memory controls simply because a
package was installed.

## Pending Changes Guidance

Keep:

- the light Docker server direction;
- the separate NVIDIA profile concept;
- Vision/Voice OpenAI default delegation to the capability packages;
- image readiness changes that separate route availability from backend config.

Revise before merge:

- bare package dependencies: decide minimal base versus server-by-default;
- `runner = []`: remove, rename, or document only after the base decision;
- profile vocabulary: keep `apple`, `gpu`, `all-apple`, and `all-gpu` as Python install profiles,
  with Gateway `apple`/`gpu` treated as full native deployment aggregates; keep `server-nvidia` as
  the explicit NVIDIA Docker profile;
- dependency floors: update Gateway to `abstractcore>=2.13.12`, `abstractvision>=0.3.3`, and
  `abstractvoice>=0.9.2`, and update Runtime to `AbstractRuntime>=0.4.8` after that Runtime release
  is published;
- port documentation versus CLI default;
- `server-nvidia` docs: mark experimental until real build and smoke validation exist;
- add `memory` extra or document why Gateway KG workflows remain opt-in;
- discovery tests for installed-but-unconfigured Voice/Vision/Memory.
- helper/discovery paths that still hardcode provider/model fallbacks instead of using the same
  Gateway-to-Core default resolver as runtime execution.
- env/config cascade tests proving Gateway-owned settings, Core settings, and capability-package
  settings do not silently overwrite each other.
- Runtime handoff tests proving Gateway translates prompt-cache, attachment byte-limit, and flow
  directory settings explicitly rather than relying on Runtime to read Gateway env names.

Do not include:

- root stale pin updates without version alignment;
- unrelated app or AI-space changes;
- venvs, caches, backup clones, and generated site artifacts.

## Promotion Criteria

Promote this when the base install persona is chosen and the server/memory/GPU profile names are
accepted.

## Validation Ideas

- Fresh venv install tests for `abstractgateway`, `abstractgateway[http]`,
  `abstractgateway[server]`, `abstractgateway[memory]`, `abstractgateway[apple]`,
  `abstractgateway[gpu]`, `abstractgateway[all-apple]`, `abstractgateway[all-gpu]`, and the
  compatibility `abstractgateway[server-nvidia]`.
- Docker build smoke for the light image.
- CUDA host smoke before treating `server-nvidia` as stable.
- Capability discovery contract tests for missing keys, missing LanceDB, disabled embeddings, and
  installed-but-unconfigured Vision/Voice.
- Package metadata tests proving the server profile pulls Core `2.13.12+`, Vision `0.3.3+`, Voice `0.9.2+`, Runtime `0.4.8+`, and does not pull local engines unless Apple/GPU/local profiles are
  selected.
- Configuration-precedence tests for request overrides, Gateway provider/model defaults, Core
  config fallback, and capability-package env fallback.
- Gateway-to-Runtime handoff tests for `_runtime.prompt_cache`, `_runtime.max_attachment_bytes`, and
  explicit workflow bundle registry paths.

## Completion Report

Implemented in Gateway only.

- Base `abstractgateway` is now the minimal runner/control-plane profile with
  `AbstractRuntime>=0.4.8`.
- Added explicit cascading profiles: `http`, `multimodal`, `server`, `memory`,
  `apple`, `gpu`, `all-apple`, `all-gpu`, `server-nvidia`, and `all`.
- Added the `abstractgateway-config` entrypoint and `abstractgateway config`
  subcommand for status inspection and private env-file initialization.
- Added Gateway-to-Runtime JSON-safe handoff for prompt-cache enablement,
  attachment limits, and workflow bundle directory state.
- Updated docs to keep Gateway auth, Core server auth, provider credentials, and
  capability-package env vars separate.
- Added package metadata, config CLI, and Runtime handoff tests.
