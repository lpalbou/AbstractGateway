"""Interpret remote vision catalog evidence for a named endpoint profile.

The vision plugin deliberately retains unknown OpenAI-compatible models. A
capability picker must distinguish those from compatible models. No generation
request is needed: use catalog metadata or the plugin's known image families.
"""
from typing import Any, Dict, Optional


def endpoint_vision_model(item: Dict[str, Any], task: Optional[str], family: str) -> Optional[str]:
    model = str(item.get("id") or item.get("model") or "").strip()
    prefix = f"{family}/"
    if model.startswith(prefix):
        model = model[len(prefix):]
    if not model:
        return None
    wanted = task or "text_to_image"
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    # AI Relays publishes explicit route support under its namespaced metadata.
    relay = raw.get("airelays") if isinstance(raw.get("airelays"), dict) else {}
    caps = relay.get("capabilities") if isinstance(relay.get("capabilities"), dict) else {}
    routes = caps.get("routes") if isinstance(caps.get("routes"), dict) else {}
    if wanted in {"text_to_image", "image_to_image"} and isinstance(routes.get("images"), bool):
        return model if routes["images"] else None
    aliases = {
        "text_to_image": {"text_to_image", "t2i", "image", "images", "image_generation", "images_generations"},
        "image_to_image": {"image_to_image", "i2i", "image_edit", "image_edits", "images_edits", "inpaint"},
        "image_upscale": {"image_upscale", "upscale"},
        "text_to_video": {"text_to_video", "t2v", "video_generation"},
        "image_to_video": {"image_to_video", "i2v"},
    }
    declared = []
    for field in ("capabilities", "tasks"):
        value = item.get(field)
        if isinstance(value, list):
            declared.extend(str(v).strip().lower() for v in value)
    if declared:
        return model if set(declared).intersection(aliases.get(wanted, {wanted})) else None
    # Same known image-family convention as AbstractVision's remote backend;
    # unknown bare model ids are not evidence of image capability.
    if wanted in {"text_to_image", "image_to_image"} and model.lower().startswith(("gpt-image-", "dall-e-")):
        return model
    return None
