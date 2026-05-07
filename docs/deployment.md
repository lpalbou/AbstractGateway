# AbstractGateway deployment

AbstractGateway can run as a Python process or as a containerized server. The
container path is the recommended baseline for a single self-contained Gateway
deployment because it packages the HTTP API, durable runner, AbstractRuntime,
and the remote-provider/multimodal AbstractCore stack together.

## Published image

Release images are published to GHCR:

```bash
docker pull ghcr.io/lpalbou/abstractgateway-server:0.2.3
```

The image installs `abstractgateway[server]`, which includes:

- `AbstractRuntime[multimodal]`
- `abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]`
- `abstractvision`
- `abstractvoice`
- `abstractagent`
- `abstractflow`
- FastAPI/Uvicorn

This profile supports hosted/commercial providers, OpenAI-compatible text
provider routing, workflow-backed and direct image generation through
Runtime/Core/AbstractVision, direct Gateway voice/STT endpoints through
AbstractVoice, and provider/session prompt-cache controls.
It intentionally stays dependency-light for heavy local model runtimes: MLX,
vLLM, HuggingFace Transformers, local Diffusers/sdcpp, AbstractVoice local
engines, and future AbstractMusic engines should remain explicit opt-in image
variants or sidecars.

## Compose quickstart

Create an env file from the template, set a strong token, then start the
server:

```bash
cp docker/abstractgateway-server/.env.example docker/abstractgateway-server/.env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
docker compose --env-file docker/abstractgateway-server/.env \
  -f docker/abstractgateway-server/compose.yml up -d
```

The default compose profile binds to `127.0.0.1:8080`, mounts a durable Gateway
data volume at `/data/gateway`, mounts bundles from `flows/bundles` at
`/data/flows`, and exposes a container workspace at `/workspace`.

Smoke checks:

```bash
curl http://127.0.0.1:8080/api/health

curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  http://127.0.0.1:8080/api/gateway/bundles
```

## Core configuration

Required:

- `ABSTRACTGATEWAY_AUTH_TOKEN`: bearer token for `/api/gateway/*`

Common:

- `ABSTRACTGATEWAY_ALLOWED_ORIGINS`: browser origin allowlist
- `ABSTRACTGATEWAY_PROVIDER` / `ABSTRACTGATEWAY_MODEL`: defaults for LLM/agent nodes
- `ABSTRACTGATEWAY_TOOL_MODE`: `approval`, `passthrough`, `delegated`, or local dev modes
- `ABSTRACTGATEWAY_STORE_BACKEND`: `file` or `sqlite`
- `ABSTRACTGATEWAY_DB_PATH`: SQLite file, when using `sqlite`
- `ABSTRACTGATEWAY_RUNNER`: `1` for combined API+runner, `0` for API-only

Provider keys and endpoints:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`
- `PORTKEY_API_KEY` / `PORTKEY_CONFIG`
- `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY`
- `LMSTUDIO_BASE_URL`
- `OLLAMA_BASE_URL`
- `VLLM_BASE_URL`

Image/voice plugin endpoints:

- `ABSTRACTVISION_BACKEND`: `openai` (OpenAI-compatible HTTP), `diffusers`, or `sdcpp`
- `ABSTRACTVISION_BASE_URL` / `ABSTRACTVISION_API_KEY` / `ABSTRACTVISION_MODEL_ID`
- `ABSTRACTVOICE_TTS_ENGINE` / `ABSTRACTVOICE_STT_ENGINE`
- `ABSTRACTVOICE_REMOTE_BASE_URL` / `ABSTRACTVOICE_REMOTE_API_KEY`
- `ABSTRACTVOICE_TTS_MODEL` / `ABSTRACTVOICE_STT_MODEL`

Filesystem/media controls from AbstractCore remain available:

- `ABSTRACTCORE_SERVER_BASE_URL_ALLOWLIST`
- `ABSTRACTCORE_SERVER_URL_FETCH_ALLOWLIST`
- `ABSTRACTCORE_SERVER_MEDIA_ROOT`
- `ABSTRACTCORE_SERVER_ALLOW_LOCAL_FILES`

## Cache and auth notes

Gateway auth is controlled by `ABSTRACTGATEWAY_*` variables and protects
`/api/gateway/*`. AbstractCore provider/server auth variables control upstream
provider access inside AbstractCore integrations. Keep those two layers
separate: clients receive only the Gateway token, while provider keys stay in
the server environment.

Prompt-cache control endpoints are exposed under `/api/gateway/prompt_cache/*`
where supported by the active provider/model. Session lifecycle routes under
`/api/gateway/sessions/{session_id}/prompt_cache/*` provide Gateway-owned
naming/status/prepare/clear/rebuild orchestration on top of those provider
controls. They are not a provider-independent local KV cache or full
CachedSession persistence system.

## Local-source image

Before a version is published to PyPI, build from the checkout:

```bash
ABSTRACTGATEWAY_INSTALL_MODE=local \
ABSTRACTGATEWAY_IMAGE_TAG=0.2.3-local \
docker compose -f docker/abstractgateway-server/compose.yml up -d --build
```

Release automation builds the published image from the PyPI package after the
PyPI release is available, matching the AbstractCore server image pattern.
