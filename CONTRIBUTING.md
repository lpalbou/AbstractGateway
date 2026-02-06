# Contributing

Thanks for your interest in improving AbstractGateway.

This repo is a Python package (`src/` layout) with a FastAPI server, a durable runner worker, and contract tests under `tests/`.

## Quick start (dev)

```bash
python -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
pip install -e ".[dev,http]"
```

Run the test suite:

```bash
pytest
```

If you only want the fast/unit/contract layer:

```bash
pytest -m basic
```

Notes:
- `integration` and `e2e` tests may require optional dependencies and/or external services (e.g. an LLM provider).
- The CLI entrypoint is `abstractgateway` (see `pyproject.toml`).

## How to contribute

1. **Open an issue** (or a draft PR) describing what you want to change and why.
2. Keep changes **small and reviewable**.
3. Add/adjust tests where it improves confidence.
4. Update docs so they remain truthful and user-facing:
   - README is the entrypoint.
   - `docs/getting-started.md` is the step-by-step guide.
   - Prefer adding FAQ entries for recurring “gotchas”.
   - Regenerate the LLM snapshot: `python scripts/generate-llms-full.py` (updates `llms-full.txt`).

## Project conventions

- Source of truth is the code in `src/`.
- Keep public docs concise, actionable, and aligned with the current behavior.
- Prefer explicit env var names as used in code (see `docs/configuration.md`).

## Release checklist (maintainers)

1. Update `CHANGELOG.md`.
2. Bump version in:
   - `pyproject.toml`
   - `src/abstractgateway/__init__.py`
   - `src/abstractgateway/app.py` (FastAPI version string)
3. Run `pytest`.
4. Build artifacts (optional): `python -m build`

## Related docs

- Package overview + quickstart: [README.md](./README.md)
- Docs index: [docs/README.md](./docs/README.md)
- Getting started: [docs/getting-started.md](./docs/getting-started.md)
