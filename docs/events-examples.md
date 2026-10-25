# Events and triggers: examples

Three end-to-end examples of events and attention: something
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

A webhook is an address on your gateway that another system calls (an HTTP `POST`) when something
happens on its side. Each call that carries a valid signature starts one run of your flow, with the
call's JSON body as input. Nobody without the secret can start it.

### 3a. Set it up

1. **Create the webhook connection.** In the web console, open **Triggers** (sidebar, *Work*) and
   press **New connection**. Under **Source** choose **When a webhook is called**, type a **Name**
   (for example `Orders`), and press **Create**.
2. **Copy the address and the secret.** The dialog shows the **Webhook address**
   (`https://<your gateway>/api/gateway/triggers/webhooks/<connection id>`) and the **Signing
   secret** (`whsec_…`), each with a copy button. The secret is shown **once**: store it where the sender
   will read it (a password manager, the sender's settings). The address stays visible later under
   the connection's row (**Events**).
3. **Let your flow receive the event.** In AbstractFlow, open the flow, select its **On Flow Start**
   node and press **Add trigger event** (Properties). The node gets an `event` output and a
   **Trigger event** node is wired to it. Save the flow.
4. **Attach the webhook to the flow.** In AbstractFlow, open the **Flow Library**, select the flow,
   and press the edit button next to **Automation**. Set **Trigger** to **When a webhook is
   called**, pick your connection under **Connection** (press **Refresh connections** if you just
   created it), and press **Save**. Then **Publish** the flow.
5. **Create the automation.** One request creates it from the flow's saved defaults (the trigger,
   the connection, the title and the inputs come from step 4):

   ```bash
   curl -sS -X POST http://127.0.0.1:8080/api/gateway/automations \
     -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     -d '{"request_id": "orders-webhook-1", "title": "Orders webhook",
          "target": {"bundle_ref": "<your bundle>@<version>", "flow_id": "<flow id>"}}'
   ```

   Without AbstractFlow, put the trigger in the same request:
   `"trigger": {"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": "<connection id>"}`
   instead.

What the flow receives: each call is one event under `event.data.events`, oldest first; the JSON
body is at `event.data.events[0].data.body` and the sender's message id at
`event.data.events[0].subject`. For a call whose body is `{"order": 42, "state": "payment_refused"}`
the run's `event` input reads (other envelope fields left out):

```json
{
  "specversion": "1.0",
  "type": "abstract.webhook.received.v1",
  "data": {
    "count": 1,
    "events": [
      {
        "subject": "msg_1761390000",
        "data": {
          "webhook_id": "msg_1761390000",
          "content_type": "application/json",
          "body": {"order": 42, "state": "payment_refused"}
        }
      }
    ]
  }
}
```

A body that is not JSON arrives as `body_text`. The body is written by someone else: the gateway marks
it untrusted, so a run treats it as data, never as instructions.

### 3b. Try it

Paste this block in a terminal, after setting `CID` (the last part of the address) and `SECRET`
(the `whsec_…` value). It sends one signed call, exactly as a sender does:

```bash
CID="tc_REPLACE_ME"
SECRET="whsec_REPLACE_ME"
GATEWAY="http://127.0.0.1:8080"

BODY='{"order": 42, "state": "payment_refused"}'
ID="msg_$(date +%s)"
TS="$(date +%s)"
KEY="$(printf %s "${SECRET#whsec_}" | base64 -d | xxd -p -c 256)"
SIG="$(printf %s "$ID.$TS.$BODY" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$KEY" -binary | base64)"

curl -sS -X POST "$GATEWAY/api/gateway/triggers/webhooks/$CID" \
  -H "Content-Type: application/json" \
  -H "webhook-id: $ID" -H "webhook-timestamp: $TS" -H "webhook-signature: v1,$SIG" \
  --data "$BODY"
```

No bearer token: the signature is the authentication. What you should see:

- the answer `202` with `{"accepted": true, "connection_id": "…", "webhook_id": "msg_…", "event_id": "…", "seq": …, "woken": 1}`
  (`woken` counts the automations the call woke);
- in the console, **Triggers** → the connection's **Events**: the call, its body under `data.body`;
- on your event stream (`GET /api/gateway/me/events`): `trigger.event`, then `run.started` and
  `run.finished` of the run the call started;
- in the desktop tray: a notification when that run has something for you (its flow ends with
  `notify`) or fails; a run that only does its work stays quiet.

The other answers: `401 webhook_signature_invalid` (the body is not the one signed, or the secret is
wrong), `401 webhook_unsigned` (a `webhook-*` header is missing), `409 webhook_stale` (the timestamp
is more than 5 minutes away from the gateway's clock), `409 webhook_replayed` (this `webhook-id` was
already accepted: a retry of a delivered call; treat it as delivered), `404` (unknown connection).
Run the block again for a second call: it uses a new id.

### Which services can call it directly

The gateway accepts the [Standard Webhooks](https://www.standardwebhooks.com/) format: the headers
`webhook-id`, `webhook-timestamp` and `webhook-signature` (`v1,<base64 HMAC-SHA256 of
"<id>.<timestamp>.<body>">`, keyed with the secret). Any sender that implements it can call the
address directly, as can your own scripts and automation tools that compute an HMAC (as in 3b).

- **Services built on Svix** (for example Clerk and Resend) sign the same way but name the headers
  `svix-id`, `svix-timestamp` and `svix-signature`. The gateway reads only the `webhook-*` names, so
  these need a small relay that renames the three headers and forwards the body unchanged.
- **GitHub** (Settings → Webhooks → Add webhook: Payload URL, Content type `application/json`,
  Secret) signs differently: one `X-Hub-Signature-256` header, an HMAC-SHA256 of the body alone with
  the secret as typed. The gateway does not accept that header, so a GitHub webhook pointed at the
  address answers `401 webhook_unsigned`. Use a relay that verifies GitHub's signature and re-signs
  the call in the Standard Webhooks format with your connection's secret.

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
