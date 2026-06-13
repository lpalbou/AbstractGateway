# Completed: Gateway PDF Runtime floor and E2E contract

## Metadata
- Created: 2026-06-06
- Status: Completed
- Completed: 2026-06-06

## ADR status
- Governing ADRs: root `docs/adr/0029-permissive-dependency-and-licensing-policy.md`, root `docs/adr/0033-install-profiles-config-entrypoints-and-server-boundaries.md`
- ADR impact: None. This item keeps Gateway aligned with existing package-boundary and dependency policy.

## Context
Runtime owns VisualFlow execution. The PDF workflow nodes (`write_pdf`, `read_pdf`) live in Runtime and use permissive dependencies (`reportlab`, `pypdf`). Gateway must depend on the Runtime release that contains those nodes and must prove that bundle-mode execution can use them through the normal Gateway run path.

## Problem
Gateway still targeted an older Runtime floor and had no host-level regression for a workflow that writes a PDF, reads it back, and exposes extracted PDF text. That left a gap between Runtime's implementation and what a clean Gateway install/run could guarantee.

## What changed
- Raised Gateway's base, `apple`, and `gpu` Runtime dependency floors to `AbstractRuntime>=0.4.28`.
- Updated Gateway install-profile tests for the current sibling package floors and for Runtime's permissive PDF dependency contract.
- Added a Gateway bundle execution test that:
  - writes a real PDF in the run workspace;
  - reads the PDF back through Runtime's `read_pdf` node;
  - verifies the extracted PDF text appears in the Gateway run ledger;
  - verifies the workspace file starts with `%PDF-`.

## Current code pointers
- `pyproject.toml`
- `CHANGELOG.md`
- `tests/test_gateway_install_profiles.py`
- `tests/test_gateway_visualflow_file_nodes.py`

## Validation
- `PYTHONPATH="src:../abstractruntime/src:../abstractcore:../abstractagent/src:../abstractmemory/src:../abstractsemantics/src:../abstractvision/src:../abstractvoice/src:../abstractmusic/src:../abstractaudio/src:../abstractvideo/src:../abstractsound/src" python -m pytest tests/test_gateway_visualflow_file_nodes.py tests/test_gateway_install_profiles.py tests/test_gateway_artifacts_endpoint.py::test_gateway_artifacts_api_list_metadata_and_download -q`
  - Result: 12 passed.

## Completion report
Gateway now installs against the Runtime PDF-node contract and has direct host-level proof that a VisualFlow PDF write/read workflow runs through Gateway bundle execution without PyMuPDF-family dependencies.

## Residual follow-up
Core item `0806_pdf_images_tables_and_extraction_strategy.md` remains the richer PDF image/table/OCR track. Gateway should relay any future capability metadata from Runtime/Core rather than guessing it.
