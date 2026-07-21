# `deep-research` Shipped Workflow

Gateway packages `deep-research@0.1.6.flow` as a supported shipped bundle. It
is available from the normal bundle registry alongside `basic-agent` when the
packaged bundle directory is used. (Formerly `dp-research` — the `dp-*` names
were retired by operator ruling 2026-07-16; version lineage continues from
`dp-research@0.1.5`, and old `dp-research@0.1.x` artifacts live under
`flows/bundles/archive/` for completed-run history only, no longer served.)

## Contract

- Bundle id: `deep-research`
- Version: `0.1.6`
- Entrypoint flow id: `deep-research`
- Interfaces: `abstractcode.agent.v1`, `abstractresearch.deep.v1`
- Editable source flows: `abstractflow/examples/flows/deep-*.json`

The workflow exposes a small product-facing input contract:

- `request`: what should be researched.
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

## Validation

From the monorepo root:

```bash
PYTHONPATH=abstractgateway/src:abstractruntime/src:abstractcore \
  pytest -q abstractgateway/tests/test_deep_research_bundle_contract.py
```

That test opens the bundle, checks the public input/output contract, verifies
the review-gated loop wiring, verifies agent tool allowlists, and loads the
bundle through `WorkflowBundleGatewayHost` with an isolated registry.
