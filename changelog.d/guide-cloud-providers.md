### Added

- **Setup guide: a Cloud providers step, and the same cards on the Providers tab** (operator finding 2026-10-29:
  "where do I configure the cloud engines?"; the Local engines step only said "cloud providers only need an API key").
  The guide has six steps; **Cloud providers** comes after Local engines: one card per cloud provider AbstractCore
  supports (read from its registry: `GET /discovery/providers` rows with `local_provider: false`, today OpenAI,
  Anthropic, OpenRouter and Portkey), an API-key field and **Check the key**: "Key works · 48 models" or one sentence
  why not. The cards are the abstractuic kit's `CloudProviderCards` (islands `mountCloudProviderCards`, ui-kit 0.9.9),
  mounted by the guide and by the Providers tab, whose **Remote providers** presets became **Cloud providers** (plus
  the custom OpenAI-compatible preset). The Default models step's "Or use an API key" uses the same kit logic and
  now lists every cloud provider, not only OpenAI and Anthropic.
- **Official marks on the engine cards** (same finding). Ollama, LM Studio, MLX, llama.cpp, vLLM and Hugging Face show
  their logo from the kit (islands `brandMarks`; each mark inlined with its source, licence and date) instead of the
  two-letter badges. `tests/test_gateway_console_brand_marks.py` pins that every engine and every AbstractCore cloud
  provider has a mark or a documented text fallback in the vendored bundle.

### Changed

- **Provider connections: `create_only`, and key checks that never echo the key** (`routes/gateway.py`,
  `provider_endpoint_profiles.py`). `POST /config/provider-endpoint-profiles` takes `create_only: true`: an existing
  id answers 409 `connection_exists` and is left unchanged (decided under the store lock). `POST
  …/discover-models` redacts the submitted key, its 8-character prefix and suffix, and any provider-masked echo
  (`sk-abcd****1234`, as OpenAI's 401 quotes it) from `error` and from a 502 detail, and adds `error_kind` (`auth`,
  `rate_limited`, `unreachable`, `other`, `no_models`) when no model is listed. Tests:
  `tests/test_gateway_cloud_provider_key_check.py`.
- **Console islands re-vendored to ui-kit 0.9.9** (kit PR pending release; re-sync from the released tag before
  merge).
