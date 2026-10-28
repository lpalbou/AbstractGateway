# 0960-gateway: [BUG] The llama.cpp engine card says "CPU on Linux" when the installer put the CUDA build in

> Package: abstractgateway (engines catalog copy)
> Type: bug
> Status: planned (not implemented)
> Created: 2026-10-28
> Priority: P2
> Labels: copy, engines, linux, gpu
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (framework root `untracked/release-closing/ovh-test.log`, step 3)

## Summary

On Linux with an NVIDIA GPU the framework installer swaps in llama.cpp's CUDA build (`llama-cpp-python
0.3.35`, `cu130`, GPU offload verified), but the setup guide's llama.cpp card reads "... CPU on Linux". The
card must say what is installed: the CUDA build (GPU) or the CPU build, from the loaded module.

## Evidence

`ovh-test.log` l.26 (installer: "llama.cpp 0.3.35 cu130 GPU offload"), l.60 (card copy); screenshot
`02-wizard-engines.png`.

## Current code reality

- `src/abstractgateway/engines_install.py` l.163: the static description "Metal on Apple Silicon, the installer's
  CUDA, Vulkan or CPU build on Windows, CPU on Linux."
- `llama_cpp.llama_supports_gpu_offload()` tells the installed build apart (the framework installer uses it to
  keep or revert the CUDA wheel).

## Scope

- In: the static text names every platform's possible builds without claiming one ("Metal on Apple Silicon;
  CUDA, Vulkan or CPU on Linux and Windows, as installed"), and the card's Ready line says which one is
  installed ("GPU offload: yes (CUDA)" / "CPU build"), from the engine probe.
- Out: changing which wheel is installed (the installer's job).

## Validation

- Test: the engine probe with GPU offload reported true renders "GPU"; false renders "CPU build". Red check: a
  static string fails the GPU case.
