# Proposed: AbstractFlow Draft Spaces And Ephemeral Runs

## Metadata
- Created: 2026-05-09
- Status: Proposed
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
- Run listing/history currently treats editor tests like any other durable run unless clients
  filter them themselves.

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
  - TTL-based cleanup for draft runs, ledgers, artifacts, and draft workspaces;
  - safe cleanup of draft bundles without touching published versions.
- Memory safety:
  - draft runs should default to isolated memory scopes or clear warnings when writing to durable
    global/session memory.

## Evidence to gather before promotion

- Confirm whether Gateway should own all cleanup directly or delegate generic retention mechanics to
  AbstractRuntime storage protocols.
- Decide whether draft bundles should live in the same `WorkflowBundleRegistry` with a flag or in a
  separate draft registry.
- Decide whether draft run metadata belongs in `StartRunRequest` top-level fields or under
  `input_data["_runtime"]`.
- Decide what default TTL is acceptable for local dev and hosted deployments.

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

Promote this when implementing AbstractFlow's `Test Draft` lifecycle. Keep the API explicit rather
than relying on the magic string `bundle_version="dev"` as the only signal.
