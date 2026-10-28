# 0959-gateway: [FEATURE] The default voice downloads at first use, with a clear message, instead of answering 500

> Package: abstractgateway (voice routes, model downloads); abstractvoice (availability check, seam)
> Type: feature
> Status: planned (not implemented); needs an operator decision against 0955's "Out" line
> Created: 2026-10-28
> Priority: P1
> Labels: voice, tts, first-run
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (framework root `untracked/release-closing/ovh-test.log`, step 4c)

## Summary

"Every profile gets local voice", but on a fresh install the default TTS route (`supertonic / supertonic-3`)
answers HTTP 500 until its weights are fetched by hand (`abstractgateway models download supertonic
supertonic-3`, 33 s on the test server). 0955 makes that a typed 409 with the download action and keeps
automatic download out of scope. This item proposes the next step for the DEFAULT voice only: the first
synthesis starts the download itself and answers a clear "downloading the default voice (N MB), try again in
about a minute" status, never a 500.

## Evidence

`ovh-test.log` l.102-110: `/voice/tts` -> 500 "Supertonic 3 artifacts are not available locally. Run: python -m
abstractvoice download --supertonic"; after `abstractgateway models download supertonic supertonic-3`, 200.

## Current code reality

- 0955 (planned) covers the typed 409 `model_not_downloaded` answer and lists "downloading automatically on first
  use" as Out ("the operator decides; a download can be gigabytes").
- The download action exists: `POST /api/gateway/models/download` with `{"provider": "supertonic", "artifact":
  "supertonic-3"}`; AbstractVoice reports the state before synthesis (`SupertonicTTSAdapter.get_unavailable_reason`).

## Scope

- In: for the route the gateway itself seeded as the default voice, and only below a size bound, the first
  request starts the download (deduplicated) and answers 202/409 with `reason_code: "model_downloading"`,
  the size and progress path; later requests answer the same until it completes, then synthesize.
- In: an installer/first-run alternative the operator may prefer: download the default voice at the end of the
  install (the framework installer now prints the command).
- Out: any non-default or large model (0955's typed answer stands for those).

## Validation

- Test: a fake engine "installed, not downloaded" plus a fake downloader: the first request starts exactly one
  download and answers `model_downloading`; after completion, 200. Red check: remove the trigger and the test
  sees 0955's `model_not_downloaded` instead.
- Real: a fresh HOME, the console's voice Test button speaks after one wait, with no command typed.
