### Fixed

- **Hugging Face results list every quant** (OVH 8443, 2026-10-29). The console asked Hugging Face without
  `extended=true`, so the exact repo `unsloth/Qwen3.8-27B-GGUF` showed 2 of its 24 quants. Every hub search now asks
  with `extended=true`.
