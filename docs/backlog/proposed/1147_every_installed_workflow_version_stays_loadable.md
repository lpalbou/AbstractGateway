# 1147 — Every installed workflow version stays loadable: new runs take the latest, a session resumes on its own version

**Status**: proposed (operator 2026-11-08: "that's extremely long, make it a proposed backlog item and try to find a simpler solution") · **Priority**: P1 (the simple fix below first; the full design later, if ever) · **Created**: 2026-11-07 · **Moved to proposed**: 2026-11-08
> Package: abstractgateway (+ abstractnote companion [0041](../../../../abstractnote/docs/backlog/planned/0041_upgrades_retarget_automations_instead_of_recreating_them.md)); abstractcode follow-up noted below.
> Operator rule (2026-11-07): "No older versions of a workflow should be deleted, normally they are all versioned. The only rule is that for new tasks and runs we always reuse the last version."

## The simple fix first (1–2 h with a test)

`abstractnote_seam.note_bundle_paths` hands the loader only the CURRENT packed file (`return [target]`,
`src/abstractgateway/abstractnote_seam.py:118-146`). Return every packed version present under
`<data dir>/extensions/abstractnote/*/abstractnote@<version>.flow` instead, newest last. Nothing else is needed for the
common case: the loader already serves many versions of one bundle (10 versions of `co-scientist` today),
`_pick_latest_version` already binds new runs and new automations to the latest, and a run's stored `workflow_id`
(`id@version:flow`) resolves again, so an old session resumes on its own version and an automation pinned to an old
version keeps running after an upgrade. The gateway's own shipped bundles (`basic-agent` 0.0.1–0.0.4, ~2,400 old runs)
get the same treatment where the files still exist in the data dir; where they do not, the run stays unresolvable and
says so. Test: pack 0.3.0, start a run and leave it waiting, pack 0.3.1, rebuild the host: the waiting run resumes on
0.3.0 and a new start runs 0.3.1; mutation check: `return [target]` again and watch it go red.

Why the workflow version changed at all: AbstractNote 0.3.0 → 0.3.1 (the Watch save fix, 2026-11-07) re-packed its
bundle; every upgrade of a bundle-carrying package does the same. Nothing was deleted; the old version stopped being
LOADED.

Everything below is the full design (write-once archive of every served version, resume-only versions, the typed
`workflow_version_unavailable` error replacing 40 silent retries, Code's fallback returning to the exact version). It
stays proposed; the simple fix above is what ships first.

## Plain description

A run records the exact workflow it started on, `abstractnote@0.3.0:note-capture`. When AbstractNote (or the
gateway itself) is upgraded and the gateway restarts, the gateway loads ONLY the version that is installed now. The
older file is still on disk, but nothing loads it. So everything that points at the old version breaks:

- AbstractCode said "Workflow version unavailable" on Notes sessions (before 0.15.3); 0.15.3 now quietly continues
  them on the installed version instead, which breaks the other half of the rule (a session resumes on its own version).
- A Notes automation pinned to the old version fails at its next tick. Live proof on this machine: the processor
  automation controller `run_bbc08b4b-2261-52bd-9462-9e97505b7df5.json` FAILED on 2026-11-07 08:57 UTC with
  `Workflow 'abstractnote@0.3.0:note-drain' not found in registry`, one minute after the 0.3.1 pack appeared.
- A waiting or running run on the old version is retried and then FAILED after 40 attempts as
  `WorkflowResolutionError` (`runner.py:175`, `runner.py:2929-2966`).

Nothing is deleted. The versions are simply not loaded. The fix is to load them.

## Current code reality (2026-11-07, main `ac0afd95`; the live gateway runs `release/0.21.0` `e51b1c79`, same code here)

- **Only one AbstractNote version is loaded.** `abstractnote_seam.note_bundle_paths` packs the installed package into
  `<data dir>/extensions/abstractnote/<content hash>/abstractnote@<version>.flow` and returns only that file
  (`src/abstractgateway/abstractnote_seam.py:118-146`, `return [target]` at :146). `_compile_workflows` loads exactly
  that list (`hosts/bundle_host.py:1557-1563`). Live data dir: four packs on disk (`extensions/abstractnote/43e5dccd38437b0a/abstractnote@0.1.0.flow`,
  `6859095c565ff2d5/…@0.1.1.flow`, `5b6906a297c58f65/…@0.3.0.flow`, `940156462b6584bd/…@0.3.1.flow`), but
  `GET /api/gateway/bundles?all_versions=true` lists only `abstractnote 0.3.1`.
- **The host already supports many versions per bundle.** `_compile_workflows` keeps `bundles_by_id[bid][bver]`
  (`hosts/bundle_host.py:1527-1543`) and the user flows dir proves it: the same listing serves `co-scientist`
  0.1.0 … 0.2.1 (10 versions) and `coding-agent` 0.1.0 … 0.2.8 side by side.
- **Shipped bundles have the same hole.** The gateway package ships one `basic-agent` (0.0.5); the live runs
  reference `basic-agent@0.0.1` … `@0.0.4` (about 2,400 runs) that no longer load after the gateway upgrades.
- **New runs already take the latest.** A start without a version resolves `latest_bundle_versions[bid]`
  (`hosts/bundle_host.py:3163-3168`), computed by semver max over the SERVING versions after skips
  (`_pick_latest_version`, `hosts/bundle_host.py:318-335`, assigned at :1724). Automations freeze a concrete
  `id@version:flow` at creation (`routes/automations.py:591-650`, the check at :642).
- **A resume looks up the exact id and fails if absent.** `runtime_and_workflow_for_run` reads
  `self.specs[run.workflow_id]`; the only fallback maps unversioned `bundle:flow` ids to the latest; otherwise
  `KeyError("Workflow '…' not registered")` (`hosts/bundle_host.py:3588-3626`). A sub-workflow start fails in the
  runtime with `Workflow '…' not found in registry` (`abstractruntime/src/abstractruntime/core/runtime.py:5062-5064`).
  In-flight pinning (`_PinnableWorkflowRegistry`, `hosts/bundle_host.py:806-823`) only covers reloads inside one
  process, not a restart.
- **What a run stores.** `run.workflow_id = "abstractnote@0.3.0:note-capture"`, `vars.workflow_selection =
  {bundle_id, bundle_version, flow_id, registry_scope}` and `vars._runtime.workflow_snapshot = {workflow_id, sha256,
  artifact_id}`: the ENTRY flow's VisualFlow JSON as an artifact (`hosts/bundle_host.py:3520-3551`,
  `abstractruntime/history_bundle.py:451-525`). Live example: `run_1367b12e-d0d7-47de-8043-85ae99000b7e.json`
  (session `note-watch-bf8040e6-…`). The snapshot omits sub-flows and derived agent/listener workflows, so it is
  evidence, not a way to rebuild the run.
- **Not involved:** `WorkflowCatalogImmutableError` (`workflow_catalog.py:50`, raised at :541-561) guards the tenant
  catalog against overwriting `id@version` with different bytes; AbstractNote does not use the catalog.
  `abstractgateway apps update` updates the Note web app (`@abstractframework/note`, `apps_manager.py:177`), not the
  `abstractnote` Python package whose version names the workflows. The trigger is: upgrade `abstractnote` (or the
  gateway, for shipped bundles), then restart or reload.

## Full design (proposed, later)

1. **Keep every version it has served, write-once.** The first time the gateway serves a framework or extension
   bundle `id@version` (shipped bundles from the package's flows dir, AbstractNote packs), it copies the `.flow`
   into one data-dir archive, `<data dir>/workflow_versions/<bundle_id>@<version>.flow`, never overwritten. If the same
   `id@version` comes with different bytes (a developer edited flows without a version bump), keep the archived
   file, log one warning naming both sha256 values, and serve the archived one for resumes (same rule as
   `WorkflowCatalogImmutableError`: a published version never changes content).
   Existing packs under `extensions/abstractnote/*/` are archived on first boot (a one-time migration, idempotent).
2. **Load the archive every boot.** `_compile_workflows` loads every archived version, in addition to what is
   installed. The duplicate rule at `hosts/bundle_host.py:1531` keeps the installed copy first.
3. **New runs bind the installed version.** For framework/extension bundles, `latest_bundle_versions[bid]` is the
   version the installed package ships, not the semver max of the archive (a downgrade must not start runs on a
   newer archived version). Archived-only versions are *resume-only*: absent from pickers, `/bundles` without
   `all_versions`, `executable_for` and the default agent; listed with `all_versions=true` as `"serving": "resume_only"`.
   A start that names an archived version explicitly is refused with 409 `workflow_version_retired` and the
   latest version in the body.
4. **Automations follow the latest.** An automation created with an unversioned `bundle_ref` (as AbstractNote's
   are) resolves the latest version at each occurrence and records the concrete `workflow_id` on the child run;
   one created with `id@version` stays pinned. The definition stores which of the two it is.
5. **A session resumes on its own version.** `runtime_and_workflow_for_run`, sub-workflow starts and the runner
   resolve the run's exact `workflow_id`, from the archive when needed. No fallback to another version, ever.
6. **A missing version is a loud, explained error.** When the exact version cannot load (archive file missing, or
   the version is skipped by the `min_runtime` gate or a compile failure, `_drop_skipped`, `hosts/bundle_host.py:1590-1625`),
   the run's resume, input and the runner fail with one typed error, `workflow_version_unavailable`, carrying
   `workflow_id`, the reason (`missing_file` | `min_runtime` | `compile`, with its sentence) and the recovery:
   "start a new conversation (it uses `<bundle>@<latest>`), or restore `<file>` into `<data dir>/workflow_versions/`".
   `GET /runs/{id}` exposes the same object so clients show it without trying. The runner fails such a run at
   once with that error instead of 40 silent retries.
7. **Docs:** `docs/architecture.md` (workflow versions), `docs/api.md` (`serving`, the two error codes),
   `docs/faq.md` ("A conversation says its workflow version is unavailable"), CHANGELOG, `llms-full.txt` regenerated.

Out:
- Deleting or garbage-collecting old versions (a later, explicit admin action only, if ever).
- Rebuilding a run from its `workflow_snapshot` artifact (it lacks sub-flows).
- The tenant catalog (already immutable per version).

## Dependencies

- abstractnote [0041](../../../../abstractnote/docs/backlog/planned/0041_upgrades_retarget_automations_instead_of_recreating_them.md):
  stop archiving and recreating automations on upgrade once old versions load again (today it does so because of this
  bug, `abstractnote/src/abstractnote/gateway/health.py:3-9`).
- abstractcode follow-up (no item filed here): once old versions load, Code web's 0.15.3 fallback "continues on the
  loaded version" (`abstractcode` `9c02013`, `web/src/workspace/catalog.ts:449`, `:485`, `:515`) must go back to the
  exact version, showing `workflow_version_unavailable` with its recovery when the gateway says so.

## Validation (full design)

- Red-first tests (each fails on main): two AbstractNote versions across an upgrade (waiting run resumes on 0.3.0, new
  start runs 0.3.1, listing semantics); the same for a shipped bundle; an unversioned automation follows the latest, a
  pinned one stays; archive file removed → typed error once, no 40-attempt loop; same `id@version` different bytes →
  archive kept, one warning; downgrade → new runs start the installed version.
- Live (hermetic gateway on a scratch data dir, never :8080): copy `extensions/abstractnote/*` packs, start, list
  versions, resume a recorded 0.3.0 session, start a new capture (0.3.1).
- Operator check after a local redeploy: an old Notes session opens in Code on its own version, no notice; a new
  Notes capture runs the latest.
