# 0978 — A gateway with `--data-dir` never writes outside it (the Core config dir is derived from the data dir by default)

**Status**: planned · **Priority**: P1 (security: isolation) · **Created**: 2026-10-29 (lead ruling after the residency audit incident; filed by the merge desk)
> Root link: AbstractFramework recurrent 1060 (security, resilience and latency standing duty).

## Why
On 2026-10-29 two scratch gateways, started by hand with their own data dir and no `ABSTRACTCORE_CONFIG_DIR` /
`ABSTRACTCORE_CONFIG_FILE`, answered capability-default PUTs by writing into the operator's shared Core store
`~/.abstractcore/config/abstractcore.json` (docs/security.md, incident 2026-10-29). The root scope writes to the
source on purpose (an admin editing a default edits the source), and the source is resolved by AbstractCore's resolver,
whose default is the machine store. The instances runner already derives `<data dir>/core-config` for its children
(`instances.py`); a gateway started directly does not, so two gateways on one machine silently share one config file.
A data dir that is not a boundary is a security defect: a test or a second operator instance can change what the
production gateway boots with.

## What
- A gateway started with an explicit data dir (`--data-dir`, `ABSTRACTGATEWAY_DATA_DIR`) derives its Core config dir
  from it (`<data dir>/core-config`, the instances runner's rule) when neither `ABSTRACTCORE_CONFIG_DIR` nor
  `ABSTRACTCORE_CONFIG_FILE` is set — one rule for `serve` and for the instances runner, in one place.
- The machine store (`~/.abstractcore/config/abstractcore.json`) is used only by a gateway with no explicit data dir
  (the operator's single-gateway install keeps today's behaviour), and the Settings → Resources page says which file
  it is writing.
- Every other write the gateway makes (users, tokens, first-run state, flows, engines' install records, logs) is
  listed once in docs/configuration.md with its path under the data dir; a test walks that list.
- A test starts a gateway with a data dir, PUTs a capability default and asserts the shared store is byte-identical
  before and after (mutation check: with the derivation removed the test is red).
- Decision to record with the operator: whether an instance that WANTS to share the machine store opts in by setting
  `ABSTRACTCORE_CONFIG_DIR` explicitly (the proposal here), since the root-scope ruling "an admin edits the source"
  stays true of whichever file is the source.

## Done when
`abstractgateway serve --data-dir <tmp>` plus a capability-default PUT leaves `~/.abstractcore/config/abstractcore.json`
unchanged (test, red without the fix); the instances runner and `serve` share the derivation; docs/configuration.md
lists every path the gateway writes; the incident entry in docs/security.md links here.
