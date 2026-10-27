"""Endpoint profiles as image providers in the Vision catalog.

WHY THIS EXISTS (operator report 2026-10-18). An OpenAI-compatible endpoint
connection (`endpoint:<id>`, e.g. a local relay that serves `gpt-image-2`)
answered text-to-image and image edits when called directly, but neither
console offered it for the image routes. `GET /vision/provider_models` was
built ONLY from AbstractVision's own backends -- which know one global
`vision_base_url` and nothing about the gateway's endpoint profiles -- so an
endpoint profile could never be an image candidate, whatever its models were.

This module decides which enabled endpoint profiles are image candidates for
a Vision task, and with which models, so the route can add them next to the
AbstractVision rows. A profile qualifies when:

- its provider family speaks the OpenAI image API (`openai`,
  `openai-compatible`: the families AbstractVision's HTTP backend drives), and
- it either DECLARES the task in its `capabilities` (an operator opt-in for
  servers whose model ids carry no family signal), or its model list holds a
  model of a KNOWN OpenAI image family (`gpt-image-*`, `dall-e-*`).

Model ids are never guessed beyond those families: an OpenAI-compatible
catalog has no universal capability schema, and offering every chat model of
a relay as an "image model" would make the picker useless.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ENDPOINT_IMAGE_PROVIDER_FAMILIES = frozenset({"openai", "openai-compatible"})
ENDPOINT_IMAGE_TASKS: Tuple[str, ...] = ("text_to_image", "image_to_image")

# Capability tokens an operator may put on a profile to declare image support
# (compared after lower-casing and mapping '-' to '_').
_DECLARED_TASK_TOKENS = {
    "text_to_image": frozenset(
        {"image", "images", "output.image", "output.image.text_to_image", "text_to_image", "t2i", "image_generation"}
    ),
    "image_to_image": frozenset(
        {"image", "images", "output.image", "output.image.image_to_image", "image_to_image", "i2i", "image_edit"}
    ),
}


def _norm_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def known_image_model_tasks(model_id: Any) -> frozenset:
    """The Vision tasks a model id of a KNOWN OpenAI image family serves.

    `gpt-image-*` serves generations and edits; `dall-e-2` both; other
    `dall-e-*` (dall-e-3) generations only. Anything else: no evidence, empty.
    A routing prefix (`openai/gpt-image-1`) is ignored.
    """
    mid = str(model_id or "").strip().lower().rsplit("/", 1)[-1]
    if mid.startswith(("gpt-image", "chatgpt-image")):
        return frozenset(ENDPOINT_IMAGE_TASKS)
    if mid.startswith("dall-e-2"):
        return frozenset(ENDPOINT_IMAGE_TASKS)
    if mid.startswith("dall-e"):
        return frozenset({"text_to_image"})
    return frozenset()


def declared_image_tasks(capabilities: Iterable[Any]) -> frozenset:
    """The Vision tasks a profile's `capabilities` list declares."""
    tokens = {_norm_token(c) for c in capabilities or ()}
    return frozenset(task for task, accepted in _DECLARED_TASK_TOKENS.items() if tokens & accepted)


def wanted_image_tasks(task: Optional[str]) -> Tuple[str, ...]:
    """The tasks a catalog request asks about; () when the task is not an image task."""
    t = str(task or "").strip()
    if not t:
        return ENDPOINT_IMAGE_TASKS
    return (t,) if t in ENDPOINT_IMAGE_TASKS else ()


def profile_is_image_family(profile: Any) -> bool:
    family = str(getattr(profile, "provider_family", "") or "").strip().lower().replace("_", "-")
    return bool(getattr(profile, "enabled", True)) and family in ENDPOINT_IMAGE_PROVIDER_FAMILIES


def endpoint_profile_image_models(
    profile: Any,
    models: Sequence[Any],
    *,
    task: Optional[str],
) -> Optional[List[str]]:
    """The models `profile` offers for `task`, or None when it is not a candidate.

    `models` is the profile's model list (its `allowed_models`, else what its
    endpoint reported). A profile that declares the task offers every listed
    model (possibly none, when the list could not be read); otherwise only
    the models of a known image family qualify, and none means no candidate.
    """
    tasks = wanted_image_tasks(task)
    if not tasks or not profile_is_image_family(profile):
        return None
    clean: List[str] = []
    seen: set = set()
    for m in models or ():
        s = str(m or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean.append(s)
    if declared_image_tasks(getattr(profile, "capabilities", ()) or ()).intersection(tasks):
        return sorted(clean)
    image_models = [m for m in clean if known_image_model_tasks(m).intersection(tasks)]
    return sorted(image_models) if image_models else None


def endpoint_vision_provider_item(profile: Any, *, models: Sequence[str], task: Optional[str]) -> Dict[str, Any]:
    vid = str(profile.virtual_provider_id)
    label = str(getattr(profile, "display_name", "") or getattr(profile, "id", "") or vid)
    return {
        "id": vid,
        "provider": vid,
        "name": vid,
        "label": label,
        "display_name": label,
        "provider_family": str(profile.provider_family),
        "backend": "openai-compatible",
        "virtual_provider": True,
        "remote": True,
        "local": False,
        "tasks": list(wanted_image_tasks(task)),
        "models": list(models),
        "source": "abstractgateway.provider_endpoint_profiles",
    }


def endpoint_vision_model_item(profile: Any, model_id: str, *, task: Optional[str]) -> Dict[str, Any]:
    known = known_image_model_tasks(model_id)
    wanted = wanted_image_tasks(task)
    tasks = [t for t in wanted if not known or t in known] or list(wanted)
    return {
        "id": model_id,
        "model": model_id,
        "label": model_id,
        "provider": str(profile.virtual_provider_id),
        "provider_family": str(profile.provider_family),
        "backend": "openai-compatible",
        "tasks": tasks,
        "capabilities": tasks,
        "remote": True,
        "source": "abstractgateway.provider_endpoint_profiles",
    }


def merge_endpoint_vision_catalog(
    response: Dict[str, Any],
    entries: Sequence[Tuple[Any, List[str]]],
    *,
    providers_only: bool,
    task: Optional[str],
    errors: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Add the endpoint candidates (`(profile, models)` pairs) to a Vision catalog payload."""
    out = dict(response)
    if errors:
        out["endpoint_provider_errors"] = dict(errors)
    if not entries:
        return out
    providers = [str(x) for x in out.get("providers") or [] if isinstance(x, str)]
    available = [str(x) for x in out.get("available_providers") or [] if isinstance(x, str)]
    raw_map = out.get("models_by_provider")
    by_provider = {str(k): list(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}
    models = [dict(x) for x in out.get("models") or [] if isinstance(x, dict)]
    details: List[Dict[str, Any]] = []
    for profile, profile_models in entries:
        vid = str(profile.virtual_provider_id)
        if vid not in providers:
            providers.append(vid)
        if vid not in available:
            available.append(vid)
        by_provider[vid] = list(profile_models)
        details.append(endpoint_vision_provider_item(profile, models=profile_models, task=task))
        if not providers_only:
            models.extend(endpoint_vision_model_item(profile, m, task=task) for m in profile_models)
    out["providers"] = providers
    out["available_providers"] = available
    out["models_by_provider"] = by_provider
    out["provider_models"] = [
        {"provider": p, "model": m, "id": f"{p}/{m}"} for p, values in by_provider.items() for m in values
    ]
    out["models"] = [] if providers_only else models
    out["endpoint_providers"] = details
    out["available"] = True
    return out
