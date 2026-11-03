### Changed

- **Setup guide and Providers tab: shorter copy, one button per cloud card** (operator feedback 2026-10-29 on the
  setup guide). The Local engines and Cloud providers subtitles are one line each ("Run AI models on this computer.
  Cloud accounts come in the next step."; "Paste a provider's API key to use its models; the key stays on this
  gateway."). The Providers tab mounts the kit's cloud cards like the guide does (one button, **Connect** or
  **Edit**, no "More settings"), and the section below them is **Advanced connection** (the full connection editor:
  allowed models, a server on your network). The cards, including **OVH AI Endpoints** and the generic
  **OpenAI-compatible endpoint**, come from the vendored ui-kit 0.9.12 (unchanged). An OVH key is truly checked once
  AbstractCore ships the public-`/models` key probe; until then OVH's public model list answers any key.
