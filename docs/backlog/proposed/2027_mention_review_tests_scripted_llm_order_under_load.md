# 2027 — test_mention_review_fixes: the scripted model answers land on the wrong call under load (two tests answer 503)

> Package: AbstractGateway (tests/test_mention_review_fixes.py, the _install scripted LLM; the mirrored-turn concurrency it exercises)
> Type: test
> Status: proposed
> Created: 2026-11-11
> Priority: P2
> Labels: tests, flaky, mentions, load

## Summary

The 0.21.0 release gate on cb7847b5 (2026-11-11 ~01:30, machine load 25–40 from three concurrent gates) failed two tests
of `tests/test_mention_review_fixes.py` in the Python 3.10 shard 2/4 with the mention door's 503 "cannot answer right
now" (`entity_cannot_answer`, the provider-error path): `test_a_failed_mirrored_turn_is_answered_and_the_visit_stays_open`
and `test_a_stop_after_the_answer_carries_nothing_into_another_visit`. The whole file passes 16/16 alone in the gate's
own 3.10 environment, and passed in every other shard and Python of the same gate. Both tests script the model with an
ordered list of answers that includes a `RuntimeError("provider exploded")` for one specific call; under load the
mirrored turns' concurrent calls consume the scripted answers in another order, so the exploding answer lands on a
different call and the first mention is refused with 503. The scripted-order dependence is the defect, not the door.

## Scope

Script the fake model by WHICH call it answers (the entity name in the prompt, or the turn id), not by call order; or
serialise the mirrored turns in the test; keep the behaviour under test (a failed mirrored turn is answered and the visit
stays open; a stop after the answer carries nothing into another visit). Until then the gate's per-job rerun of the shard
is the evidence (ADR 0050: a flake is fixed or quarantined with an item; this is the item).

## Validation

The file green 20 times in a row under a simulated load (two parallel full-suite runs) on 3.10 and 3.13.
