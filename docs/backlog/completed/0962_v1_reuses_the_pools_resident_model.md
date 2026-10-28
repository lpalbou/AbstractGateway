# 0962-gateway: [BUG] `/v1` loads a second copy of a model the runtime pool already holds

> Package: abstractgateway
> Type: bug
> Status: fixed on branch release/0.17.3, NOT released (paused 2026-10-28; target 0.17.3)
> Created: 2026-10-28
> Priority: P1
> Labels: openai-api, v1, models, residency, llama.cpp
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (framework root `untracked/release-closing/QA.md` item 4); left open by [0961](0961_fresh_linux_fixes_app_matrix_and_auth_status.md)
> Fixed in: AbstractCore 2.31.1 (Core backlog 0859, `docs/backlog/completed/0859_v1_chat_reuses_the_resident_provider.md`)
> Number: 0962

## Summary

On the fresh Linux install, `/v1/chat/completions` answered 500 "Failed to create llama_context": with a
2.5 GB GGUF loaded through the gateway's model pool (12.3 GB of the 16 GB GPU used), each `/v1` request built a
SECOND llama.cpp instance of the same model. The gateway serves `/v1` in-process through Core's chat route
(`core_endpoint.CoreEndpoint` -> Runtime's `server_facade` -> `abstractcore.server.app`), and that route created a
provider per request when its own registry had none, never the pool's resident instance.

## What changed

- The cause and the fix are in AbstractCore 2.31.1 (0859): the chat route lends the process's alive `huggingface`
  instance of the same model (`process_residency.resident_provider`) instead of `create_llm`, and a HuggingFace
  instance runs its generations one at a time so the pool's runs and `/v1` requests can share it. No gateway code
  change was needed; the gateway raises its floor so an install cannot keep the old Core.
- `pyproject.toml`: `abstractcore>=2.31.1` and `abstractcore[embeddings]>=2.31.1` (from 2.30.0);
  `tests/test_gateway_install_profiles.py`, `docs/configuration.md` and `llms.txt` follow.
- `tests/test_v1_reuses_resident_model.py`: the gateway's mounted `/v1` with a resident model registered where a
  real `HuggingFaceProvider` registers itself (`hf_residency`); three chats with different temperatures use it,
  `create_llm` is never called, the resident model is never unloaded; with no resident model the route builds its own.

## Evidence

- `tests/test_v1_reuses_resident_model.py`: 2 passed with AbstractCore 2.31.1; against AbstractCore 2.31.0
  (`PYTHONPATH` = a 2.31.0 checkout): `test_v1_chat_uses_the_pools_resident_model_not_a_second_copy` FAILED
  "/v1 built 3 second copies of a resident model".
- Real weights on Metal (scratch gateway, `unsloth/Qwen3.5-4B-GGUF` loaded through `POST /api/gateway/models/load`,
  then `/v1/chat/completions`): Core 2.31.0: holders 1 -> 2 -> 3 -> 4, gateway RSS 11.2 -> 43.4 GB over three calls.
  Core 2.31.1: holders stay at 1 and RSS at 11.1-11.2 GB over three sequential `/v1` calls and three concurrent ones (one streamed), all HTTP 200 (unsloth/Qwen3.5-4B-GGUF, scratch gateway; before: holders 1 -> 2 -> 3 -> 4, RSS 11.2 -> 43.4 GB).
- Full suite: see the 0.17.3 PR.
