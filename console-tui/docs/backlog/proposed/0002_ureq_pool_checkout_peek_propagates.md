# 0002 — Upstream (ureq 2.12.1): pool-checkout health peek propagates its error instead of discarding the socket

- Status: proposed (upstream filing candidate; found by the round-4
  transport audit, 2026-07-25, forensically verified live)
- Affects: any ureq 2.x consumer with pooling on, against servers whose
  idle sockets die with RST (uvicorn + macOS fin_timeout = the exact
  console incident)

## The hole

`unit.rs:362` — `let server_closed = stream.server_closed()?;` — the
checkout health peek's ERROR propagates before either retry arm exists
(send_prelude retry at ~266, idempotent-read retry at ~300). A clean
queued FIN reads as EOF and recovers invisibly ("dropping stream from
pool"); an RST-latched socket (server FIN at 5s + macOS reaps
FIN_WAIT_2 at +60s → RST) makes the peek return Err(ECONNRESET), which
fails the whole request as `Transport { kind: Io }` — the caller sees
"Connection reset by peer" for a request that never left the machine.

A health check whose FAILURE means "connection dead" should discard the
socket and continue to a fresh connection, exactly like its EOF branch.

## Console-side status

Moot for this app since 0.3.4 (pooling disabled — the measured benefit
window was ≤5s against a burst-then-idle usage profile). Recorded so
the upstream issue can be filed with the full repro: raw-socket state
trace (ALIVE → EOF at t+5.03s → ECONNRESET at t+65.46s) and a ureq
probe reproducing the verbatim error with the console's builder
settings.
