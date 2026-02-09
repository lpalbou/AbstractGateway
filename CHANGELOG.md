# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-02-09

### Changed

- Dependency bumps (see `pyproject.toml`):
  - `AbstractRuntime>=0.4.2` (and `AbstractRuntime[abstractcore]>=0.4.2` for HTTP/voice/telegram/all extras)
  - `abstractagent>=0.3.1`, `abstractvoice>=0.6.3`, `abstractflow>=0.3.7`
  - `abstractcore[media,tools]>=2.11.8` (via `abstractgateway[all]`)
- Documentation refresh for external users:
  - added explicit AbstractFramework ecosystem context
  - updated minimum versions in install snippets to match `pyproject.toml`
  - kept the architecture diagram as the canonical “shape of the system”
- Version metadata alignment:
  - `pyproject.toml`, `src/abstractgateway/__init__.py`, and `src/abstractgateway/app.py` now agree on `0.2.1`

## [0.1.1] - 2026-02-04

### Changed

- Documentation refresh for external users:
  - new FAQ (`docs/faq.md`)
  - clarified quickstart + smoke checks in `README.md`
  - tightened getting started, configuration, security, and API overview docs
  - improved cross-linking in `CONTRIBUTING.md` and `SECURITY.md`
  - refreshed `llms.txt` / `llms-full.txt` for agent ingestion (index + full snapshot)
- Version bump to reflect the documentation release (`0.1.0` → `0.1.1`).

### Notes

- No intentional runtime behavior changes in this release; it is documentation-focused.

## [0.1.0] - 2026-02-03

### Added

- Initial public package for AbstractGateway (`abstractgateway`).
