# Acknowledgments

AbstractGateway stands on the shoulders of many open-source projects and contributors.

This list is **non-exhaustive**. The canonical dependency list for this package is in `pyproject.toml`.

## Core dependencies

- **AbstractRuntime**: durable run model, workflow registry, file/SQLite stores, and runtime tick loop.

## Optional integrations (feature-dependent)

These are not required for the base gateway, but are used by optional modes/features:

- **FastAPI** (via **Starlette**) + **Pydantic**: HTTP API surface and request/response models (`abstractgateway[http]`).
- **Uvicorn**: ASGI server used by `abstractgateway serve` (`abstractgateway[http]`).
- **python-multipart**: multipart upload support for bundle/attachment endpoints (`abstractgateway[http]`).
- **AbstractFlow**: VisualFlow JSON directory mode and workflow authoring/bundling workflows (see `abstractgateway[visualflow]`).
- **AbstractCore** integration (via `abstractruntime[abstractcore]`): LLM/tool execution wiring, embeddings client, Telegram TDLib wrapper.
- **AbstractAgent**: Visual Agent nodes in bundle mode.
- **AbstractMemory** + **LanceDB**: `memory_kg_*` nodes in bundle mode (knowledge graph storage).
- **TDLib**: Telegram Secret Chats support when using the TDLib transport.

## Dev/test tooling

- **pytest** and **httpx**: test suite and HTTP client utilities used under `tests/`.
- **hatchling**: Python packaging/build backend.

## Contributors

Thank you to everyone who reports issues, improves documentation, and contributes code.
