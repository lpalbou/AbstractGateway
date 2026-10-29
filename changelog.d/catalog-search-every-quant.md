### Fixed

- **"Every quant" applies to Hugging Face results** (OVH 8443, 2026-10-29). With the switch on, the console asked
  Hugging Face without `extended=true`, so the exact repo `unsloth/Qwen3.8-27B-GGUF` showed 2 of its 24 quants. The hub
  search now asks with `extended=true` when the switch is on, and turning the switch on or off asks the Hugging Face
  results shown again.
