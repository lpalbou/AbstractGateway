### Changed

- **Cloud providers: one Connect per card, OVH AI Endpoints and any OpenAI-compatible endpoint** (operator feedback
  2026-10-29 on the setup guide). The guide's Cloud providers step and the Providers tab mount the kit's cards (ui-kit
  0.9.10): ONE action per card, **Connect** (**Reconnect** when the provider already has a connection), which checks
  the key and, only when it works, saves it as a NEW connection ("Connected · 48 models", or the reason with nothing
  stored). After AbstractCore's native providers come **OVH AI Endpoints** (its base URL preset) and **Other
  OpenAI-compatible endpoint** (Name, Base URL, API key), both AbstractCore's `openai-compatible` provider. The cards
  no longer carry "More settings"; the Providers tab's full editor is the **Advanced connection** entry. The Local
  engines and Cloud providers subtitles are one line each.
- **OpenAI's official mark on its card** (kit `brandMarks`); Anthropic, Portkey and OVHcloud keep text badges whose
  notes quote each brand's terms. `tests/test_gateway_console_brand_marks.py` also pins that the OVH preset and the
  generic card ride on AbstractCore's `openai-compatible` provider.
- **Console islands re-vendored to ui-kit 0.9.10** (kit PR pending release; re-sync from the released kit before
  merge). An OVH key is truly checked once AbstractCore ships the public-`/models` key probe (core PR); until then
  OVH's public model list answers any key.
