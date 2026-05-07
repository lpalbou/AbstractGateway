# AbstractGateway Server Image

This image packages the AbstractGateway HTTP/SSE server for durable
AbstractRuntime runs:

```bash
ghcr.io/lpalbou/abstractgateway-server:0.2.2
```

Release images are published for `linux/amd64` and `linux/arm64`.

The image installs:

```bash
abstractgateway[server]==<version>
```

The `server` extra includes `AbstractRuntime[multimodal]`, AbstractCore
remote/commercial provider support, OpenAI-compatible text provider routing,
workflow-backed image generation through AbstractVision, direct Gateway
voice/audio endpoints through AbstractVoice, provider-level prompt-cache
helpers, media parsing, tool helpers, token counting, compression helpers,
FastAPI/Uvicorn, AbstractAgent, and AbstractFlow compatibility. It intentionally
does not bundle
local model runtimes such as MLX, vLLM, HuggingFace Transformers, local
Diffusers/sdcpp vision backends, or local voice/music generation engines.

## Run

Keep secrets in an uncommitted env file:

```bash
ABSTRACTGATEWAY_AUTH_TOKEN=replace-with-a-gateway-token
ABSTRACTGATEWAY_PROVIDER=openai-compatible
ABSTRACTGATEWAY_MODEL=your-model
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...
PORTKEY_API_KEY=pk_...
PORTKEY_CONFIG=pcfg_...
OPENAI_COMPATIBLE_BASE_URL=http://host.docker.internal:1234/v1
OPENAI_COMPATIBLE_API_KEY=optional
ABSTRACTVISION_BASE_URL=http://host.docker.internal:8000/v1
ABSTRACTVISION_API_KEY=optional
ABSTRACTVISION_MODEL_ID=gpt-image-1
ABSTRACTVOICE_TTS_ENGINE=auto
ABSTRACTVOICE_STT_ENGINE=auto
ABSTRACTVOICE_REMOTE_BASE_URL=
ABSTRACTVOICE_REMOTE_API_KEY=
```

Then run:

```bash
docker run --rm --name abstractgateway-server \
  -p 127.0.0.1:8080:8080 \
  --env-file .env \
  -v "$PWD/runtime/gateway:/data/gateway" \
  -v "$PWD/flows/bundles:/data/flows:ro" \
  -v "$PWD/workspace:/workspace" \
  ghcr.io/lpalbou/abstractgateway-server:0.2.2
```

`ABSTRACTGATEWAY_AUTH_TOKEN` is the gateway bearer token. Clients send it as
`Authorization: Bearer <token>`, including from Swagger UI's `Authorize`
button. Provider keys stay inside the container.

For local OpenAI-compatible endpoints such as LM Studio or Ollama's `/v1`
server, point the container at a URL reachable from Docker:

```bash
docker run --rm --name abstractgateway-server \
  -p 127.0.0.1:8080:8080 \
  -e ABSTRACTGATEWAY_AUTH_TOKEN="$ABSTRACTGATEWAY_AUTH_TOKEN" \
  -e ABSTRACTGATEWAY_PROVIDER="openai-compatible" \
  -e ABSTRACTGATEWAY_MODEL="your-model" \
  -e OPENAI_COMPATIBLE_BASE_URL="http://host.docker.internal:1234/v1" \
  -e OPENAI_COMPATIBLE_API_KEY="$OPENAI_COMPATIBLE_API_KEY" \
  ghcr.io/lpalbou/abstractgateway-server:0.2.2
```

## Docker Compose

```bash
cp docker/abstractgateway-server/.env.example docker/abstractgateway-server/.env
docker compose --env-file docker/abstractgateway-server/.env \
  -f docker/abstractgateway-server/compose.yml up -d
```

Useful compose variables:

- `ABSTRACTGATEWAY_PORT`: host port, default `8080`
- `ABSTRACTGATEWAY_HOST_FLOWS_DIR`: host directory containing `*.flow` bundles
- `ABSTRACTGATEWAY_STORE_BACKEND`: `file` or `sqlite`
- `ABSTRACTGATEWAY_PROVIDER` / `ABSTRACTGATEWAY_MODEL`: defaults for bundles
- `ABSTRACTGATEWAY_TOOL_MODE`: `approval`, `passthrough`, `delegated`, or local dev modes
- `ABSTRACTGATEWAY_EMBEDDING_PROVIDER` / `ABSTRACTGATEWAY_EMBEDDING_MODEL`: optional embedding space
- `ABSTRACTVISION_*`: AbstractVision image backend or OpenAI-compatible image endpoint
- `ABSTRACTVOICE_*`: AbstractVoice TTS/STT backend, local/remote engine, and model controls

Release scope: TTS and STT are direct Gateway endpoints. Generated images are
available through Runtime/Core workflows with AbstractVision installed and
configured; Gateway does not yet expose a direct image-generation endpoint.

For unreleased local checkouts, build the image from this repository:

```bash
ABSTRACTGATEWAY_INSTALL_MODE=local \
ABSTRACTGATEWAY_IMAGE_TAG=0.2.2-local \
docker compose -f docker/abstractgateway-server/compose.yml up -d --build
```

## Smoke Checks

```bash
curl http://localhost:8080/api/health

curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  http://localhost:8080/api/gateway/bundles
```
