### Added

- **The kill switch: `POST /api/gateway/entities/{name}/lifecycle`** `{action: "stop" | "resume" | "retry"}`
  (operator ruling 2026-11-08; contract `CONTRACT-phases.md` v2 §3; admin-only).
  - `stop` is instant and unconditional, in a fixed order:
    1. The home's lifecycle `generation` is bumped first. This is the fence.
    2. `paused` is written.
    3. Cancellation is signalled at once: the loop's durable stop command, the summon seat's run tree, an in-flight
       visit turn's run tree, and the queued admissions (failed with "Stopped. Resume him first.").
    4. Each teardown then runs on its own: the loop gets SIGTERM, then SIGKILL after 3 s; the durable visit closes with
       `closed_by=pause`; the hosted chat closes without reflection.
  - The response lists one row per item (`run`, `visit`, `chat`, `loop`, `maintenance`, `queue`) with a stable
    `outcome` and, on failure, a `code` and a sentence. It also carries `complete`.
  - A repeated Stop retries whatever is still outstanding.
  - Every writer that began before a Stop gives up instead of undoing it: the unbounded visit wait (both lanes), the
    home-lease wait, an operator sleep's maintenance pass (`finally`), and a mention's answer (neither recorded in the
    session nor mirrored).
  - `resume` clears stopped, writes awake (never a sleep) and restarts own time when `personal` is on.
  - `retry` re-runs the gateway's own-time reconcile.
- `entity_seat.cancel_run_tree_outcomes`: one outcome per run (`cancelled`, `already_terminal`, `missing`, `failed`
  with `run_cancel_failed` or `run_children_unknown`) instead of a log line. `cancel_run_tree` keeps its shape.

### Changed

- **Stopped is the one visit refusal** (operator 2026-11-09: a visit never fails). Both visit doors and the mention door
  answer 409 `{"detail": "Stopped. Resume to visit.", "code": "stopped_use_resume"}`.
  - `entity_asleep` is gone: an open visit's turns are never refused because of a sleep.
  - `entity_paused` is replaced by `stopped_use_resume`.
  - Hosted-chat refusals that carry a code are served as `{detail, code}` with the `X-Gateway-Error-Code` header.
- The durable visit status (`GET /entities/{name}/visit`) also carries `participants` and `created_at`.
