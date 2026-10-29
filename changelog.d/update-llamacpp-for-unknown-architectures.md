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

### Fixed

- **Updating llama.cpp never replaces a GPU build by the CPU wheel, nor llama.cpp under a loaded model.** On an
  NVIDIA host the CUDA build comes from PyTorch's CUDA or, failing that, from the installed llama.cpp's own CUDA
  runtime; when neither has one the update is refused with the sentence ("This computer has an NVIDIA GPU, but
  neither the gateway's PyTorch nor its llama.cpp has a CUDA build ..."). `POST /engines/llamacpp/install` refuses
  with "Unload <model> first: llama.cpp cannot be updated while it holds a model." while a GGUF is resident
  (in-process or in the model host child). After the update the model host child is restarted so the new llama.cpp
  takes effect, and the result says so.
