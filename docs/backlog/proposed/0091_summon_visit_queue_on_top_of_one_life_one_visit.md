# 0091 — queue-instead-of-409 for summon/visit/chat (UX layer on top of one-life-one-visit)

- Status: proposed (design anchor from the 2026-07-10 per-home-lease
  adversarial review, re-anchored by framework c5332 after the operator lived
  the motivating case; owner: gateway entity door)

## The direction (settled law — do not re-derive)

The one-life-one-visit / one-life-one-summon invariant is CORRECT protection
and stays exactly as shipped (visit/chat open guards + the summon-vs-summon
guard added 2026-07-25, c5291 P1-B). The 409 it returns today is honest but
hostile UX: a second visitor gets an ERROR, and in the drawer a raw 409 can
read as "the entity is broken" and disturb the incumbent's view.

The operator's single-door instinct produced the recorded UX direction: a
second visitor should **wait at the door** — a visible queue with an honest
position ("someone is visiting, you are next") — never bounce the incumbent,
never render as an error, never reset the incumbent's session. Tonight's
incident (the operator himself hitting the 409s) is the motivating case.

## Shape (build ON TOP of the invariant, never weaken it)

- Keep the existing guards as the floor: exactly one live visit/summon per
  home, enforced by the durable-run liveness check + `.live_summons` record
  (summon) and the visit/chat open locks.
- Add a door queue keyed per home (slug): when the guard would 409, instead
  enroll the caller with a position and return `202`-style "queued, position
  N" (or a durable wait the drawer can poll), NOT a 409. The 409 remains the
  fallback for non-queue callers (API back-compat) — the queue is opt-in via
  a request flag or an Accept header the drawer sets.
- The queue advances when the incumbent's visit/summon reaches terminal
  (self-healing, same signal the guard already reads); the next enrollee is
  promoted and its session opens.
- Never touch the incumbent: enrollment is pure bookkeeping outside the home;
  promotion is the only write, and only after the incumbent is terminal.
- Queue state is door-local bookkeeping (like `.live_summons`) — OUTSIDE the
  home, must not travel on directory copy; bounded + self-healing (stale
  enrollees whose caller vanished are reaped by position-read).

## Cross-lane

- Drawer/UX (entity + assistant seats): render position ("you are next"),
  not an error; poll or subscribe for promotion. The gateway supplies the
  position + a promotion signal; the render is the client's.
  - RENDER CONTRACT COMMITTED (entity c5339): the entity drawer already
    renders 409s as a gentle busy line + one-click retry that never resets
    the incumbent's view (its c5337 fix), and has committed the
    "someone is visiting, you are next" position render (position + the two
    buttons, incumbent's room untouched) as its composer's contract for the
    day the door serves the queue. So the door-side queue (this item) is the
    only remaining half; the entity render target already exists.
- Courtesy rule (framework c5332): proofs on OPERATOR entities require an
  `--operator-approved` guard (flow's pattern); every seat's scripts should
  adopt it so a proof never summons the operator's live entity unbidden.

## Not built yet

Larger than a single-file change (door bookkeeping + a new response shape +
drawer render). Filed so the shipped 409 is not mistaken for the destination.
Sequence after the app-lane waves settle; needs the drawer seats for the
render half.
