# Push to the watch and the iPhone

When something needs you — an approval, a question, a published alert, an automation's result or
failure — the AbstractAssistant apps on your Apple Watch and iPhone show it within seconds, even
when the app is closed. This page explains how that works, the one flag the gateway operator sets,
how to run your own relay with your own Apple key, and exactly what leaves your gateway.

It builds on [your event stream](./api.md#your-event-stream-apigatewaymeevents): a push is only a
hint that the stream holds something new; the app reads the real event from your gateway. For the
other notification paths (the desktop tray, email) see [tray.md](./tray.md) and
[email.md](./email.md).

## For people: nothing to configure

Sign in to the AbstractAssistant app on your watch or iPhone and allow notifications. The app
registers itself with your gateway at sign-in and refreshes the registration at least once a week;
signing out removes it. The system's notification permission for the app is the only switch.

You can see the devices that get your alerts in the console: **My email → Notifications →
Devices** (web and terminal console) lists each watch or iPhone with its name, when it last
registered, the last alert sent, and the last error in plain words.

What you get:

| Something waits for you | Alert | Level |
|---|---|---|
| a run asks for your approval | Approval needed | time-sensitive |
| a run asks you a question | Question | time-sensitive |
| a flow published an alert to you | the publication's title | passive |
| an automation has a result | *<automation> has a result* | passive |
| an automation failed | *<automation> failed* | passive |

An approval that is answered before the push leaves (on another device, in the console) is never
pushed. A watch registered in the last 24 hours takes the alerts; otherwise the iPhone does, so
your wrist rings once. A registration not refreshed for 7 days expires.

## For the operator: one flag

```bash
abstractgateway serve --push-relay https://push.example.org
```

`--push-relay <URL|off>` names the relay this gateway sends push requests to. The default is `off`;
the packaged default becomes the publisher's relay once it is live. There is no environment
variable.

- With `off`, the gateway runs no push dispatcher at all: no thread, no outbound connection.
- With a relay but no registered device, the dispatcher does no work and opens no connection. The
  gateway contacts the relay only once a device has registered.
- In the split layout (`serve --no-runner` plus `abstractgateway runner`), give the flag to both
  processes: the runner process sends, the API process answers the device registrations.

Delivery is retried for an hour (after 2 s, 10 s, 60 s, 5 min, then every 15 min). An approval or
a question that could not be delivered in that hour is recorded as `notification.failed` in your
event stream; each delivered alert is recorded as `notification.sent` with channel `watch` or
`phone` and `via: "push"`. A relay outage writes at most one log line per hour.

State lives in the data folder of each account (`<data dir>/push/`, private files):
`devices.json` (the registrations), `cursor.json` (the last event read; the first start begins at
the newest event, so history is never pushed) and `outbox.sqlite3` (one row per alert and device,
at most 5,000, finished rows kept 7 days). `<gateway data dir>/push/token_owners.json` records which
account holds each device token: when another account signs in on the same app, the token moves
to it.

## Run your own relay

Apple only delivers notifications signed with the app publisher's key, so a self-hosted gateway
cannot talk to Apple directly. The relay is the small service that holds that key. It ships with
the gateway:

```bash
pip install "abstractgateway[push-relay]"
abstractgateway push-relay \
  --apns-key-file AuthKey_ABC123DEFG.p8 --key-id ABC123DEFG --team-id TEAM123456 \
  --topic com.example.assistant.watch --topic com.example.assistant.iphone \
  --listen 127.0.0.1:8095
```

- `--apns-key-file`, `--key-id`, `--team-id`: the APNs authentication key (`.p8`) from your Apple
  developer account, its Key ID and your Team ID.
- `--topic`: the bundle ids of the apps you build and sign; any other topic is refused.
- `--listen` (default `127.0.0.1:8095`): put a TLS reverse proxy in front of it, and pass
  `--forwarded-allow-ips` with the proxy's address so the per-IP limit sees the caller.

The `push-relay` extra adds the HTTP/2 client Apple requires (`httpx[http2]`) and `cryptography`
for the provider token. A gateway that only sends to a relay needs neither.

Then point your gateway at it with `--push-relay https://your-relay.example`. A gateway accepts
registrations for the two packaged apps (`ai.abstractframework.assistant.watch`,
`ai.abstractframework.assistant.iphone`) and for the topics your relay lists at `GET /v1/info`.

The relay is stateless: it stores nothing, keeps its rate limits in memory and reuses its signed
provider token for at most 40 minutes. Limits: 30 alerts an hour and 300 a day per device token,
600 requests an hour per source address.

## Privacy: what the relay and Apple see

The relay accepts no text. A request is exactly:

```json
{"topic": "ai.abstractframework.assistant.watch", "env": "production",
 "token": "<APNs device token>", "kind": "approval",
 "collapse": "<32 hex>", "seq": 42, "reg": "<16-32 hex>"}
```

- `kind` is one of `approval`, `question`, `notify`, `result`, `failure`.
- `collapse` is `sha256("attention:" + record_key)` cut to 32 hex characters: an opaque id that
  lets the system replace a notification instead of showing it twice. Run ids and record keys never
  leave the gateway.
- `seq` is the event's number in your stream, so the app can fetch exactly that event.
- `reg` is a handle the app chose at sign-in; an alert meant for an earlier sign-in shows as
  *From an earlier sign-in*.

The relay and Apple therefore see the device token, the app, the kind of alert, the event number
and the time — never a title, a name, a prompt or any content. The relay's logs hold
`sha256(token)` cut to 12 characters, never a token. The relay also sees your gateway's IP address,
and only once a device has registered.

### The fixed messages

The relay builds the notification itself, always the same localized message per kind
(`title-loc-key: push.<kind>.title`, `loc-key: push.<kind>.body`), so a device token in the wrong
hands can never carry words under the app's name. The app's notification service extension then
fetches the event from your gateway and replaces the generic text with the real one (the name of
the automation, the title of the publication). If the fetch times out, the generic text stays.

The APNs request: `apns-push-type: alert`, `apns-priority` 10 for approvals and questions and 5
otherwise, `apns-collapse-id` = `collapse`, expiry after 24 hours, and the body
`{aps: {alert: {title-loc-key, loc-key}, sound: "default", mutable-content: 1,
interruption-level: "time-sensitive" | "passive", thread-id: <kind>}, s: <seq>, r: <reg>}`.

## Reference

- Routes: [api.md, Push devices](./api.md#push-devices-apigatewaymepushdevices).
- Relay answers: 200 `{status: sent}`; 200 `{status: gone, since_ms}` when the token is no longer
  valid (the gateway removes a registration only when Apple's time is later than its last
  refresh); 200 `{status: rejected, reason}`; 429 or 503 `{status: retry, after_s}`; 400
  `{status: invalid, reason}` for a malformed request (413 past 1 KB).
- Evidence: `src/abstractgateway/push/` (`dispatcher.py`, `store.py`, `relay.py`, `settings.py`),
  `src/abstractgateway/routes/push.py`, `src/abstractgateway/console_push.py`.
