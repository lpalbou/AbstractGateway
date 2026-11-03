# Model signals, fit default and manufacturer marks — 0994

Single-agent implementation and visible review: live [dark](models-dark.png),
[light](models-light.png), [vision input](vision-input.png),
[mobile](mobile-light.png). Exact header order: manufacturer mark + model name,
capability icons, total parameters, fold chevron. Active parameter count is in
the shared tooltip. The balanced 195px desktop cards/44px rows remain. On narrow
cards the title gets its own line so a Starter badge and four capabilities cannot
reduce the model name to one letter.

Vision input uses a labelled amber eye; thinking uses success green and tools
info blue, with subtle token-derived discs. Text is muted. Shape/accessibility
labels carry the meaning as well as colour. Vision is shown only for server
`capabilities.vision === true` (e.g. Qwen3-VL/Qwen3.5); text-only Qwen3 is not
invented into a vision model. Image generation remains distinct.

Fits this computer is on for a fresh view. Explicit off serializes as `fits=0`,
survives reload, and remains reversible. Downloaded clears fit and quant filters
to expose all local inventory; Browse restores its prior filters. Existing
`fits=1` links still parse as on and normalize to the default URL.

Official organization artwork for Qwen, Meta Llama, DeepSeek, Google, Mistral AI,
Microsoft and NVIDIA is in the shared kit registry; existing shared brands can
also render. Other vendors get a text identity fallback. No guessed or redrawn
logo. Manufacturer comes from catalog vendor, not the build publisher (e.g.
unsloth is not used as Qwen's brand). The 7 new original raster files total
28,572 bytes, inline in the kit bundle: no per-card remote requests. Source URLs,
original colours, dimensions, MIME and SHA-256 are recorded/checked by UIC 0076;
see `abstractuic/docs/icons.md` and `ui-kit/src/brand_icons.tsx`.

63 broad regression checks passed initially; a hidden-native-switch browser
click and old count expectation were corrected. Final catalog/browser/sync gate:
33 passed; final mobile-title/browser/merged gate: 7 passed. Browser assertions:
162, all passing (default/off/reload, order, colours, actual inline logos,
capability names, light/dark/mobile, loading/retry and management operations).
Kit: 492 brand, 42 tooltip, 32 top-bar checks, CSS balance, 21 theme-token blocks
and strict contrast on 20 named themes passed.

Commits `9fd6038a`, `b3908d01`, shared kit `4d0347e`. Healthy graceful deployment
after zero-inflight preflight. Live light/dark checks all pass: exact order,
alignment, real Qwen mark and vision, default fit/off link/reload, Downloaded/
Browse restoration, disclosure, details/copy, themed hover, mobile and healthy
Gateway. No page errors or model-management writes. Note's deployed frontend
also survives Gateway restart (verified through its normal app handover; a
first unauthenticated app-page probe correctly returned 401).
[Machine receipt](validation.json).

No ADR change or new residual implementation work. Unsupported manufacturer
marks retain a documented fallback; publication is outside this local deployment.
Agora unavailable (connection refused).
