"""Named env-kill incident (operator dm#177): the gateway TTS/STT engine.

`ABSTRACTVOICE_TTS_ENGINE` (a foreign package's env namespace) used to
silently override the gateway-configured voice engine — the exact "they keep
screwing up OUR GATEWAY DEFAULT" incident. The fix inverts precedence: the
console-editable capability-defaults route (output.voice / input.voice
provider) WINS; the env chain is a labeled #FALLBACK below it. Because the
capability route is the SAME config runtime/core execute from, advertising
now aligns with execution (no split-brain divergence).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic


def _resolver():
    from abstractgateway.routes.gateway import _resolved_voice_engine

    return _resolved_voice_engine


def test_configured_engine_wins_over_foreign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident: a configured gateway engine beats an exported
    ABSTRACTVOICE_TTS_ENGINE."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_voice_engine", lambda kind: "supertonic" if kind == "tts" else None)
    monkeypatch.setenv("ABSTRACTVOICE_TTS_ENGINE", "openai")  # the foreign override
    monkeypatch.delenv("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", raising=False)

    assert _resolver()("tts") == "supertonic", "gateway config must win over the abstractvoice env"


def test_env_fallback_when_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """No config = env is the honest #FALLBACK (nothing breaks for a
    deployment that only sets env — the no-silent-flip invariant)."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_voice_engine", lambda kind: None)
    monkeypatch.delenv("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", raising=False)
    monkeypatch.setenv("ABSTRACTVOICE_TTS_ENGINE", "piper")

    assert _resolver()("tts") == "piper"


def test_gateway_env_still_beats_foreign_env_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Within the fallback chain, the gateway-namespaced var still precedes
    the foreign one (unchanged ordering)."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_voice_engine", lambda kind: None)
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", "gatewaypick")
    monkeypatch.setenv("ABSTRACTVOICE_TTS_ENGINE", "foreignpick")

    assert _resolver()("tts") == "gatewaypick"


def test_none_when_neither_config_nor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_voice_engine", lambda kind: None)
    monkeypatch.delenv("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", raising=False)
    monkeypatch.delenv("ABSTRACTVOICE_TTS_ENGINE", raising=False)

    assert _resolver()("tts") is None


def test_shadowed_env_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A set env that LOSES to config is logged once (#FALLBACK) so a stale
    export is visible — B3 of the compat contract."""
    import logging

    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_voice_engine", lambda kind: "supertonic")
    # "Once" is enforced by a process-global set (adversary fix: the warning
    # used to fire on EVERY call, and both resolvers sit on console-POLLED
    # routes). Start this test from an empty one so it is order-independent.
    monkeypatch.setattr(gw, "_ENV_SHADOWED_BY_CONFIG_WARNED", set())
    monkeypatch.setenv("ABSTRACTVOICE_TTS_ENGINE", "openai")
    with caplog.at_level(logging.WARNING, logger="abstractgateway.voice"):
        for _ in range(3):
            assert _resolver()("tts") == "supertonic"
    hits = [r for r in caplog.records if "shadowed env" in r.getMessage().lower() or "#fallback" in r.getMessage().lower()]
    assert len(hits) == 1, f"expected exactly one #FALLBACK warning across 3 calls, got {len(hits)}"


def test_configured_engine_reads_the_right_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """_configured_voice_engine parses the output.voice (tts) / input.voice
    (stt) route provider, only when the route is `configured`."""
    import abstractgateway.routes.gateway as gw

    payload = {
        "routes": [
            {"key": "output.voice", "provider": "supertonic", "configured": True},
            {"key": "input.voice", "provider": "whisper", "configured": False},
        ]
    }
    monkeypatch.setattr(
        "abstractgateway.core_config.gateway_capability_defaults_payload",
        lambda *a, **k: payload,
    )
    assert gw._configured_voice_engine("tts") == "supertonic"
    assert gw._configured_voice_engine("stt") is None  # not configured → None


def test_configured_engine_never_raises_on_bad_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gw

    def _boom(*a, **k):
        raise RuntimeError("core server unreachable")

    monkeypatch.setattr("abstractgateway.core_config.gateway_capability_defaults_payload", _boom)
    assert gw._configured_voice_engine("tts") is None  # degrades, never breaks discovery
