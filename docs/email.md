# Email: one user, one runtime, one mailbox

Every signed-in person on an AbstractGateway can connect **their own** mailbox. The account is
stored encrypted in that user's data home and is used for three things:

- **automations on new mail** — the `email.received@1` trigger (for example "forward invoices",
  "tell me when X writes", or an AI triage that opens a session with you);
- **email notifications** — job finished/failed, approval needed and automation results, emailed
  to you through your own account;
- **account recovery** — "Forgot your token?" and "Email me a sign-in code" on the sign-in page.

Email is optional. Without an account there is no sending and no email notification; the gateway
has no shared "system" sender. The mailbox is only read: nothing marks messages read, moves or
deletes them.

This page is the user and operator guide. The HTTP surface is listed in [api.md](api.md#email),
the security model in [security.md](security.md#per-user-email), and the consoles in
[console.md](console.md).

## Connect your mailbox

You can connect from any of these surfaces; they share the same fields and words:

- **Web console** → Users tab → **My email** (every signed-in user).
- **Terminal console** (`abstractgateway-console`) → Users screen → `@`.
- **HTTP**: `PUT /api/gateway/me/email`.

Give the address, the password (or app password), and the IMAP and SMTP servers:

```bash
curl -sS -X PUT -H "Authorization: Bearer <your gateway token>" -H "Content-Type: application/json" \
  "$BASE_URL/api/gateway/me/email" -d '{
    "address": "me@example.com",
    "password": "<app password>",
    "imap": {"host": "imap.example.com", "port": 993, "security": "ssl"},
    "smtp": {"host": "smtp.example.com", "port": 587, "security": "starttls"}
  }'
```

The gateway signs in to both servers first and stores nothing when a leg fails; the answer names
the cause and the fix (for example `email_auth_failed`: "The IMAP server rejected the user name or
password … many providers need an app password when two-step verification is on"). TLS
certificates and host names are always verified; there is no unencrypted mode.

Many providers (Gmail, iCloud, Fastmail, most hosted IMAP) accept an **app password** when
two-step verification is on.

### OAuth2 sign-in (Google, Microsoft)

Microsoft 365 / Outlook needs OAuth2. In **My email → Sign in with OAuth2**, pick the provider and
start the sign-in:

- **Microsoft** uses a device code by default: open the link shown in any browser, on any machine,
  and enter the code.
- **Google** uses a browser on the gateway's own computer (the provider redirects to a one-shot
  listener on `127.0.0.1` there). Google does not allow the Gmail scope in its device flow.

Which OAuth client signs in: the client id you give, else the gateway's client (an administrator
setting, see below), else the built-in AbstractFramework client when one is registered for that
provider. Tokens are refreshed automatically and stored encrypted, like passwords.

Administrators set a bring-your-own client per provider with
`PUT /api/gateway/admin/email/oauth-clients/{google|microsoft}` (`client_id`, `client_secret`,
`tenant`). The secret is sealed at rest and never returned; the read shows `client_secret_set`.

## Recipient policy and send limits

Every send — an agent's `send_email`, an automation's send action, a notification, a recovery
code — goes through the same checks, in this order:

1. email is on (your switch **and** the administrator's);
2. the **recipient policy**: `allowlist` (only the listed addresses and domains) or `denylist`
   (everyone except them). Entries are exact addresses or domains; a subdomain matches only when
   written as its own entry. The policy applies to To, Cc and Bcc, and a message with any refused
   recipient is refused whole, naming the refused addresses. A new account starts with an allowlist
   holding your registered address;
3. the **send limits**: 20 messages per rolling hour and 100 per day by default, editable by you.

On top of the policy, the approval gate still decides whether an agent's send runs unattended: a
send to anyone but you (or an automation's pre-authorised recipients) waits for your approval.

```bash
curl -sS -X PUT -H "$AUTH" "$BASE_URL/api/gateway/me/email/policy" \
  -d '{"mode": "allowlist", "entries": ["me@example.com", "mycompany.com"]}'
curl -sS -X PUT -H "$AUTH" "$BASE_URL/api/gateway/me/email/limits" -d '{"per_hour": 20, "per_day": 100}'
```

## Automations on new mail

The mail watcher reads your mailbox only while at least one of your automations is active on the
`email.received@1` trigger, checking every 60 seconds. It is read-only (IMAP `EXAMINE` and
`BODY.PEEK`), keeps a durable cursor, survives a server rebuilding the folder (UIDVALIDITY change:
it re-reads by date and passes messages it already has), and moves the cursor past a message only
after that message is durably stored in your runtime's event inbox. A message that cannot be read
on three polls in a row is recorded and passed, so one bad message never blocks your mailbox.
Connection problems back off from 60 seconds to 15 minutes; nothing is paused, and the status
shows the cause and the fix.

How often an automation **runs** on new mail is its own trigger setting: every 60 seconds when it
needs no model (`"uses_model": false`), once an hour by default when it runs a model (summarise,
classify, draft replies, AI triage), on the batch of messages received since its last run. Each
automation reads a message at most once. See [automations.md](automations.md) for the trigger
configuration and the send-email action.

Inbound mail is data, never instructions: it reaches a model inside a fixed "untrusted" frame, and
nothing it says can widen who a run may mail or which tools it has.

## Notifications

**My email → Notifications** (or `GET/PUT /api/gateway/me/notifications`) chooses which events
email you:

| Event | When |
|---|---|
| `automation_result` | an automation occurrence asked to notify, and the automation is set to "email me the result" (`notify.channels` holds `email`) |
| `automation_failed` | an occurrence failed after its retries (same channel rule) |
| `approval_needed` | one of your runs waits for your approval or answer |
| `job_finished` / `job_failed` | a run started with `_runtime.notify = {"on": ["finished", "failed"], "channels": ["email"]}` |

Notices go to your registered address (else your mailbox address), sent by your own account, from
a durable outbox: each notice is queued once and sent once. If the gateway stops in the middle of a
send, that notice is marked `unknown` and never resent automatically. Temporary SMTP refusals are
retried with backoff; sign-in and permanent refusals are shown with their cause and fix. Over your
send limits, the waiting notices go out as one digest when the window allows. **Send test
notification** checks the whole path. Notification emails use fixed templates; the only
model-written text is an automation's own `notify` title and body, labelled as such. Replying to a
notification does nothing.

## Account recovery by email

When at least one account on the gateway has email configured, the sign-in page offers
**Forgot your token?** and **Email me a sign-in code**. Enter your Gateway user and choose one; an
8-digit code is sent to your registered address through your own account. The code works once,
expires after 10 minutes and allows 5 tries. "Forgot your token?" issues a new token (shown once;
the old one stops working) and signs you in; "Email me a sign-in code" signs you in.

The answer is the same whether or not the account exists or has email, requests are rate-limited
per account and per client address, codes are stored only as keyed hashes, and every issue and use
is recorded in the audit log without the code. Users without email use the administrator's token
rotation as before.

## Administrators

Administrators can turn email **on or off per user** (web console Users table → **Email off / Email
on**, terminal console Users screen → `x`, or `PUT /api/gateway/admin/users/{id}/email`). Off means
no watcher, no sending and no email notifications; the user's settings are kept. The Users table
shows each user's mailbox state (`connected`, `not connected`, `needs action`, `turned off by an
administrator`, …). Administrators see the state, the address and the last error — never messages,
the user's recipient list or credentials.

The administrator's own account (the default runtime) is configured like everyone else's.

## Where things are stored

| What | Where |
|---|---|
| Account settings, policy, limits | `<plane>/email/account/abstractcore.json` |
| Password / OAuth tokens | `<plane>/email/account/email/secret.enc` (AES-256-GCM; key in the OS keychain, or a 0600 key file when there is none) |
| Watcher state | `<plane>/email/watcher.json`; the cursor and received mail in `<runtime data dir>/event_inbox/` |
| Notification preferences and outbox | `<plane>/email/notifications.json`, `<plane>/email/outbox.sqlite3` |
| Per-user email switch | `<data_dir>/auth/email_capability.json` |
| OAuth clients (admin) | `<data_dir>/email/oauth_clients/secret.enc` |
| Recovery codes (hashed) | `<data_dir>/auth/recovery_codes.json` |

`<plane>` is `<data_dir>` for the default runtime (single-user gateways and the administrator) and
`<data_dir>/users/<tenant>/<runtime>` for every other user.

## Migrating from the environment variables

Email is configured per user, never through environment variables. On the first start, a gateway
that still has the `ABSTRACT_EMAIL_*` variables (or values saved through the process manager's
environment overrides) imports that account **once** into the administrator's email settings; from
then on the variables are ignored, and each one still set is named at startup and in the
administrator's **My email** notices with the setting that replaced it. The email bridge
(`ABSTRACT_EMAIL_BRIDGE`) is replaced by the per-user watcher and the `email.received@1` trigger.
Maintenance notices go to the administrator's registered address through the administrator's own
account (`ABSTRACT_BACKLOG_EMAIL_TO` and the related account variables are ignored).

The admin-only `/api/gateway/email/*` routes remain as deprecated aliases acting on the calling
administrator's own account, and will be removed in a later minor release; use `/api/gateway/me/email`.
