# Apple Silicon local Gateway + Flow quickstart

This guide starts a local-only AbstractGateway and AbstractFlow editor on an Apple
Silicon Mac. It uses PyPI packages, not a source checkout.

## 1. Create an environment

```bash
python3 -m venv ~/.venvs/abstractframework-local
source ~/.venvs/abstractframework-local/bin/activate
python -m pip install -U pip
```

## 2. Install Gateway and Flow

```bash
pip install "abstractgateway[apple]" abstractflow
```

`abstractgateway[apple]` is the native Apple profile. It cascades into the local
AbstractCore, AbstractRuntime, AbstractVision, and AbstractVoice capability
packages needed by Gateway.

## 3. Configure local engines

Use Gateway env vars for Gateway internals, then set framework model defaults through capability
routes owned by the execution host.

```bash
export ABSTRACTGATEWAY_USER_AUTH=1
export ABSTRACTGATEWAY_DATA_DIR="$HOME/.abstractgateway-local"

# Local image generation on MPS through AbstractVision/Diffusers.
export ABSTRACTGATEWAY_VISION_BACKEND="diffusers"
export ABSTRACTGATEWAY_VISION_MODEL_ID="runwayml/stable-diffusion-v1-5"
export ABSTRACTGATEWAY_VISION_DIFFUSERS_DEVICE="mps"

# Alternative: Apple-local MLX-Gen through AbstractVision q4 presets.
# Pre-download with: abstractvision download flux2-klein-4b --provider mlx-gen
# export ABSTRACTGATEWAY_VISION_BACKEND="mlx-gen"
# export ABSTRACTGATEWAY_VISION_MODEL_ID="mlx-gen/flux2-klein-4b"

# Local voice generation and transcription through AbstractVoice.
export ABSTRACTGATEWAY_VOICE_TTS_ENGINE="piper"
export ABSTRACTGATEWAY_VOICE_STT_ENGINE="faster_whisper"
export ABSTRACTGATEWAY_VOICE_STT_MODEL="base"
```

Set the default text route:

```bash
abstractgateway-config set-default input.text \
  --provider lmstudio \
  --model qwen/qwen3.6-35b-a3b \
  --base-url http://127.0.0.1:1234/v1
```

Optional prefetch for voice models:

```bash
abstractvoice-prefetch --piper en
abstractvoice-prefetch --stt base
```

## 4. Start Gateway

```bash
abstractgateway serve --host 127.0.0.1 --port 8080
```

In another terminal with the same venv:

```bash
source ~/.venvs/abstractframework-local/bin/activate
export ABSTRACTGATEWAY_DATA_DIR="$HOME/.abstractgateway-local"
export GATEWAY_ADMIN_TOKEN="$(cat "$ABSTRACTGATEWAY_DATA_DIR/auth/bootstrap-admin-token")"
```

Check that catalogs are not just defaults:

```bash
curl -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  http://127.0.0.1:8080/api/gateway/voice/voices

curl -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  http://127.0.0.1:8080/api/gateway/audio/speech/models

curl -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  http://127.0.0.1:8080/api/gateway/audio/transcriptions/models

curl -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  "http://127.0.0.1:8080/api/gateway/vision/provider_models?task=text_to_image"

curl -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  http://127.0.0.1:8080/api/gateway/vision/models
```

If these return only `gateway_static` defaults, stop the Gateway process and
restart the one from this venv. A common mistake is leaving an older globally
installed `abstractgateway serve --port 8080` process running.

## 5. Start Flow

```bash
npx @abstractframework/flow --gateway-url http://127.0.0.1:8080 --port 3003
```

Open `http://127.0.0.1:3003` and sign in as Gateway user `admin` with
`$GATEWAY_ADMIN_TOKEN`.

In the editor, media nodes use Gateway catalogs:

- Generate Image: image provider/model
- Generate Voice: voice profile/clone plus TTS model
- Transcribe Audio: STT model
- Listen Voice: voice-input wait metadata
