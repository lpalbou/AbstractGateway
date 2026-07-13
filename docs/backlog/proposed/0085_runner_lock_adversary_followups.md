# Proposed: Runner singleton-lock adversary follow-ups (F3-F13, non-blocking wave)

## Metadata
- Created: 2026-07-11
- Status: Proposed
- Completed: N/A
- Roadmap phase: hardening (runner handover shipped 2026-07-11; this is its residue)
- Effort: M (many small items; each is S)

## ADR status
- Governing ADRs: None specific. The runner-lock design itself (flock
  singleton + cooperative takeover + drain-before-release) is recorded in the
  handover thread (flow c582 -> gateway co-sign) and its tests
  (`tests/test_gateway_runner_singleton_lock.py`).

## Context

Flow's P0 runner-lock hardening wave (2026-07-11) gave the GatewayRunner a
per-data-dir flock singleton, a cooperative takeover handshake, heartbeats,
and honest health. One fable5 adversary reviewed the wave; its two blocking
findings were FIXED before co-sign (F1: `stop()` released the flock without
draining in-flight ticks -> drain-before-release + hold-on-timeout; F2: a
stale takeover file naming a reused-alive pid wedged acquirers into a
permanent yield-loop -> unconditional takeover-file clear on acquire; both
pinned in the runner suite). Findings F3-F13 were non-blocking and are FILED
HERE so they survive as a wave instead of evaporating.

## The findings (adversary a40053a6, verbatim in substance)

Runner-side (gateway `src/abstractgateway/runner.py`):

- **F3 — unguarded cursor load in `_run` can kill the worker thread while it
  holds the flock.** An exception between acquire and the loop's try block
  leaves the lock held by a dead thread until process exit (deadlock-shaped,
  loud only at the next acquire attempt). Fix: wrap acquire->loop in
  try/finally + release.
- **F4 — promotion path terminally FAILs runs on a stale-registry holder.**
  A holder that never resolved a workflow this session can, after the 10s
  promotion window, mark runs FAILED that a fresher peer could execute.
  Fix: gate promotion on having-resolved-at-least-one-workflow-this-session.
- **F5 — `/api/health` blocks on `_service_lock` during per-principal
  construction.** A slow principal build (network-bound registry init) makes
  the health endpoint hang — the exact moment monitoring most needs it.
  Fix: serve health from a non-blocking snapshot.
- **F6 — a wedged holder self-reports "active".** The active classification
  reads the lock file, not the heartbeat age, so a holder wedged mid-tick
  stays "active" forever. Fix: fold heartbeat-age into the active
  classification (stale heartbeat -> degraded, loudly).
- **F7 — promotion race can overwrite CANCELLED with FAILED.** Between the
  promotion read and the terminal save, an operator cancel can land; the
  promotion save then clobbers CANCELLED with FAILED. Fix: re-check
  terminality immediately before save (compare-and-refuse).
- **F8 — degraded false-positive when `poll_interval > stale_after`.** The
  staleness threshold is independent of the poll cadence, so a slow-poll
  configuration self-reports degraded while healthy. Fix: derive
  `stale_after` from the poll interval (floor at N polls).

Launcher-side (duplicated copies in `scripts/` — F9-F11 also live in the
OBSERVER and FLOW launcher copies; those seats own their copies and were
flagged on the hub thread):

- **F9 — machine-wide kill scoping.** `stop_processes`/`free_port` pgrep
  patterns can match unrelated processes machine-wide (another checkout,
  another user's session). Fix: scope to the repo path / exact command line.
- **F10 — probe misdiagnosis.** The launcher's "is it ours" probe can
  misclassify a foreign process listening on the port as the app to replace.
  Fix: verify process identity (cmdline match) before TERM/KILL.
- **F11 — loose observer/flow pgrep substrings.** The observer/flow launcher
  copies match on loose substrings (e.g. a bare app name) that collide with
  editors/browsers holding similar strings. Fix: tighten patterns in the
  duplicated copies (each owner's lane; gateway's copies fixed under this
  item).
- **F12/F13 — remaining launcher-copy hygiene from the same review** (same
  class as F9-F11: kill-scoping + pattern hygiene in the per-app launch
  scripts that duplicate `scripts/lib/apps_common.sh` helpers). Folded into
  the launcher slice of this wave; verify against the adversary transcript
  (a40053a6) when picking this up.

## Direction

One wave, runner items first (F3-F8 are all in `runner.py` + health route and
each is a small, testable change with an obvious pin), launcher items second
(F9-F13 touch `scripts/` copies and need coordination with observer + flow
for their duplicated copies — flag both seats when the launcher slice
starts). Nothing here blocks entity-lane work; the P0/P1 class is already
fixed and pinned.

## Acceptance

- Each F-item lands with a named regression test (runner suite for F3-F8;
  bats-style or subprocess probes for the launcher slice where testable).
- No behavior change to the shipped handover semantics (drain-before-release,
  unconditional takeover clear, newest-process-wins) — those are pinned and
  this wave must keep their tests green.
