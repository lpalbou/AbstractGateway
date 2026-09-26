# `deep-research` Shipped Workflow

Gateway packages `deep-research@0.1.8.flow` as a supported shipped bundle. It
is available from the normal bundle registry alongside `basic-agent` when the
packaged bundle directory is used. It replaces the `dp-research` bundle id,
which is no longer shipped.

## Contract

- Bundle id: `deep-research`
- Version: `0.1.8`
- Entrypoint flow id: `deep-research`
- Interfaces: `abstractcode.agent.v1`, `abstractresearch.deep.v1`
- Editable source flows: `abstractflow/examples/flows/deep-*.json`

The workflow exposes a small product-facing input contract:

- `request`: what should be researched. AbstractCode and other
  `abstractcode.agent.v1` hosts send the user's message as `prompt`; the
  workflow researches `prompt` when `request` is empty.
- `viewpoint`: the angle, thesis, audience stance, or evaluation lens.
- `effort`: `quick`, `standard`, or `thorough`.
- `provider` / `model`: optional overrides. Leave blank to use Gateway/Core
  defaults.

All other knobs are derived from `effort`, including review-round count,
investigation agent iteration cap, deadline/source-budget guidance,
source/citation policy, export title, and export prefix. The derived review
round count is enforced by a root `For` control node: each round runs
investigation, persists the latest evidence, runs adversarial review, persists
reviewer guidance, and feeds that guidance into the next investigation pass.

The end node's `success` output is `true` when the run produced a report.

Derived `deadline_minutes` and `max_sources` are carried into prompts and audit
objects; they do not preempt an in-flight provider call.

## Tool And Export Policy

Research agents pin a read-only evidence allowlist:
`web_search`, `fetch_url`, `skim_websearch`, `skim_url`, `read_file`,
`skim_files`.

Adversarial review runs through structured LLM calls without write or shell
tools. Export uses deterministic Runtime nodes: `write_file`, `write_pdf`, and
`write_docx`.

Export paths append a sanitized run timestamp to the effort-derived output
prefix (`reports/deep-*-research`) to avoid overwrites. The final manifest is
built after file writes and includes actual Markdown/PDF/DOCX paths, byte
counts, PDF/DOCX hashes, and content types.

## Related docs

- [shipped-workflows.md](./shipped-workflows.md): every bundle a fresh install serves
- [api.md](./api.md): starting runs and streaming their ledger
