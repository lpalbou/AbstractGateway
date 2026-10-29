### Changed

- **Engine installation contract** (ADR 0003, `docs/engines-install.md`: every engine on every OS in one table).
  Engines are installed once per computer under the gateway's service account and shared by every user; no install
  starts a compiler: every native package of AbstractCore's settings is wheels-only (`--only-binary`), an install of a
  setting that includes `llama-cpp-python` names its prebuilt wheel (a Hugging Face install on the OVH server started
  building llama.cpp from source), and llama.cpp no longer falls back to a source build or asks for a compiler or the
  Apple tools: a host without a prebuilt build gets one sentence and no Install button. Ollama on Linux is always its
  release archive under the service account, also when the gateway runs as root (never `curl | sh`); its card no
  longer says "for this account". Updates are atomic: a Python engine whose install or load check fails gets its
  changed packages put back to their previous versions, and Ollama's archive and the macOS apps are unpacked or
  copied beside the installed ones and swapped in.
