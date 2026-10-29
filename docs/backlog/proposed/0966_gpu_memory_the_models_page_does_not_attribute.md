# 0966: GPU memory the Models page does not attribute

Status: proposed (2026-10-29). Found on the OVH 8080 gateway after the pass-one redeploy
(gateway 9e8d493d, core d146ef95, runtime f20f37dd; Quadro RTX 5000, 15,360 MiB).

## Numbers

- `nvidia-smi`: the 8080 gateway process (pid 46324) holds **14,344 MiB**; the 8443 gateway 118 MiB.
- Right after boot, before any chat: 9,414 MiB (the warmed default text model only).
- After one `basic-agent` chat on the default model: 14,344 MiB (+4,930 MiB).
- The Models page / `GET /host/state` lists two resident rows: the text model
  `mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF` (5.38 GiB weights + 2.05 GiB KV, ctx 65,536)
  and the embedding `Qwen/Qwen3-Embedding-0.6B-GGUF` (CPU lane, no size). `task_counts`: tts 0, stt 0.
- So about **4.8 GiB** of device memory is held by this process with no row naming it.

## Why it matters

The residency failsafe (AbstractCore ADR 0010) decides from free device memory and evicts only
what it can see. Memory held by something unlisted is neither evictable nor explained: the next
load is refused ("only 0.9 GB is free") with nothing on the page to unload.

## To do

1. Find the holder (candidates: speech engines warmed on first use, compute buffers of a second
   llama.cpp context, torch's CUDA cache after the chat, memory/KG embeddings on CUDA).
2. Every in-process holder of device memory gets a residency row (task, size) or is counted in a
   "process overhead" line, so the page sums to what `nvidia-smi` reports within a stated margin.
3. A test that goes red when a warmed engine holds memory without a row.
