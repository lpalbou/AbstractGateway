# 0972 — CI installs every seam package the gateway mounts, so seam routes are covered

**Status**: planned · **Priority**: P1 (security) · **Created**: 2026-10-29
> Root link: AbstractFramework recurrent 1060 (gateway security, resilience and latency standing duty).

## Why
The gateway mounts other packages' routers when they are installed (`abstractnote_seam.py`: AbstractNote's
`/api/gateway/note/...`). Gateway CI installs only `.[dev]`, so none of those seams exist in CI: the route
authorization contract (`tests/test_gateway_route_authorization_contract.py`) walked a route table without them and
stayed green while 14 note write routes had no authorization decision on every real install (docs/security.md,
incident 2026-10-29; fixed by PR #55). A check whose absent-input case passes is decoration.

## What
- The CI test job installs every package a seam mounts (today: `abstractnote`, at the floor the install profiles
  pin), in a dedicated step that names them, so a seam package that stops installing fails CI loudly.
- A test asserts the seam is MOUNTED in CI (`abstractnote_available()` is true and the note routes are in the live
  route table), so a silently missing install cannot bring the old green back.
- The contract test's note rows are then exercised on every run; a new seam write route goes red in CI the day it
  lands.
- Document the rule in docs/security.md (route authorization) and in the seam module: a seam is covered by CI or
  it is not shipped.

## Done when
CI red on a branch that adds a note write route without a decision; green on main; the mounted-seam test fails
when `abstractnote` is removed from the CI install step.
