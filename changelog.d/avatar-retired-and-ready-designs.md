### Changed

- **Identity looks follow the avatar catalog's retired and ready designs** (abstractavatar 0.2.0,
  `entity_identity.py`). A stored or requested design the catalog retired is accepted and drawn as its
  replacement (`eye` as `watch-well`, the retired robots as `watch-hover`, `watch-shape-wave` as
  `watch-wave`, the others as `watch-halo`): never refused, never drawn blank. The record keeps the id it
  was given; identity payloads (`GET /entities/{name}/identity`, the list rows, `GET /sessions/{sid}/identity`)
  serve the drawn design as `avatar.style`, the stored id as `avatar.style_requested`, and the drawn
  design's `watch_code`. An unknown design is refused with the ready designs only, never the experimental
  ones (still accepted by id). The legacy appearance shapes write ready designs: `wave` -> `watch-wave`,
  `kitty` and `face` -> `watch-halo` (read back as `planet`); `planet` over a shape design (`watch-wave`
  reads as `wave`) writes the entity's default orb when it is ready, else `watch-halo`. A missing or
  incompatible abstractavatar native library answers 503 on identity sets and reads instead of a 500. A gateway with an abstractavatar older than
  0.2.0 answers 503 naming the version instead of mis-resolving.

### Fixed

- **Setting the `kitty` or `face` appearance shape no longer fails** after abstractavatar 0.2.0 removed
  the `watch-shape-*` designs (`PUT /entities/{name}/appearance`, `appearance` on `POST /entities`).
