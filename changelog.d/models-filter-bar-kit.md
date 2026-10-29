### Changed

- **The Models page's filters are one compact, aligned bar: the kit's filter bar** (operator design finding
  2026-10-29; abstractuic ui-kit backlog 0074). The first line holds the Catalog / Hugging Face switch, the search
  box and **Fits this computer**; then one row per filter (Quantization, Provider, Capability, Status) with its name
  in a fixed left column and its chips on the right, wrapping under the chips, never under the name. Every chip
  has the same size (no 44 px ovals on phones: the hit area grows invisibly) and its count is small grey text.
  There is no "Every quant" switch on any tab (operator ruling, #79). The setup guide's
  model step shows the same bar. Until an abstractuic release carries the bar, the console serves a verbatim copy
  of the kit's `af-filterbar` CSS and a verbatim port of its markup functions, both pinned by tests against the
  kit.
