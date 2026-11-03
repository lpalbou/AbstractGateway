### Changed

- **Setup guide and Providers tab: shorter copy, one button per cloud card.** The Local engines and Cloud providers
  subtitles are one line each ("Run AI models on this computer. Cloud accounts come in the next step."; "Paste a
  provider's API key to use its models; the key stays on this gateway."). The Providers tab mounts the kit's cloud
  cards like the guide does (one button, **Connect** or **Edit**, no "More settings"), and the section below them is
  **Advanced connection** (the full connection editor: allowed models, a server on your network). The cards include
  **OVH AI Endpoints** and the generic **OpenAI-compatible endpoint**. An OVH key is checked with a test request
  (AbstractCore's key probe for endpoints that list their models without a key), so a wrong key is refused.
