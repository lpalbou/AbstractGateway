# AbstractGateway deployment

AbstractGateway can run as a Python process or as a containerized server. The
container path is the recommended baseline for a single self-contained Gateway
deployment because it packages the HTTP API, durable runner, AbstractRuntime,
and the Runtime-owned provider/tool stack together.

## Published image

Release images are published to GHCR. The default image is the light,
portable server image:

```bash
docker pull ghcr.io/lpalbou/abstractgateway:0.13.0
```

NVIDIA hosts can try the experimental full GPU image when local
vLLM/HuggingFace/Diffusers engines are wanted. This image is published
best-effort until it has a real CUDA build and smoke gate:

```bash
docker pull ghcr.io/lpalbou/abstractgateway:0.13.0-gpu
```

The image names `ghcr.io/lpalbou/abstractgateway-server:*` and
`ghcr.io/lpalbou/abstractgateway-server-nvidia:*` are published as aliases for
existing deployments; use `abstractgateway` for new ones.

The default image installs the base `abstractgateway` package, which includes:

- `AbstractRuntime`
- `AbstractMemory[lancedb]>=0.3.0`
- `abstractagent`
- FastAPI/Uvicorn

This profile supports hosted/commercial providers, OpenAI-compatible text
and multimodal provider routing, Runtime-owned tool execution, KG memory, and
provider/session prompt-cache controls. Remote embeddings are included through
the `embedding.text` capability route for hosted providers, LM Studio, vLLM,
other OpenAI-compatible endpoints, or a remote AbstractCore server. Local
sentence-transformer embeddings and hardware-local model runtimes remain
explicit opt-ins, so the base Linux image does not pull PyTorch/CUDA runtime
packages. MLX, vLLM, HuggingFace
Transformers, local Diffusers/sdcpp, AbstractVoice local engines, and local
AbstractMusic engines belong in native `abstractgateway[apple]` or
`abstractgateway[gpu]` installs.

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
docker run --rm --name abstractgateway \
  -p 8080:8080 \
  -e ABSTRACTGATEWAY_DATA_DIR=/data \
  -e ABSTRACTGATEWAY_USER_AUTH=1 \
  -e OPENAI_BASE_URL="http://model-runner.docker.internal/engines/v1" \
  -v "$PWD/runtime:/data" \
  ghcr.io/lpalbou/abstractgateway:latest
```

Set the execution-host text route separately:

```bash
docker exec abstractgateway abstractgateway-config set-default input.text \
  --provider openai-compatible \
  --model your-model \
  --base-url http://model-runner.docker.internal/engines/v1
```

Other host-native endpoints are also valid: LM Studio at
`http://host.docker.internal:1234/v1` with `LMSTUDIO_BASE_URL`, Ollama at
`http://host.docker.internal:11434` with `OLLAMA_BASE_URL`, or `mlx_lm.server`
exposed on a host port. For fully native non-Docker installs with local engines, use
`pip install "abstractgateway[apple]"` on Apple Silicon, and
`pip install "abstractgateway[gpu]"` on GPU workstations or NVIDIA Docker builds.

## Compose quickstart

Create an env file from the template, adjust provider keys/defaults, then start
the server. The default env keeps user auth enabled and bootstraps
`default/admin` if missing:

```bash
cp docker/abstractgateway-server/.env.example docker/abstractgateway-server/.env
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
data volume at `/data`, and exposes a container workspace at `/workspace`. It
serves the workflows the image ships with — `basic-agent`, `coding-agent`,
`deep-research`, `co-scientist`, and more
([shipped-workflows.md](./shipped-workflows.md)).

To serve your own bundles instead, point `ABSTRACTGATEWAY_HOST_FLOWS_DIR` at
your bundle directory (mounted read-only at `/data/flows`) and set
`ABSTRACTGATEWAY_FLOWS_DIR=/data/flows`:

```bash
ABSTRACTGATEWAY_HOST_FLOWS_DIR=/path/to/bundles \
ABSTRACTGATEWAY_FLOWS_DIR=/data/flows \
  docker compose -f docker/abstractgateway-server/compose.yml up -d
```

Smoke checks:

```bash
curl http://127.0.0.1:8080/api/health

ADMIN_TOKEN="$(docker compose -f docker/abstractgateway-server/compose.yml exec -T abstractgateway cat /data/auth/bootstrap-admin-token)"
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8080/api/gateway/me
```

## Core configuration

Required for hosted/container user-auth mode:

- `ABSTRACTGATEWAY_USER_AUTH=1`: enables Gateway user tokens and per-user routing
- `ABSTRACTGATEWAY_BOOTSTRAP_ADMIN=1`: creates `default/admin` if missing

Optional:

- `ABSTRACTGATEWAY_AUTH_TOKEN`: a shared admin bearer token for
  server/operator scripts; browser apps use Gateway user accounts

Common:

- Browser origins and trust proxy are settings, not variables: the console's
  Network → *Advanced: reverse proxy*, the TUI's Connection screen, or
  `abstractgateway network set --allowed-origins https://gateway.example.com --trust-proxy on`
  (inside a container: `docker exec <container> abstractgateway network set …`).
  They apply to the next request. `ABSTRACTGATEWAY_ALLOWED_ORIGINS` in the
  container environment still pins the origins (reported as
  `overridden_by_env`). `ABSTRACTGATEWAY_TRUST_PROXY` does NOT pin trust proxy:
  it is used only while nothing is saved, and a saved switch (from the
  console, the TUI or `network set`, possibly in a mounted data folder) wins
  over it; check `abstractgateway network status` in the container. See
  [configuration.md](./configuration.md#reverse-proxy-allowed-origins-and-trust-proxy).
- `input.text` capability route: default for LLM/agent nodes
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
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` for generic OpenAI-compatible endpoints
- `OPENAI_COMPATIBLE_BASE_URL` / `OPENAI_COMPATIBLE_API_KEY` (aliases); prefer `OPENAI_BASE_URL` for AbstractCore discovery
- `LMSTUDIO_BASE_URL`
- `OLLAMA_BASE_URL`
- `VLLM_BASE_URL`

Image/voice plugin endpoints:

- `ABSTRACTVISION_BACKEND`: `openai`, `openai-compatible`, `diffusers`, or `sdcpp`
- `ABSTRACTGATEWAY_VISION_BACKEND` / `ABSTRACTGATEWAY_VISION_BASE_URL` / `ABSTRACTGATEWAY_VISION_API_KEY` / `ABSTRACTGATEWAY_VISION_MODEL_ID` (the `ABSTRACTVISION_*` names also work)
- `ABSTRACTGATEWAY_VOICE_TTS_ENGINE` / `ABSTRACTGATEWAY_VOICE_STT_ENGINE` (`openai` by default in the server image; the `ABSTRACTVOICE_*` names also work)
- `ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL` / `ABSTRACTGATEWAY_VOICE_REMOTE_API_KEY`
- `ABSTRACTGATEWAY_VOICE_TTS_MODEL` / `ABSTRACTGATEWAY_VOICE_STT_MODEL`

Core catalog proxying:

- `ABSTRACTCORE_SERVER_BASE_URL`: explicit standalone Core server URL for voice, TTS/STT, and vision catalog routes
- `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN`: Core server auth token, separate from Gateway auth
- `ABSTRACTGATEWAY_CORE_CATALOG_TIMEOUT_S`: timeout for catalog routes

Filesystem/media controls from AbstractCore remain available:

- `ABSTRACTCORE_SERVER_BASE_URL_ALLOWLIST`
- `ABSTRACTCORE_SERVER_URL_FETCH_ALLOWLIST`
- `ABSTRACTCORE_SERVER_MEDIA_ROOT`
- `ABSTRACTCORE_SERVER_ALLOW_LOCAL_FILES`

## Single machine without Docker

On a desktop or laptop, `abstractgateway service install` registers the
gateway as a per-user login service (macOS LaunchAgent, Linux systemd user
unit or XDG autostart entry, Windows Run entry) that runs plain `serve`, so the
[network exposure](./configuration.md#network-exposure-localhost--local-network--internet)
setting decides the bind (seeded to `localhost`, i.e. `127.0.0.1`, on install),
with data in the per-user data folder. See [first-run.md](./first-run.md). Containers and
servers use the explicit configuration shown on this page: the image sets
`--host 0.0.0.0` with user accounts on.

**A hung gateway restarts itself.** `serve` runs an event-loop watchdog: when
the loop has not run for `--watchdog-seconds` (default 30), the gateway dumps
every thread's stack to its log and exits with code 75. The LaunchAgent
(`KeepAlive` with `SuccessfulExit: false`) and the systemd unit
(`Restart=on-failure`) restart any non-zero exit; in a container, use a
restart policy (`restart: unless-stopped`) for the same effect. `GET
/api/health` reports `watchdog: {enabled, limit_s, last_tick_age_s}`. See
[troubleshooting.md](./troubleshooting.md#the-log-shows-fatal-gateway-watchdog-and-the-gateway-restarted-exit-code-75).

## Behind a reverse proxy (one block, apps included)

The console, the API and every browser app share the gateway's one address:
the apps are served at `/apps/<app>/` by the gateway itself
([apps.md](./apps.md#apps-are-served-through-the-gateway)). So one proxy
block covers everything. It must pass WebSocket upgrades (Flow's live
editor), must not buffer (live updates are server-sent events), and must keep
the `Host` the browser used (the sign-in handover is bound to it):

```nginx
server {
    listen 443 ssl;
    server_name gateway.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 1h;
        client_max_body_size 0;
    }
}

# In the http {} block:
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Run nginx on the gateway machine, as here, so the gateway believes its
`X-Forwarded-For` and `X-Forwarded-Proto` (it believes them from a loopback
peer only). An https page calling its own address through such a proxy
(Origin `https://` + the kept `Host`) is accepted without further setup. A
proxy on another machine, or one that rewrites `Host`, needs the browser
origin set once:
`abstractgateway network set --allowed-origins https://gateway.example.com`.
`X-Forwarded-For $remote_addr` replaces anything the browser sent. The apps
themselves always listen on `127.0.0.1`; nothing else needs to reach them,
and no other port needs to be exposed. A tunnel (Cloudflare Tunnel,
Tailscale Funnel, ngrok) to the gateway's port works the same way.

Reached through Tailscale? On the gateway machine run
`tailscale serve --bg http://127.0.0.1:<port>` and open
`https://<host>.<tailnet>.ts.net/`; `tailscale serve reset` undoes it. Voice
and camera in the browser need this https address. See
[configuration.md](./configuration.md#reached-through-tailscale-https).

## Where local clients find the gateway (`~/.abstractframework/gateway.json`)

The gateway does not always listen on 8080 (the installer moves it when 8080
is busy, and an admin can change the port). Clients that cannot ask
`abstractgateway` (the terminal apps, the browser apps started by hand with
`npx`, the frozen Assistant app) read one small file instead:

```json
{"schema": 1, "url": "http://127.0.0.1:8081", "port": 8081,
 "data_dir": "/home/me/.local/share/abstractgateway",
 "updated_at": "2026-09-27T12:00:00Z", "written_by": "serve"}
```

- `abstractgateway serve` writes it (mode 0600, atomically) once its listener
  is bound, with the port it bound, but only when the file is absent and the
  gateway uses the default data folder, or when the file names this
  gateway's own data folder. A test or second gateway with its own data
  folder never changes it. The installer writes it too
  (`"written_by": "installer"`), and its uninstall deletes this one file.
- `network set` does not change it: clients move when the gateway does, at the
  restart that binds the new port. `serve` never deletes it.
- It holds no token and no liveness information. Readers accept it only with
  `schema` 1, a URL on `127.0.0.1`, `::1` or `localhost`, and (Linux, macOS)
  when it belongs to them; anything else is ignored with one warning.
- The order a client follows: its launch flag (`--gateway-url`), its legacy
  environment variable, its saved sign-in (a saved `http://127.0.0.1:8080`
  gives way to the file), this file, then `http://127.0.0.1:8080`.

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
ABSTRACTGATEWAY_IMAGE_TAG=0.13.0-local \
docker compose -f docker/abstractgateway-server/compose.yml up -d --build
```

Release automation builds the published image from the PyPI package after the
PyPI release is available, matching the AbstractCore server image pattern.
