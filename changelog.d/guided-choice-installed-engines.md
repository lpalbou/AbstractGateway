### Fixed

- **The guided choice offers only models this computer can run** (OVH 8443, 2026-10-29). The setup guide's "Other
  models that fit" listed Ollama models on a server without Ollama, and picking one would set a route that cannot
  answer. The list now keeps only models whose engine is installed here (the engine inventory, the 0958 rule; a GGUF
  runs on llama.cpp). While the inventory loads it says "Checking the engines on this computer…". Each row names its
  engine readably ("llama.cpp", "Ollama"), never "HuggingFace (huggingface)".
