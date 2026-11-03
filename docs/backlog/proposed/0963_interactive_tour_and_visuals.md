# 0963-gateway: Interactive tour of the console and consistent visuals across apps and docs

> Package: abstractgateway (console), abstractuic, root docs
> Type: design / onboarding
> Status: proposed
> Created: 2026-10-29
> Priority: P3
> Labels: onboarding, tour, visuals, docs

## Summary

Operator idea (2026-10-29): a "virtual tour" of the gateway, as interactive highlights on the real
console pages ("click here", "this is where models live") rather than generated pictures, to explain
how the gateway works and how to set it up; offered after the first-run wizard and from Help. Where an
illustration, diagram or icon genuinely helps (docs, the wizard's capability badges, empty states), it can
be generated with the operator's local tool (/Users/albou/projects/mlx-gen) but only opportunistically and
in ONE consistent style across apps and docs; a small style note (palette, stroke, icon grid) lives in
abstractuic so every app and doc reuses the same set.

## Acceptance (for promotion)
- [ ] The tour runs on the real pages (kit component in abstractuic, consumed by the console).
- [ ] A visual style note in abstractuic and the first consistent icon set (capability badges).
