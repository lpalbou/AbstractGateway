# Events and triggers: examples

Three end-to-end examples of the events-and-attention track (framework backlog 1048): something
happens outside (a mail, a feed post, a call to a webhook), an automation runs a workflow on it, the
workflow's model decides whether you need to know, and you are told on your event stream, in the
gateway tray, the Assistant, Code and the watch.

Each example is also a hermetic test under `tests/e2e_events/` (scratch data folder and HOME, no
provider, fake mail servers, nothing on port 8080):

```bash
cd abstractgateway
python -m pytest tests -q -k e2e_events                    # the three examples
python -m pytest tests/e2e_events -q --allow-network      # + the live Python Insider feed
```

## The pieces

- A **trigger connection** is one instance of a source that needs your settings: a feed URL
  (`feed.item@1`), a webhook secret (`webhook.received@1`). A mailbox is the connection of
  `email.received@1` (`config.account`; `self` is your primary mailbox). Connections are yours: list,
  create, test-fire and delete them with `/api/gateway/triggers/connections` or the console's
  **Triggers** page (`/console#triggers`).
- An **automation** binds a workflow to a trigger (`POST /api/gateway/automations`, or Flow's
  Automation dialog). Each occurrence runs the workflow with the trigger's event: as
  `input_data.event` (CloudEvents 1.0: `type`, `subject`, `time`, `data`) and, for a string `prompt`,
  framed as untrusted content appended to it. Connection sources deliver a batch:
  `event.data.count` and `event.data.events[i].data`.
- **Attention**: an occurrence whose output carries `notify: {title, body}` (or `notify: true`) raises
  an attention item; one that fails after its retries raises a failure item. Ordinary results are
  quiet.
- **Your event stream** (`GET /api/gateway/me/events?after=<sequence>`, live:
  `GET /api/gateway/me/events/stream`) carries `trigger.event`, `run.started`, `run.finished`,
  `attention.raised`, `attention.seen` and `notification.sent|failed`. The tray, the Assistant (desktop
  and watch) and Code follow it.

The workflow the examples use, "judge", is three nodes (its JSON is `judge_flow()` in
`tests/e2e_events/e2e_support.py`; build the same in Flow):

```
On Flow Start (prompt) ──▶ LLM Call  prompt ◀── start.prompt
                                     system  "If it needs the person's attention, answer a short title
                                              and a body; otherwise an empty title and body."
                                     response schema {title: string, body: string}
                           ──▶ On Flow End  notify ◀── LLM data
                                            response ◀── LLM response
```

The model decides; an empty title and body is quiet, anything else raises attention. No keyword
rule is involved.

In the commands below, replace `<token>` with your gateway token (the console's **Account** page)
and `http://127.0.0.1:8080` with your gateway's address. `jq` reads the answers.

## 1. Watch a mailbox and tell me when something important arrives

Connect the mailbox (a further read-only mailbox is `PUT /api/gateway/me/email/accounts/<id>`, IMAP
only; see [email.md](email.md)):

```bash
curl -sS -X PUT http://127.0.0.1:8080/api/gateway/me/email \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"address": "me@example.org", "password": "<app password>",
       "imap": {"host": "imap.example.org", "port": 993, "security": "ssl"},
       "smtp": {"host": "smtp.example.org", "port": 587, "security": "starttls"}}'
```

Create the automation on new mail, running your judge workflow:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/gateway/automations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"request_id": "mailbox-watch-1", "title": "Mailbox watch",
       "target": {"bundle_ref": "<your bundle>@<version>", "flow_id": "judge",
                  "input_data": {"prompt": "A new mail arrived. Tell me only if it needs me today."}},
       "trigger": {"source_id": "email.received", "source_version": 1,
                   "config": {"uses_model": true, "every": "60s"}},
       "context": {"mode": "independent"}}' | jq .automation_id
```

The mailbox is read from the moment the automation exists (mail already there is history). When a
mail arrives, one occurrence runs with it framed in its prompt; when the model says it matters, the
attention item lands on your stream:

```bash
curl -sS "http://127.0.0.1:8080/api/gateway/me/events?after=0&limit=100" \
  -H "Authorization: Bearer <token>" | jq '.events[] | select(.type == "attention.raised")'
```

and the gateway tray shows one notification ("Mailbox watch — It has a result for you"), recorded on
the stream as `notification.sent {channel: "tray"}`. Opening the automation marks it seen
(`attention.seen`), which lowers every badge.

A second mailbox (`config.account: "<id>"`) is accepted by the gateway but not yet by AbstractRuntime's
`email.received@1` (it reads only `self` today); that form is a pending test in
`tests/e2e_events/test_e2e_mailbox_watch.py`.

## 2. A digest of a news feed

```bash
CID=$(curl -sS -X POST http://127.0.0.1:8080/api/gateway/triggers/connections \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"source_id": "feed.item", "source_version": 1, "label": "Python Insider",
       "config": {"url": "https://blog.python.org/feeds/posts/default", "every": "1h",
                  "initial_items": "deliver", "max_items": 5}}' | jq -r .connection_id)

curl -sS -X POST http://127.0.0.1:8080/api/gateway/automations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d "{\"request_id\": \"feed-digest-1\", \"title\": \"Python news digest\",
       \"target\": {\"bundle_ref\": \"<your bundle>@<version>\", \"flow_id\": \"judge\",
                    \"input_data\": {\"prompt\": \"New posts on a feed I follow. Write me a short digest if any of them matters.\"}},
       \"trigger\": {\"source_id\": \"feed.item\", \"source_version\": 1, \"config\": {}, \"connection_id\": \"$CID\"},
       \"context\": {\"mode\": \"independent\"}}" | jq .automation_id
```

The gateway polls each feed at its `every` (at least 60 s; 15 minutes by default), with conditional
GET. `initial_items: "deliver"` makes the posts already there the first digest (`"skip"`, the default,
starts from the next post). All new posts of one poll are ONE occurrence: `event.data.count` and each
post at `event.data.events[i].data` (`title`, `link`, `summary`, `published`). See the posts the
connection received:

```bash
curl -sS "http://127.0.0.1:8080/api/gateway/triggers/connections/$CID/events?limit=20" \
  -H "Authorization: Bearer <token>" | jq '.events[].data.title'
```

On your stream: `trigger.event`, `run.started`, `run.finished` for the occurrence and, when the model
wrote a digest, `attention.raised`.

## 3. Run this flow when an external system calls my webhook

Create the connection; the answer shows the secret ONCE and the address to give the sender:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/gateway/triggers/connections \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"source_id": "webhook.received", "source_version": 1, "label": "Orders", "config": {}}' \
  | jq '{connection_id, ingress_url, secret}'
```

Bind an automation to it (as in example 2, with `"source_id": "webhook.received"` and the
`connection_id`). The sender signs each call per [Standard Webhooks](https://www.standardwebhooks.com/):
`webhook-signature: v1,<base64 HMAC-SHA256 of "<webhook-id>.<webhook-timestamp>.<body>">`, keyed with
the secret's base64 part. From a shell (no bearer token: the signature is the authentication):

```bash
CID=<connection_id>; SECRET=<secret>
KEY=$(printf %s "${SECRET#whsec_}" | base64 -d | xxd -p -c 256)
ID=msg_$(date +%s); TS=$(date +%s); BODY='{"order": 42, "state": "payment_refused"}'
SIG=$(printf %s "$ID.$TS.$BODY" | openssl dgst -sha256 -mac HMAC -macopt hexkey:$KEY -binary | base64)
curl -sS -X POST "http://127.0.0.1:8080/api/gateway/triggers/webhooks/$CID" \
  -H "webhook-id: $ID" -H "webhook-timestamp: $TS" -H "webhook-signature: v1,$SIG" \
  -H "Content-Type: application/json" --data "$BODY"
```

Answers: `202` accepted (`{accepted, event_id, seq, woken}`); `401 webhook_signature_invalid` for a
body that is not the one signed (or a wrong secret), `401 webhook_unsigned` without the headers;
`409 webhook_stale` outside the 5-minute window; `409 webhook_replayed` for an id already accepted (a
sender retrying a call that succeeded gets 409: treat it as delivered); `404` for an unknown connection.
The occurrence runs with the body at `event.data.events[0].data.body` and the webhook id as
`event.data.events[0].subject`.

## Following your stream live

```bash
curl -sN "http://127.0.0.1:8080/api/gateway/me/events/stream?heartbeat_s=15" \
  -H "Authorization: Bearer <token>" -H "Last-Event-ID: <last sequence you read>"
```

Each event is `id: <sequence>`, `event: <type>`, `data: <the event JSON>`; reconnecting with the last
`id` you read gets exactly what came after it.

### Showing a notification without doubling the tray

An app that notifies (the Assistant desktop, Code) follows one rule so an approval is announced
once per machine: on `attention.raised`, wait a moment, read the last events, and stay silent if a
`notification.sent` for the same `record_key` from another channel with the same `device` is less
than a minute old. Otherwise notify, then report it:

```bash
curl -s -X POST "http://127.0.0.1:8080/api/gateway/me/events/notifications" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"channel":"assistant","outcome":"sent","record_key":"<record_key>","device":"<this machine id>"}'
```

The machine id is the first 16 hex characters of the sha256 of the lower-cased host name (what
the gateway tray sends). `channel` is one of `tray`, `assistant`, `code`, `watch`.

## Trying it by hand without a model or a mailbox

The console's **Triggers** page has **Test** on each connection: it appends a synthetic event
(`data.test: true`) and wakes the bound automations, so an automation, its occurrence and your stream
can be checked before any real mail, post or call.
