### Added

- **Every memory is formed with its authored title and summary** (AbstractRuntime 4eefddb and bb6d17a, operator
  rulings 2026-11-09):
  - In a durable visit, the formation step's authoring call (an `LLM_CALL` tagged `capability: summaries`) goes to the
    entity's summaries model, never its mind. `open_entity_runtime(capability_handlers=)` is wired for it.
  - The LLM_CALL handlers forward `response_schema` / `response_schema_name` as structured output, and the parsed
    object comes back in `result.data`. A schema the converter refuses is a failed call, never an unstructured answer.
  - A hosted chat session knows its capability models (`ChatSession.llm_for_capability`). Without them every turn
    formed pending records.
  - When a durable visit closes on a home with no own time running, the titles and summaries a failed authoring call
    left pending are authored. This runs beside the close, bounded by `rebuild.cap_per_turn`, under the home's writer
    lease taken at once or not at all.
  - The own-time process receives the private resolution of every capability model on a provider connection
    (`spawn_loop_process(capability_endpoint_profiles=)`), so its summaries client works there too.

### Fixed

- **A message's numbers stay the mind's.** The visit turn stats (`stats.input_tokens` / `output_tokens`) leave out the
  summaries model's authoring call. Before, its unreported usage made the whole turn's numbers unknown.
