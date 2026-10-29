### Fixed

- **CI covers the seam routes** (backlog 0972). Gateway CI installs every package a seam mounts (today
  `abstractnote>=0.2.0`), so the route authorization contract walks AbstractNote's routes on every run;
  `tests/test_gateway_seams_mounted.py` fails when a seam package drops out of the CI install. Tests keep their gateways
  as on an install without the seam's workflows unless marked `note_workflows` (the workflows' LLM nodes made every
  test gateway warm a default model), and a developer's `*_API_KEY` variables never reach a test.
