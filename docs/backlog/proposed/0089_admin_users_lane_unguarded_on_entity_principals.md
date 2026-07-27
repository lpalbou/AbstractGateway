# 0089 — PATCH/DELETE /admin/users is unguarded on entity principals (rotate mints a live entity credential; delete opens identity capture)

- Status: SHIPPED 2026-07-25 (operator order c5305; fable5-validated). Guards
  live at the `GatewayUserRegistry` chokepoint (`update_user`/`delete_user`
  raise `EntityPrincipalGuardError`; routes translate to 403), extended to the
  reservation-transfer lane (transfer cannot rewrite an entity principal's
  runtime_id — adversary P0) and the generic create lane (POST refuses
  `roles=["entity"]` — adversary P1). Pinned by
  tests/test_gateway_admin_users_kind_and_entity_guards.py. The PRE-CREATION
  adoption half (a human record adopted as an entity's principal) is NOT
  closed here — it is `entities.py` adoption policy and needs a ruling; filed
  as 0090.
- Originally: proposed (P1; found 2026-07-25 by the console-tui lane's
  adversarial investigation of the users/entities conflation, verified against
  the live tree — routes/gateway.py ~400-442, users.py
  update_user/delete_user, entities.py _ensure_entity_principal ~1256-1272)
- Owner: gateway (entity-principals GW-H surface)

## The gap

Entity principals (roles=["entity"], minted at entity creation per the GW-H
entities-as-users design) are reachable through the generic admin users lane,
and neither the routes layer nor GatewayUserRegistry checks roles:

1. **`PATCH {user} rotate_token: true` mints a live entity credential that by
   design must not exist.** Entity principals are born with the token
   discarded (door-issued secrets never travel, entities.py ~1229). A rotation
   hands the operator a working bearer that authenticates AS the entity
   (scopes entity:<slug>) into the generic per-principal machinery — and
   materializes the phantom plane `users/<tenant>/<slug>/runtime` that
   service.py's eager-rehydration skip (~927) deliberately avoids creating.
   Zero utility, pure hazard.
2. **`DELETE {user}` on an entity principal removes the name-collision guard
   and invites identity capture.** The principal's registry record is what
   blocks user_id/runtime_id = slug for anyone else. After delete (+
   reservation release), an operator can create a HUMAN user named e.g.
   `hypnos` — and the next entity create/adopt call adopts that human record
   as the entity's principal (`_ensure_entity_principal` refuses only
   admin-role records). A human bearer then authenticates under the entity's
   name.
3. Delete also mints a retained-runtime reservation `(tenant, <slug>)` whose
   console purge path targets `users/<tenant>/<slug>` — bounded (it cannot
   reach the home at `entities/<slug>`), but the operator is invited to
   "delete data" on a plane named for a living entity.

## Proposed guard

Refuse `rotate_token`, `token`, `roles`, and `runtime_id` edits AND DELETE on
role=entity principals, naming the lane: `"this principal belongs to summoned
entity '<slug>' — manage it under /entities"`. `enabled` may stay editable
(consumed as "entity principal disabled at the door" by admin_runtimes)
though note it currently gates only `authenticate()`, which no entity token
exercises.

## Related observation

16 entities vs 14 entity principals live right now (castor, mnemosyne
pre-date GW-H minting; re-running create mints them) — principal rows are not
the entity roster; no surface may derive one list from the other.

## Consumer-side state

The console-tui (0.3.4) now partitions its users table exactly like the web
console (humans only + count note); entity rows are unreachable by
rotate/delete there BY CONSTRUCTION. This item is the server-side half — the
API lane itself must refuse, because any client (curl included) can reach it.
