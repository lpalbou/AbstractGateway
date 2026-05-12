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

Use Gateway-scoped env vars so the Gateway catalog routes and runtime use the
same settings.

```bash
export ABSTRACTGATEWAY_AUTH_TOKEN="dev-local-token"

# Local LLM routing. Replace the model with the local MLX model you want.
export ABSTRACTGATEWAY_PROVIDER="mlx"
export ABSTRACTGATEWAY_MODEL="mlx-community/Qwen3-4B-4bit"

# Local image generation on MPS through AbstractVision/Diffusers.
export ABSTRACTGATEWAY_VISION_BACKEND="diffusers"
export ABSTRACTGATEWAY_VISION_MODEL_ID="runwayml/stable-diffusion-v1-5"
export ABSTRACTGATEWAY_VISION_DIFFUSERS_DEVICE="mps"

# Local voice generation and transcription through AbstractVoice.
export ABSTRACTGATEWAY_VOICE_TTS_ENGINE="piper"
export ABSTRACTGATEWAY_VOICE_STT_ENGINE="faster_whisper"
export ABSTRACTGATEWAY_VOICE_STT_MODEL="base"
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
export ABSTRACTGATEWAY_AUTH_TOKEN="dev-local-token"
```

Check that catalogs are not just defaults:

```bash
curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  http://127.0.0.1:8080/api/gateway/voice/voices

curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  http://127.0.0.1:8080/api/gateway/audio/speech/models

curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  http://127.0.0.1:8080/api/gateway/audio/transcriptions/models

curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  "http://127.0.0.1:8080/api/gateway/vision/provider_models?task=text_to_image"

curl -H "Authorization: Bearer $ABSTRACTGATEWAY_AUTH_TOKEN" \
  http://127.0.0.1:8080/api/gateway/vision/models
```

If these return only `gateway_static` defaults, stop the Gateway process and
restart the one from this venv. A common mistake is leaving an older globally
installed `abstractgateway serve --port 8080` process running.

## 5. Start Flow

```bash
abstractflow serve \
  --host 127.0.0.1 \
  --port 3003 \
  --gateway-url http://127.0.0.1:8080 \
  --gateway-token "$ABSTRACTGATEWAY_AUTH_TOKEN"
```

Open `http://127.0.0.1:3003`.

In the editor, media nodes use Gateway catalogs:

- Generate Image: image provider/model
- Generate Voice: voice profile/clone plus TTS model
- Transcribe Audio: STT model
- Listen Voice: voice-input wait metadata

