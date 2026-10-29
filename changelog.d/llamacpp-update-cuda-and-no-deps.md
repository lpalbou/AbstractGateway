### Fixed
- "Update llama.cpp" on a Linux NVIDIA server now installs the CUDA wheel (the engine installer detects the GPU with `nvidia-smi -L`, as the root installer does) and updates llama.cpp alone (`--no-deps`), instead of re-resolving AbstractCore's gpu setting, which built stable-diffusion-cpp-python from source and failed without a C compiler.
