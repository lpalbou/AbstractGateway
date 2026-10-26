"""Login sessions in memory, the file as the source of truth (gateway load 2026-10-26).

The auth middleware (`GatewaySecurityMiddleware._authenticate_session_cookie/_header` ->
`GatewaySessionStore.authenticate_session`) read the whole sessions file, rewrote it to bump
`last_seen_at`, and read the users file and the session secret on EVERY browser request, on
the event loop. Now: memory within SESSION_CACHE_TTL_S, own writes replace the memory at
once, other writers are seen past the TTL, `last_seen_at` is written at most once a minute.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


class _Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = datetime.datetime(2026, 10, 26, 3, 0, 0, tzinfo=datetime.timezone.utc)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += datetime.timedelta(seconds=seconds)


@pytest.fixture()
def store(tmp_path: Path, monkeypatch):
    import abstractgateway.security.sessions as sm

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "auth" / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "auth" / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSION_SECRET", "test-secret")
    clock = _Clock()
    monkeypatch.setattr(sm, "_monotonic", lambda: clock.mono)
    monkeypatch.setattr(sm, "_now", lambda: clock.wall)
    reads: list[Path] = []
    real_read = sm._read_session_file
    monkeypatch.setattr(sm, "_read_session_file", lambda p: (reads.append(p), real_read(p))[1])
    writes: list[str] = []
    monkeypatch.setattr(sm, "_submit_session_write", lambda fn, *a: (writes.append(a[0].session_id), fn(*a)))
    sm._SESSION_CACHE.clear()
    sm._REGISTRY_CACHE.clear()
    yield sm, clock, reads, writes
    sm._SESSION_CACHE.clear()
    sm._REGISTRY_CACHE.clear()


def _legacy_session(sm, token: str = "tok"):
    from abstractgateway.security.principal import GatewayPrincipal

    principal = GatewayPrincipal(
        user_id="admin", tenant_id="default", roles=("admin",), token_fingerprint=sm.token_fingerprint(token), source="legacy-token"
    )
    cookie, _csrf, record = sm.GatewaySessionStore().create_session(principal)
    return record.session_id, sm.legacy_token_fingerprints((token,))


def test_1000_requests_read_the_sessions_file_at_most_once_per_ttl(store) -> None:
    sm, clock, reads, writes = store
    sid, fps = _legacy_session(sm)
    sm._SESSION_CACHE.clear()  # a restarted process: nothing in memory yet
    reads.clear()
    # 1,000 requests 10 ms apart (10 s: five TTLs), from fresh store objects as the middleware makes them.
    for _ in range(1000):
        assert sm.GatewaySessionStore().authenticate_session(sid, legacy_token_fingerprints=fps) is not None
        clock.advance(0.01)
    span = 1000 * 0.01
    assert len(reads) <= span / sm.SESSION_CACHE_TTL_S + 1, f"{len(reads)} reads of the sessions file"
    assert len(reads) == 1  # the file never changed: the first read only, then stats past each TTL
    assert writes == []  # last_seen_at was set at creation: no touch within the minute


def test_a_changed_file_is_read_once_per_ttl_not_once_per_request(store) -> None:
    sm, clock, reads, _writes = store
    sid, fps = _legacy_session(sm)
    path = sm.gateway_session_store_path_from_env()
    reads.clear()
    for i in range(1000):
        if i % 50 == 0:  # another process rewrites the file every 0.5 s
            data = json.loads(path.read_text(encoding="utf-8"))
            data["updated_at"] = f"x{i}"
            path.write_text(json.dumps(data), encoding="utf-8")
        assert sm.GatewaySessionStore().authenticate_session(sid, legacy_token_fingerprints=fps) is not None
        clock.advance(0.01)
    assert len(reads) <= 1000 * 0.01 / sm.SESSION_CACHE_TTL_S + 1, f"{len(reads)} reads"


def test_own_writes_apply_at_once_and_other_writers_after_the_ttl(store) -> None:
    sm, clock, _reads, _writes = store
    sid, fps = _legacy_session(sm)
    auth = lambda: sm.GatewaySessionStore().authenticate_session(sid, legacy_token_fingerprints=fps)  # noqa: E731
    assert auth() is not None  # created a moment ago: in memory, no read needed
    # Another process expires it in the file: seen once the memory is older than the TTL.
    path = sm.gateway_session_store_path_from_env()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sessions"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    clock.advance(sm.SESSION_CACHE_TTL_S + 0.1)
    assert auth() is None
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"] == []  # dropped from the file too
    # This process's logout is immediate, inside the TTL.
    sid2, fps2 = _legacy_session(sm)
    assert sm.GatewaySessionStore().authenticate_session(sid2, legacy_token_fingerprints=fps2) is not None
    assert sm.GatewaySessionStore().delete_session(sid2) is True
    assert sm.GatewaySessionStore().authenticate_session(sid2, legacy_token_fingerprints=fps2) is None
    # A revoked legacy token refuses at once.
    sid3, _ = _legacy_session(sm, token="old")
    assert sm.GatewaySessionStore().authenticate_session(sid3, legacy_token_fingerprints=sm.legacy_token_fingerprints(("new",))) is None


def test_last_seen_is_written_at_most_once_a_minute_off_the_caller(store) -> None:
    sm, clock, _reads, writes = store
    sid, fps = _legacy_session(sm)
    path = sm.gateway_session_store_path_from_env()
    for _ in range(300):  # 300 requests over 3 minutes
        assert sm.GatewaySessionStore().authenticate_session(sid, legacy_token_fingerprints=fps) is not None
        clock.advance(0.6)
    assert 2 <= len(writes) <= 3, writes
    row = json.loads(path.read_text(encoding="utf-8"))["sessions"][0]
    assert sm._parse_dt(row["last_seen_at"]) >= clock.wall - datetime.timedelta(seconds=sm.SESSION_TOUCH_INTERVAL_S + 1)


def test_a_disabled_account_is_refused_at_once(store) -> None:
    sm, _clock, _reads, _writes = store
    from abstractgateway.users import GatewayUserRegistry

    registry = GatewayUserRegistry()
    rec, _token = registry.create_user(user_id="alice", roles=["user"])
    sm.GatewaySessionStore()  # same file paths as the middleware
    from unittest import mock

    with mock.patch.object(sm, "principal_barred_from_shared_runtime", lambda p: False):
        _cookie, _csrf, srec = sm.GatewaySessionStore().create_session(rec.to_principal())
        assert sm.GatewaySessionStore().authenticate_session(srec.session_id) is not None
        registry.update_user(user_id="alice", enabled=False)
        assert sm.GatewaySessionStore().authenticate_session(srec.session_id) is None
