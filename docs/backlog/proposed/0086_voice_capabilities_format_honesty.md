# 0086: Voice capabilities format honesty (stream is wav-only)

> Package: abstractgateway
> Type: bug
> Created: 2026-07-15
> Priority: P3
> Labels: seat-gateway, voice, discovery-honesty

## Summary

The assistant seat's TTS-latency investigation (dm:assistant--gateway seq 2,
2026-07-15) found a discovery-honesty gap: the gateway's voice capabilities
advertise `formats: ["wav", "mp3"]` while the TTS STREAM endpoint accepts
wav only (422 otherwise). A client composing `formats` with
`preferred_delivery_mode` can be misled into requesting an mp3 stream that
the endpoint refuses.

## Direction

Either serve per-delivery-mode format lists (stream: wav; artifact:
wav/mp3) or clamp the advertised set to what every delivery mode accepts —
the capabilities payload must never advertise a combination a route
refuses. Test-pin the composition (formats × delivery modes all succeed).

Related (abstractvoice scope, recorded in the same DM, not this card):
first-chunk sizing ignores max_chars in `_find_cut_index`; no request
parameter for segment sizing; strictly serial segment synthesis under a
per-VoiceManager lock serializes concurrent TTS.

## Receipts

- Origin: assistant DM 2026-07-15 (root cause was client-side; these were
  the server-side observations; :8080 endpoints measured healthy live)
