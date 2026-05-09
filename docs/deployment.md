# AbstractGateway deployment

AbstractGateway can run as a Python process or as a containerized server. The
container path is the recommended baseline for a single self-contained Gateway
deployment because it packages the HTTP API, durable runner, AbstractRuntime,
and the remote-provider/multimodal AbstractCore stack together.

## Published image

Release images are published to GHCR. The default image is the light,
portable server image:

```bash
docker pull ghcr.io/lpalbou/abstractgateway-server:0.2.6
```

NVIDIA hosts can try the experimental full GPU image when local
vLLM/HuggingFace/Diffusers engines are wanted. This image is published
best-effort until it has a real CUDA build and smoke gate:

```bash
docker pull ghcr.io/lpalbou/abstractgateway-server-nvidia:0.2.6
```

The default image installs the base `abstractgateway` package, which includes:

- `AbstractRuntime[multimodal]`
- `abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]`
- `AbstractMemory[lancedb]>=0.2.6`
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
engines, and future AbstractMusic engines remain explicit opt-ins.

The NVIDIA image installs `abstractgateway[gpu]` and uses a CUDA/PyTorch base.
It is experimental and release automation publishes it as
best-effort for `linux/amd64`; the default image remains the release-grade
portable `linux/amd64` and `linux/arm64` image. Treat the NVIDIA image as
production-ready only after a CUDA host build/smoke gate is added and passes.

### Apple Silicon / MLX

There is no Apple/MLX Gateway Docker image target. MLX uses Apple's Metal
stack, while Docker Desktop runs Linux containers without Metal/MPS device
access. The supported Docker shape is a lightweight Gateway container calling a
host-native OpenAI-compatible inference endpoint:

```bash
docker run --rm --name abstractgateway-server \
  -p 127.0.0.1:8080:8080 \
  -e ABSTRACTGATEWAY_AUTH_TOKEN="$ABSTRACTGATEWAY_AUTH_TOKEN" \
  -e ABSTRACTGATEWAY_PROVIDER="openai-compatible" \
  -e ABSTRACTGATEWAY_MODEL="your-model" \
  -e OPENAI_COMPATIBLE_BASE_URL="http://model-runner.docker.internal/engines/v1" \
  -v "$PWD/runtime/gateway:/data/gateway" \
  -v "$PWD/flows/bundles:/data/flows:ro" \
  ghcr.io/lpalbou/abstractgateway-server:0.2.6
```

Other host-native endpoints are also valid: LM Studio at
`http://host.docker.internal:1234/v1`, Ollama's OpenAI-compatible API at
`http://host.docker.internal:11434/v1`, or `mlx_lm.server` exposed on a host
port. For fully native non-Docker installs with local engines, use
`pip install "abstractgateway[apple]"` on Apple Silicon, and
`pip install "abstractgateway[gpu]"` on GPU workstations or NVIDIA Docker builds.

## Compose quickstart

Create an env file from the template, set a strong token, then start the
server:

```bash
cp docker/abstractgateway-server/.env.example docker/abstractgateway-server/.env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
docker compose --env-file docker/abstractgateway-server/.env \
  -f docker/abstractgateway-server/compose.yml up -d
```

For the experimental NVIDIA image on a GPU host with the NVIDIA Container
Toolkit:

```bash
docker compose --env-file docker/abstractgateway-server/.env \
  -f docker/abstractgateway-server/compose.yml \
  -f docker/abstractgateway-server/compose.nvidia.yml up -d
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
- `ABSTRACTGATEWAY_MEMORY_STORE_BACKEND`: `lancedb` or `memory` for KG workflows and `/kg/query`; `sqlite` works when the installed AbstractMemory build exposes `SQLiteTripleStore`

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

- `ABSTRACTVISION_BACKEND`: `openai`, `openai-compatible`, `diffusers`, or `sdcpp`
- `ABSTRACTVISION_BASE_URL` / `ABSTRACTVISION_API_KEY` / `ABSTRACTVISION_MODEL_ID` (OpenAI defaults are used by the server image)
- `ABSTRACTVOICE_TTS_ENGINE` / `ABSTRACTVOICE_STT_ENGINE` (`openai` by default in the server image)
- `ABSTRACTVOICE_REMOTE_BASE_URL` / `ABSTRACTVOICE_REMOTE_API_KEY`
- `ABSTRACTVOICE_TTS_MODEL` / `ABSTRACTVOICE_STT_MODEL`

Core catalog proxying:

- `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL`: explicit standalone Core server URL for voice/vision catalog routes
- `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN`: Core server auth token, separate from Gateway auth
- `ABSTRACTGATEWAY_CORE_CATALOG_TIMEOUT_S`: timeout for catalog routes

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
ABSTRACTGATEWAY_IMAGE_TAG=0.2.6-local \
docker compose -f docker/abstractgateway-server/compose.yml up -d --build
```

Release automation builds the published image from the PyPI package after the
PyPI release is available, matching the AbstractCore server image pattern.
