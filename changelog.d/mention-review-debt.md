### Fixed

- **Mentions, the 2026-11-09 review debt** (REVIEW-1 items 5, 6, 11, 12; mentions-review probes B to M).
  - **In a visit lane, the mentioned entity reads that visit's exchange.** It gets the visitors' words, taken from the
    door's own records and never from the rendered prompt with its memories, plus the visited entity's answers.
    Before, it read nothing.
  - **A reused mention visit is sent only the turns newer than its last mention.** The same conversation was quoted
    into the same visit again and again.
  - **A turn too long to quote is skipped on its own, with a note.** The newest whole turns that fit are still quoted;
    before, one long turn meant nothing was quoted. `context.skipped_oversize` counts the skipped turns, and quoted
    turns name their attachments ("(attached: contract.pdf)").
  - **`mention_id`** (optional): a resend with the same id returns the first answer and never asks a second one.
  - **A mirrored turn reports its own new answer, or a plain error.** It never repeats the visited entity's previous
    reply. `POST /visit/{run_id}/turn` answers `answered: true|false`.
  - **503 `entity_cannot_answer` is a plain sentence** ("Castor cannot answer right now. Try again in a moment.").
    The provider's error and model list go to the log only.
  - **Every mention refusal has a code and a sentence, and always carries the caller's own `session_id`.** An
    uncoded busy refusal from the visit door ("a visit is opening… retry shortly") is `in_another_conversation`.
  - **`repeat_of` is accepted only for the caller's own turn of this conversation carrying the same words.**
    Anything else is 422 `repeat_of_unknown`.
  - **A reused visit closed by its idle close between the check and the turn is replaced by a fresh visit, once.**
    Before, the caller got a passed-through 409.
  - **Per-conversation mention locks are reference-counted and removed when unused.** The map used to grow forever.
  - **`POST /entities/{name}/summon` drops a client's `_meta.repeat_of` and `_meta.mention`.** These keys are written
    only by the mention route, after validation (review finding 3, summon half; the `/runs/start` rule of 0093590f).

  Not fixed here: a model failure during a mirrored turn still ends the visited entity's visit run, because a failed
  turn fails the visit workflow in AbstractRuntime. That needs the runtime to park a failed turn instead.
