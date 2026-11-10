### Changed

- **The line the conversation's agent gets when a message mentions entities is imperative and last** (Castor E2E
  2026-11-10: the agent read the softer line and still answered for Castor after a long workspace search). It is
  now "The mentioned entities (Castor) answer this message themselves. Unless the user addressed you directly,
  reply with an empty message.", the last system instruction before the person's words. The mentions are the
  composer's chips, never read from the text. A real model may still answer: whether the agent takes part at all
  is an operator decision still pending (an opt-in agent).
