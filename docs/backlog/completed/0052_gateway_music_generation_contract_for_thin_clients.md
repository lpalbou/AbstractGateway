# Completed: Gateway music generation contract for thin clients

## Metadata
- Created: 2026-05-21
- Status: Completed
- Completed: 2026-05-22

## ADR status
- Governing ADRs: None
- ADR impact: None; this item is surface adoption and backlog cleanup, not a new durable policy.

## Context

This item was opened when Gateway did not yet expose Runtime-backed music
generation or music discovery to thin clients.

By the time of the 2026-05-22 audit, the direct Runtime-backed surface already
existed in code:

- `POST /api/gateway/runs/{run_id}/music/generate`
- `GET /api/gateway/audio/music/providers`
- `GET /api/gateway/audio/music/models`
- `capabilities.contracts.flow_editor.media.generated_music`
- `capabilities.contracts.assistant.media.generated_music`

The backlog item had become stale, but one small truth gap remained:
`generated_music.workflow.available` was still hard-coded `false` even though
Runtime already supported workflow-level music generation.

## What changed

Implemented on 2026-05-22:

1. Confirmed the direct music route and discovery/catalog routes are already
   present and Runtime-backed.
2. Updated Gateway's thin-client contract so
   `generated_music.workflow.available` follows the same Runtime-backed
   capability truth as the direct route instead of remaining hard-coded false.
3. Closed the stale proposal and recorded the real shipped outcome in
   `completed/`.

## Resulting contract behavior

- Thin clients can feature-detect direct music generation and music catalogs
  from Gateway contract alone.
- Flow-style clients can also treat workflow-backed music generation as
  available when the Gateway Runtime capability stack actually supports it.

## Validation

- `python -m pytest -q tests/test_capabilities_endpoint_contract.py tests/test_generated_media_gateway_contract.py tests/test_gateway_capability_catalog_proxy.py`

Result:
- focused verification passed: `16 passed`

## Completion report

This item was mostly a backlog-truth cleanup, but it also closed the remaining
workflow-availability mismatch.

Files/symbols touched:
- `src/abstractgateway/routes/gateway.py`
  - `_generated_media_contract()`
- `tests/test_capabilities_endpoint_contract.py`
- `docs/backlog/overview.md`

Behavior changes:
- `generated_music.workflow.available` is now truthful instead of artificially
  disabled.

Residual follow-up:
- Gateway still does not provide one unified cross-modality
  “find models by arbitrary capability” query; higher apps continue to compose
  music/vision/voice/text discovery from the existing catalog family.
