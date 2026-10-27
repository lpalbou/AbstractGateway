# 0941-gateway: [IMPROVEMENT] Recognise an endpoint's image models through AbstractVision's registry (name and aliases)

> Package: abstractgateway (consumer); abstractvision (the registry, its backlog 0025)
> Type: improvement
> Status: planned
> Created: 2026-10-27
> Priority: P2
> Labels: vision, endpoints, catalog, models

## Summary

The vision catalog lists the image models served by an endpoint a person configured. When the endpoint's own catalog says nothing about a model, the gateway decides by the model's name with its own prefix rule (`gpt-image-*`, `dall-e-*`). Replace that rule by AbstractVision's registry lookup (name and aliases), so a relay's `flux-schnell` or a new OpenAI image model is recognised from data, in one place.

## Why

Operator, 2026-10-27: "i want the models served by an endpoint i configured to appear in my list of available vision models"; "if an openai compatible endpoint actually gives you the information, we should use it ... but i would still rely on the model name + its aliases". The endpoint's word is used since 0.16.0 (`apply_endpoint_evidence`); the name rule is still a hard-coded prefix list, duplicated in AbstractVision.

## Current code reality

- `src/abstractgateway/endpoint_vision_catalog.py`: `known_image_model_tasks` (prefixes), `endpoint_profile_image_models` (name rule for the list of every provider), `endpoint_model_evidence` + `apply_endpoint_evidence` (a named endpoint's own catalog first).
- AbstractVision `assets/vision_model_capabilities.json` has no cloud model and no aliases yet (its backlog 0025).

## Scope

- In: `known_image_model_tasks` delegates to AbstractVision's `model_tasks()`; the AbstractVision floor; tests (an alias recognised, an unknown name not listed, the endpoint's word still winning).
- Out: the registry itself (AbstractVision 0025).

## Dependencies

AbstractVision 0025 released.

## Validation

`tests/test_endpoint_vision_catalog.py` with a model known only by an alias; the prefix list removed (a grep finds none).
