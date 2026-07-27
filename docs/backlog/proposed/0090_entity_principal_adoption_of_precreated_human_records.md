# 0090 — entity-principal adoption can capture a pre-created human record (identity-capture half not closed by 0089's users-lane guards)

- Status: proposed (P1; found 2026-07-25 by the fable5 conformance review of
  the 0089 users-lane guard wave, verified against the live tree)
- Owner: gateway (entity-principals GW-H surface; `entities.py` adoption path)

## The gap

The 0089 wave (shipped 2026-07-25: `principal_kind` field + `?kind=` filter +
PATCH/DELETE guards on `roles=["entity"]` principals + a transfer-lane guard)
closed the *delete-then-recreate* capture and the runtime_id sidesteps. It did
NOT close the *pre-creation adoption* half, because that path never touches an
entity-role record:

1. An admin creates a **human** record under a future entity's slug
   (`POST /admin/users {user_id: "hypnos", roles: ["user"]}` — allowed; the
   generic-create guard only refuses `roles=["entity"]`).
2. `_ensure_entity_principal` (`entities.py` ~1256-1272) adopts ANY existing
   non-admin record as the entity's principal (it refuses only admin-role
   records). The entity's real principal is now a `principal_kind="human"`
   record holding a **live token** the operator was handed at create time.
3. None of the 0089 guards apply to that record (they key on the `entity`
   role, which it does not carry), so it stays fully mutable/rotatable, and
   `?kind=entity` never lists it — the entity is invisible to the entity
   census while its credential is a live human bearer.

## Why it was scoped out of 0089

0089 guards the *entity-role* records reachable through the users lane. The
adoption path is an `entities.py` policy question, not a users-lane guard:
what should adoption do when a record already exists under the slug? Options
have real tradeoffs and need an operator ruling:

- **(a) Refuse adoption of a record that carries a live token / was created
  through the generic admin lane** (mint a fresh entity principal beside it,
  or refuse the whole create) — safest, but breaks any legitimate migration
  where an operator pre-provisions the record.
- **(b) On adoption, STAMP the record into the entity plane** — force
  `roles += ["entity"]`, discard/rotate-away the token, so the 0089 guards
  then apply and `?kind=entity` lists it. Closes the capture and the census
  blind spot; changes the record under the operator's feet (acceptable — it
  is becoming an entity principal by adoption).
- **(c) Leave as-is, document the operator hazard** — weakest.

Recommendation: **(b)** — adoption is the moment the record BECOMES an entity
principal, so stamping the entity role + discarding the bearer there makes the
0089 guard set and the census both correct with no new special-casing. Needs
the maintainer's ruling before implementing (identity-shaping of an existing
record is his call).

## Related (P2, same review)

- The generic `POST /admin/users` still returns the issued token in its
  response for human records (by design) — fine, but the docs line "entity
  principals are minted at entity creation" is now literally true again since
  the generic lane refuses `roles=["entity"]` (0089 wave). Keep that in sync
  if (b) changes the adoption record's role.
- Config CLI (`ensure_bootstrap_admin_user`) pointed at an entity slug raises
  `EntityPrincipalGuardError` as an unhandled traceback — the refusal is
  correct (it prevents promoting an entity principal to admin) but should
  surface a clean message. Cosmetic.
