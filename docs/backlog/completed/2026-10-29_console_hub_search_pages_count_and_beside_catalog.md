# Console Models: Hugging Face search pages, a visible count, and results beside the catalog

**Status**: completed (2026-10-29, unreleased) · **Priority**: P1 · **Created**: 2026-10-29
**Cross-package**: AbstractCore 0864 (lpalbou/AbstractCore#14: `catalog(hub_page=, hub_page_size=)`, `hub.search`,
exact handles, the query split), AbstractRuntime 0864 (lpalbou/AbstractRuntime#22: the facade passthrough).
**Source**: operator QA on the OVH release-test server. Front item 4: HF search paging and a visible count. Batch
item (a): an exact HF handle always finds the model, and HF results always appear beside catalog results.

## Why
- On the server, the Hugging Face search returned 20 results per lane with no count and no way to see more.
- Searching `Qwen/Qwen3.5-9B` found neither that repo nor the catalog's Qwen3.5 9B. Both causes are fixed in
  AbstractCore 0864.
- Catalog mode never showed Hugging Face results at all: they only appeared after switching mode and pressing
  Enter.

## What shipped
- `src/abstractgateway/console_catalog.py`:
  - `mcHubFetch` asks for `hub_page` beyond the first.
  - `mcHubMore` ("Show more results", `data-mc-action="hub-more"`) appends the next page. A row already shown is
    drawn once.
  - `mcHubMoreMarkup` shows "Showing N Hugging Face results; more are available.", the button, and the
    missing-handle sentence.
  - `mcHubBeside` / `mcHubBesideMarkup`: in Catalog mode, a query of three characters or more asks the Hub 700 ms
    after typing pauses. The `hf_search` rows come under "From Hugging Face" with the same filters. Switching to
    Hugging Face mode with the same words reuses the answer.
- `routes/gateway.py` / `core_config.py`: `hub_page` (>= 1) and `hub_page_size` (1-100) query parameters, passed
  to the facade only when given.
- `docs/console.md`: the Catalog / Hugging Face paragraph.

## Evidence
- `tests/test_gateway_console_catalog.py` (node VM). Each test goes red with its fix removed (mutation-checked:
  the hub-more action, the markup, the mount-time beside search, the mode-switch reuse, the missing-handle
  sentence).
  - `test_hugging_face_results_page_with_a_count_and_show_more`: 4 cards, then 8, then 10, with the count text,
    `hub_page=2` and `3` requested, and no button on the last page.
  - `test_a_handle_hugging_face_does_not_have_is_said`.
  - `test_catalog_search_shows_hugging_face_results_beside_the_catalog`: one Hub request, the section with 3
    cards, and no second request on the mode switch.
- `tests/test_gateway_models_engines.py`: the route passes `hub_page=2&hub_page_size=10` through, and `hub_page=0`
  is a 422.

## Follow-ups
- The terminal console's Models tab (9) has the same Catalog / Hugging Face choice without pages. It is the same
  `hub_page` route, not done here.
- Release order: AbstractCore, then AbstractRuntime, then this gateway. Raise the floors at release.
