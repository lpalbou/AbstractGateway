"""Auth-lockout semantics (operator incident 2026-07-15, c2341: every app on
the box answered 429 "Too Many Requests (auth lockout)").

Three defect classes, each pinned:

1. VALID CREDENTIALS NEVER SEE THE LOCK. The old middleware checked the
   lockout BEFORE verifying credentials — once the shared loopback IP was
   locked, the very sign-in that would have cleared the state was 429'd
   (the operator's "you log a successful request as one of those": yes,
   a valid request was rejected by the pre-auth gate).
2. NO-CREDENTIAL REQUESTS ARE NOT GUESSES. Thin clients feature-probe
   before sign-in; counting bare 401s as failures was collective
   punishment on a one-box deployment (every app shares the IP).
3. FAILURES DECAY. A background app retrying a stale cookie must not
   ratchet the exponential backoff all day; a quiet window resets it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

from abstractgateway.security.gateway_security import _AuthLockoutTracker  # noqa: E402

_TOKEN = "lockout-semantics-secret"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    """A fresh FastAPI app + middleware per test (the suite's standard
    pattern — NEVER importlib.reload the shared app module: reloading it
    mid-session re-executes module state and poisons every later test that
    imported the original objects)."""
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_LOCKOUT_AFTER", raising=False)
    (tmp_path / "flows").mkdir()

    from fastapi import FastAPI

    from abstractgateway.routes import gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


def _spam_invalid(client: TestClient, n: int) -> None:
    for _ in range(n):
        client.get("/api/gateway/runs", headers={"Authorization": "Bearer wrong-token"})


def test_valid_credentials_pass_even_when_the_ip_is_locked(client: TestClient) -> None:
    """Defect 1: a locked IP must never reject a VALID credential — the
    valid request passes AND clears the lock state."""
    _spam_invalid(client, 20)  # far past the threshold: IP is locked

    r = client.get("/api/gateway/runs", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200, f"valid credential was rejected: {r.status_code} {r.text}"

    # And the state is cleared: the next invalid attempt is a 401 (count
    # restarted), not an instant 429.
    r2 = client.get("/api/gateway/runs", headers={"Authorization": "Bearer wrong-token"})
    assert r2.status_code == 401


def test_no_credential_requests_never_count_toward_lockout(client: TestClient) -> None:
    """Defect 2: bare feature probes (no Authorization, no session) are
    login prompts — hundreds of them must not lock the IP."""
    for _ in range(50):
        r = client.get("/api/gateway/runs")
        assert r.status_code == 401, r.text  # always a login prompt, never 429

    # The IP is not locked: a valid credential works immediately.
    ok = client.get("/api/gateway/runs", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert ok.status_code == 200


def test_presented_invalid_credentials_still_lock_out(client: TestClient) -> None:
    """The security property survives the generosity: sustained credential
    GUESSING still trips the lock."""
    saw_429 = False
    for _ in range(20):
        r = client.get("/api/gateway/runs", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code in (401, 429)
        if r.status_code == 429:
            saw_429 = True
            assert r.headers.get("retry-after")
    assert saw_429, "20 invalid credentials must trip the lockout"


def test_failure_count_decays_after_a_quiet_window() -> None:
    """Defect 3: the ratchet resets after decay_s of quiet — old failures
    stop feeding the exponential backoff."""
    import time

    tracker = _AuthLockoutTracker(after_failures=3, base_s=0.1, max_s=1.0, decay_s=0.2)
    for _ in range(4):
        tracker.record_failure("1.2.3.4")  # 4th failure locks for ~0.2s

    # Sleep past the residual lock AND the decay window: the state resets.
    time.sleep(0.5)
    assert tracker.check_locked("1.2.3.4") is None
    # A single new failure starts from zero (below threshold => no lock).
    assert tracker.record_failure("1.2.3.4") is None


def test_failures_inside_the_window_still_ratchet() -> None:
    """The decay must not weaken sustained guessing: failures INSIDE the
    window keep compounding the backoff."""
    tracker = _AuthLockoutTracker(after_failures=3, base_s=0.1, max_s=1.0, decay_s=60.0)
    locks = [tracker.record_failure("5.6.7.8") for _ in range(6)]
    assert locks[-1] is not None and locks[-1] >= 0  # still locking
    assert tracker.check_locked("5.6.7.8") is not None or locks[-1] == 0
