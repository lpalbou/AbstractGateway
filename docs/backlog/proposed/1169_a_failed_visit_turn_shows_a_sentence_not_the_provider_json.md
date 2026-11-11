# 1169 — A failed visit turn shows a sentence in the bubble, never the provider's raw JSON

> Package: AbstractGateway (the visit turn's failure text), AbstractRuntime (turn_error on the turn), AbstractEntity / kit (the bubble: sentence + provider text on hover / in the ledger)
> Type: enhancement
> Status: proposed
> Created: 2026-11-11
> Priority: P2
> Labels: entities, wording, errors

## Summary

Operator screenshot 2026-11-11 02:47 (a visit): the entity's reply bubble read "[this turn could not be answered: Effect
failed after 1 attempts: … OpenAI-compatible server API error (400): {\"error\": \"Engine protocol predict stream
returned an error: {\\"code\\":500,\\"message\\":\\"got exception: speculative batch index 8 is not inside the
current sub-batch [0, 8)\\"…". The failure itself was the provider's (LM Studio's llama.cpp engine failing inside its
own speculative decoding; nothing of ours sends a draft or speculative parameter): the operator disables speculative
decoding for that model in LM Studio. What is ours: the raw provider JSON lands verbatim in the entity's bubble. House
rule (end-user copy): one sentence, "Aletheia could not answer: her model's server failed.", with the provider's text on
hover and in the ledger.

## Scope

The gateway serves `turn_error` as {sentence, provider_text, code}; the bubble shows the sentence; the ledger row and the
hover carry the provider text whole (never truncated).

## Validation

A scripted provider 400/500 during a visit turn: the bubble shows the sentence; the ledger holds the full text.
