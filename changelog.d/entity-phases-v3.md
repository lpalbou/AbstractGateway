### Changed

- **Stop takes own time back; Resume is idle** (contract-phases v3 D3, operator 2026-11-09). `POST …/lifecycle
  {action: "stop"}` also revokes the personal grant (marker `personal_grant_revoked`, `revoked_by: kill_switch`) and
  forgets the phase to return to. Its answer carries `own_time_revoked` and an `own_time` row, "Own time was revoked."
  `resume` writes awake and the entity is `idle`: nothing from before the Stop is restored, and own time starts only
  when the operator turns personal on again.
