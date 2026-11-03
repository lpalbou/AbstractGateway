# 0973 — The console greys Download and Use as default for a GGUF its llama.cpp cannot load

**Status**: completed (2026-10-29, unreleased) · **Priority**: P1 · **Created**: 2026-10-29
**Cross-package**: reads AbstractCore 0867 (an installed GGUF row carries `loadable: false` and `unloadable_reason`,
lpalbou/AbstractCore#19) and 0868 (a catalog or Hub artifact carries them from the Hub's `gguf.architecture`,
lpalbou/AbstractCore#21). Until those land, nothing is flagged and nothing changes.

## Why
On the OVH server XHToken/Spark-X2.6-4B-GGUF (architecture `spark2_5`) downloaded and could be made the default
text model. It then failed at its first call, because that llama-cpp-python build cannot load it.

## What shipped
- `console_catalog.py` `mcUnloadable(a)`: the sentence when the catalog artifact says `loadable: false`, or when the
  installed row for the same files (`GET /models/installed`) does. Otherwise empty.
- `mcActionMarkup`: such a build shows a greyed **Download** (not downloaded) or a greyed **Use as default**
  (downloaded), with the sentence as its tooltip, and carries no action. Delete stays available.
- Test: `tests/test_gateway_console_catalog.py::test_a_gguf_this_llamacpp_cannot_load_greys_download_and_use_as_default`.
  Mutation-checked: without the installed-row lookup, or without the Download greying, it fails.

## Also
- The not-in-catalog rows' **Use as default** (#45) applies the same check (merged together 2026-10-29).
