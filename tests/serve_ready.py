"""Readiness of a real `abstractgateway serve` started by a test: it ANSWERS HTTP.

A boot log line, an audit record or a file in the data folder is written before uvicorn binds
the port, so a test that connects right after seeing one races the bind (`Connection refused`
on a busy CI runner: `test_a_real_serve_audits_the_flag_at_boot[True]`, main 264ec73, 3.10).
Every test that starts a real server waits here instead: `GET /api/health` must answer 200
(and `ready(body)` hold, when given) before the test talks to it.

The bound is a hang guard (`patient`), never a latency assertion; an exited server fails at
once with the tail of its log.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from timing import patient  # tests/timing.py (wall-clock budgets)

# Straight to loopback: a proxy from the environment must never relay a test's probe.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log_tail(log_path: Optional[Path]) -> str:
    if log_path is None:
        return ""
    try:
        return "\n" + Path(log_path).read_text(errors="replace")[-3000:]
    except OSError:
        return ""


def wait_until_answers(
    base_url: str,
    *,
    proc: Any = None,
    log_path: Optional[Path] = None,
    timeout_s: float = 120.0,
    path: str = "/api/health",
    ready: Optional[Callable[[Dict[str, Any]], bool]] = None,
    poll_s: float = 0.2,
) -> Dict[str, Any]:
    """Block until `GET base_url + path` answers 200 (and `ready(body)` holds); return the body.

    Fails (AssertionError, with the log tail) when `proc` exits first or when `patient(timeout_s)`
    passes without an answer."""
    url = base_url.rstrip("/") + path
    deadline = time.monotonic() + patient(timeout_s)
    last: str = "no answer yet"
    while True:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"the gateway exited (code {proc.returncode}) before answering {url}" + _log_tail(log_path))
        try:
            with _OPENER.open(url, timeout=5) as response:
                raw = response.read()
                if response.status == 200:
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        body = {}
                    body = body if isinstance(body, dict) else {}
                    if ready is None or ready(body):
                        return body
                    last = f"answered, not ready yet: {str(body)[:200]}"
                else:
                    last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last = repr(exc)
        if time.monotonic() >= deadline:
            raise AssertionError(f"{url} did not answer within {patient(timeout_s):.0f} s ({last})" + _log_tail(log_path))
        time.sleep(poll_s)
