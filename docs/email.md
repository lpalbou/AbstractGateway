# Email: one user, one runtime, one mailbox

Two things carry the word "email", and the gateway names them differently everywhere:

- your **email address** — where sign-in codes, "Forgot your token?" and notifications go, and the
  first address your agents may write to. It has no password. An administrator sets it when
  creating your user, or you set it yourself (`PUT /api/gateway/me/email/address`);
- your **mailbox** — a connection you make (Google or Microsoft sign-in, or address + password for
  other providers) so that your agents and automations can read and send mail as you. Only you
  connect it; administrators never see or touch it.

Every signed-in person on an AbstractGateway can connect **their own** mailbox. It is stored
encrypted in that user's data home and is used for:

- **automations on new mail** — the `email.received@1` trigger (for example "forward invoices",
  "tell me when X writes", or an AI triage that opens a session with you);
- **notifications** — "Job failed" and "Approval needed", emailed to you through your own mailbox,
  plus the automation results and job completions you ask for per automation or per run;
- **sign-in by email** — "Forgot your token? Email me a sign-in code" on the sign-in page.

Nothing reads your mail by default: no watcher runs until you create an email-triggered
automation, and your agents get no email tools until you switch **Agent email tools** on. Without an account there is no sending and no
email notification; the gateway has no shared "system" sender. The mailbox is only read: nothing marks messages read, moves or
deletes them.

This page is the user and operator guide. The HTTP surface is listed in [api.md](api.md#email),
the security model in [security.md](security.md#per-user-email), and the consoles in
[console.md](console.md).

## Connect your mailbox

You can connect from any of these surfaces; they share the same fields and words:

- **Web console** → Users & Entities → **My email address and mailbox** (every signed-in user): the
  **Mailbox** card, tab **Other** for address + password, **Google** or **Microsoft** to sign in with
  the provider.
- **Terminal console** (`abstractgateway-console`) → Users screen → `@`.
- **HTTP**: `PUT /api/gateway/me/email`.

Give the address and the password (or app password). The gateway finds the IMAP and SMTP servers
from the address — a table of common providers, the domain's own autoconfig file, the Thunderbird
ISPDB, DNS SRV records, then the domain's MX hosts (AbstractCore's deterministic discovery):

```bash
curl -sS -X PUT -H "Authorization: Bearer <your gateway token>" -H "Content-Type: application/json" \
  "$BASE_URL/api/gateway/me/email" -d '{"address": "me@fastmail.com", "password": "<app password>"}'
```

`POST /api/gateway/me/email/discover {"address": "..."}` shows what discovery finds without
connecting (`found`, `source`, `imap`, `smtp`, `username`, and `tried`: every step with its result).
When no step finds both servers, the connect answers `400 email_discovery_failed` with the same
`tried` list and the message "Couldn't find the mail servers for <domain>. Open Server settings and
enter them."; give the servers yourself then (`imap` and `smtp`: `host`, `port`, `security`).
`username` defaults to the form the provider's configuration names, else the address;
`display_name` is optional.

Connect saves and tests in one call: the gateway signs in to both servers first and stores nothing
when a step fails. The error names the step (`detail.step`: `imap` or `smtp`) and says it the way
the consoles show it — "Sign-in refused by imap.example.com — check the password." or "Couldn't
reach smtp.example.com:465." — next to the cause and the fix (for example `email_auth_failed`:
"many providers need an app password when two-step verification is on"). TLS certificates and
host names are always verified; there is no unencrypted mode.

Many providers (Gmail, iCloud, Fastmail, most hosted IMAP) accept an **app password** when
two-step verification is on.

### OAuth2 sign-in (Google, Microsoft)

Microsoft 365 / Outlook needs OAuth2. In the **Mailbox** card, pick the **Google** or **Microsoft**
tab and select **Sign in with Google** / **Sign in with Microsoft** (your own client id and secret go
under the tab's **Advanced**):

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

`GET /api/gateway/me/email` lists `oauth_providers` (`[{id, available, reason}]`): a provider is
available with the gateway's own client or a built-in one; otherwise `reason` says so.

While mailboxes are off for a user, that user's **Connect**, **Test** and OAuth sign-in are refused
(`409 email_disabled`, "Your admin turned mailboxes off for your account.") before any connection is
made; the stored settings are kept.

### What `GET /me/email` tells the account page

Besides the mailbox settings and status (never a secret):

| Field | Meaning |
|---|---|
| `email_address` | your email address as stored on your user record (`""` when none) |
| `registered_address` | "self" for your runs: your email address, else your connected mailbox's own address |
| `email_available` | your administrator allows mailboxes ("Mailboxes for users") |
| `notifications` | `{"job_failed": bool, "approval_needed": bool}`; `notifications_unavailable_reason` says why they cannot send yet ("Connect a mailbox first.") |
| `agent_tools` | `{"on", "available", "unavailable_reason", "active"}`: your switch, whether it can be switched on now, and why not ("Connect a mailbox first.", "Your admin turned mailboxes off.", "Your admin turned agent email tools off.") |
| `oauth_providers` | `[{"id": "google" \| "microsoft", "available", "reason"}]` |

**Use this mailbox** (`PUT /api/gateway/me/email/enabled {"enabled": false}`) keeps the settings but
stops watching, sending and notifications until you switch it back on. **Folder**
(`PUT /api/gateway/me/email/folder {"folder": "Archive"}`, empty = INBOX) changes the folder your
agents and the mail watcher read without reconnecting; the watcher starts that folder from mail that
arrives after the change.

## Agent email tools (off by default)

Your agents and workflows — chats, workflow runs and automations, from every client (Code,
Assistant, Observer, the consoles) — get the email tools (list and search mail, read a message,
list folders, send, reply, download an attachment) only when **all** of these hold:

1. **Agent email tools for users** is on (the administrator's Advanced setting; on by default);
2. your mailbox is connected and in use, and mailboxes are allowed for you;
3. you switched **Agent email tools** on (your account page in the web console, the terminal
   console, or `PUT /api/gateway/me/email/agent-tools {"enabled": true}`). Your own switch is off
   by default; while it cannot be switched on, it shows the reason.

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
a send to you runs without asking in chats and automations alike. "You" is your registered address: the email on your user account (or the gateway's registered
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

Two switches, both on by default; nothing is sent until your mailbox is connected and in use.
Set them on your account page or with `PUT /api/gateway/me/email/notifications`
(`{"job_failed"?: bool, "approval_needed"?: bool}`):

| Switch | Emails you when |
|---|---|
| **Job failed** (`job_failed`) | an automation of yours failed after its retries |
| **Approval needed** (`approval_needed`) | one of your runs waits for your approval or answer |

Two options stand on their own, with no switch involved:

| Option | Emails you when |
|---|---|
| an automation's **Email me the result** (`notify.channels` holds `email`) | an occurrence of that automation asked to notify |
| a run's **email me when done** (`_runtime.notify = {"on": ["finished", "failed"], "channels": ["email"]}`) | that run finished or failed, as asked |

`GET/PUT /api/gateway/me/notifications` keep working: the earlier five-event body is accepted,
`automation_failed` counts for `job_failed`, and `automation_result` / `job_finished` are ignored
as preferences (the per-automation and per-run options above decide). Saved preferences carry
over the same way: `job_failed` is on when `job_failed` or `automation_failed` was on,
`approval_needed` keeps its value, and preferences never saved take the new defaults.

Notices go to your registered address (else your mailbox address), sent by your own account, from
a durable outbox: each notice is queued once and sent once. If the gateway stops in the middle of a
send, that notice is marked `unknown` and never resent automatically. Temporary SMTP refusals are
retried with backoff; sign-in and permanent refusals are shown with their cause and fix. Over your
send limits, the waiting notices go out as one digest when the window allows. **Send test
notification** checks the whole path. Notification emails use fixed templates; the only
model-written text is an automation's own `notify` title and body, labelled as such. Replying to a
notification does nothing.

## Sign-in by email

When at least one account on the gateway has a connected mailbox, the sign-in page offers
**Forgot your token? Email me a sign-in code**. Enter your Gateway user and select the link: it
shows "Sending…", then the code step with what happened ("A sign-in code is on its way to
l•••@•••." or why no code was sent), a field for the code, **Use code**, **Send a new code** (after
30 seconds) and **Back to token**. The 8-digit code is sent to your email address through your own mailbox. The code works once, expires
after 10 minutes and allows 5 tries; it signs you in (`purpose: "sign_in"`, the default), and your
account page can then rotate your token. Clients may also ask for `purpose: "reset_token"`, which
issues a new token (shown once; the old one stops working) with the session.

`POST /api/gateway/session/recovery/request {"user_id": "..."}` answers what happened:

| Answer | Body |
|---|---|
| sent | `{"sent": true, "to": "l•••@•••", "expires_in_s": 600, "message": "A sign-in code is on its way to l•••@•••. It expires in 10 minutes."}` — the first character of the address, never the domain |
| no address | `{"sent": false, "reason_code": "no_email_address", "message": "This account has no email address, so a code can't be sent. Ask your gateway admin for a token."}` — also for an unknown account, a deactivated one, or one without a mailbox to send with |
| an address, but no mailbox | `{"sent": false, "reason_code": "no_mailbox", "message": "This account has an email address, but no mailbox is connected to send the code from. Ask your gateway admin for a token."}` |
| the mail server refused or could not be reached | `{"sent": false, "reason_code": "send_failed", "message": "The code couldn't be emailed (<cause>). Try again, or ask your gateway admin for a token."}` — the request waits up to 12 s for the real outcome; a send still in flight after that is answered as on its way |
| rate limited | `{"sent": false, "reason_code": "too_many_requests", "retry_after_s": N, "message": "Too many codes requested for this account. Try again in N minutes."}` |
| sign-in by email off | `404 recovery_off` |

The trade-off is deliberate: a requester can learn that an account id has an email address, which
makes the page honest ("a code is on its way" or "ask your admin"). Requests are rate-limited per
account (3 per 15 minutes) and per client address (10 per 15 minutes) before any lookup or
background send, codes are stored only as keyed hashes, and every request, issue and use is
recorded in the audit log without the code. An administrator who prefers no such answer turns
**Sign-in by email** off under Advanced; users without an email address use the administrator's
token rotation.

## Administrators

Administrators decide what is **available** to users. The Users tab has one switch,
**Mailboxes for users**; two more sit under its Advanced disclosure
(`GET/PUT /api/gateway/admin/email/capabilities`, which returns each one's label and description):

| Capability | Label | Default | Meaning |
|---|---|---|---|
| `email` | Mailboxes for users | on | users may connect their own mailbox for their agents, automations and notifications (off: no watcher, no sending, no notifications; settings are kept) |
| `email_agent_tools` | Agent email tools for users (Advanced) | on | users may let their agents use their mailbox; each user still switches it on for themselves |
| `email_recovery` | Sign-in by email (Advanced) | on (gateway-wide only) | "Forgot your token? Email me a sign-in code" on the sign-in page |

```bash
curl -sS -X PUT -H "$ADMIN" "$BASE_URL/api/gateway/admin/email/capabilities" -d '{"email": false}'
curl -sS -X PUT -H "$ADMIN" "$BASE_URL/api/gateway/admin/email/capabilities" -d '{"reset": ["email"]}'
```

Per-user overrides (`PUT /api/gateway/admin/users/{user_id}/email {"enabled"?, "agent_tools"?}`)
are honoured; the consoles do not create them and show an existing one (a `user` source in
the `email_account.capabilities` of `GET /api/gateway/admin/users`) as "not allowed for this user"
with a **Reset** action (`{"inherit": ["email", "email_agent_tools"]}`).

Agent email tools became available by default with `capabilities.json` version 3. On the first
start, the gateway upgrades the file so that nobody gains tools they could not use before: a user
whose own agent-tools switch was on while the tools were not available to them gets a per-user
`email_agent_tools: false` override, recorded in the audit log as `email.capabilities_migrated`.

An administrator creates a user with their email address (`POST /api/gateway/admin/users`,
`"email"`) and changes it with `PATCH /api/gateway/admin/users/{user_id}` (`"email"`); the user can
set it too. **Active** is `"enabled"` in the same PATCH (false = signed out and unable to sign in);
an administrator cannot deactivate their own account (`409 cannot_deactivate_self`, "You can't
deactivate your own account.") nor the last active administrator (`409 last_admin`).

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
administrator's account page (**My email address and mailbox**) with the setting that replaced it. The email bridge
(`ABSTRACT_EMAIL_BRIDGE`) is replaced by the per-user watcher and the `email.received@1` trigger.
Maintenance notices go to the administrator's registered address through the administrator's own
account (`ABSTRACT_BACKLOG_EMAIL_TO` and the related account variables are ignored); the same notice
is sent at most once per UTC day.

The admin-only `/api/gateway/email/*` routes remain as deprecated aliases acting on the calling
administrator's own account, and will be removed in a later minor release; use `/api/gateway/me/email`.
