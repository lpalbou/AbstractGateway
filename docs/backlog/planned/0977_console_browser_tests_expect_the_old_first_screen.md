# 0977 — The console's four browser tests fail on main: a fresh gateway now opens on the setup screen

**Status**: planned · **Priority**: P2 (CI truth) · **Created**: 2026-10-29 (worker A, during the top-bar set; filed by the merge desk)
> Root link: AbstractFramework recurrent 1060 (security, resilience and latency standing duty — CI that lies is a resilience defect).

## Why
Since the first-run onboarding (1069: the admin password + wizard, gateway #35 and its siblings) a fresh gateway opens on
the SETUP screen, not on the console's old first screen. The console's four browser tests (headless Chromium against a
fresh gateway) still expect the old first screen, so they fail on main regardless of any PR — worker A saw all four red
on main while landing #71 and left them untouched there (they are not #71's to fix). A test that is red on main teaches
every reader to ignore red.

## What
- The browser tests drive a fresh gateway through its first-run setup (or start it already claimed, the way the other
  console tests build their fixture) before asserting the console, so they are green on main again.
- One of them asserts the setup screen itself on a fresh gateway (the new truth), so the onboarding cannot regress
  silently.
- The console browser lane is wired into the sharded CI like the other suites (it must be red when the console breaks;
  today it is red for the wrong reason and nobody looks).

## Done when
`pytest tests/<console browser tests>` is green on main; one test asserts the setup screen on a fresh gateway; the
lane runs in CI; the four tests go red when the console's first screen regresses (mutation-checked).
