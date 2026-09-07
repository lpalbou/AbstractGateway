# 0001 — Worker second lane (slow reads) — DESIGN RECORDED, deliberately not built

- Status: proposed / dormant (round-4 review P2-1; build only on recurring
  operator pain)
- Decision at recording time: the serial worker STAYS — total order is a
  correctness feature (journal order == command order; write→verify→publish
  atomicity; a stale read can never overwrite a write's publish).

## The real cost being traded

One `rx.recv()` loop serializes everything, so a slow op head-of-line-blocks
fast ones. Concrete: enter Runtimes on a large data dir (the
`include_sizes=true` disk walk rides the 300s agent) → switch to Users →
"⟳ loading…" until the walk finishes. Sharpest: a PROBE queues behind it
while `ConnPhase::Probing` was already set at handle time.

## The minimal safe shape (when pain recurs — do NOT improvise under incident pressure)

One second lane carrying ONLY non-journaling, non-shared-domain ops:
`LoadModels`, `LoadVoices`, `DiscoverModels`, `SandboxTest`, `TestRoute`,
`EntityVerify` — none call `finish_write`; each publishes a single-slot or
per-key signal. Requirements the design MUST carry:

1. **Client-generation stamp**: a slow-lane request in flight across a
   `Connect` holds the OLD client and would publish a stale result after
   `reset_domains`. Every slow-lane publish must carry the client generation
   it was made under, checked in the posted closure (the `VoicesData`
   fetched-for-pair pattern, generalized).
2. **Shared client slot**: `require_client` moves from the loop-local
   `Option<GatewayClient>` to `Arc<RwLock<Option<GatewayClient>>>`.
3. The busy strip already renders `Vec<BusyOp>` — UI side is ready.

Estimated ~80–120 lines. A bare thread pool is FORBIDDEN by the total-order
argument above (do-not-touch #2 of the round-4 review).
