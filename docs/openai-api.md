# OpenAI API

AbstractGateway serves an OpenAI-compatible API on its own listener, so any app,
SDK or tool that speaks the OpenAI API can use the models this gateway reaches
(local engines and connected providers) through one address:

```
http://<gateway host>:<port>/v1
```

The API key is the caller's own **gateway token**. There is no separate process
or port: the API follows the gateway's [Network](./configuration.md#network-exposure-localhost--local-network--internet)
setting, and AbstractCore answers each request behind it.

## Turn it on

Open the web console, **Models → OpenAI API**. The page has five cards:

- **Status**: Running or Stopped, the base URL with **Copy**, the **Endpoint**
  switch (on: apps can connect; off: apps get 404 and open requests end),
  **Restart** (ends open requests, keeps serving) and **Check setup** (settings,
  AbstractCore's server, whether the gateway listens where *Who can connect*
  needs it, and how many models `/v1/models` lists).
- **Connect your app**: the base URL and the API key. The key is the token you
  sign in with; **New key** replaces your gateway token and shows the new one
  once (your old token stops working everywhere). An admin signed in with the
  operator token uses that token.
- **Access**: *Authentication* and *Who can connect* (below). Changes apply to
  the next request.
- **Docs**: what is supported, links to this page and to AbstractCore's server
  docs, and a first request (curl, Python, JavaScript) with your base URL.
- **Recent requests**: time, client (account and address), model, tokens in and
  out, latency and status, newest first, refreshed every 5 s. **Open** opens the
  run in Observer when the request named one. An admin sees every request;
  anyone else sees their own.

Every signed-in account can open the page and copy the base URL; only an admin
changes the settings.

## Connect

Use `provider/model` as the model name, as listed by `GET /v1/models`.

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "ollama/qwen3:4b", "messages": [{"role": "user", "content": "Hello"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="YOUR_GATEWAY_TOKEN")
reply = client.chat.completions.create(
    model="ollama/qwen3:4b",
    messages=[{"role": "user", "content": "Hello"}],
)
print(reply.choices[0].message.content)
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://127.0.0.1:8080/v1", apiKey: "YOUR_GATEWAY_TOKEN" });
const reply = await client.chat.completions.create({
  model: "ollama/qwen3:4b",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(reply.choices[0].message.content);
```

The console fills in your real base URL and the gateway's default text model.

## What is supported

Checked with the official `openai` Python SDK:

| Endpoint | Notes |
| --- | --- |
| `GET /v1/models` | `{object: "list", data: [{id, object: "model", created, owned_by}]}` |
| `GET /v1/models/{id}` | one model; `404 model_not_found` otherwise |
| `POST /v1/chat/completions` | `id`, `object`, `created`, `model`, `choices[].message`, `finish_reason`, `usage.{prompt,completion,total}_tokens`; `tools` and `tool_choice` with structured `tool_calls` (`finish_reason: "tool_calls"`) and `role: "tool"` results; `stream: true` as SSE `data:` chunks (`chat.completion.chunk`, role on the first delta, `finish_reason` on the last chunk) ending with `data: [DONE]`; `stream_options.include_usage` adds a final chunk with `choices: []` and `usage`; `max_completion_tokens` (or `max_tokens`) |
| `POST /v1/embeddings` | `encoding_format` `float` or `base64` (the SDK's default) |

Also served when an engine for the capability is set up (AbstractCore answers;
not part of the SDK checks): `POST /v1/responses`, `POST /v1/audio/speech`,
`POST /v1/audio/transcriptions`, `POST /v1/audio/translations`,
`POST /v1/images/generations`, `POST /v1/images/edits`,
`POST /v1/images/variations`.

Not yet: `response_format` other than `{"type": "text"}` (JSON mode,
structured outputs), `n` greater than 1, `logprobs`, `logit_bias` (each answers
`400 unsupported_parameter` with the parameter named), and files, batches,
assistants, fine-tuning, moderations and realtime (`404`).

Accepted and ignored: `user`, `store`, `metadata`, `service_tier`,
`parallel_tool_calls` and the `OpenAI-Organization` / `OpenAI-Project` headers.

### Errors

Every error uses the OpenAI envelope, so SDKs raise their usual exceptions:

```json
{"error": {"message": "Incorrect API key provided: use your gateway token.", "type": "invalid_request_error", "param": null, "code": "invalid_api_key"}}
```

| Status | When | `code` |
| --- | --- | --- |
| 400 | invalid request or unsupported parameter | `unsupported_parameter` or none |
| 401 | missing or wrong key | `invalid_api_key` |
| 403 | the client is outside *Who can connect*, or a page without a key from an origin that is not accepted | `client_not_allowed`, `origin_not_allowed` |
| 404 | unknown route, unknown model, or the API is stopped | `model_not_found`, `endpoint_stopped` |
| 429 | too many refused keys from one address | `auth_lockout` |

Each response carries `x-request-id`.

## Access

**Authentication**

- **Protected (API key)** (default): every request sends a gateway token as
  `Authorization: Bearer <token>`. The gateway resolves it to the account; the
  token never reaches AbstractCore. Repeated wrong keys from one address are
  locked out like sign-in attempts.
- **Open (no key)**: requests without a key are served. SDKs still require some
  key value; any text works, for example `not-needed` (a key that is not a gateway token is served as anonymous). Open applies only to
  direct clients on this machine, your network or your VPN: a request through a
  proxy, in Internet mode, or with proxies elsewhere trusted needs a key. Cloud
  providers whose keys are stored on the gateway still need a key; open clients
  can use local engines or send their own provider key in
  `X-AbstractCore-Provider-API-Key`.

**Who can connect** filters on the client address:

| Choice | Admits |
| --- | --- |
| This machine only (default) | loopback |
| Devices on my network | also private LAN and link-local addresses |
| Tailnet (shown when this device is on Tailscale) | also Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) |
| Anywhere | every address; needs **Internet** on the Network page (with its confirmation) and Protected |

The client address is the socket peer, or the first `X-Forwarded-For` hop when
the peer is a proxy on this machine (`tailscale serve`, a local nginx) or when
**Trust proxies on other machines** is on. A proxy on this machine that forwards
without `X-Forwarded-For` counts as *Anywhere*. The gateway must also listen
where you allow: the page says so when Network is set to this computer only.

**Browsers.** A web page that sends a key may call the API from any origin (the
key is the credential; no cookie is used). Without a key, only the gateway's
[accepted origins](./configuration.md#detected-addresses-are-accepted-origins)
may call it.

## Request log

Each `/v1` request writes one line to the gateway's audit log
(`<data dir>/audit_log.jsonl`) with an `openai_api` object: client, model,
`prompt_tokens`, `completion_tokens`, `stream`, and `run_id` when the request
sent `X-AbstractCore-Run-Id`. Prompts and replies are never logged. The log
shares the audit log's rotation and retention (`ABSTRACTGATEWAY_AUDIT_LOG_*`);
with the audit log off, the card stays empty.

## Admin API

| Method | Route | Who | Result |
| --- | --- | --- | --- |
| GET | `/api/gateway/openai-api` | any account | status `gateway_openai_api_v1`: `enabled`, `access`, `reach`, `base_url`, `reach_options[]`, `warnings[]`, `listener`, `tailscale`, `key`, `support`, `example_model`, `writable` |
| GET | `/api/gateway/openai-api/logs?limit=` | any account | `{rows[], scope}`: every request for an admin, the caller's own otherwise |
| POST | `/api/gateway/admin/core-endpoint` | admin | `{enabled?, access?: token\|open, reach?: machine\|network\|tailnet\|anywhere}`; `409` with the reason when refused |
| POST | `/api/gateway/admin/core-endpoint/restart` | admin | ends open requests; `{ended_requests}` |
| POST | `/api/gateway/admin/core-endpoint/check` | admin | `{checks[{id, ok, text}], ok}` |
| GET | `/api/gateway/admin/core-endpoint` | admin | the same status |

Settings live in `<data dir>/config/core_endpoint.json` (owner-only, atomic
writes).

## Moving from `/core/v1`

`/core/v1/...` answers `308 Permanent Redirect` to `/v1/...` (method and body
kept) and is deprecated: point your apps at `/v1`. The dedicated endpoint token
of gateway 0.12.0 is still accepted as a key, and its routes
(`/api/gateway/admin/core-endpoint/token/reveal|rotate`) remain, both
deprecated: use gateway tokens. A 0.12.0 settings file without *Who can
connect* reads as **Devices on my network**.
