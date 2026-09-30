# Email: one user, one runtime, one mailbox

Every signed-in person on an AbstractGateway can connect **their own** mailbox. The account is
stored encrypted in that user's data home and is used for three things:

- **automations on new mail** — the `email.received@1` trigger (for example "forward invoices",
  "tell me when X writes", or an AI triage that opens a session with you);
- **email notifications** — job finished/failed, approval needed and automation results, emailed
  to you through your own account;
- **account recovery** — "Forgot your token?" and "Email me a sign-in code" on the sign-in page.

Email is optional and nothing is active by default: no watcher runs until you create an
email-triggered automation, notifications stay in the console until you choose email, and your
agents get no email tools until you turn them on. Without an account there is no sending and no
email notification; the gateway has no shared "system" sender. The mailbox is only read: nothing marks messages read, moves or
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

Google and Microsoft sign-ins always use the provider's built-in endpoints and scopes. A request
that gives `token_endpoint`, `authorization_endpoint`, `device_authorization_endpoint` or `scopes`
for them is refused with `403 email_oauth_override_refused` (the answer names the fields), for
administrators too, and a `tenant` must be a tenant id or domain. Explicit endpoints and scopes
are only for `provider: "custom"`, which only an administrator may use, and which always brings
its own client id. The gateway's client and the built-in client are only ever sent to their
provider's own endpoints, so no request can send the administrator's client secret, or make the
gateway connect, to a host a user chose.

Administrators set a bring-your-own client per provider over HTTP with
`PUT /api/gateway/admin/email/oauth-clients/{google|microsoft}` (`client_id`, `client_secret`,
`tenant`); the consoles do not edit OAuth clients yet. The secret is sealed at rest and never
returned; the read shows `client_secret_set`.

While an administrator has email turned off for a user, that user's **Connect**, **Test** and
OAuth sign-in are refused (`409 email_disabled`) before any connection is made; the stored
settings are kept.

## Agent email tools (off by default)

Your agents and workflows — chats, workflow runs and automations, from every client (Code,
Assistant, Observer, the consoles) — get the email tools (list and search mail, read a message,
list folders, send, reply, download an attachment) only when **all** of these hold:

1. an administrator made **Agent email tools** available to you (they are not, by default);
2. your account is connected and turned on, and email is allowed for you;
3. you turned on **Agent email tools** (My email in the web console, **Policy, limits & tools** in
   the terminal console, or `PUT /api/gateway/me/email/agent-tools {"enabled": true}`). When they
   are not available the switch reads "not available — ask your admin".

The rule is applied twice: when your toolsets are built (the tools are listed only then; turning
the switch reloads your workflows so the change applies at once) and again when a tool runs (a
call without all three is refused with the cause and the fix). `GET /api/gateway/discovery/tools`
shows the email tools as enabled only for a caller whose agent tools are active; otherwise each
disabled email row names why: not available to you (ask your administrator), email turned off for
you by an administrator, no connected account, or your own switch is off. Every email tool
call is an ordinary tool call recorded in the run's ledger. Your own send-email actions in
automations (fixed templates you wrote), notifications and recovery codes do not need the switch:
they only need a connected, allowed account.

A run started without a tool list (`POST /runs/start` with no `input_data.tools`) gets its
workflow's default tools plus the email tools while your agent email tools are active; an explicit
tool list, even an empty one, is used as given.

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
send to anyone but you (or an automation's pre-authorised recipients) waits for your approval, and
a send to you runs without asking in chats and automations alike. "You" is the address My email
shows as your registered address: the email on your user account (or the gateway's registered
address for the operator), else the connected mailbox's own address.

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

Mail that is already in your mailbox when the watcher starts is never an event. The watcher marks
where new mail starts when you connect an account and whenever an email automation becomes active
after a time with none (creating or resuming one takes that mark within seconds, so a message you
send to test it right after counts); mail that arrived while none of your email automations was
active is not processed later. A gateway restart keeps the mark: mail that arrives while the gateway is down is
read when it is back.

How often an automation **runs** on new mail is its own trigger setting: every 60 seconds when it
needs no model (`"uses_model": false`), once an hour by default when it runs a model (summarise,
classify, draft replies, AI triage), on the batch of messages received since its last run. Each
automation reads a message at most once. See [automations.md](automations.md) for the trigger
configuration and the send-email action.

Inbound mail is data, never instructions: it reaches a model inside a fixed "untrusted" frame, and
nothing it says can widen who a run may mail or which tools it has.

An automation never runs on mail the framework sent itself. Every message sent automatically
through your account (notifications, recovery codes, and anything an automation sends, including
its send-email action) carries `Auto-Submitted: auto-generated` (RFC 3834) and an
`X-AbstractFramework-Automation` header, and its Message-ID is recorded in your outbox. The
watcher skips such messages from your own address, and a recorded Message-ID even when a server
dropped the headers, so a filter that matches an automation's own result email ("Email me the
result") never re-triggers it. By default the trigger also ignores automatic mail from others
(auto-replies, vacation notices, other automations); set `"auto_submitted": "admit"` in its
configuration to run on those too.

## Notifications

Email notifications are opt-in: every event is off until you turn it on, and the console stays
the default channel. **My email → Notifications** (or `GET/PUT /api/gateway/me/notifications`)
chooses which events email you:

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
per account (3 per 15 minutes) and per client address (10 per 15 minutes) before any work starts
(a flood of requests costs no background sends), codes are stored only as keyed hashes, and every issue and use
is recorded in the audit log without the code. Users without email use the administrator's token
rotation as before.

## Administrators

Administrators decide what is **available** to users, with a gateway-wide default and per-user
overrides (web console Users tab → **Email for users** and the row buttons; terminal console Users
screen → `@` → **Email for users (admin)**, `x` and `X`; or the HTTP routes):

| Capability | Default | Meaning |
|---|---|---|
| `email` | on | users may connect their own mailbox (off: no watcher, no sending, no email notifications; settings are kept) |
| `email_agent_tools` | off | users may turn on Agent email tools for their own agents |
| `email_recovery` | on (gateway-wide only) | "Forgot your token?" and "Email me a sign-in code" on the sign-in page |

```bash
curl -sS -X PUT -H "$ADMIN" "$BASE_URL/api/gateway/admin/email/capabilities" -d '{"email_agent_tools": true}'
curl -sS -X PUT -H "$ADMIN" "$BASE_URL/api/gateway/admin/users/alice/email" -d '{"enabled": true, "agent_tools": false}'
curl -sS -X PUT -H "$ADMIN" "$BASE_URL/api/gateway/admin/users/alice/email" -d '{"inherit": ["email_agent_tools"]}'
```

Sign-in by email means that whoever controls a user's mailbox can sign in as that user; turn it off
where mailboxes are not as well protected as gateway tokens. The Users table
shows each user's mailbox state (`connected`, `not connected`, `needs action`, `turned off by an
administrator`, …). Administrators see the state, the address and the last error — never messages,
the user's recipient list or credentials. The administrator's server file helpers (`/files/*`,
workspace import and export) never serve the gateway data folder, where every user's runs,
received mail and sealed credentials live, even when it sits inside the server workspace.

The administrator's own account (the default runtime) is configured like everyone else's. It is the
gateway's account, separate from AbstractCore's own local account (`abstractcore email`); on the
first start, when the gateway has no account for the administrator and AbstractCore has one, that
account is copied once into the gateway settings (the consoles say which account you are editing).

## Where things are stored

| What | Where |
|---|---|
| Account settings, policy, limits | `<plane>/email/account/abstractcore.json` |
| Password / OAuth tokens | `<plane>/email/account/email/secret.enc` (AES-256-GCM; key in the OS keychain, or a 0600 key file when there is none) |
| Watcher state | `<plane>/email/watcher.json`; the cursor and received mail in `<runtime data dir>/event_inbox/` |
| Notification preferences and outbox | `<plane>/email/notifications.json`, `<plane>/email/outbox.sqlite3` (created with the first notice) |
| What is available to users (defaults + per-user) | `<data_dir>/auth/capabilities.json` |
| Agent email tools choice | `<plane>/email/agent_tools.json` |
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
account (`ABSTRACT_BACKLOG_EMAIL_TO` and the related account variables are ignored); the same notice
is sent at most once per UTC day.

The admin-only `/api/gateway/email/*` routes remain as deprecated aliases acting on the calling
administrator's own account, and will be removed in a later minor release; use `/api/gateway/me/email`.
