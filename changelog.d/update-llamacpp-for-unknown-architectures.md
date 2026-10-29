### Added

- **"Update llama.cpp" on a model row a newer llama.cpp loads** (operator 2026-10-29: "all of them should be known
  and supported"). A GGUF whose architecture the installed llama.cpp does not know but the newest llama-cpp-python
  does (AbstractCore `llamacpp_update`, e.g. XHToken/Spark-X2.6-4B's `spark2_5` with llama-cpp-python 0.3.36) offers
  one admin click that runs the engine installer's llama.cpp job and reloads the catalog; only a model no release
  knows stays greyed with its sentence.

### Changed

- **The engine installer's llama.cpp is 0.3.36 on Linux and Windows** (it knows spark2_5), and on an NVIDIA host it
  installs the CUDA build matching PyTorch's CUDA (cu125 for CUDA 12, cu130 for CUDA 13; `--reinstall-package` so a
  CPU build of the same version is replaced) instead of the CPU wheel, which dropped GPU offload. Apple Silicon
  stays on 0.3.28: the 0.3.36 Metal wheel fails its zip CRC (`lib/cmake/ggml/ggml-config.cmake`), like 0.3.32-0.3.35.
