# Engine installation, per engine and per OS

What `POST /api/gateway/engines/{id}/install` (and `abstractgateway engines install`) runs on each
operating system, what it requires, where the files go and how the GPU backend is chosen. The rules
are ADR 0003 (engine installation contract): an engine is installed once per computer, under the
gateway's service account (the account the gateway process runs as), and shared by every user of
the gateway; never with root when the vendor offers a non-root path; prebuilt binaries and wheels
only, never a source build or a compiler; the same states and sentences on every OS; an update is
atomic (the old version stays if the new one fails). Audited 2026-10-29 against
`src/abstractgateway/engines_install.py` and AbstractCore's `config/engines.py`.

| Engine | OS | What runs | Requires | Where it goes | Prebuilt / source | GPU backend |
|---|---|---|---|---|---|---|
| llama.cpp | Linux x86_64, arm64 | `uv pip install abstractcore[gpu]==<installed> llama-cpp-python==0.3.36 --find-links <abetlen index> --only-binary <native>` | nothing (no compiler, no root) | the gateway's Python environment | prebuilt wheel only; a failed wheel fails the install | CUDA wheel (cu125 / cu130) matched to the installed PyTorch CUDA or the installed llama.cpp build on an NVIDIA machine, refused rather than CPU on an NVIDIA machine with no CUDA stack; CPU wheel otherwise |
| llama.cpp | macOS arm64 | `abstractcore[apple]` with `llama-cpp-python==0.3.28` from the Metal index, `--only-binary` | macOS 14+, native (not Rosetta) Python | the gateway's Python | prebuilt only | Metal |
| llama.cpp | macOS Intel, Windows arm64, Linux other arch | not offered: one sentence, no Install button | — | — | (no prebuilt build: never built from source) | — |
| llama.cpp | Windows x86_64 | not installed by the gateway: the row shows the AbstractFramework installer command (`install.ps1`), which adds the prebuilt build | — | the gateway's Python | prebuilt (installer) | CUDA / Vulkan / CPU, chosen by the installer from the driver |
| Ollama | Linux | official `ollama-linux-<arch>.tar.zst` from GitHub (SHA-256 checked), unpacked beside the installed one and swapped in; `ollama serve` as a systemd user unit (or a detached process) | `zstd` (system tool); no root, also when the gateway runs as root | `~/.local/bin/ollama`, `~/.local/lib/ollama` of the service account | vendor binary | Ollama's bundled CUDA / ROCm / CPU runners, chosen by Ollama at run time |
| Ollama | macOS | signed `Ollama-darwin.zip` from GitHub (SHA-256 and Team ID `3MU9H2V9Y9` checked), copied beside the installed app and swapped in | macOS 14+ | `/Applications` when the account can write it, else `~/Applications` (admin password only if `/Applications` is asked for explicitly and not writable) | vendor binary | Metal |
| Ollama | Windows | AbstractCore's plan: `winget install Ollama.Ollama` (per-user), else Ollama's `install.ps1` (OllamaSetup.exe, per-user) | winget or PowerShell; no admin | `%LOCALAPPDATA%\Programs\Ollama` | vendor binary | CUDA / ROCm / CPU (Ollama) |
| LM Studio | Linux x86_64, arm64 | LM Studio's `install.sh` (headless daemon `llmster` and the `lms` CLI) | `libatomic1` system library (an administrator installs it once; the job says the line); no root | `~/.lmstudio` of the service account | vendor binary | LM Studio's runtimes (CUDA / Vulkan / CPU), chosen by LM Studio |
| LM Studio | macOS arm64 | signed disk image from lmstudio.ai (Team ID `D65G88RHWN` checked), copied beside the installed app and swapped in, `lms server start` | macOS 14+, Apple silicon | `/Applications` or `~/Applications` | vendor binary | Metal / MLX |
| LM Studio | macOS Intel | not offered (no build) | — | — | — | — |
| LM Studio | Windows | AbstractCore's plan: LM Studio's `install.ps1` (llmster + `lms`, per-user), or `winget install ElementLabs.LMStudio` | PowerShell or winget; no admin | `%USERPROFILE%\.lmstudio` | vendor binary | CUDA / Vulkan / CPU (LM Studio) |
| vLLM | Linux with an NVIDIA GPU | `abstractcore[gpu]==<installed>` (vLLM, PyTorch) with `--torch-backend=auto` (uv) and `--only-binary <native>` | NVIDIA driver; no root | the gateway's Python | prebuilt wheels only | CUDA |
| vLLM | macOS, Windows, Linux without NVIDIA | not offered: one sentence (remote vLLM server / WSL2) | — | — | — | — |
| Hugging Face | Linux x86_64, arm64 | `abstractcore[gpu]==<installed>`, `--only-binary <native>` (and the llama.cpp wheel, which the setting includes) | no root | the gateway's Python | prebuilt only | PyTorch's CUDA build on NVIDIA (uv `--torch-backend=auto` on Windows), CPU otherwise |
| Hugging Face | macOS arm64 | `abstractcore[apple]==<installed>`, `--only-binary <native>` | native Python | the gateway's Python | prebuilt only | MPS (Metal) |
| Hugging Face | Windows x86_64 | `abstractcore[gpu]==<installed> --torch-backend=auto`, `--only-binary <native>` (the Windows setting leaves llama.cpp out) | no admin | the gateway's Python | prebuilt only | PyTorch CUDA build for the driver found, else CPU |
| MLX | macOS arm64 (14+, native Python) | `abstractcore[apple]==<installed>`, `--only-binary <native>` | macOS 14+ | the gateway's Python | prebuilt only | Metal |
| MLX | every other OS / arch | not offered: one sentence | — | — | — | — |

`<native>` is `llama-cpp-python`, `torch`, `torchvision`, `torchaudio`, `vllm`, `mlx`, `mlx-lm`,
`mlx-metal` (`engines_install.PREBUILT_ONLY`). Every Python install keeps the gateway's own
`abstract*` packages at the versions it runs, and every Python engine update is atomic: the
packages it changed are reinstalled at their previous versions when the install or the load check
fails.

## Known gaps (2026-10-29)

- **Windows Ollama and LM Studio** run AbstractCore's vendor commands through the generic job
  ("Running the vendor installer for …"): the states are the shared ones, but the progress and
  failure sentences are less specific than on macOS and Linux, and the vendor installers handle
  their own updates (atomicity is the vendor's). Not run on a real Windows desktop yet.
- **LM Studio on Linux** is the vendor's script: its update is the vendor's (not swapped by the
  gateway), and `libatomic1` stays an administrator step on minimal images.
- **Ollama on Linux** needs `zstd`, a system tool; the AbstractFramework server installer adds it.
