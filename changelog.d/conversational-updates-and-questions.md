### Added

- Task starts, Flow entity summons and canonical entity visits can opt into public actor updates and conversational questions with a strict `conversation` boolean.
- Canonical visit state exposes safe public updates, durable accepted human messages, replayed question/answer/timeout/retained-reply history and the actual pending question; owned `/guidance` and `/answer` routes preserve participant attribution and the visit recall boundary without bypassing tool approvals. Canonical timestamps/order and explicit omission counts support chronological history across reconnects.
- Explicit original human display text on ordinary guidance is projected into replay with server-stamped provenance and an acceptance status, separate from actor adoption.
- The existing runner wakes timed clarification questions through Runtime's durable deadline indexes; essential questions remain indefinite and tool approvals never expire into permission.
- The package agent skill documents these unreleased conversation routes and question semantics.
