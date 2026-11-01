### Added

- **Decisions: AbstractCore's `decision` output modality through the gateway** (AbstractCore 2.32.0,
  AbstractRuntime 0.16.0). Text in, calibrated typed answers out (yes/no, choice, score with confidence),
  no text generation, no artifact.
  - `POST /v1/decisions` is served at the OpenAI API door (body `{model, state, questions, truncate?}`,
    AbstractCore answers); Guest (Open mode, no key) may use it like chat and speech: model use, no tools.
  - `POST /api/gateway/runs/{run_id}/decisions` body `{state, questions, model?, truncate?}` runs one
    durable child run through `run_facade.decide` and answers the child run id with `decision` (the
    System One body) and `answers`; a missing runtime facade or a refused decision answers
    `ok: false` with `code` (`decision_unavailable`, `decision_error`, `decision_no_answers`) like the
    media routes.
  - `GET /discovery/capabilities` lists `decision` among the capability plugins and carries a
    `decision` contract (`common`, `flow_editor`, `assistant`): the VisualFlow `decide` node and the
    direct endpoint, beside `media`, not inside it.
  - Console: the `output.decision` route offers the discovery catalog filtered on that route; the
    Models page has a Decision capability filter (from the catalog row's `decision` tag or capability);
    Decisions badges and rows on the first-run and advanced model pages; the tray lists the configured
    decision model under Decision; the Rust console labels the route and the loaded-model task.
  - Floors: `abstractcore>=2.32.0`, `AbstractRuntime>=0.16.0`.
