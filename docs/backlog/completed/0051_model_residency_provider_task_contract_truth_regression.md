# Completed: Model residency provider/task contract truth regression

## Metadata
- Created: 2026-05-21
- Status: Completed
- Completed: 2026-05-22

## ADR status
- Governing ADRs: None
- ADR impact: None; this is contract truth and Runtime adoption work inside the existing Gateway -> Runtime boundary.

## Context

Gateway already exposed `/api/gateway/models/loaded|load|unload` through
Runtime's public host facade, but the thin-client contract still hard-coded
`supports.text_generation|image_generation|tts|stt=true` in
`common.model_residency`.

That drift mattered to higher apps, especially AbstractFlow, because local
Runtime media residency intentionally fails closed while remote Core-backed
Runtime reports task-specific truth. Thin clients were therefore offered author
time controls that Runtime could later reject as `supported:false`.

## What changed

Implemented on 2026-05-22:

1. Gateway capability construction now calls Runtime's public
   `get_model_residency_capabilities(...)` host-facade method instead of
   hard-coding support flags.
2. `common.model_residency` now preserves:
   - `tasks`
   - `supports`
   - `task_capabilities`
   - `supported_tasks`
   - `unsupported_tasks`
   - `mode`
   - `relay_only`
   - `capabilities_source`
   - `diagnostics`
3. The contract now advertises `music_generation` explicitly as unsupported when
   Runtime reports that truth.
4. When residency capabilities cannot be queried, Gateway fails closed in the
   contract instead of pretending support.

## Resulting contract behavior

- Local Runtime-backed Gateway now advertises text residency support and media
  residency non-support truthfully.
- Remote Core-backed Gateway now advertises text/image/tts/stt support while
  preserving Runtime's explicit `music_generation` unsupported state.
- Thin clients can gate UI from Gateway contract truth alone without issuing
  synthetic warmup calls.

## Validation

- `python -m pytest -q tests/test_gateway_model_residency_endpoints.py tests/test_capabilities_endpoint_contract.py`

Result:
- focused verification passed: `9 passed`

## Completion report

This closes the real scope of the regression item.

Files/symbols touched:
- `src/abstractgateway/routes/gateway.py`
  - `_gateway_model_residency_contract_descriptor()`
- `tests/test_gateway_model_residency_endpoints.py`
- `tests/test_capabilities_endpoint_contract.py`

Behavior changes:
- Gateway no longer hard-codes media residency support.
- Runtime's public residency truth is now visible to higher apps through the
  shared Gateway contract.

Residual follow-up:
- Gateway still does not expose provider/task pair truth beyond task-level
  support because Runtime does not yet publish pairwise residency capability
  metadata.
