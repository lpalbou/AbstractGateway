# OpenAI API

AbstractGateway serves an OpenAI-compatible API on its own listener, so any app,
SDK or tool that speaks the OpenAI API can use the models this gateway reaches
(local engines and connected providers) through one address:

```
http://<gateway host>:<port>/v1
```

The API key is one of your account's **API keys**: named keys you make on the
OpenAI API page, one per app, valid at `/v1` only (see [API keys](#api-keys)).
There is no separate process or port: the API follows the gateway's [Network](./configuration.md#network-exposure-localhost--local-network--internet)
setting, and AbstractCore answers each request behind it.

## Turn it on

Open the web console, **Models → OpenAI API**. An admin sees five cards; any
other account sees Status (running or not, the base URL), Connect your app
(their own API keys), Docs and Recent requests (their own):

- **Status**: Running or Stopped, the base URL with **Copy**; for an admin, the **Endpoint**
  switch (on: apps can connect; off: apps get 404 and open requests end),
  **Restart** (ends open requests, keeps serving) and **Check setup** (settings,
  AbstractCore's server, whether the gateway listens where *Who can connect*
  needs it, and how many models `/v1/models` lists).
- **Connect your app**: the base URL and your **API keys**. **New key** asks for
  a name (the app that will use it, for example "laptop Cursor"), then shows the
  key once with **Copy**: copy it into the app right away, the gateway never
  shows it again. The list shows each key's name, when it was made, when it was
  last used and from which address, and its fingerprint; **Revoke** asks
  **Revoke** / **Cancel** and refuses the key from the next request on. With no
  key yet, the card says so.
- **Access** (admin): *Authentication*, who requests without a key run as, and
  *Who can connect* (below). Changes apply to the next request.
- **Docs**: what is supported, links to this page and to AbstractCore's server
  docs, and a first request (curl, Python, JavaScript) with your base URL and
  `YOUR_API_KEY`, or the key you just made (masked on the page; **Copy example**
  copies it in clear).
- **Recent requests**: time, client (account, the API key's name, and address), model, tokens in and
  out, latency and status, newest first, refreshed every 5 s. A row opens to the
  request and the response as recorded (see [Request log](#request-log)) with
  **Open in Observer** when the request belongs to a run. An admin sees every
  request; anyone else sees their own.

## Connect

Use `provider/model` as the model name, as listed by `GET /v1/models`.

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "ollama/qwen3:4b", "messages": [{"role": "user", "content": "Hello"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="YOUR_API_KEY")
reply = client.chat.completions.create(
    model="ollama/qwen3:4b",
    messages=[{"role": "user", "content": "Hello"}],
)
print(reply.choices[0].message.content)
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://127.0.0.1:8080/v1", apiKey: "YOUR_API_KEY" });
const reply = await client.chat.completions.create({
  model: "ollama/qwen3:4b",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(reply.choices[0].message.content);
```

Tools with command-line flags take the same two values, for example
[aider](https://aider.chat):

```bash
aider --openai-api-base http://127.0.0.1:8080/v1 --openai-api-key YOUR_API_KEY --model openai/ollama/qwen3:4b
```

The console fills in your real base URL and the gateway's default text model.

## API keys

Each account makes its own named keys for this API, one per app or device:

- **Made with a name** on the OpenAI API page (**New key**) or with
  `POST /api/gateway/me/openai-keys {"label": "laptop Cursor"}`. The key
  (`sk-agw-...`) is answered once, by that request. The gateway keeps only its
  PBKDF2 hash and its fingerprint (the first 12 hex digits of its SHA-256) in
  the account's record, so a lost key cannot be shown again: make a new one.
- **Valid at `/v1` only.** A key never signs in to the console and never calls
  the gateway API: anywhere under `/api/gateway/` (sign-in included) it answers
  `401` with `This is an API key for /v1; sign in with your gateway token.`
  It cannot touch runs, files, email or workflows.
- **Runs as its account.** Requests made with it are that account's: its models
  and providers, its OpenAI API switch (off: every key of the account answers
  `403 openai_api_off`) and its Active state (`403 account_inactive`).
- **Revoked one at a time, immediately.** **Revoke** (or
  `DELETE /api/gateway/me/openai-keys/{fingerprint}`) refuses the key from the
  next request on with `401 invalid_api_key`; the account's other keys keep
  working.
- **Named in the log.** Each request records the key's name and fingerprint, so
  [Recent requests](#request-log) shows which app called. The key list shows
  when each key was last used and from which address.
- **Admins** see every account's keys (names, dates, fingerprints, never a key)
  in **Accounts → OpenAI API** and can revoke any of them.

Names are unique per account (case-insensitive, up to 80 characters). For
compatibility, an account's gateway token is still accepted at `/v1`; the page
offers named keys, so a key pasted into an app can be revoked without changing
how you sign in.

## What is supported

Checked with the official `openai` Python SDK:

| Endpoint | Notes |
| --- | --- |
| `GET /v1/models` | `{object: "list", data: [{id, object: "model", created, owned_by}]}` |
| `GET /v1/models/{id}` | one model; `404 model_not_found` otherwise |
| `POST /v1/chat/completions` | `id`, `object`, `created`, `model`, `choices[].message`, `finish_reason`, `usage.{prompt,completion,total}_tokens`; `tools` and `tool_choice` with structured `tool_calls` (`finish_reason: "tool_calls"`) and `role: "tool"` results; `stream: true` as SSE `data:` chunks (`chat.completion.chunk`, role on the first delta, `finish_reason` on the last chunk) ending with `data: [DONE]`; `stream_options.include_usage` adds a final chunk with `choices: []` and `usage`; `max_completion_tokens` (or `max_tokens`) |
| `POST /v1/chat/completions` with `response_format` | structured outputs: `{"type": "json_object"}` and `{"type": "json_schema", "json_schema": {"name", "schema", "strict"?}}`, also through the SDK's `chat.completions.parse(response_format=<Pydantic model>)`; for every provider (see below); with `stream: true` the validated JSON arrives as one content chunk |
| `POST /v1/embeddings` | `encoding_format` `float` or `base64` (the SDK's default) |

**Structured outputs on every provider.** AbstractCore's structured-output
support runs the request: a provider that can constrain decoding receives the
schema as the constraint (Ollama `format`, LM Studio, llama.cpp and other
OpenAI-compatible servers `response_format`, MLX or Transformers with Outlines,
OpenAI, Anthropic's forced tool); any other model gets the schema in its prompt.
Every answer is then validated against your schema (type, properties, required,
`additionalProperties`, items, enum, const, anyOf/oneOf/allOf, local `$ref`,
length, pattern and number bounds); an answer that does not match is retried
with the violation fed back, and one that still does not match answers
`500 structured_output_invalid`. A schema that cannot be used answers
`400 invalid_response_format` before any model runs. The answer's `usage` is not
reported for structured requests yet.

Also served when an engine for the capability is set up (AbstractCore answers;
not part of the SDK checks): `POST /v1/responses`, `POST /v1/audio/speech`,
`POST /v1/audio/transcriptions`, `POST /v1/audio/translations`,
`POST /v1/images/generations`, `POST /v1/images/edits`,
`POST /v1/images/variations`.

Not yet: `response_format` together with `tools`, `text.format` on
`/v1/responses`, `n` greater than 1, `logprobs`, `logit_bias` (each answers
`400 unsupported_parameter` with the parameter named), and files, batches,
assistants, fine-tuning, moderations and realtime (`404`).

Refused with `400 unsupported_parameter` (the field named in `param`), in a JSON
body, a form body or the query string: fields that would point a provider
somewhere else or carry a credential: `base_url`, `api_base`, `api_key`,
`provider`, `provider_hint`, `provider_kwargs`, `headers`, `extra_headers`,
`default_headers`, `endpoint`, `upstream`, `upstream_base_url`, `base_url_key`,
`organization`, `project`. The model is chosen by `model` alone, through this
gateway's providers and their stored settings. To use your own key for a cloud
provider, send it in the `X-AbstractCore-Provider-API-Key` header.

Accepted and ignored: `user`, `store`, `metadata`, `service_tier`,
`parallel_tool_calls` and the `OpenAI-Organization` / `OpenAI-Project` headers.

### Errors

Every error uses the OpenAI envelope, so SDKs raise their usual exceptions:

```json
{"error": {"message": "Incorrect API key provided: it is not a key of this gateway, or it was revoked. Make a key on the gateway's OpenAI API page.", "type": "invalid_request_error", "param": null, "code": "invalid_api_key"}}
```

| Status | When | `code` |
| --- | --- | --- |
| 400 | invalid request, unsupported parameter, unusable `response_format` | `unsupported_parameter`, `invalid_response_format` or none |
| 401 | missing, wrong or revoked key | `invalid_api_key` |
| 403 | the account's OpenAI API switch is off, or the account is deactivated or archived (`account_inactive`); the client is outside *Who can connect*; Guest asked for more than models; the account Open mode runs as is unavailable; a page without a key from an origin that is not accepted | `openai_api_off`, `account_inactive`, `client_not_allowed`, `guest_not_allowed`, `open_account_unavailable`, `origin_not_allowed` |
| 404 | unknown route, an unknown provider or model, or the API is stopped | `model_not_found`, `endpoint_stopped` |
| 429 | too many refused keys from one address | `auth_lockout` |
| 500 | a structured answer that still does not match `response_format` | `structured_output_invalid` |

Each response carries `x-request-id`.

## Access

**Who may use it.** Identities are the gateway's own accounts. A caller's key is
one of their account's [API keys](#api-keys) (or, for compatibility, their
gateway token) and the request runs as that account. Each account has an
**OpenAI API** switch (Accounts → the row's **OpenAI API** button, admins): on
by default for an active account; off, every key of that account answers
`403 openai_api_off` at `/v1` (its console sign-in is unchanged). The first time
the endpoint is turned on, the switch is written on for every active account
and off for inactive ones. Without a key (Open mode), requests run as the
account the admin chose: by default the built-in **Guest**, which may only use
models (`/v1/models`, chat, responses, embeddings, text-to-speech and image
generation when an engine is routed) with no tools, no files, attachments or
image/audio inputs, no workspace, email or run tools, never any `/api/gateway`
route, and anonymous towards AbstractCore so only local engines answer; or a user account, whose models and providers it then uses
and whose log shows the requests. Never an admin, and never an account whose
switch is off. The operator's own token (not an account) is always accepted.

**Authentication**

- **Protected (API key)** (default): every request sends an API key as
  `Authorization: Bearer <key>`. The gateway resolves it to the account; the
  key never reaches AbstractCore. Repeated wrong keys from one address are
  locked out like sign-in attempts.
- **Open (no key)**: requests without a key are served, as Guest or the chosen
  account (above). SDKs still require some key value; any text works, for
  example `not-needed` (a key that is not a key of this gateway is served without
  one). Open applies only to direct clients on this machine, your network or
  your VPN: a request through a proxy, in Internet mode, or with proxies
  elsewhere trusted needs a key. As Guest, cloud providers whose keys are stored
  on the gateway still need a key; open clients can use local engines or send
  their own provider key in `X-AbstractCore-Provider-API-Key`.

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
(`<data dir>/audit_log.jsonl`; no other store) with an `openai_api` object:
client, `key_label` and `key_fingerprint` for a named API key, model, `prompt_tokens`, `completion_tokens`, `stream`, `run_id` when the
request sent `X-AbstractCore-Run-Id`, `run_as` for an Open-mode account, and the
`request` and `response` as they crossed `/v1`. A streamed answer is recorded
assembled (content, tool calls, finish reason, usage). Credentials are removed
before the line is written: fields named like a credential (`api_key`,
`authorization`, `token`, `password`, `secret`, ...) and every occurrence of the
caller's key and the internal endpoint key read `[redacted]`; inline media
(`data:` URLs) is replaced by its type and size; a side larger than 256 KB is
kept to that size and marked truncated. Who reads it: an admin, every request,
with the full recorded request and response (credentials removed as above);
anyone else, their own. The log shares the audit log's rotation and retention
(`ABSTRACTGATEWAY_AUDIT_LOG_*`); with the audit log off, the card stays empty.

## Admin API

| Method | Route | Who | Result |
| --- | --- | --- | --- |
| GET | `/api/gateway/openai-api` | any account | status `gateway_openai_api_v1`: everyone `role`, `writable`, `enabled`, `running`, `base_url`, `key{own_token, user_id, named_keys, fingerprint, allowed}` (`named_keys`: true when the caller's account makes API keys, false for the operator's own token; `fingerprint`: first 12 hex digits of the gateway token's SHA-256, never the token), `docs`, `support`, `example_model` (a text model `/v1/models` lists; null when stopped); an admin also `access`, `reach`, `reach_options[]`, `open_account`, `open_account_options[]`, `warnings[]`, `listener`, `tailscale`, `open_requests`, `legacy_base_url` |
| GET | `/api/gateway/openai-api/logs?limit=` | any account | `{rows[{request_id, ts, client, user_id, key_label, key_fingerprint, ip, method, path, model, prompt_tokens, completion_tokens, stream, duration_ms, status, run_id, observer_path, recorded}], scope}`: every request for an admin, the caller's own otherwise |
| GET | `/api/gateway/openai-api/logs/{request_id}` | any account | `{row{..., request, response, user_agent}}`, the recorded request and response (redacted); someone else's id answers 404 for a non-admin |
| GET | `/api/gateway/me/openai-keys` | any account | `{account, keys[{label, fingerprint, created_at, created_by, last_used_at, last_client}]}`, never a key; `409 no_account` for the operator's own token |
| POST | `/api/gateway/me/openai-keys` | any account | `{label}` → `{key, item}`: the key, answered only here; `400 label_required`/`label_too_long`, `409 label_taken` |
| DELETE | `/api/gateway/me/openai-keys/{fingerprint}` | any account | revokes one of your keys at once → `{revoked}`; `404 key_not_found` |
| GET | `/api/gateway/admin/accounts/{id}/openai-keys` | admin | that account's keys (same items) |
| DELETE | `/api/gateway/admin/accounts/{id}/openai-keys/{fingerprint}` | admin | revokes one of that account's keys at once |
| POST | `/api/gateway/admin/core-endpoint` | admin | `{enabled?, access?: token\|open, reach?: machine\|network\|tailnet\|anywhere, open_account?: guest\|<user id>}`; `409` with the reason when refused |
| PUT | `/api/gateway/admin/accounts/{id}/openai-api` | admin | `{enabled}`: the account's OpenAI API switch; answers the account row (`openai_api`) |
| POST | `/api/gateway/admin/core-endpoint/restart` | admin | ends open requests; `{ended_requests}` |
| POST | `/api/gateway/admin/core-endpoint/check` | admin | `{checks[{id, ok, text}], ok}` |
| GET | `/api/gateway/admin/core-endpoint` | admin | the same status |

Settings live in `<data dir>/config/core_endpoint.json` (owner-only, atomic
writes). API keys live in the user registry (`users.json`, each account's
`openai_keys`: name, PBKDF2 hash, fingerprint, creation time); when each key was
last used is kept beside it in `<data dir>/auth/openai_key_usage.json`. Errors
of the key routes answer `{"detail": {"reason_code", "message"}}`.

## Moving from `/core/v1`

`/core/v1/...` answers `308 Permanent Redirect` to `/v1/...` (method and body
kept) and is deprecated: point your apps at `/v1`. The dedicated endpoint token
of gateway 0.12.0 is still accepted as a key (deprecated: use API keys);
`/api/gateway/admin/core-endpoint/token/rotate` still makes a new one (answered
once), and the reveal route is gone: no route returns a stored token. A 0.12.0 settings file without *Who can
connect* reads as **Devices on my network**.
