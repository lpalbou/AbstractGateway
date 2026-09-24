# Acknowledgments

AbstractGateway stands on the shoulders of many open-source projects and contributors.

This list is **non-exhaustive**. The canonical dependency list for this package is in `pyproject.toml`.

## Core dependencies

- **AbstractRuntime**: durable run model, workflow registry, file/SQLite stores, and runtime tick loop.
- **AbstractCore** (through AbstractRuntime): providers, tools, media capabilities, the model catalog, engine detection and host jobs.
- **AbstractAgent**: Visual Agent nodes in bundle mode.
- **AbstractMemory** + **LanceDB**: `memory_kg_*` nodes in bundle mode (knowledge graph storage).
- **FastAPI** (via **Starlette**) + **Pydantic**: HTTP API surface and request/response models.
- **Uvicorn**: ASGI server used by `abstractgateway serve`.
- **python-multipart**: multipart upload support for bundle/attachment endpoints.
- **PyYAML**: entity seed documents.

## Optional integrations (feature-dependent)

- **pystray** + **Pillow** (`abstractgateway[tray]`): the desktop tray icon.
- **Node.js** (installed by the gateway from the `nodejs-wheel-binaries` build when needed) and the **npm** registry: the browser apps.
- **AbstractFlow**: workflow authoring and bundling (Gateway runs `.flow` bundles without depending on it).
- **TDLib**: Telegram Secret Chats support when using the TDLib transport.

## Dev/test tooling

- **pytest** and **httpx**: test suite and HTTP client utilities used under `tests/`.
- **hatchling**: Python packaging/build backend.

## Contributors

Thank you to everyone who reports issues, improves documentation, and contributes code.
