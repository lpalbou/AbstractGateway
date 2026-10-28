# 0955-gateway: [BUG] A TTS model that is not downloaded answers HTTP 500 instead of a typed "download it" status

> Package: abstractgateway (routes, voice catalog); abstractuic (shared voice picker state); abstractruntime (retry classification, noted)
> Type: bug
> Status: planned (not implemented)
> Created: 2026-10-28
> Priority: P1
> Labels: voice, tts, first-run, error-contract, thin-clients
> Root link: AbstractFramework release-closing wave 2026-10-28, defect D5 (E2E on released gateway 0.17.0)

## Summary

On a fresh install the default TTS route (`supertonic` / `supertonic-3`, from AbstractCore's
`capability_defaults.py`) has its runtime (onnxruntime) but not its model files. `POST
/api/gateway/runs/{run_id}/voice/tts` then answers **HTTP 500** with AbstractVoice's CLI hint as the detail
("Supertonic 3 artifacts are not available locally. Run: python -m abstractvoice download --supertonic"),
after about 3 s of retries. The console and the apps show a raw server error. The route must answer a typed
"model not downloaded" status naming the model and the download action the gateway already serves.

## Evidence

E2E run on released gateway 0.17.0, fresh HOME and data dir (framework root
`untracked/release-closing/e2e/`, gitignored):

- `logs/tts-default.headers`: `HTTP/1.1 500 Internal Server Error`, `x-gateway-duration-ms: 3074.1`.
- `tts-default.out`: `{"detail":"TTS child run did not complete successfully: Effect failed after 3 attempts:
  Supertonic 3 artifacts are not available locally.\nRun: python -m abstractvoice download --supertonic\nOr in
  the REPL: /tts_download supertonic"}`.
- `logs/gateway-run1.log` l.15-23: three `LLM_CALL failed | error=Supertonic 3 artifacts are not available
  locally` lines (the effect is a voice synthesis; the retry policy retries a deterministic missing-file error).

## Current code reality

- `src/abstractgateway/routes/gateway.py` `voice_tts` (l.12756): the synthesis child ends `FAILED`, then
  l.12923 calls `_gateway_completed_child_result(child, operation="TTS")`.
- `_gateway_completed_child_result` (l.14211): any non-`COMPLETED` child raises `HTTPException(status_code=500,
  ...)` at **l.14213-14216** with the child's error string. This is where the error escapes. Nothing tells a
  missing model from a crash.
- `voice_tts_stream` (l.12951): `setup_error` at l.13071 maps every setup failure to 500 the same way.
- The voice catalog (`/voice/voices`, l.17355-17366) lists Supertonic when `_voice_engine_installed("supertonic",
  "tts")` holds: runtime installed, model presence not checked. The picker offers a voice that cannot speak yet.
- The download action exists: `POST /api/gateway/models/download` (l.27383) with `{"provider": "supertonic",
  "artifact": "supertonic-3"}`, the same pair `capability_defaults.py` records as the route's `download`. The
  console's Models catalog already shows downloaded / not downloaded (`console_catalog.py`).
- AbstractVoice knows the state before synthesis: `SupertonicTTSAdapter.is_available()` /
  `get_unavailable_reason()` (`abstractvoice/adapters/tts_supertonic.py` l.130-150, `is_supertonic_cached`).

## Operator decision (2026-10-28, relayed by the release-closing lead)

NO silent automatic download, and NO 500. At first use with no weights the TTS route answers a clear,
actionable sentence (what is missing, its size, the one command), and the console's / apps' voice picker
offers a "Download the default voice (xx MB)" action that runs the documented download with progress, only
after the person's click. This item carries that rule; 0959 ("the default voice downloads at first use",
filed on the open branch `docs/backlog-linux-server-findings`) is merged into it and closes as merged when
that branch lands. Concretely: the typed answer is 409 `model_not_downloaded` with the model id, its size in
MB, the command, and the download route (shape below plus `size_mb` and `command`); the console's voice
picker action reuses the console's model-download action; the shared kit picker's action is a separate
abstractuic item (proposed, contract: this 409 shape). Not in 0.17.2 (that release ships the app-matrix,
network-status and engine-card fixes); next gateway batch.

## Scope

- In: before starting the child (or on its failure), detect "engine installed, model not downloaded" for the
  resolved TTS provider/model and answer **409** (the gateway's existing "refused, fix the state first" code,
  e.g. `routes/core_endpoint.py`) with a typed body:
  `{"detail": {"reason_code": "model_not_downloaded", "message": "<plain sentence>", "provider": "supertonic",
  "model": "supertonic-3", "download": {"method": "POST", "path": "/api/gateway/models/download", "body":
  {"provider": "supertonic", "artifact": "supertonic-3"}}}}`. No retries, no 3 s wait.
- Same typed answer on `voice_tts_stream` (before the stream opens); the entity routes
  (`routes/entities.py` l.5451-5478) delegate to `voice_tts` / `voice_tts_stream` and inherit it (test one).
- The voice catalog marks each TTS model `downloaded: true|false` so a picker can show the state before a click.
- Generic: keyed on the engine's availability check, not on Supertonic's message text, so Qwen3-TTS and other
  local engines get the same answer.
- Out: downloading automatically on first use (the operator decides; a download can be gigabytes).

## Dependencies

- abstractuic (separate item there): the shared voice picker (`ui-kit` voice settings) shows "Not downloaded,
  Download" from `downloaded: false` or a `model_not_downloaded` answer and calls the download route; apps adopt
  by pin bump, never natively.
- abstractruntime (noted, not required): do not retry a deterministic availability error, and label the effect
  as a voice synthesis rather than `LLM_CALL` in the log.

## Validation

- Test (`tests/`): a fake voice engine reporting "installed, not downloaded"; `POST /runs/{id}/voice/tts`
  answers 409 with `reason_code == "model_not_downloaded"`, the provider/model ids and the download path/body;
  the streamed route answers the same before streaming; the catalog row reads `downloaded: false`.
- Red check: remove the typed branch and the same test fails on the 500 (assert the status and `reason_code`,
  not merely "not 200").
- A real crash in synthesis still answers 500 (the typed answer is not a catch-all).
- Real E2E on a fresh HOME: the console Test button and Code's read-aloud show the download prompt; after
  `POST /models/download` completes, the same request answers 200 with audio.
