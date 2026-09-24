# Contributing

Thanks for your interest in improving AbstractGateway.

This repo is a Python package (`src/` layout) with a FastAPI server, a durable runner worker, and contract tests under `tests/`.

## Quick start (dev)

```bash
python -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
pip install -e ".[dev]"
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

### Tests never touch your home or the network

`tests/conftest.py` makes every run hermetic, whatever your shell exports:

- **Home and caches.** `HOME` (and `USERPROFILE` on Windows) points at a
  temporary directory for the whole session and a fresh one for each test, so
  everything the code derives from the home directory lands there: the
  AbstractCore config, models, embeddings and blocs under `~/.abstractcore`,
  the Hugging Face cache (`HF_HOME`, `HF_HUB_CACHE`), the data registry, and the gateway's own data, flows and AbstractCore config store (per-test directories).
  Path settings exported in your shell (the `ABSTRACT*`/`HF_*` directory, file
  and cache variables, `XDG_*`) are cleared for the run. This happens when the
  conftest is imported, before any package or `huggingface_hub` loads; a test
  fails loudly if `huggingface_hub` froze its cache path on your real home.
- **Network guard.** Sockets refuse any non-loopback destination and name
  lookup, and also the live local services on loopback: the gateway (8080), LM
  Studio (1234), Ollama (11434) and 18850. Any other loopback port stays open,
  so `TestClient`, fake servers and scratch-port fixtures work. A refused
  attempt fails the test and is listed under "network guard" at the end of the
  run with the host and port it tried to reach. Point such a test at a fake or
  a scratch port; the `fake_public_dns` fixture answers name lookups for code
  that resolves a host before a faked fetch.
- **Subprocess guard.** A child process has its own sockets, so a real engine
  CLI would slip past the network guard. Launching `lms`, `ollama`, `open` or
  `xdg-open` (through `subprocess`, `asyncio` subprocesses or `os.system`,
  including `sh -c "…"` and `env …` forms) is refused, fails the test, and is
  listed under "subprocess guard" at the end of the run. Fake the CLI instead:
  record the argv in a double, or register a stand-in script with the
  `fake_cli` fixture (`fake_cli("lms", "#!/bin/sh\necho ok\n")` returns its
  path; only that file may run).
- **Opting out, with a reason.** `@pytest.mark.desktop("reason")` marks a test
  that drives the real engine CLIs or the desktop; it is skipped unless you
  run `pytest --allow-desktop` (a `network` test may launch them too). `@pytest.mark.network("reason")` marks a test
  that genuinely needs the network (a Hub lookup, a real download, a live
  provider). Such tests are skipped unless you run `pytest --allow-network`.
  `@pytest.mark.real_home("reason")` marks a test that READS your real home
  (for example installed tokenizers); `HOME` still stays temporary, and the
  test gets the real path as `ABSTRACT_TEST_REAL_HOME`. A marker without its
  reason is a collection error, as is a test module that reads the real-home
  path without the marker. `pytest --markers` lists all three.

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
