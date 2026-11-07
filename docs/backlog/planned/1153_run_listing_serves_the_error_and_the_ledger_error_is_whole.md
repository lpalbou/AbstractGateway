# 1153-gateway: [DEFECT] The run listing serves each run's error, and the ledger error is stored whole

> Package: abstractgateway (routes/gateway.py run listing ~10244; ledger error recording)
> Type: defect (ADR 0054: stored text is never truncated; values as recorded)
> Status: planned
> Created: 2026-11-08 (found by the adversarial review of the Observer Board redesign, untracked/observer-board/REVIEW-1.md)
> Priority: P1 (part of the current release: small, and every client that lists runs is blind to failures without it)
> Labels: runs, api, truncation, observer

## Summary

Two findings from the Board review, both on the gateway side:

1. **The run listing hard-codes `error: None`** (`routes/gateway.py` ~10244, the row builder of the list route), while
   `GET /runs/{id}` carries the real reason ("Effect failed after 3 attempts: SpeculationUnavailableError…"). Every
   client that paints a board from the listing shows "failed" with no reason, groups unrelated failures together, and
   must fetch each record one by one to learn why (Observer does, then discarded it: fixed on its side). The listing
   row must carry the run's `error` as recorded (the whole text; ADR 0054), plus `paused`/`pause_reason` when the
   record has them, instead of the literal placeholders.
2. **The ledger's recorded error is cut at 400 characters** when a step fails (the review names the cut; find the
   `[:400]` on the error path when recording the failed step/effect). ADR 0054: the whole error is stored; a client may
   fold it.

## Scope

- Listing rows read `error`, `paused`, `pause_reason`, `paused_at`, `resumed_at` from the run record (or the index row
  if the index carries them; if not, the index gains them at write time so the listing stays one read).
- The ledger stores the failure text whole; the API serves it whole.
- Tests: a failed run appears in the listing with its full error; a 2,000-character error is stored and served whole;
  a mutation that restores `error: None` turns the listing test red.
- Docs: `docs/api.md` run listing fields; CHANGELOG fragment.

## Out of scope

- Observer's own display (fixed in Observer 0.13.0 after the review); the Assistant's stored automation titles (its
  own item, 1154).
