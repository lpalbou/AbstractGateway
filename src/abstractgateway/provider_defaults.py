"""Gateway provider/model default resolution.

THE CASCADE CONTRACT (one place, ordered; highest wins):

  1. EXPLICIT REQUEST PINS -- provider/model on the request itself: a run's
     `input_data._runtime.provider|model`, a flow node's own provider/model, or
     the `provider`/`model` arguments of a Gateway LLM helper route. Resolved
     per call. A default NEVER overwrites one.
  2. FLOW DEFAULTS -- provider/model declared by the workflow where it declares
     them. These reach execution as part of the effect payload, so they are
     already pins by the time a call is made.
  3. GATEWAY CONSOLE DEFAULT -- the execution-host capability default the
     operator set in the console. Applies whenever 1 and 2 said nothing.
  4. FLOW-SCANNED BOOTSTRAP -- a provider/model scavenged from any LLM node of
     the loaded bundles. LAST resort, only when no console default exists at
     all: it exists so a fresh install with no configuration can still load and
     run a bundle. It is deliberately BELOW the console default -- above it, a
     stale or unrelated bundle could hijack every Auto node in the host.

Note the asymmetry between 2 and 4, which reads like an inversion but is not:
a flow default that an author WROTE travels with the call (tier 1/2); tier 4 is
a guess made ABOUT flows by scanning them, and a guess must never outrank the
operator's own setting.

WHERE THE CONSOLE DEFAULT LIVES. AbstractCore stores exactly one text
provider/model, under the capability route `input.text`, and exposes
`output.text` as a derived read-only view of it (see
`abstractcore/config/manager.py::set_capability_default`, which canonicalizes a
written `output.text` to `input.text`, and `get_capability_default`, which
derives the `output.text` row back out). So:

  - `output.text` is the CANONICAL READ for the text-GENERATION default -- it
    is the route the spec catalog labels `text_generation`, and it is what this
    module reads first, BY NAME.
  - `input.text` is the storage key and the migration fallback: a config that
    only carries the legacy route still resolves.

Reading these by name replaced a `for kind in ("output", "input")` scan whose
correctness depended on iteration order rather than on saying what it wanted.

THE SAME CASCADE, PER MODALITY. Text is not special; it is just the tier-4 case.
Every modality runs tiers 1-3 identically, and ONLY text has a tier 4:

  1. EXPLICIT PINS -- a flow's `image_provider`/`image_model`,
     `video_provider`, `tts_provider`, `stt_provider`, `music_provider` (and
     their camelCase/`provider_*` aliases). A media node that names a provider
     is NEVER clobbered by a default: the merge in AbstractRuntime
     (`integrations/abstractcore/llm_client.py::_with_capability_default_route`)
     returns the spec untouched the moment provider, model or base_url is set.
     "Auto"/absent is what makes a node eligible for a default.
  2. FLOW DEFAULTS -- as for text: already pins by the time the effect executes.
  3. CONSOLE / CORE DEFAULT PER MODALITY -- the capability default route for
     that modality, resolved through THE ONE TABLE
     (`abstractcore/config/capability_defaults.py::_OUTPUT_ROUTE_TABLE`), which
     maps the generation-task vocabulary to a route key and gives each one a
     broad modality fallback:
         image_generation / text_to_image  -> output.image.text_to_image  -> output.image
         image_edit / image_to_image       -> output.image.image_to_image -> output.image
         image_upscale                     -> output.image.image_upscale  -> output.image
         text_to_video                     -> output.video.text_to_video  -> output.video
         image_to_video                    -> output.video.image_to_video -> output.video
         tts                               -> output.voice
         stt / transcription               -> input.voice
         music_generation                  -> output.music
         sound_generation                  -> output.sound
         text_to_scene3d / image_to_scene3d-> output.scene3d.*            -> output.scene3d
         text_generation                   -> output.text (derived from input.text)
     A bare request with a SOURCE IMAGE attached resolves to the edit/i2v/i23d
     variant rather than the text-to-X one -- that is what the caller meant.
  4. FLOW-SCANNED BOOTSTRAP -- TEXT ONLY. There is deliberately no media
     equivalent: scavenging an image model out of an unrelated bundle is a
     guess with no fallback story, and an unconfigured media modality should
     say so rather than route somewhere arbitrary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

# The canonical route key for "the execution host's default text-generation
# provider/model", then the storage key read as a migration fallback. Both are
# named once, on the AbstractCore config seam, and re-exported here under the
# names this module's error messages use.
from .core_config import (
    TEXT_ROUTE_KEY as TEXT_GENERATION_ROUTE_KEY,
    TEXT_ROUTE_KEYS as TEXT_DEFAULT_ROUTE_KEYS,
    TEXT_ROUTE_STORAGE_KEY as TEXT_GENERATION_FALLBACK_ROUTE_KEY,
)


class ProviderModelConfigError(ValueError):
    """Raised when an LLM helper cannot resolve a provider/model pair."""


@dataclass(frozen=True)
class ProviderModelResolution:
    provider: Optional[str]
    model: Optional[str]
    source: Optional[str]
    error: Optional[str] = None

    def require(self) -> tuple[str, str]:
        if self.provider and self.model:
            return self.provider, self.model
        raise ProviderModelConfigError(self.error or provider_model_config_error())


def _clean_provider(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text or None


def _clean_model(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _gateway_capability_text_default(
    *, base_dir: Optional[Path] = None
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Read the console's text-generation default BY NAME.

    The read itself lives on the AbstractCore config seam
    (`core_config.text_default`), which resolves the canonical key
    (`output.text`) first and the storage key (`input.text`) second and names
    which one answered. See this module's docstring for why both keys exist.
    """
    try:
        from .core_config import text_default

        row = text_default(base_dir=base_dir)
        provider = _clean_provider(row.get("provider"))
        model = _clean_model(row.get("model"))
        if provider and model:
            return provider, model, str(row.get("source") or "abstractcore_config")
    except Exception:
        pass
    return None, None, None


def provider_model_config_error(*, purpose: str = "LLM helper") -> str:
    """THE ERROR IS THE UX. A first-run operator meets this before any document,
    so it names the exact command for BOTH entry points rather than pointing at
    a console screen they may not be looking at."""
    return (
        f"No provider/model is configured for {purpose}. Provide provider and model in the request, "
        "supply workflow defaults, or set the execution-host text default "
        f"(capability route {TEXT_GENERATION_ROUTE_KEY}; stored as {TEXT_GENERATION_FALLBACK_ROUTE_KEY}) "
        "with either entry point: "
        f"`abstractcore config set-default {TEXT_GENERATION_ROUTE_KEY} --provider <provider> --model <model>`, "
        "or `PUT /api/gateway/config/capability-defaults/output/text "
        '{"provider": "<provider>", "model": "<model>"}` (console: Capability defaults). '
        "Inspect what is set with `abstractcore config defaults` or "
        "`GET /api/gateway/config/capability-defaults`."
    )


def resolve_gateway_provider_model(
    *,
    provider: Any = None,
    model: Any = None,
    flow_defaults: Optional[Tuple[str, str]] = None,
    base_dir: Optional[Path] = None,
    purpose: str = "LLM helper",
) -> ProviderModelResolution:
    """Resolve the provider/model cascade used by Gateway LLM helper paths.

    Implements tiers 1, 3 and 4 of THE CASCADE CONTRACT in this module's
    docstring. Tier 2 (flow defaults) never reaches here: an authored flow
    default travels on the effect payload and is already a pin by call time.
    `flow_defaults` here is tier 4 -- the flow-SCANNED bootstrap guess.
    """

    provider_s = _clean_provider(provider)
    model_s = _clean_model(model)
    source: Optional[str] = "request" if provider_s or model_s else None

    if provider_s or model_s:
        if provider_s and model_s:
            return ProviderModelResolution(provider=provider_s, model=model_s, source=source)
        return ProviderModelResolution(
            provider=provider_s,
            model=model_s,
            source=source,
            error=provider_model_config_error(purpose=purpose),
        )

    # Tier 3: the operator's console default. Read by name -- canonical
    # `output.text`, then legacy `input.text`.
    if not provider_s or not model_s:
        cfg_provider, cfg_model, cfg_source = _gateway_capability_text_default(base_dir=base_dir)
        cfg_provider = _clean_provider(cfg_provider)
        cfg_model = _clean_model(cfg_model)
        if cfg_provider and cfg_model:
            provider_s = cfg_provider
            model_s = cfg_model
            source = cfg_source or "abstractcore_config"

    # Tier 4: flow-scanned bootstrap. Strictly BELOW the console default --
    # above it, a stale or unrelated bundle could hijack every Auto node.
    if (not provider_s or not model_s) and flow_defaults:
        flow_provider = _clean_provider(flow_defaults[0])
        flow_model = _clean_model(flow_defaults[1])
        if flow_provider and flow_model:
            provider_s, model_s = flow_provider, flow_model
            source = "flow_defaults"

    if provider_s and model_s:
        return ProviderModelResolution(provider=provider_s, model=model_s, source=source or "resolved")
    return ProviderModelResolution(
        provider=provider_s,
        model=model_s,
        source=source,
        error=provider_model_config_error(purpose=purpose),
    )
