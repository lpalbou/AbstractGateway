# Proposed: AbstractFlow Draft Spaces And Ephemeral Runs

## Metadata
- Created: 2026-05-09
- Status: Partially implemented
- Completed: N/A

## Context

AbstractFlow should use AbstractGateway as its runtime host. The editor will generate many authoring
test runs that are useful during design but should not pollute production history, published bundle
versions, semantic memory, or long-term ledger storage.

Current Gateway reality:

- `/api/gateway/visualflows` stores editable VisualFlow JSON.
- `/api/gateway/visualflows/{flow_id}/publish` packs a `.flow` bundle and installs it.
- `/api/gateway/runs/start` starts durable Runtime runs.
- Gateway creates a per-run workspace when `workspace_root` is omitted.
- Gateway accepts sanitized top-level `run_lifecycle` metadata, hides
  `purpose: "draft_test"` runs from default `/runs`, rejects accidental production starts of
  explicit `draft.*` bundle versions, and exposes `/runs/purge_drafts` for expired ephemeral
  draft-test run trees.
- Gateway-created default workspaces now include `.abstractgateway-workspace.json` ownership
  markers so purge can delete only Gateway-owned workspace directories.

## Why this might matter

Without a Gateway-owned draft/private lifecycle, AbstractFlow has to fake draft semantics with
`bundle_version: "dev"` and client-side history filtering. That is brittle and leaks authoring noise
into the deployment composition root.

## Proposed direction

Add Gateway support for draft/private Flow authoring:

- Draft VisualFlow space:
  - editable Gateway records remain separate from published reusable bundles;
  - draft publish can use a dedicated draft bundle namespace or temporary bundle install location.
- Ephemeral/private run metadata:
  - `purpose: "draft_test"` or `run_mode: "draft"`;
  - `visibility: "private"`;
  - `source: "abstractflow.editor"`;
  - `retention`/`expires_at`/`ttl_s`;
  - optional `owner_session_id` or `editor_session_id`.
- Run history defaults:
  - hide draft/private runs unless `include_drafts=true` or equivalent;
  - preserve current-session draft runs for Flow debugging.
- Cleanup:
  - TTL-based cleanup for draft runs, ledgers, artifacts, command records, and Gateway-owned
    workspaces is implemented through `/runs/purge_drafts`;
  - safe cleanup of draft bundles without touching published versions.
- Memory safety:
  - draft runs should default to isolated memory scopes or clear warnings when writing to durable
    global/session memory.

## Evidence to gather before promotion

- Gateway owns draft-test cleanup policy and delegates only optional storage deletion mechanics to
  AbstractRuntime.
- Draft bundles live in the same `WorkflowBundleRegistry` with `metadata.lifecycle.channel`
  and `draft.*` version classification.
- Draft run metadata belongs in `StartRunRequest.run_lifecycle`, normalized into private run vars
  as `_run_lifecycle`.
- Default draft-run TTL is seven days and can be overridden with
  `ABSTRACTGATEWAY_DRAFT_RUN_RETENTION_TTL_S` / `ABSTRACTGATEWAY_DRAFT_RUN_TTL_S`.

## Validation ideas

- Gateway TestClient:
  - save VisualFlow;
  - draft publish;
  - draft start;
  - verify default `/runs` hides draft run;
  - verify `include_drafts=true` shows it;
  - run cleanup and verify run/ledger/artifacts/workspace are removed or archived according to
    policy.
- Flow integration smoke:
  - "Test Draft" produces draft/private run metadata;
  - "Publish" produces durable reusable bundle version.

## Non-goals

- Do not make AbstractFlow a runtime store owner.
- Do not weaken Gateway auth/CORS for editor convenience.
- Do not delete production runs through the draft cleanup path.

## Guidance for future agents

AbstractFlow's `Test Draft` lifecycle is now implemented. Keep this item proposed only for the
remaining optional hardening around draft-bundle cleanup and default memory-scope isolation; do not
reopen run-tree purge unless the tests around `/runs/purge_drafts` fail.
