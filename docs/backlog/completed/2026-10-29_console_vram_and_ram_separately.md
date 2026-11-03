# Console Models: the machine line shows VRAM and RAM separately

**Status**: completed (2026-10-29, unreleased) · **Priority**: P2 · **Created**: 2026-10-29
**Source**: operator QA on the OVH release-test server (2026-10-28), batch item (e).

## Why
The server has a Quadro RTX 5000 with 16 GB and 26.8 GiB of RAM. Its Models page read "This computer: cuda ·
28.7 GB memory · models up to about 16.1 GB": the RAM figure stood alone and the GPU's memory appeared only as a
ceiling. The host profile serves both, `vram_bytes` (16106127360) and `ram_bytes` (28734308352).

## What shipped
`console_catalog.py`, `mcHostMarkup`:
- A GPU with its own memory gives "Quadro RTX 5000 · 16.1 GB VRAM · 28.7 GB RAM".
- "models up to about …" is shown only when the ceiling differs from the VRAM.
- Unified memory (Apple silicon) keeps one figure: "137 GB unified memory · models up to about 103 GB".

## Evidence
- `tests/test_gateway_console_catalog.py::test_the_machine_line_shows_vram_and_ram_separately`: the server's profile
  and a unified-memory profile. It goes red without the VRAM figure (mutation-checked). The console catalog suite
  passes.
