# 0937-abstractgateway: [FEATURE] A session's title written by the model after its first answer

> Package: abstractgateway
> Type: feature
> Created: 2026-10-26
> Priority: P3 (nice to have, not required now)
> Labels: sessions, titles, clients, llm

## Summary

Give every session a short title written by a model once its first turn has completed, stored
where the title lives today (the session's `title` tag), so every client (desktop, iPhone, Watch,
web) shows a meaningful name instead of the first words of the first message. Clients change
nothing: they already display the tag.

## Why

Operator, 2026-10-26, from the Apple Watch review: the Sessions list on the wrist shows each
conversation as its opening sentence, truncated; the desktop picker falls back to
`yy/mm/dd - first query`. A title such as "Survival analysis by treatment arm" reads at a glance;
"Compare overall survival by treatment arm in the ADTT…" does not. The assistant's own backlog
item 0006 (durable session topics and summaries) asks for canonical topic metadata from the
gateway for the same reason; this item is its smallest useful piece.

## Current code reality

- `src/abstractgateway/routes/gateway.py`: a session's title is a tag; at session creation the
  title is the request's `title` or `_derive_title(messages)` (the first message's words), and
  later reads take `tags.get("title")`. Nothing rewrites it after the first answer.
- The AbstractAssistant desktop deliberately disabled its own background title generation in
  gateway mode (`update_active_session_title_async`, assistant backlog 0006): a title written by
  one client would differ from the others' and would be lost to them.
- The iPhone and Watch clients read the session title from the gateway and rename through it;
  they derive nothing locally.

## Proposed direction

- When a session's first root run completes with a written answer (the same place the session
  tags are updated at the end of a turn), start one small `llm_call` with the first prompt and
  the first answer: "a title of at most six words, no punctuation, in the person's language".
  Write it to the `title` tag only if the tag still holds the derived default (a title the person
  or a client set is never overwritten).
- Use the session's own provider and model (or the gateway's default text route) with a hard
  cap on tokens; a failure leaves the derived title and logs once. No retries, no queue: a
  missed title is cosmetic.
- Expose nothing new: the tag is the contract. Optionally re-title after the fifth turn when the
  subject has moved on, behind the same "still the default" rule.

## Non-goals

No summaries, no topics, no per-turn metadata (assistant 0006 keeps those). No client-side
generation. No renaming of sessions the person already named.

## Promotion criteria

- The operator asks for it, or assistant 0006 is promoted and needs it as its first slice.

## Validation ideas

- A test starting a session, completing one turn against a fake provider, and reading the tag.
- A test proving a person-set title survives the first completed turn.
- The Watch's Sessions list and the desktop picker, read live, show the written titles.
