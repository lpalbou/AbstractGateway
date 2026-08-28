# 0093 — Multi-machine model resource pool (aggregate, then place)

- Status: proposed
- Date: 2026-08-27
- Origin: operator request while landing the host-state/model-residency
  surface ("if running for instance mlx on a remote machine, i do not know
  if and how you could account about that, unless developing a multi-machine
  pool of resource abstraction… or even provision model on a given machine
  based on current available memory and gpu usage"). Related:
  abstractruntime `docs/backlog/proposed/0038_core_server_pool_residency_affinity.md`.

## What already works (single remote machine)

A remote machine that runs an AbstractCore server is already fully
accounted for today, because the runtime relays core-owned truth:

- `GET /acore/memory` on that server reports its RAM/device/host block, and
  the gateway's `GET /host/state` `memory` section carries it when the
  gateway runs in remote mode (`ABSTRACTCORE_SERVER_BASE_URL`).
- `GET /acore/models/loaded` on that server includes its own registry plus
  its local ollama/LM Studio sweep, so remotely resident models appear in
  the gateway's rows with `size_bytes`, `modalities`, `locked`, etc.
- Every residency row and memory snapshot now carries a host identity
  (`host_id`/`host_name`, `memory.host`), stamped by whichever core server
  served it. This was added deliberately as the aggregation seam.

The gap is plural: ONE gateway currently binds ONE core backend per plane.
There is no fan-out, no aggregate view over N machines, and no placement.

## Proposed shape (three increments, each independently useful)

1. **Pool registry + aggregate read.** Gateway config lists N core server
   base URLs (`model_pool: [{base_url, label}]`). A `GET /host/state?scope=pool`
   (or `/pool/state`) fans out the three reads (memory, models/loaded,
   prompt_cache stats) with short timeouts, concatenates rows — which
   already carry `host_id` — and reports per-host `memory` sections plus a
   `hosts: [...]` array. Unreachable hosts appear as degraded entries,
   never dropped silently. No writes, no placement. The consoles get a
   host column/grouping for free because rows are already host-tagged.
2. **Targeted operations.** `POST /models/load|unload|lock|unlock` accept a
   `host_id` (resolved to the pool entry's base_url and relayed to that
   core server). The runtime's remote client already talks to one core per
   facade; the gateway resolves host → base_url before relaying. Locks
   stay core-owned per machine (no distributed lock state).
3. **Placement assistance (not automation).** `GET /models/placement_estimate?
   provider=&model=&context_length=` runs the context/fit estimate against
   each pool host's live memory snapshot and returns a ranked list
   ("fits on mbp-m5 (calibrated 32K), does not fit on mini-m2"). The
   operator (or an authored workflow via the MODEL_RESIDENCY effect)
   chooses; the gateway does NOT auto-provision. Auto-placement policies,
   if ever wanted, become a separate opt-in proposal on top of this.

## Non-goals (explicit)

- No cluster membership/gossip; the pool is static operator config.
- No cross-machine model migration, no auto-eviction, no TTL reapers —
  same visibility-plus-explicit-control doctrine as the single-host surface.
- No new identity scheme: `host_id` (stable hostname hash, already shipped)
  is the join key; collisions across NAT'd identical hostnames are accepted
  and disambiguated by the pool `label`.

## Groundwork already in place

- `host_id`/`host_name` on rows and memory snapshots (core `utils/hostinfo.py`).
- Frozen `model_residency_row_v1` with additive-fields tolerance — a `host`
  column is a client rendering change, not a schema break.
- Context/fit estimator with calibration (`/acore/models/context_estimate`)
  — placement estimate is a fan-out of an existing read.
- Per-section degradation idiom in `/host/state` — pool aggregation reuses it.

## Open questions

- Auth to remote core servers (per-host tokens in pool config?).
- Whether the runtime facade should learn multi-backend natively (0038's
  `core_uuid` affinity) or the gateway stays the only aggregator. Leaning:
  gateway aggregates; runtime stays single-backend per facade instance.
