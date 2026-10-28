# 0965-gateway: Device management per account (discussion item)

> Package: abstractgateway (accounts, sessions, push registrations, API keys, audit); gateway console (web islands via AbstractUIC, TUI `abstractgateway-console`)
> Type: discussion / feature exploration
> Status: proposed (operator: "the idea is not fully formulated, value to explore"; filed 2026-10-28, closing pass)
> Created: 2026-10-28
> Priority: normal
> Labels: accounts, devices, sessions, push, security, console, privacy
> Related: root 1060 (gateway security / resilience / latency standing duty), root 1049 (Apple push relay for watch and phone), gateway 0940 (push every registered device), gateway 0964 (first connection sets the admin password), AbstractUIC 0062 (the Devices page as a kit island)

## Summary

The gateway already knows, per user, where that person is connected from: push registrations (iPhone, watch),
session tokens issued to the desktop / iPhone / watch / web apps / consoles (`X-Client-Id`, `X-Client-Version`
in the audit log), named API keys (root 1000) and the `/v1` key owner. Nothing shows this to the person. A
"Devices" page per account in the console (web and TUI) would list each place the account is used from (device,
app, version, last seen, push registered, address or tailnet node), with revoke/block, rename and "send a test
push". This item frames the questions before any design is committed.

## Questions to settle (discussion)

1. What counts as a device? A push registration is a device; a session token is a sign-in; a browser tab is
   neither. Proposal: a device = a stable client identity (`X-Client-Id` + platform) that the client registers
   once (`POST /devices`), to which sessions and push registrations attach; unregistered clients appear as
   "sign-ins" (token, app, last seen) without device-level actions.
2. How do sessions map to devices? Today a session token carries no device id. Either the client sends its
   device id at login (then the session records it) or the gateway infers from `X-Client-Id` per request (weak:
   any client chooses the header). The first is the honest path; old clients show as "unknown device".
3. Blocking vs revocation: revoking a token ends one sign-in; blocking a device must also refuse its future
   logins (needs the device identity to be unforgeable: a per-device secret issued at registration, not a header).
   What does "block" mean for a watch paired through the phone?
4. Privacy of addresses: IPs and tailnet names are personal data; show them only to the account's owner and
   admins, truncated by default, with the `trust_proxy` attribution rules (`docs/security.md`) and a retention
   window (last seen only, no history) unless the audit log already keeps it.
5. Admin view: an admin sees every account's devices (support) — the owner rule (`403`/`404` for non-admins)
   applies as for memory routes; actions are audit-logged.
6. Push test: reuses 0940's "push every registered device" path with a labeled test alert; the watch and phone
   relay (root 1049) must say which registration answered.

## Possible shape (not decided)

- Routes: `GET /me/devices`, `POST /devices` (register), `PATCH /devices/{id}` (rename), `DELETE /devices/{id}`
  (revoke: sessions + push registration), `POST /devices/{id}/block`, `POST /devices/{id}/push-test`; admin
  variants under `/admin/accounts/{user}/devices`.
- Console: a "Devices" tab on the account page (web island in AbstractUIC, TUI screen in `abstractgateway-console`).
- Clients: desktop / iPhone / watch / web apps register once and send the device id at login.

## Next step

A short design note answering 1-4 with the operator (decision gate), then split into gateway routes, kit island,
TUI screen and client registration items.
