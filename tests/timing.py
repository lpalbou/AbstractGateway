"""Wall-clock budgets in the gateway tests: reported by default, enforced on request.

Operator rule (2026-10-27): a gateway test never fails because of a timeout or a slow machine.
A shared CI runner, or the operator's Mac busy with other work, can pause the whole process for
seconds; a latency budget that fails then says nothing about the code. So every test keeps its
STRUCTURAL assertion (the work ran off the event loop, the stream closed, nothing was dropped...)
unconditionally, and its wall-clock budget goes through `timing_budget`:

- by default it prints one `[timing] <what>: <actual> vs limit <limit>` line and never fails;
- with `ABSTRACTGATEWAY_TIMING_STRICT=1` in the environment it asserts the budget (a quiet
  machine measuring a latency change on purpose).

`patient(timeout_s)` is for a wait whose expiry fails the test (a `wait_until`, a `join`, an
`Event.wait` asserted true): it multiplies the bound by 5 unless strict, so a slow machine waits
longer instead of failing. Both read the variable at call time.
"""

from __future__ import annotations

import os
import sys

STRICT_ENV = "ABSTRACTGATEWAY_TIMING_STRICT"
PATIENCE = 5.0


def timing_strict() -> bool:
    return str(os.environ.get(STRICT_ENV) or "").strip() == "1"


def _fmt(value: float, unit: str) -> str:
    if unit == "ms":
        return f"{value:.1f} ms"
    if unit == "s":
        return f"{value * 1000:.1f} ms" if value < 1.0 else f"{value:.2f} s"
    return f"{value:g} {unit}"


def timing_budget(actual: float, limit: float, what: str, *, unit: str = "s", inclusive: bool = False,
                  at_least: bool = False) -> bool:
    """`actual < limit` (`<=` when `inclusive`; `actual >= limit` when `at_least`, for a count of
    things done within a time window): asserted under ABSTRACTGATEWAY_TIMING_STRICT=1, otherwise
    reported on stderr. Returns whether the budget held."""
    if at_least:
        ok = actual >= limit
    else:
        ok = actual <= limit if inclusive else actual < limit
    word = "at least" if at_least else "limit"
    line = f"[timing] {what}: {_fmt(actual, unit)} vs {word} {_fmt(limit, unit)}{'' if ok else ' (OVER)' if not at_least else ' (UNDER)'}"
    if timing_strict():
        assert ok, line
    print(line, file=sys.stderr, flush=True)
    return ok


def patient(timeout_s: float) -> float:
    """A wait bound that only a hang should reach: x5 unless ABSTRACTGATEWAY_TIMING_STRICT=1."""
    return float(timeout_s) if timing_strict() else float(timeout_s) * PATIENCE
