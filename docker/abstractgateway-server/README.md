# AbstractGateway Server Image

This image packages the AbstractGateway HTTP/SSE server for durable
AbstractRuntime runs:

```bash
ghcr.io/lpalbou/abstractgateway:0.2.27
```

Release images are published for `linux/amd64` and `linux/arm64`.
A separate experimental full NVIDIA image is published best-effort for
`linux/amd64`:

```bash
ghcr.io/lpalbou/abstractgateway:0.2.27-gpu
```

Legacy aliases `ghcr.io/lpalbou/abstractgateway-server:*` and
`ghcr.io/lpalbou/abstractgateway-server-nvidia:*` are still published for a
transition period. New deployments should use `abstractgateway`.

The image installs:

```bash
abstractgateway==<version>
```

The default image uses the base remote-light server install. It includes
`AbstractRuntime`, Runtime-owned provider/tool and
multimodal facades, OpenAI-compatible text/media provider routing,
provider/session prompt-cache helpers, KG memory via AbstractMemory/LanceDB,
tool helpers, FastAPI/Uvicorn, AbstractAgent, and AbstractFlow compatibility.
It intentionally does not bundle local sentence-transformer embeddings or model
runtimes such as PyTorch/CUDA, MLX, vLLM, HuggingFace Transformers, local
Diffusers/sdcpp vision backends, or local voice/music generation engines.
Remote embeddings remain supported through the `embedding.text` capability
route when it targets hosted providers, LM Studio, vLLM, OpenAI-compatible
embedding endpoints, or a remote AbstractCore server.

The NVIDIA image installs `abstractgateway[gpu]` on a CUDA/PyTorch base and
adds vLLM/HuggingFace, local Diffusers image generation, and local
voice engines. It is experimental until a real CUDA host build/smoke gate is
part of release validation. Apple MLX inference is not packaged as a Docker image because
Linux containers do not have access to Apple's Metal/MLX runtime. For Apple
Silicon, run the MLX-capable service natively on macOS and point this container
at its OpenAI-compatible endpoint. Docker Model Runner is reachable from
containers at `http://model-runner.docker.internal/engines/v1`; LM Studio can
serve local MLX models on `http://localhost:1234/v1`; Ollama can serve its
OpenAI-compatible API on `http://localhost:11434/v1`, including builds/models
that route through its native MLX runner.

## Run

Keep secrets in an uncommitted env file:

```bash
ABSTRACTGATEWAY_USER_AUTH=1
ABSTRACTGATEWAY_BOOTSTRAP_ADMIN=1
ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=0
ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN=
# Optional legacy shared admin token; browser apps should use Gateway user tokens.
ABSTRACTGATEWAY_AUTH_TOKEN=
ABSTRACTGATEWAY_PROVIDER=openai-compatible
ABSTRACTGATEWAY_MODEL=your-model
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...
PORTKEY_API_KEY=pk_...
PORTKEY_CONFIG=pcfg_...
OPENAI_BASE_URL=http://host.docker.internal:1234/v1
# Legacy/compatibility alias used by some operators; AbstractCore discovery uses OPENAI_BASE_URL.
OPENAI_COMPATIBLE_BASE_URL=
OPENAI_COMPATIBLE_API_KEY=optional
LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
OLLAMA_BASE_URL=http://host.docker.internal:11434
ABSTRACTVISION_BASE_URL=https://api.openai.com/v1
ABSTRACTVISION_API_KEY=optional
ABSTRACTVISION_MODEL_ID=gpt-image-1
ABSTRACTVOICE_TTS_ENGINE=openai
ABSTRACTVOICE_STT_ENGINE=openai
ABSTRACTVOICE_REMOTE_BASE_URL=
ABSTRACTVOICE_REMOTE_API_KEY=
```

Then run:

```bash
docker run --rm --name abstractgateway \
  -p 8080:8080 \
  --env-file .env \
  -v "$PWD/runtime:/data" \
  -v "$PWD/workspace:/workspace" \
  ghcr.io/lpalbou/abstractgateway:latest
```

The container uses Gateway user auth by default. On first start the entrypoint
creates the `default/admin` user and writes the raw token once to
`runtime/auth/bootstrap-admin-token` inside the mounted data directory. Use that
token in `/console` and then create/rotate/manage users from the Gateway
Console. Set `ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=1` only for local demos
where Docker logs are private. Provider keys stay inside the container.

For local OpenAI-compatible text endpoints such as Docker Model Runner, LM
Studio, `mlx_lm.server`, or Ollama's `/v1` server, point the container at a URL
reachable from Docker:

```bash
docker run --rm --name abstractgateway \
  -p 8080:8080 \
  -e ABSTRACTGATEWAY_USER_AUTH=1 \
  -e ABSTRACTGATEWAY_PROVIDER="openai-compatible" \
  -e ABSTRACTGATEWAY_MODEL="your-model" \
  -e OPENAI_BASE_URL="http://host.docker.internal:1234/v1" \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -v "$PWD/runtime:/data" \
  ghcr.io/lpalbou/abstractgateway:latest
```

For LM Studio specifically, prefer the named provider so Gateway model
discovery uses `LMSTUDIO_BASE_URL`:

```bash
docker run --rm --name abstractgateway \
  -p 8080:8080 \
  -e ABSTRACTGATEWAY_USER_AUTH=1 \
  -e ABSTRACTGATEWAY_PROVIDER="lmstudio" \
  -e ABSTRACTGATEWAY_MODEL="your-loaded-model-id" \
  -e LMSTUDIO_BASE_URL="http://host.docker.internal:1234/v1" \
  -v "$PWD/runtime:/data" \
  ghcr.io/lpalbou/abstractgateway:latest
```

Use `http://model-runner.docker.internal/engines/v1` for Docker Model Runner,
or `http://host.docker.internal:11434/v1` when routing through Ollama's
OpenAI-compatible server. These text endpoints are not assumed to provide
`/v1/images/generations`.

## Docker Compose

```bash
cp docker/abstractgateway-server/.env.example docker/abstractgateway-server/.env
docker compose --env-file docker/abstractgateway-server/.env \
  -f docker/abstractgateway-server/compose.yml up -d
```

On an NVIDIA host with the NVIDIA Container Toolkit, use the experimental
overlay:

```bash
docker compose --env-file docker/abstractgateway-server/.env \
  -f docker/abstractgateway-server/compose.yml \
  -f docker/abstractgateway-server/compose.nvidia.yml up -d
```

Useful compose variables:

- `ABSTRACTGATEWAY_PORT`: host port, default `8080`
- `ABSTRACTGATEWAY_HOST_FLOWS_DIR`: host directory containing `*.flow` bundles
- `ABSTRACTGATEWAY_STORE_BACKEND`: `file` or `sqlite`
- `ABSTRACTGATEWAY_MEMORY_STORE_BACKEND`: `lancedb` or `memory`; `sqlite` works when the installed AbstractMemory build exposes `SQLiteTripleStore`
- `ABSTRACTGATEWAY_PROVIDER` / `ABSTRACTGATEWAY_MODEL`: transitional text fallback; prefer execution-host `output.text`
- `ABSTRACTGATEWAY_TOOL_MODE`: `approval`, `passthrough`, `delegated`, or local dev modes
- `ABSTRACTCORE_SERVER_BASE_URL`: optional standalone Core server for capability defaults and dynamic catalogs
- `embedding.text` capability default: configure through `abstractgateway-config set-default embedding.text ...`; remote/provider-backed embeddings work in the default image, local HuggingFace/sentence-transformer embeddings require an image built with `abstractgateway[embeddings]`
- `ABSTRACTVISION_*`: AbstractVision image backend or OpenAI-compatible image endpoint
- `ABSTRACTVOICE_*`: AbstractVoice TTS/STT backend, local/remote engine, and model controls
- `ABSTRACTGATEWAY_USER_AUTH`: `1` for per-user Gateway tokens and browser sessions
- `ABSTRACTGATEWAY_BOOTSTRAP_ADMIN`: `1` to ensure `default/admin` exists at container start
- `ABSTRACTGATEWAY_EXTRAS`: build-time install extra for local image builds (empty for the default image, `gpu` for NVIDIA)

Release scope: TTS, STT, and generated images are direct Gateway endpoints.
Generated images are also available through Runtime workflows with a configured
image backend.

For unreleased local checkouts, build the image from this repository:

```bash
ABSTRACTGATEWAY_INSTALL_MODE=local \
ABSTRACTGATEWAY_IMAGE_TAG=0.2.27-local \
docker compose -f docker/abstractgateway-server/compose.yml up -d --build
```

## Smoke Checks

```bash
curl http://localhost:8080/api/health

ADMIN_TOKEN="$(docker exec abstractgateway cat /data/auth/bootstrap-admin-token)"
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8080/api/gateway/me
```
