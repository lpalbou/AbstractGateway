# Console Models: "Every quant" (each model's whole quant set, on demand)

**Status**: completed (2026-10-29, unreleased) · **Priority**: P1 · **Created**: 2026-10-29
**Cross-package**: AbstractCore 0865 (lpalbou/AbstractCore#16: `catalog(extended=True)`), AbstractRuntime
(lpalbou/AbstractRuntime#22: the facade passes `extended`). This branch is stacked on the Hugging Face pages branch
(#36).
**Source**: operator QA batch item (b): "quantization filter gets All so any quant can be installed".

## What shipped
- `console_catalog.py`: an **Every quant** switch beside **Fits this computer** (`data-mc-every`).
  - It is a hash key: `#catalog?every=1`.
  - When on, the catalog is loaded with `?hub=true&extended=true`, and the extra quants (`extended: true`) are
    drawn as rows of the same cards, with the quant filter, fit and **Download**.
  - Off, or **Clear filters**, loads the plain catalog again.
- `routes/gateway.py` / `core_config.py`: the `extended` query parameter, passed only when true. An AbstractRuntime
  without it gets a 501 `abstractruntime_too_old`, "Every quant of a model needs a newer AbstractRuntime…".
- `docs/console.md`.

## Evidence
- `tests/test_gateway_console_catalog.py::test_every_quant_asks_for_the_whole_quant_set_and_shows_it`: one plain
  load, the extended URL when the switch is on, 10 more artifacts drawn, the hash, and plain again when off. It goes
  red with the plain URL kept (mutation-checked).
- `tests/test_gateway_models_engines.py`: `extended` reaches the facade.
- Catalog, delete, models-engines and first-run console suites: 67 passed.

## Changed 2026-10-29 (operator ruling)

The switch is gone. The catalog always asks `hub=true&extended=true`; the quantization filter's "All" lists the whole set, and 4-bit, 8-bit and Other filter it. The `every` link parameter is ignored.
