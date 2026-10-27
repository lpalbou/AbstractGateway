# 0947-gateway: [IMPROVEMENT] The console's capability editor adopts the shared voice section (choosing a voice plays it)

> Package: abstractgateway (consumer); abstractuic ui-kit 0.9.1 (the picker, its backlog 0035)
> Type: improvement
> Status: planned — implemented on branch `feat/console-voice-select-plays` (PR open, not merged)
> Created: 2026-10-27
> Priority: P1
> Labels: console, voice, ui-kit, islands
> Root link: AbstractFramework backlog 1055 (voice library track); abstractuic backlog 0035
> Number: 0947 (0943–0946 are taken by other open branches)

## Summary

Multimodal → **Edit multimodal capability** for `output.voice` mounts the kit's voice picker
(islands `mountVoiceSettings`, the component the entity tab and the apps render) instead of its own
provider / model / voice selects. Choosing a voice plays a short sample at once; one play/stop toggle
at the right of the voice row replays from 0 or stops; the **Test** button and its replay panel are
gone for voice routes (Test stays for text routes, where it probes the model).

## Why

Operator, 2026-10-27: "selecting a voice should play the voice, no need to show a UI with replay … a
user can rapidly scroll between different voices and pick the one he likes"; then "we must have
consistency here, and we must have it through the reuse of components from abstractuic". The
console's own list showed cloned voices by raw 32-hex ids in one flat list, while AbstractCode (kit
0.9.0) shows them by name under Your voices / Engine voices / Shared voices with a language filter.

## What changed

- Console (`console.py`): `#modal-default-voice-picker` + `defaultVoicePickerProps` /
  `renderDefaultVoicePicker` / `defaultVoiceSample` (the run-scoped TTS lane with `timeout_s: 25`, the
  kit's `opts.signal` passed to the request and the audio download). `voiceCatalog` is the one
  `GET /voice/voices` call of both kit pickers. The save reads provider / model / voice from the pick
  (a profile is stored as `options.voice`); an untouched pick keeps the stored voice offline. A
  missing islands bundle is said in the dialog; nothing native stands in. Removed:
  `loadDefaultVoices`, `fetchDefaultVoices`, `voiceOptionsFromCatalog`, `voiceUnavailableReason`,
  the voice select.
- Islands re-vendored from ui-kit 0.9.1 (`console_islands.py`, `console_themes.py`).
- `GET /voice/voices` without `providers_only` lists the cloud voice providers (openai,
  openai-compatible) in `tts_providers` too: the kit picker reads that call.

## Findings left open

- The qwen3-tts **Base** checkpoint (`Qwen/Qwen3-TTS-12Hz-0.6B-Base`) is not in AbstractVoice's
  `tts_models_by_provider` (it is a cloning model: `list_cloning_models`), so no picker lists it as a
  model; choosing a clone selects it (the clone's record carries it). Listing it is an
  AbstractVoice / kit decision, not a console filter.
- The kit picker offers its "Workflow / Gateway default" provider choice; in a capability editor that
  choice cannot be saved (Save says to pick a provider and model). A `VoiceSettings` `inheritLabel`
  prop would let the console hide or rename it (abstractuic follow-up).
- Console TUI (`console-tui/src/ui/routes.rs`): its route editor has a voice combobox and a Test that
  synthesizes but does not play (the TUI plays audio elsewhere through `afplay`/`paplay`). Not
  changed here (operator: report only).

## Validation

- `tests/test_gateway_console_voice_preview.py` (sample call + cancel, mount / update / unmount,
  no-bundle message, no native list, vendored kit >= 0.9.1); `test_gateway_console.py` (the VM drives
  the island props: catalog call, sample, save); `test_gateway_console_offline.py` (save from the
  pick: offline keeps the voice, profile stored as voice, clear); `test_gateway_capability_catalog_proxy.py`
  (cloud providers in the full catalog). Mutation-checked red: no signal, no unmount on close, the
  pick not saved, the cloud providers not listed.
- Kit behaviour: abstractuic `ui-kit/scripts/check_voice_library.mjs` 0947 checks (choice plays,
  newer choice aborts, stop stops, play replays from 0 without a request, busy, error line).
