"""While the gateway runs, the Mac does not idle-sleep (keep_awake.py, operator ruling 2026-11-02).

Pins: `serve` takes the assertion before it serves and gives it back when it stops (also on
Ctrl-C); off macOS it is a no-op; the IOKit calls work on a real Mac (darwin only, released at once).
"""

from __future__ import annotations

import sys
from typing import Any, List

import pytest

from abstractgateway import keep_awake

pytestmark = pytest.mark.basic


def test_off_macos_it_is_a_no_op(capsys: pytest.CaptureFixture[str]) -> None:
    assert keep_awake.hold(platform="linux") is None
    assert "not available on linux" in capsys.readouterr().err
    keep_awake.release(None)  # nothing to give back


@pytest.mark.parametrize("behaviour", [None, "keyboard-interrupt"])
def test_serve_holds_while_it_serves_and_releases_when_it_stops(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> None:
    from abstractgateway import host_control, tray_supervisor
    from tests import test_gateway_cli_serve_host_controls as harness

    host_control._reset_for_tests()  # what the harness module's autouse fixture does
    harness._FakeServer.instances.clear()
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "operator-token-for-tests")
    monkeypatch.delenv("ABSTRACTGATEWAY_RUNNER", raising=False)
    events: List[str] = []

    def fake_hold() -> int:
        events.append("hold")
        return 7

    monkeypatch.setattr(keep_awake, "hold", fake_hold)
    monkeypatch.setattr(keep_awake, "release", lambda aid: events.append(f"release:{aid}"))
    run = harness._FakeServer.run

    def run_and_record(self: Any) -> None:
        events.append("serving")
        run(self)

    monkeypatch.setattr(harness._FakeServer, "run", run_and_record)
    try:
        harness._serve(monkeypatch, behaviour=behaviour)
    finally:
        host_control._reset_for_tests()
        tray_supervisor._serve_context.clear()
    assert events == ["hold", "serving", "release:7"]


@pytest.mark.skipif(sys.platform != "darwin", reason="IOKit power assertions are macOS only")
def test_iokit_assertion_acquires_and_releases_on_a_real_mac() -> None:
    aid = keep_awake.hold()
    try:
        assert isinstance(aid, int) and aid > 0
    finally:
        keep_awake.release(aid)
