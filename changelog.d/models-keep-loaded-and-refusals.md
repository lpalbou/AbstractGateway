### Added

- **Models page: "Keep loaded" and refused loads (AbstractCore ADR 0010).** The lock toggle reads "Keep loaded" /
  "Kept loaded" with the registry lock icon and a tooltip that says what it does (a kept model is never evicted to make room for another
  model); the last load the fit check refused shows as an error line with its sentence ("X needs 13.7 GB but only
  6.1 GB is free on the GPU (kept: ...). Choose a smaller model, for example ..."), from the runtime's residency
  diagnostics (`refusals`).
