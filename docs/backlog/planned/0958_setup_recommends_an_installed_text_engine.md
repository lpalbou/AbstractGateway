# 0958-gateway: [BUG] The setup guide recommends LM Studio on a headless Linux server where llama.cpp and vLLM are installed

> Package: abstractgateway (console setup guide, recommended defaults); abstractcore (`RECOMMENDED_MODELS`, seam)
> Type: bug
> Status: implemented (gateway half; the engine-aware pick in AbstractCore stays its own item). Verified read-only on the OVH server 2026-10-29: Core's pick lmstudio/qwen3.5-9b (status unknown, `lms` not installed) -> huggingface `unsloth/Qwen3-8B-GGUF:Q4_K_M` (Qwen3 8B, 5.1 GB) through llama.cpp
> Created: 2026-10-28
> Priority: P1
> Labels: first-run, setup-guide, recommendations, linux, headless
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (framework root `untracked/release-closing/ovh-test.log`, step 3)

## Summary

On a fresh Linux/NVIDIA server the setup guide's step 3 shows "Recommended for this computer:
lmstudio · qwen/qwen3.5-9b" with "The `lms` CLI is not installed; GET http://localhost:1234/v1/models
unreachable", while its step 2 lists llama.cpp (CUDA build), vLLM and HF transformers as Ready. The fresh-install
defaults also point `input.text`/`output.text` at that missing engine. The guide must recommend an engine that
is installed (llama.cpp with a small GGUF model on a server), and offer LM Studio only where it can run.

## Evidence

`ovh-test.log` l.59-64; screenshots `02-wizard-engines.png`, `03-wizard-default-model.png` (framework root
`untracked/release-closing/`). The working path, measured there: `unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M`
(2.5 GB) through llama.cpp on the GPU.

## Current code reality

- AbstractCore `abstractcore/config/capability_defaults.py` l.376-394: `input.text` recommends
  `lmstudio / qwen/qwen3.5-9b` (operator ruling 2026-08-01: LM Studio off Apple silicon). `pick_for` (l.366)
  drops an accelerator pick only when `only_if_installed` and the engine is reported missing; the portable pick
  is returned even when its engine is not installed and cannot be installed (no desktop session).
- Gateway: the setup card (`src/abstractgateway/console.py` l.15330, "Recommended for this computer") and
  `core_config.apply_recommended_gateway_capability_defaults` (l.468) render and seed that pick as is.

## Scope

- In (gateway): when the recommended text engine is not installed and another local text engine is Ready,
  the card recommends the installed one (Linux: llama.cpp with a GGUF that fits, from the catalog's `--fits`
  rows) and names the original pick as an alternative with why it is not first ("LM Studio is a desktop app,
  not installed here"). Fresh-install defaults follow the same rule.
- Seam (abstractcore, separate item there): an engine-aware pick (`only_if_installed` on the portable route, or a
  headless-Linux pick); the 2026-08-01 ruling is the operator's to revisit, not this item's.
- Out: installing LM Studio on a server.

## Validation

- Test: host facts `{platform: linux, accelerator: cuda, engines_installed: {lmstudio: false, llamacpp: true}}`
  make the card and the seeded defaults name llama.cpp; with LM Studio installed they keep LM Studio. Red check:
  drop the rule and the first test fails.
- Real: fresh Linux/NVIDIA install, the guide's "Use recommended defaults" yields a working first chat.
