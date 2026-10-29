### Added

- **Device: GPU / CPU for the text embedding route** (operator ruling 2026-10-29: the embedding model's device is the
  person's choice). The route editor of `embedding.*` routes has a Device control ("GPU when there is room (default)",
  GPU, CPU) stored as the route's `options.device`, and says where the loaded embedder runs now and what it measured on
  the GPU ("Now on the GPU (1.2 GB of GPU memory, measured).", or why it is on the CPU). The gateway passes the setting to
  AbstractCore's embedding manager (`device=`).
