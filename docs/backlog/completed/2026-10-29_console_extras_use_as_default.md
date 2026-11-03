# Console Models: a text model outside the catalog can be the default

**Status**: completed (2026-10-29, unreleased) · **Priority**: P1 · **Created**: 2026-10-29
**Cross-package**: AbstractCore 0866 (lpalbou/AbstractCore#18): a GGUF-only cached repo's `kind` / `tasks` come
from its header.
**Source**: operator QA batch item (c): "NOT IN THE CATALOG models must be usable as providers/models".

## Why
The Models page lists downloaded models the catalog does not know under **Not in the catalog**. Those rows had only
the trash button. A text model there (mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF on the server) could be used
only by typing it into the capability editor. The capability editor already lists it: discovery returns every
installed repo.

## What shipped
`console_catalog.py`:
- `mcExtraCanBeDefault(r)`: the row's `tasks` include `text_generation` and its kind is not `embedding`.
- `mcDefaultMarkup`, shared with the catalog rows: **Use as default**, or "Default text model" when it already is.
- A row whose tasks are unknown (an AbstractCore before 0866) offers nothing, as before; the default is never
  guessed.

## Evidence
- `tests/test_gateway_console_models_merged.py::test_a_text_model_outside_the_catalog_can_be_the_default`: the text
  GGUF offers it; a TTS GGUF, an embedding repo and an unknown row do not. It goes red without the change
  (mutation-checked).
