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
you omit `flow_id`. The name and the one-line description are what the
Workflows page and the console TUI show for each bundle.

| Bundle | Version | Name | `flow_id` (interfaces) | What it does |
| --- | --- | --- | --- | --- |
| `basic-agent` | 0.0.5 | Basic agent | `81795ea9`* (`abstractcode.agent.v1`) | Chat agent: answers a prompt in one agent loop that can use tools and memory, and returns the reply. The default agent for chat hosts and entities. |
| `coding-agent` | 0.2.8 | Coding agent | `coding-agent`* (`abstractcode.coding.v1`) | Takes a build request and a workspace, writes the code, and runs build and run checks each round until they pass; returns a report. |
| | | Coding agent (chat) | `coder` (`abstractcode.agent.v1`) | Chat version of the coding agent: builds what the prompt asks in the session workspace, checks it each round, and replies with a report. |
| `deep-research` | 0.1.8 | Deep research | `deep-research`* (`abstractcode.agent.v1`, `abstractresearch.deep.v1`) | Takes a research question, searches the web, has three critics review the draft, and writes a cited report (Markdown, PDF, DOCX). See [deep-research.md](./deep-research.md). |
| `co-scientist` | 0.2.1 | Co-scientist | `co-scientist`* (`abstractresearch.coscientist.v1`) | Takes a research goal, grounds it in web sources, then generates, debates and ranks hypotheses over cycles; returns a research overview. |
| `abstractassistant-orchestrator` | 0.0.0 | AbstractAssistant Orchestrator | `d5d4e5a1`* (`abstractassistant.agent.v1`) | The menu-bar Assistant's workflow: sends each request to a tools agent or to image, video or music generation, and returns the reply. |
| `docs-qa` | 0.1.2 | Docs Q&A | `docsqa001`* | Answers a question about an app from that app's documentation (its llms.txt), citing it, and says so when the docs do not cover it. History comes from the run's session; also auto-published into the tenant workflow catalog at boot. 0.1.2 changes only the name and description; 0.1.1 and 0.1.0 stay installed for what pins them. |
| `react-agent` | 0.1.0 | ReAct agent | `react`* (`abstractcode.agent.v1`) | Chat agent (ReAct): reasons step by step and calls tools until it can answer the prompt, then returns the reply. |
| `codeact-agent` | 0.1.0 | CodeAct agent | `codeact`* (`abstractcode.agent.v1`) | Chat agent (CodeAct): works on the prompt mainly by writing and running Python code, then returns the reply. |
| `memact-agent` | 0.1.0 | MemAct agent | `memact`* (`abstractcode.agent.v1`) | Chat agent (MemAct): keeps a long-term memory that it reads and updates on each turn, can call tools, and returns the reply. |

A source checkout (`flows/bundles/` in the repository) also carries building
blocks that the wheel does not ship: `map-reduce` (Map-reduce),
`structured-extract` (Structured extraction), `adversarial-review` (Adversarial
review) and six deliberation patterns (`meta-baseline`, `meta-consensus`,
`meta-debate`, `meta-deliberate`, `meta-perspectives`, `meta-reflect`).

The names and descriptions come from one table in the AbstractFlow repository
(`scripts/workflow_labels.py`); `scripts/relabel_shipped_bundles.py check`
verifies that the build scripts and the latest shipped bundles carry it.

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

Both consoles carry a **Workflows** surface — a tab in the web console, screen
3 in the console-TUI — listing one row per workflow bundle registered on this
gateway: its name, what it does, its latest version (and how many older ones),
its source ("Shipped with the gateway", "Imported" or "Published from
AbstractFlow") and the apps that use it. Select a workflow to see its versions
and its entrypoints with their names, descriptions and the apps that use them.
Below the list, **Default workflow per app** chooses which workflow runs when an
app asks for "an agent" without naming one (see
[console.md](./console.md#workflows)).

From there you can:

- **Import** one or more `.flow` bundles. Each file reports whether the gateway
  is serving the result, not merely that the upload succeeded.
- **Export** a version as its original `.flow` bytes, byte-identical to what is
  installed, so it can be archived or re-installed elsewhere. In the TUI, `e`
  writes the file next to your working directory.
- **Delete** a single version or every version of a workflow, behind a
  confirmation that states what is irreversible. Deleting a workflow that ships
  with the gateway removes its file: nothing puts it back at the next restart,
  only reinstalling the gateway does. In the TUI, `d` removes the selected
  version and `D` the whole workflow.

Bundle files the gateway cannot run are listed separately under **Broken
workflows**, with the versions affected and the reason. They stay on disk until
you remove them.

## Who can change the registry

The gateway's own bundle directory is the shared set: every user sees it and can
run what it contains. Changing it is an operator act, so installing, replacing,
removing, deprecating and reloading workflows there require an admin principal.
Listing and running remain available to every user.

Under hosted user auth each principal also gets its own workflow registry, and
you can install and archive workflows there without admin rights — the shared
directory stays visible and read-only alongside it. The rule is ownership: you
may change the registry you own.

The same check covers every route that writes the registry, including
`POST /bundles/upload`, `POST /bundles/{bundle_id}/archive`, `POST /bundles/reload`
and `POST /visualflows/{flow_id}/publish`. A non-admin request against the
shared registry returns `403`.

Workflows are archived, never deleted (`DELETE /bundles/{bundle_id}` answers
`410`). Bundles that ship with the gateway, `basic-agent.flow` included, cannot
be archived either (`409`); an admin hides one from users with the **Available
to users** switch on the console's Workflows page.

## Customizing the registry

- The shipped registry is used when `ABSTRACTGATEWAY_FLOWS_DIR` is unset;
  point it at your own bundle directory to serve a custom set
  ([configuration.md](./configuration.md)).
- Shipped bundle versions are immutable pins; newer versions install alongside
  them through the normal bundle upload or workflow catalog routes.
- The editable sources for the shipped workflows are VisualFlow JSON files in
  the AbstractFlow repository (`examples/flows/`), packed into `.flow` bundles
  with AbstractRuntime's `abstractruntime.workflow_bundle.pack_workflow_bundle`.
