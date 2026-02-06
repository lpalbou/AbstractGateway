from __future__ import annotations

from abstractgateway.routes.gateway import _normalize_run_context_media


def test_normalize_run_context_media_lifts_common_shapes() -> None:
    input_data = {
        "context": {
            "messages": [
                {
                    "role": "user",
                    "content": "what is in this image?",
                    "attachments": [{"$artifact": "a1", "content_type": "image/png", "filename": "x.png"}],
                },
                {
                    "role": "user",
                    "content": "and this too",
                    "media": [{"artifact_id": "a2", "content_type": "image/jpeg", "filename": "y.jpg"}],
                },
            ]
        },
        # Some clients send top-level attachments/media.
        "attachments": [{"$artifact": "a3"}],
        "media": [{"artifact_id": "a4"}],
    }

    out = _normalize_run_context_media(dict(input_data))
    ctx = out.get("context")
    assert isinstance(ctx, dict)
    atts = ctx.get("attachments")
    assert isinstance(atts, list)

    ids: set[str] = set()
    for it in atts:
        if isinstance(it, dict):
            aid = it.get("$artifact") or it.get("artifact_id")
            if isinstance(aid, str) and aid:
                ids.add(aid)
    assert ids == {"a1", "a2", "a3", "a4"}

