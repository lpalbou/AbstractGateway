# Shipped Workflows

A fresh Gateway install serves a ready-to-use workflow registry. In bundle
mode (the default), Gateway loads the `.flow` bundles packaged with the wheel,
so you can run a proven coding agent, a deep-research pipeline, and a
co-scientist hypothesis engine out of the box — no manual bundle install.

List them on a running gateway:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8080/api/gateway/bundles"
```

## The shipped set

| Bundle | Version | Entrypoints (interfaces) | What it does |
| --- | --- | --- | --- |
| `basic-agent` | 0.0.4 | `basic-agent` (`abstractcode.agent.v1`) | The framework default chat agent: one Agent node with tools, memory, and status updates. Serves entity phases and chat hosts. |
| `coding-agent` | 0.2.6 | `coder` (`abstractcode.agent.v1`), `coding-agent` (`abstractcode.coding.v1`) | Verify-gated coding: a builder agent writes code, an independent verifier runs build/execute/match gates each round, and failures feed back as reprompts until gates pass. `coder` is the chat entrypoint; `coding-agent` is the structured pipeline. |
| `deep-research` | 0.1.7 | `deep-research` (`abstractcode.agent.v1`, `abstractresearch.deep.v1`) | Production research with adversarial review, a verified source ledger, and Markdown/PDF/DOCX export. See [deep-research.md](./deep-research.md). |
| `co-scientist` | 0.2.0 | `co-scientist` (`abstractresearch.coscientist.v1`) | Multi-agent hypothesis engine: literature grounding through the deep-research investigation flows, then cycles of generation, reflection, Elo-ranked pairwise debate, and evolution into a final reviewed research overview. |
| `abstractassistant-orchestrator` | 0.0.0 | AbstractAssistant Orchestrator (`abstractassistant.agent.v1`) | Orchestrator for the compact AbstractAssistant tray surface. |
| `docs-qa` | 0.1.0 | `docs-qa` | Documentation Q&A grounded on the asking app's `llms.txt` corpus; also auto-published into the tenant workflow catalog at boot. |
| `react-agent` / `codeact-agent` / `memact-agent` | 0.1.0 | `react` / `codeact` / `memact` (`abstractcode.agent.v1`) | Native ReAct / CodeAct / MemAct agent loops (abstractagent). |

Interfaces are how clients pick workflows without knowing bundle internals:
anything declaring `abstractcode.agent.v1` can serve a plain chat prompt.
AbstractCode TUI resolves its default agent through that contract — a saved
preference first, then `coding-agent`'s `coder` when installed, then
`basic-agent` — so a stock Gateway gives it the verify-gated coder by
default.

## Running one

Start a run with the normal runs API (see [api.md](./api.md) for the full
contract and streaming). `flow_id` selects an entrypoint; omit it to run the
bundle's default entrypoint:

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"bundle_id":"coding-agent","flow_id":"coder","input_data":{"prompt":"Write a CLI that ..."}}' \
  "http://127.0.0.1:8080/api/gateway/runs"
```

`deep-research` takes `request`, `viewpoint`, and `effort` inputs
([deep-research.md](./deep-research.md)); `co-scientist` takes a research
goal and scales its effort with the cycle budget.

## Customizing the registry

- The shipped registry is used when `ABSTRACTGATEWAY_FLOWS_DIR` is unset;
  point it at your own bundle directory to serve a custom set
  ([configuration.md](./configuration.md)).
- Shipped bundle versions are immutable pins; newer versions install alongside
  them through the normal bundle upload or workflow catalog routes.
- The editable sources for the shipped workflows are VisualFlow JSON files in
  the AbstractFlow repository (`abstractflow/examples/flows/`), packed with
  `abstractflow bundle pack`.
