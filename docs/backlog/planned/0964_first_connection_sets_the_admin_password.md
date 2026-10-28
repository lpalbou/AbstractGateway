# 0964-gateway: A fresh install asks for the admin password on the first connection; sign-in by account + password or token

> Package: abstractgateway (first run, user accounts, console sign-in, installer summary); clients: gateway console (web + TUI), AbstractAssistant desktop / iPhone / watch, AbstractCode, the web apps' sign-in pages (AbstractUIC identity widget)
> Type: feature
> Status: planned (not implemented; filed by the operator's request on 2026-10-28, closing pass)
> Created: 2026-10-28
> Priority: P1
> Labels: first-run, user-accounts, sign-in, installer, security, console, clients
> Root link: AbstractFramework root backlog 1060 (gateway security / resilience / latency standing duty)

## Summary

Today a fresh install creates the admin account `default/admin` and the only way in is the installer-printed
bootstrap token (`<data dir>/auth/bootstrap-admin-token`, printed by `abstractgateway serve`, or the one-time
`#claim=` link; `docs/security.md`, `docs/first-run.md`). `POST /api/gateway/session/login` takes a token only.
The operator wants the person's path to be a password, with the token kept for automation:

1. A default admin account exists on every fresh install (as today: `default/admin`, no password set).
2. The FIRST connection, from the console or any client, asks the person to set the admin password and shows
   how to recover it later (the recovery code by email where configured, otherwise the token file on the
   gateway machine, or the operator's `abstractgateway claim`). Until the password is set, the only credentials
   that work are the bootstrap token / claim link, and every signed-in surface shows "Set the admin password".
3. Later connections sign in with account + password, or with a token (automation, CLIs, agents).
4. The installer's summary says "Open <console url> and set the admin password" instead of printing a long
   token; the token stays in the `0600` file and `--no-print-token` becomes the default of the installer path.

## Why

A long token in the installer's output is copied by hand, pasted into chats, and lost; a person's mental model of
"a server I sign in to" is an account and a password. The token flow is right for machines and stays.

## Design (to be refined in the implementation item)

- Account record: `password_hash` (argon2id or scrypt via the existing keyed-hash helpers, never a reversible
  store), `password_set_at`, `must_set_password` (true on a fresh install's admin until set).
- Routes: `POST /session/login` accepts `{token}` (unchanged) or `{account, password}`; `POST /session/password`
  (set/change; requires the current credential: a token principal of the same account, the current password, or
  a recovery code); `GET /session/state` (or the existing capability/`me` route) reports `must_set_password` so
  every client can show the step. The console's sign-in page gains the account + password form; the claim link
  lands on "Set your password" instead of a token echo.
- Installer and `serve` output: the summary names the console URL and the one-time claim link; the token is
  mentioned as "for automation: `<data dir>/auth/bootstrap-admin-token`".
- Clients: console sign-in page (web + `abstractgateway-console` TUI), AbstractAssistant desktop / iPhone / watch
  sign-in with account + password (the watch through the phone's pairing, never typing a password on the wrist),
  AbstractCode (web + terminal), the shared identity widget in AbstractUIC (`AfTopBarActions` identity menu) so
  every web app gets the same form; each keeps "Sign in with a token" as the secondary path.

## Security considerations (must hold)

- No default password, ever: the account is created without one; nothing accepts an empty or well-known password.
- Password policy: minimum length (12) and a breach-list check are server-side; the client only mirrors the rule.
- Rate limiting and lockout: the existing auth lockout (`ABSTRACTGATEWAY_LOCKOUT_AFTER/BASE_S/MAX_S`, per account
  and client address, proxy attribution rules in `docs/security.md`) covers password attempts; a failed password
  attempt counts exactly like a failed token.
- The last-admin rule (`admin_accounts.py`, reason `last_admin`): the last enabled admin can never be disabled,
  demoted or locked out of every credential; setting/changing the admin password never removes the token file.
- Recovery: the email recovery code flow (`/session/recovery/request|redeem`) sets a new password; without email,
  the operator's path is the token file / `abstractgateway claim` on the gateway machine; both are audit-logged
  (`auth.password_set`, `auth.password_login_failed`).
- Transport: a password is accepted only over loopback, LAN with the gateway's TLS, or a trusted proxy; the
  `internet` network mode refuses password login without TLS (same rule as the token today).
- Sessions issued by a password login are the same session tokens as today (revocation, expiry unchanged).

## Acceptance

- Fresh install: `abstractgateway serve` + console → "Set the admin password" is the first screen; after it, the
  console and a client sign in with `default/admin` + password; the bootstrap token still signs in an API client.
- Installer summary (install.sh / install.ps1 / the framework bootstrap) prints the console URL and the sentence
  above, no token; `docs/first-run.md`, `docs/security.md`, `docs/configuration.md` updated.
- Tests: no password → password login refused with a typed reason; wrong password ×N → lockout; last-admin
  invariant; recovery sets a password; `must_set_password` cleared exactly once; audit records present.
- Client items filed in each client's backlog and linked here before this item closes.
