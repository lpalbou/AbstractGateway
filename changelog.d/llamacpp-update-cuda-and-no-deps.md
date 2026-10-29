### Fixed
- "Update llama.cpp" on a Linux NVIDIA server now installs the CUDA wheel (the engine installer detects the GPU with `nvidia-smi -L`, as the root installer does) and updates llama.cpp alone (`--no-deps`), instead of re-resolving AbstractCore's gpu setting, which built stable-diffusion-cpp-python from source and failed without a C compiler.
- A CUDA llama.cpp wheel that installs but carries no libggml-cuda library or reports no GPU offload now fails the job with a readable sentence (before, it was only a warning in the log, and the CPU build stayed in place silently).
