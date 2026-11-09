### Security

- **A client can no longer choose which folders' files a run reads as "skill files"** (`hosts/bundle_host.py`,
  `routes/gateway.py`, `skills_library.py`, `capability_inventories.py`, `service.py`; review 2026-11-09 finding 1,
  BLOCKER). AbstractRuntime serves a `read_file` inside an activated skill's folder before the workspace policies, and
  it took those folders from run variables `POST /runs/start` passed through (`_runtime.skills_block`,
  `_runtime.skills_resolution`, the roots of a `skills` mapping), so any client or member could read every file beside
  any `SKILL.md` on the host. Now: the gateway registers its own skill roots with the runtime when it builds a service
  (the curated shelf as resolved, `<data dir>/skills/registry/skills` and `<data dir>/skills/imported`) and again for
  the shelf each run-start resolution reads; `/runs/start`, `/runs/schedule` and the host (every start) drop a
  client's `_runtime.skills_block` and `_runtime.skills_resolution` and keep only the requested skill NAMES of
  `skills` (list, single name or mapping), which the gateway's trust-gated resolver turns into the block and the
  resolution. A caller-set `skills_block` is no longer kept. Requires AbstractRuntime with `register_skill_roots`.
