### Changed

- **Pause is a kill switch: while the gateway is paused, nothing moves forward** (`host_control.py`). Pause used
  to stop the workflow runner only: a paused gateway still ran the entity sweeps (an idle sleep embedded memories
  and wrote titles, summaries and briefs, which loaded the embedding model and the summaries model again after
  they were unloaded), served generations on `/v1` and the gateway API, polled its bridges and mailboxes, and left
  entities' own-time loops running. Now, while paused:
  - no model work leaves the gateway: the model host proxy refuses every generation, embedding, speech stream,
    prompt-cache operation and model load (`GatewayPaused`: `409` `gateway_paused`; `503` with the OpenAI error
    envelope on `POST /v1/...`), and so do the two calls that go straight to an endpoint (the remote embeddings
    route, the triage LLM assist). Reading what is loaded and unloading it stay available;
  - the entity sweeps (repair, need-check, idle sleep, authoring), the queue sweeper, the Telegram and Agora
    bridges, the mail and feed watchers, the push dispatcher and the backlog runner wait for Resume;
  - a visit, chat turn, summon, meet, speech or re-embed is refused at its door before anything is written, and
    so is a sleep with its maintenance pass;
  - an entity's own-time loop freezes at its next boundary and continues the same day at Resume (it reads the
    pause file the gateway names in `ABSTRACTRUNTIME_HOST_PAUSE_FILE`; needs the AbstractRuntime that has
    `identity.life.host_pause`, and with an older one the loop is not started while paused);
  - a client built in the model host loads no model (an embeddings client, whose build loads its model, is not
    built until Resume), and **Keep loaded** and a new default model are recorded and applied at Resume.

  Work that was in flight is not failed by a pause: a workflow step, or a pass a sweep started, waits at its
  next model call and continues at Resume. A model call already running when the pause lands finishes.

### Fixed

- **An idle entity with nothing new is no longer put to sleep every 24 hours** (`entity_idle_sleep.sleep_due`).
  The hours threshold fired on the clock alone, so an entity nobody visited and nobody gave time to slept once a
  day forever, each night embedding, dreaming and writing a brief over the same memories. The clock now counts
  only when at least one memory formed since his last completed sleep (as the fragmented-share threshold already
  required). His first sleep still comes `sleep.after_hours` after his birth, because what his birth formed has
  never been consolidated.
