### Changed

- **The gateway's sleeps run the runtime's night** (AbstractRuntime `build_consolidator`, 539244d `embedder=`). The
  operator's sleep (`POST /entities/{name}/state` asleep with the dream pass) and the idle sweep's sleep called
  memory's `sleep_pass` alone, so they skipped the night's metadata repair, the topics stage and the night voice.
  They now run the same night the own-time loop runs: memory's sleep pass, then the repair of titles and summaries,
  then his topics (`topics.sleep_communities`: what has no vector is embedded, then memory forms the night's version
  of his communities), then the night voice. It runs under the pass's lifecycle fence (a Stop ends it at the next
  step, nothing is written after it), with the home's own embedder (memory refuses a rewrite of a vectored record
  without it), the entity's summaries capability and mind clients, and the night's lines in the home's
  `own_time.log`. The sleep's result is the runtime's night (`formed`, `dream_record_id`, `authoring`,
  `communities`, `engine`: memory's own report); `abstractgateway entity state ... --dream` says the dream and the
  topics version.
