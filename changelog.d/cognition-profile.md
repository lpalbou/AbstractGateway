### Added

- **An entity's cognition profile: what each part of its thinking uses, and how many memories a turn brings to
  mind** (`entity_cognition_profile.py`, `routes/entities.py`, `docs/entities.md`). `GET
  /api/gateway/entities/{name}/cognition-profile` answers, for `summaries`, `understanding` and `extraction`, the
  stored `choice` (`{"inherit": "mind"}` by default, or `{provider, model, thinking, max_input_tokens}`), the
  `effective` model and its `source` (`profile` or `mind`); `recall` (`shelf_size`, default 50, `source`,
  `applies_to_running_loop: "on restart"`); `rebuild` (`sensitivity` 0.5, `cap_per_turn` 3, `source`, `sources`);
  and the `mind`. `PUT` takes any subset of `capabilities`, `recall` and `rebuild`: an absent field keeps its value,
  `null` returns it to its default. It is checked like the mind's route: the reasoning-effort words, `max_input_tokens`
  4096 to 140000, `shelf_size` a whole number 1 to 400, `sensitivity` 0 to 1, `cap_per_turn` a whole number 0 to 10
  (text, decimals and true/false are refused), an `endpoint:` provider that is an enabled connection, and for the
  entity's creator a model the gateway offers (`403 not_offered`; a capability saved unchanged is kept). Refusals are
  sentences such as "Memories per turn must be a whole number from 1 to 400.". Each change records a
  `cognition_profile_changed` marker (old, new, by, as) in the entity's history before the file changes; when the
  marker cannot be recorded the answer is 503 and nothing is written. The route is an entity setting (an admin or
  the creator; "Only an admin or <name>'s creator can change how it thinks."), and the console names the marker
  "Thinking settings changed". Requires the AbstractRuntime release that ships `identity.cognition_profile`.

### Changed

- **Visits, summons and own time use the entity's own recall width** (`entity_chat.py`, `entity_visits.py`,
  `entity_gate.py`, `entity_loop.py`, `routes/entities.py`). When a request names no `shelf_size`, a visit
  (`chat/open`), a durable visit, a summon and the start of the entity's own time take `recall.shelf_size` from its
  cognition profile, else 50 as before; a request's own value still wins. `loop_spawn.json` now records only a width
  the start request named, so a restart after a crash reads the profile again.
