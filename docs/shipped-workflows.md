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

`flow_id` values below are what you pass to the API — an entrypoint's display
name is not resolvable. A `*` marks the bundle's default entrypoint, used when
you omit `flow_id`.

| Bundle | Version | `flow_id` (interfaces) | What it does |
| --- | --- | --- | --- |
| `basic-agent` | 0.0.4 | `81795ea9`* (`abstractcode.agent.v1`) | The framework default chat agent: one Agent node with tools, memory, and status updates. Serves entity phases and chat hosts. |
| `coding-agent` | 0.2.6 | `coder` (`abstractcode.agent.v1`), `coding-agent`* (`abstractcode.coding.v1`) | Verify-gated coding: a builder agent writes code, an independent verifier runs build/execute/match gates each round, and failures feed back as reprompts until gates pass. `coder` is the chat entrypoint; `coding-agent` is the structured pipeline. |
| `deep-research` | 0.1.8 | `deep-research`* (`abstractcode.agent.v1`, `abstractresearch.deep.v1`) | Production research with adversarial review, a verified source ledger, and Markdown/PDF/DOCX export. See [deep-research.md](./deep-research.md). |
| `co-scientist` | 0.2.0 | `co-scientist`* (`abstractresearch.coscientist.v1`) | Multi-agent hypothesis engine: literature grounding through the deep-research investigation flows, then cycles of generation, reflection, Elo-ranked pairwise debate, and evolution into a final reviewed research overview. |
| `abstractassistant-orchestrator` | 0.0.0 | `d5d4e5a1`* (`abstractassistant.agent.v1`) | Orchestrator for the compact AbstractAssistant tray surface. |
| `docs-qa` | 0.1.0 | `docsqa001`* | Documentation Q&A grounded on the asking app's `llms.txt` corpus; also auto-published into the tenant workflow catalog at boot. |
| `react-agent` / `codeact-agent` / `memact-agent` | 0.1.0 | `react`* / `codeact`* / `memact`* (`abstractcode.agent.v1`) | Native ReAct / CodeAct / MemAct agent loops (abstractagent). |

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
curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"bundle_id":"coding-agent","flow_id":"coder","input_data":{"prompt":"Write a CLI that ..."}}' \
  "http://127.0.0.1:8080/api/gateway/runs/start"
```

`deep-research` takes `request`, `viewpoint`, and `effort` inputs
([deep-research.md](./deep-research.md)); `co-scientist` takes a `research_goal`
and scales its effort with `max_cycles`.

Markdown, PDF, and DOCX export work on a base install. `co-scientist` also
draws two figures (an architecture diagram and an Elo trajectory) through the
`write_chart` node, which needs `matplotlib`. That package is not part of the
base install: without it the run still completes and reports its findings, and
only the figures are skipped. Install it if you want them:

```bash
pip install matplotlib
```

## Managing workflows from the console

Both consoles carry a **Workflows** surface — a tab in the web console, step 6
in the console-TUI — listing every workflow registered on this gateway with how
many published and draft versions each has. Select a workflow to see its
versions and its entrypoints with their declared interfaces.

From there you can:

- **Import** one or more `.flow` bundles. Each file reports whether the gateway
  is serving the result, not merely that the upload succeeded.
- **Export** a version as its original `.flow` bytes, byte-identical to what is
  installed, so it can be archived or re-installed elsewhere. In the TUI, `e`
  writes the file next to your working directory.
- **Delete** a single version or every version of a workflow, behind a
  confirmation that states what is irreversible. In the TUI, `d` removes the
  selected version and `D` the whole workflow.

Versions the gateway is not serving are listed separately under **Not loaded**,
with the reason and the file path. They stay on disk until you remove them.

## Who can change the registry

The gateway's own bundle directory is the shared set: every user sees it and can
run what it contains. Changing it is an operator act, so installing, replacing,
removing, deprecating and reloading workflows there require an admin principal.
Listing and running remain available to every user.

Under hosted user auth each principal also gets its own workflow registry, and
you can install and remove workflows there without admin rights — the shared
directory stays visible and read-only alongside it. The rule is ownership: you
may change the registry you own.

The same check covers every route that writes the registry, including
`POST /bundles/upload`, `DELETE /bundles/{bundle_id}`, `POST /bundles/reload`
and `POST /visualflows/{flow_id}/publish`. A non-admin request against the
shared registry returns `403`.

`basic-agent.flow` is the default framework agent and the gateway verifies it at
startup, so `DELETE` refuses to remove it and answers `409`. To replace the
default agent, install the replacement bundle first, then remove the old file.

## Customizing the registry

- The shipped registry is used when `ABSTRACTGATEWAY_FLOWS_DIR` is unset;
  point it at your own bundle directory to serve a custom set
  ([configuration.md](./configuration.md)).
- Shipped bundle versions are immutable pins; newer versions install alongside
  them through the normal bundle upload or workflow catalog routes.
- The editable sources for the shipped workflows are VisualFlow JSON files in
  the AbstractFlow repository (`examples/flows/`), packed into `.flow` bundles
  with AbstractRuntime's `abstractruntime.workflow_bundle.pack_workflow_bundle`.
