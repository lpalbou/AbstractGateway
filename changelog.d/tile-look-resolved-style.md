### Changed

- `GET /entities/{name}/tile` serves `look.style` as the design DRAWN (a retired avatar id resolves to its replacement, an unknown id to the catalog default, as the identity route does) and adds `look.style_requested`, the id as stored.
