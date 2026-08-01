"""The image backend gets the env-kill contract voice already has (dm#177).

Image advertising resolved
`_env_first("ABSTRACTVISION_BACKEND", "ABSTRACTCORE_VISION_BACKEND", default="openai")`
and never looked at the operator's `output.image` capability default. Two
consequences, both the "they keep screwing up OUR GATEWAY DEFAULT" class:

  * a stale `ABSTRACTVISION_*` export (a foreign package's env namespace)
    outranked the setting the operator made in the console; and
  * with no env at all, the hardcoded ``"openai"`` WAS a second, gateway-side
    store of the image default -- exactly the duplicate the one-store ruling
    forbids. The store is AbstractCore's.

Same inversion as voice: the capability-defaults route WINS, the env chain is a
labeled #FALLBACK below it, and a shadowed env is logged once.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic


def _resolver() -> str:
    from abstractgateway.routes.gateway import _resolved_vision_backend

    return _resolved_vision_backend()


def test_configured_image_default_wins_over_foreign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "mlx-gen" if key == "output.image" else None,
    )
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "openai")  # the foreign override
    monkeypatch.delenv("ABSTRACTCORE_VISION_BACKEND", raising=False)

    assert _resolver() == "mlx-gen", "gateway config must win over the abstractvision env"


def test_env_fallback_when_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """No config = env is the honest #FALLBACK (no silent flip for a deployment
    that only sets env)."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_capability_route_provider", lambda key: None)
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "sdcpp")
    assert _resolver() == "sdcpp"


def test_hardcoded_openai_is_the_last_resort_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old duplicate default survives ONLY where nothing is configured."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(gw, "_configured_capability_route_provider", lambda key: None)
    monkeypatch.delenv("ABSTRACTVISION_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_VISION_BACKEND", raising=False)
    assert _resolver() == "openai"

    # ...and the moment the operator configures one, it no longer applies.
    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "huggingface" if key == "output.image" else None,
    )
    assert _resolver() == "huggingface"


def test_backend_is_normalized_the_same_way_from_either_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Downstream compares against dashed lowercase ids; config must not skip it."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "MLX_Gen" if key == "output.image" else None,
    )
    assert _resolver() == "mlx-gen"


def test_shadowed_env_is_logged_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A stale export that LOSES must be visible, not silent -- and ONCE.

    Adversary catch: the docstring said "logged once" while the call logged
    unconditionally, and `_resolved_vision_backend` sits under
    `_generated_media_contract`, which the console POLLS. "Logged once" that is
    really "logged at poll frequency" is a warning nobody will ever read again.
    Same for `_resolved_voice_engine` on the voice catalog routes.
    """
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "mlx-gen" if key == "output.image" else None,
    )
    monkeypatch.setattr(gw, "_ENV_SHADOWED_BY_CONFIG_WARNED", set())
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "openai")
    with caplog.at_level("WARNING", logger="abstractgateway.vision"):
        for _ in range(5):
            assert _resolver() == "mlx-gen"
    hits = [rec for rec in caplog.records if "#FALLBACK image backend" in rec.getMessage()]
    assert len(hits) == 1, f"expected exactly one warning across 5 calls, got {len(hits)}"


def test_shadowed_voice_env_is_also_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The voice twin of the same claim, which had the same defect."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "supertonic" if key == "output.voice" else None,
    )
    monkeypatch.setattr(gw, "_ENV_SHADOWED_BY_CONFIG_WARNED", set())
    monkeypatch.setenv("ABSTRACTVOICE_TTS_ENGINE", "openai")
    monkeypatch.delenv("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", raising=False)
    with caplog.at_level("WARNING", logger="abstractgateway.voice"):
        for _ in range(5):
            assert gw._resolved_voice_engine("tts") == "supertonic"
    hits = [rec for rec in caplog.records if "#FALLBACK voice" in rec.getMessage()]
    assert len(hits) == 1, f"expected exactly one warning across 5 calls, got {len(hits)}"


def test_direct_image_configured_follows_the_configured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capability probe must agree with the resolved backend.

    `diffusers`/`huggingface` needs no extra env to be usable, so configuring it
    makes direct image generation available even with no ABSTRACTVISION_* export
    at all -- previously impossible, because the resolver never saw the config.
    """
    import abstractgateway.routes.gateway as gw

    for name in (
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTVISION_BACKEND",
        "ABSTRACTCORE_VISION_BACKEND",
        "ABSTRACTVISION_BASE_URL",
        "OPENAI_BASE_URL",
        "ABSTRACTVISION_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "huggingface" if key == "output.image" else None,
    )
    assert gw._gateway_direct_image_configured() is True


def test_configured_route_provider_reads_the_core_store_not_a_gateway_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway keeps ZERO local storage: the read goes to the core payload."""
    import abstractgateway.core_config as cd
    import abstractgateway.routes.gateway as gw

    calls: list[str] = []

    def fake_payload(**_kwargs):
        calls.append("read")
        return {
            "ok": True,
            "routes": [
                {"key": "output.image", "provider": "mlx-gen", "model": "flux", "configured": True},
                {"key": "output.voice", "provider": "abstractvoice", "configured": True},
                {"key": "output.music", "source": "not_configured", "configured": False},
            ],
        }

    monkeypatch.setattr(cd, "gateway_capability_defaults_payload", fake_payload)

    assert gw._configured_capability_route_provider("output.image") == "mlx-gen"
    assert gw._configured_capability_route_provider("output.voice") == "abstractvoice"
    assert gw._configured_capability_route_provider("output.music") is None
    assert gw._configured_capability_route_provider("output.video") is None
    assert calls, "the provider must come from the core capability-defaults payload"


def test_voice_engine_still_resolves_through_the_shared_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice and image now share ONE reader -- no per-modality copy."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: {"output.voice": "supertonic", "input.voice": "whisper"}.get(key),
    )
    assert gw._configured_voice_engine("tts") == "supertonic"
    assert gw._configured_voice_engine("stt") == "whisper"


# ---------------------------------------------------------------------------
# THE ROUTE HIERARCHY (operator question 2026-08-01)
# ---------------------------------------------------------------------------
#
# `output.image` is the PARENT of `output.image.*`, not the only key. Reading
# the parent alone inverted the hierarchy, and it is the shape the console
# produces: the Multimodal tab writes TASK rows, so a fully configured host
# routinely has all three `output.image.*` rows set and `output.image` empty.
# Reproduced live on the operator's gateway: `_resolved_vision_backend()`
# returned the hardcoded `openai` for a host executing mlx-gen.


def test_task_row_alone_is_enough_to_configure_the_image_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gw

    configured = {
        "output.image": None,  # the parent the console cannot write from its grid
        "output.image.text_to_image": "mlx-gen",
        "output.image.image_to_image": "mlx-gen",
        "output.image.image_upscale": "mlx-gen",
    }
    monkeypatch.setattr(gw, "_configured_capability_route_provider", configured.get)
    monkeypatch.delenv("ABSTRACTVISION_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_VISION_BACKEND", raising=False)

    assert _resolver() == "mlx-gen", (
        "advertising must resolve the way execution does — task row first, "
        "parent second — or the gateway advertises a backend it will not run"
    )


def test_parent_row_still_answers_when_no_task_row_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh-install seed writes the PARENT and nothing else."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: "mlx-gen" if key == "output.image" else None,
    )
    monkeypatch.delenv("ABSTRACTVISION_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_VISION_BACKEND", raising=False)
    assert _resolver() == "mlx-gen"


def test_broad_only_modalities_resolve_at_their_own_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """voice/sound/music have no task rows — the cell IS their primary key,
    which is why the modality row shape can never be deleted."""
    import abstractgateway.routes.gateway as gw

    monkeypatch.setattr(
        gw,
        "_configured_capability_route_provider",
        lambda key: {"output.voice": "supertonic", "output.music": "musicgen"}.get(key),
    )
    assert gw._configured_modality_route_provider("voice") == "supertonic"
    assert gw._configured_modality_route_provider("music") == "musicgen"
    assert gw._configured_modality_route_provider("sound") is None
