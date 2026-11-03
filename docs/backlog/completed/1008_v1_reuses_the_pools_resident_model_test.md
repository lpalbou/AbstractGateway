# 1008-gateway: [TEST] `/v1` reuses the model the runtime pool holds: a gateway-level test

> Package: abstractgateway
> Type: test (regression guard for a fixed bug)
> Status: completed 2026-11-03 (salvaged from PR #22, unreleased branch `release/0.17.3`, where it was numbered 0962)
> Created: 2026-10-28
> Priority: P1
> Labels: openai-api, v1, models, residency, llama.cpp
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (QA item 4); release wave 2026-11-03 (PR triage: salvage #22)
> Fixed in: AbstractCore 2.31.1 (Core backlog 0859); main's Core floor (`>=2.32.0`) already carries it
> Number: 1008 (0962 is taken on main)

## Summary

On the fresh Linux install, `/v1/chat/completions` answered 500 "Failed to create llama_context": with a GGUF
loaded through the gateway's model pool, each `/v1` request built a SECOND llama.cpp instance of the same model.
AbstractCore 2.31.1 fixed it (the chat route lends the process's resident `huggingface` instance). The gateway had
no test that its mounted `/v1` gets that behaviour.

## What changed

- `tests/test_v1_reuses_resident_model.py`: the gateway's mounted `/v1` (`core_endpoint.CoreEndpoint`) with a
  resident model registered where a real `HuggingFaceProvider` registers itself (`hf_residency`). Three chats with
  different temperatures use it, `create_llm` is never called and the resident model is never unloaded; with no
  resident model the route builds its own. No gateway code change.

## Evidence

- 2 passed on today's main with AbstractCore 2.32.0. On the 0.17.3 branch it failed against AbstractCore 2.31.0
  ("/v1 built 3 second copies of a resident model").
