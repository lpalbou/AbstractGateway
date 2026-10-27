# 0940-gateway: [BUG] Push every registered device of the account, never "phone first"

> Package: abstractgateway
> Type: bug
> Status: planned
> Created: 2026-10-26
> Priority: P1
> Labels: push, apple, watch, iphone, assistant

## Review gate (read first)

Before writing any code, the agent that picks this item up MUST have the design reviewed by
**Fable** (`claude-fable-5-1`) **and** by one adversarial architect subagent, and record both
verdicts under Receipts. The architect attacks the design below (double alerts on the wrist,
stale registrations, channels, relay limits, the docs/tests listed here); Fable reviews the
architect's verdict and the final diff. Implementation starts only once both are recorded.

## Summary

A push must reach every device the person has registered for push: their iPhone(s) AND their
watch(es). The dispatcher instead applies a "phone-first" rule: when any phone of the account
refreshed its registration in the last 24 hours, only phones are pushed, and the watch gets the
alert only if Apple mirrors the iPhone's notification to it. Remove the rule: send to every
active registration of the account, filtered only by a published alert's explicit `channels`.

## Why

Operator, 2026-10-26: "a push is a push. If I have registered multiple devices, the push should
appear on these devices. There is no such thing as iphone first… I often go outside with only my
watch, so it needs to be fully functional." The same day the operator ruled that the iPhone and
watch apps are independent devices (each signs in on its own; the iPhone→watch sign-in handoff
was removed from AbstractAssistant, ADR 0005 amendment in that repo).

Failure today: with the iPhone left at home (or off, or unlocked in hand), its registration is
still "fresh" for 24 h, so `alert_targets` returns only the phone and the watch on Wi-Fi/LTE
receives nothing. Mirroring only happens when the paired iPhone receives the alert while locked
and the watch is worn.

## Current code reality

- `src/abstractgateway/push/dispatcher.py:117-133` `alert_targets`: `asked` filter, then fresh
  phones, else fresh watches, else any phone, else any watch. Docstring at `:20-27` contradicts
  itself ("phone-first among them" then "watch-first among them").
- `src/abstractgateway/push/store.py:30` `DEVICE_TTL_S = 7 days` (expiry, `:121`); `:32`
  `WATCH_FRESH_S = 1 day` (only used by the rule above, imported at `dispatcher.py:54`).
- Docs disagree three ways: `docs/push.md:34` ("a watch registered in the last 24 hours takes the
  alerts; otherwise the iPhone"), `docs/push.md:38` (phone first), `docs/api.md:1320` ("watch
  first among them"), `CHANGELOG.md:10` (phone-first) vs `:16` (watch-first).
- Tests encoding the rule: `tests/test_push_dispatcher.py:4,248-271` (`test_phone_first`, and the
  two-device test expecting one alert), `tests/test_notify_me_channels.py:4` (channel sets).
- Every push already carries `apns-collapse-id = sha256("attention:" + record_key)[:32]`, which
  the apps also use as the notification identifier of their own catch-up notifications.
- Relay limits are per device token (`push/relay.py:59`), so fan-out does not starve a device.

## What Apple does (evidence gathered 2026-10-26, to re-check)

- Verified: WWDC19 session 208 ("Creating Independent watchOS Apps") — send to both devices
  simultaneously and "the system will appropriately de-duplicate the notifications". An Apple
  engineer (developer forums thread 737026): with both apps installed the watch waits for the iOS
  notification and de-duplicates on a matching notification **identifier**; if the phone never
  fires, the watch alerts after a delay.
- Belief, not verified: that `apns-collapse-id` is the identifier matched; the behaviour with the
  phone unlocked; the delay when the watch is away from the phone. Must be verified on hardware.

## Scope

### In scope

- `alert_targets` returns every active (non-expired) registration of the principal, after the
  `asked` (channels) filter. Delete `WATCH_FRESH_S` and the tiers.
- Raise `DEVICE_TTL_S` (proposal: 30 days) so a watch whose app is not opened for a week keeps
  receiving pushes; uninstalled apps are already removed through APNs "gone" answers. Confirm
  the cleanup path before changing the TTL.
- Rewrite the docstring, `docs/push.md`, `docs/api.md:1320` and a CHANGELOG entry so all say the
  same thing: every registered device, Apple de-duplicates a watch and its own iPhone.
- Tests: replace `test_phone_first` with an every-device test, two-device send expects both
  tokens, channel tests (`["watch","phone"]` and absent) expect both; a mutation check
  (reinstating the tier turns the new test red).

### Out of scope

- Removing an alert from other devices once it is consumed: the apps replay the ledger and the
  event stream, so an answered item leaves every app by design (operator, 2026-10-26). No silent
  "seen" push.
- Client rendering (`interruptionLevel`), registration refresh from the notification extension:
  AbstractAssistant's own items.

## Dependencies and implementation guidance

Clients need no change for the fan-out: each app registers its own token at its own sign-in
(`app: "phone"|"watch"`). If hardware shows a double alert on the wrist, do NOT restore
phone-first; bring the evidence back to the operator (options: identifier strategy, a
client-side rule in the iPhone's notification service extension).

## Acceptance Criteria

- [ ] `alert_targets` returns every active registration of the principal (phones and watches), filtered only by `channels`; `WATCH_FRESH_S` is gone.
- [ ] A watch-only person (no phone registered) and a person whose phone is registered both get the alert on the watch directly.
- [ ] `channels: ["watch"]` pushes only watches, `["phone"]` only phones, absent pushes all.
- [ ] Docs, API reference, docstring and CHANGELOG state one rule.
- [ ] Fable and the adversarial architect verdicts are recorded under Receipts before implementation.
- [ ] Hardware check recorded (paired iPhone + watch, one account): phone locked + watch worn → one alert on the wrist; phone in airplane mode, watch on Wi-Fi/LTE → the alert arrives on the watch.

## Testing

`python -m pytest tests/test_push_dispatcher.py tests/test_notify_me_channels.py tests/test_push_relay.py -q`, then a real push to a paired iPhone and Apple Watch signed into the same account (`watch/apple/scripts/deploy-watch.sh` and `phone/iphone/scripts/deploy-phone.sh` in AbstractAssistant) for the two hardware cases above.

## Receipts

- 2026-10-26: filed from the AbstractAssistant session after an adversarial review of the
  dispatcher (code references above re-checked against the live files). No code changed.
