# 1010-abstractgateway: [IMPROVEMENT] Expose typed clone failures and explicit segment retries

> Created: 2026-11-06
> Type: improvement
> Priority: P2
> Labels: voice, streaming, contracts
> Status: proposed

- Work-item id: abstractgateway-1010
- Owner: framework (intake; implementation belongs to Gateway with Runtime/Voice)
- Thread: Unavailable; agora connection refused at intake.
- Hub refs: None; mirror registration pending hub availability.

## Summary

Expose package-owned clone validation/retry policy and structured failure details
through the Runtime facade and Gateway contract. Let clients explicitly retry
the failed segment/remainder using the same voice, without parsing exception
prose, regenerating accepted segments, or silently switching to a default voice.

## Context and current code reality

- Incident 2026-11-05: Qwen clone scores 0.945/0.941 failed threshold 0.950;
  Assistant then regenerated a default voice. Assistant `270f37f` removed its
  automatic selected-voice fallback. The engine already retries the clone once.
- `src/abstractgateway/routes/gateway.py::VoiceTTSRequest` has explicit
  voice/profile/provider/model and request_id, but no clone validation/retry policy.
- `src/abstractgateway/voice_stream.py` emits terminal exceptions as an `error`
  string. The engine's `ClonedVoiceNotAppliedError` has richer segment, score,
  threshold and partial-delivery fields; preserve them through the owning facade
  rather than matching class-name strings in clients.
- AbstractVoice ADR 0009 and completed item 0066 already supply segment offsets
  and `segment_end`; `tests/test_voice_stream_segments.py` pins forwarding.
  Existing boundary resume must be reused, not replaced by a duplicate protocol.
- Partial-segment resume is approximate under ADR 0009. Do not promise exact
  recovery inside already-delivered generated audio or imply rejected audio is cached.

## Proposed direction and scope

1. Coordinate AbstractVoice `0067_explicit_clone_validation_and_segment_retry_policy.md`
   first: Voice owns validation modes, retry budgets and identity semantics.
2. Extend Core/plugin and Runtime facade contracts as required, then forward
   capabilities/options through Gateway. Advertise unsupported options honestly.
3. Define versioned error fields: stable code, failed segment/text range,
   last completed boundary, partial-delivery state, effective selectors/policy,
   attempt count and diagnostic scores/threshold. Keep human-readable prose.
4. Define explicit retry with unchanged selectors and original-text provenance.
   Prefer existing boundary/remainder requests where sufficient; introduce a
   server retry handle only if evidence requires it. If a handle is used, specify
   ownership, expiry, cancellation and idempotency. A failed idempotency key must
   not be reused as a fresh retry, and duplicate retry submissions must not bill twice.
5. Specify partial playback honestly: retry an undelivered segment without replaying
   accepted predecessors; do not automatically replay an already-heard segment.

## Non-goals

- No implementation now: operator explicitly deferred Gateway-and-below changes.
- No Gateway speaker scoring, threshold defaults, direct AbstractVoice imports,
  automatic default-voice generation, or new global environment toggles.
- No promise that discarded candidate audio can be recovered. Candidate retention
  is a separate design only if Voice's eventual policy needs it.

## Dependencies and ADR status

- [AbstractVoice proposed 0067](../../../../abstractvoice/docs/backlog/proposed/0067_explicit_clone_validation_and_segment_retry_policy.md);
  existing Voice ADRs 0006/0007/0009 and completed 0066.
- Gateway ADR 0001: all lower-level access through Runtime facades; ADR 0002:
  inference stays outside the Gateway process. Ship after required facade support.
- ADR impact: retain those ownership rules; document the new retry/idempotency
  contract in an appropriate owning ADR before completion. This proposal does not
  establish a new durable policy or authorize implementation.

## Acceptance Criteria

- [ ] Clients can identify clone-check rejection and its retry boundary from typed
  fields without parsing exception names or English messages.
- [ ] Requested policy is forwarded unchanged or explicitly refused if unsupported;
  omission has documented backwards-compatible behavior.
- [ ] Explicit retry preserves voice identity, avoids accepted-prefix regeneration,
  and has tested idempotency, cancellation and partial-delivery semantics.
- [ ] Stream failures and nonstream failures share compatible error codes; older
  clients can still consume human-readable errors and existing segment events.
- [ ] End-to-end evidence shows the requested clone on retry, generation counts,
  preserved completed segments and measured latency; no default-voice fallback.

## Testing

- `python -m pytest tests/test_voice_stream_segments.py tests/test_voice_audio_api_contract.py tests/test_voice_stream_keepalive.py tests/test_gateway_import_boundary.py -q`
- Add contract tests for opening rejection, later-segment rejection, partial
  delivery, unsupported policy, duplicate retry, cancellation and account isolation.
- Run one real Gateway/Voice rejection-and-explicit-retry scenario after Voice 0067;
  report boundaries and synthesis counts, not just an HTTP success status.

## Promotion criteria and expected outcome

Promote when the operator schedules lower-level work and Voice/Runtime owners
agree the package policy and typed contract. Expected outcome: thin clients can
offer an honest, optional same-voice retry with bounded compute and no hidden
whole-reply regeneration. No engine or Gateway behavior changed at intake.
