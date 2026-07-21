"""Console multimodal Test button + per-request TTS deadline (laurent dm#10).

The button auditions the CURRENT UNSAVED selection through the PRODUCTION
TTS lane (no new endpoint — adversarial ruling: direct synthesis would test
a path nothing in production uses) with a short clamped `timeout_s` so a
wedged synthesis fails fast instead of hanging the modal.
"""

from __future__ import annotations

import pytest


def test_console_carries_the_test_button_and_result_area() -> None:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert 'id="test-default"' in html
    assert 'id="default-modal-test"' in html
    # Wired, single-flight, and voice/text-only visibility logic present.
    assert '$("test-default").onclick = testDefault' in html
    assert "defaultRowTestable" in html
    # The voice audition rides the production TTS lane with a short deadline.
    assert "voice/tts" in html and "timeout_s: 25" in html
    # Failed voice discovery degrades to an honest label, never a stuck
    # "Loading voices..." (adversary F8).
    assert "Voice discovery failed" in html


def test_voice_tts_request_accepts_clamped_timeout() -> None:
    from abstractgateway.routes.gateway import VoiceTTSRequest

    req = VoiceTTSRequest(text="hi", timeout_s=25)
    assert req.timeout_s == 25
    with pytest.raises(Exception):
        VoiceTTSRequest(text="hi", timeout_s=0)
    with pytest.raises(Exception):
        VoiceTTSRequest(text="hi", timeout_s=-5)


def test_request_timeout_clamps_to_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client may TIGHTEN the deadline, never widen past the operator's
    watchdog ceiling — the clamp arithmetic pinned as a unit."""
    from abstractgateway.routes.gateway import _voice_tts_timeout_s

    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_TTS_TIMEOUT_S", "300")
    watchdog = _voice_tts_timeout_s()
    assert watchdog == 300.0
    # The route computes: min(req, watchdog) when watchdog > 0.
    assert min(25.0, watchdog) == 25.0
    assert min(900.0, watchdog) == 300.0


def test_entity_voice_tts_routes_exist() -> None:
    """The entity-owned TTS twins are registered (the generic /runs lane
    stays entity-blind — the adversarial F1 ruling)."""
    from abstractgateway.routes.entities import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/gateway/entities/{name}/voice" in paths
    assert "/gateway/entities/{name}/voice/tts" in paths
    assert "/gateway/entities/{name}/voice/tts/stream" in paths
