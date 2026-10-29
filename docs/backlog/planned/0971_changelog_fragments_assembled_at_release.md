# 0971-gateway: [PROCESS] CHANGELOG [Unreleased] conflicts on every merge; move to per-PR changelog fragments assembled at release (towncrier-style)

> Package: abstractgateway (CHANGELOG.md, console-tui/CHANGELOG.md, release workflow)
> Type: process
> Status: planned (not implemented)
> Created: 2026-10-29
> Priority: P2
> Labels: changelog, merge-conflicts, release-process
> Root link: AbstractFramework release-closing wave 2026-10-29 (merge desk request); same item planned in abstractcore and abstractruntime

## Summary

Every pull request adds its bullet to the single `## [Unreleased]` block of `CHANGELOG.md` (and of
`console-tui/CHANGELOG.md`). With several PRs open at once, each merge makes every other open PR conflict on that
block, so each PR needs a hand or scripted union merge (and a fresh CI run) before it can land. In the
2026-10-28/29 wave the merge desk union-merged main into most PRs at least once, and the block now carries
repeated `### Added` / `### Fixed` headings.

## Scope

- In: one fragment file per PR (for example `changelog.d/<PR or backlog id>.<added|changed|fixed|removed>.md`),
  assembled into the version's section at release (towncrier, or a small script in `scripts/`), for the gateway
  and the console-tui changelogs. The release skill and `docs/` contributor notes say how to write one.
- In: a CI check that a PR touching `src/` or `console-tui/src/` adds a fragment (or carries a "no changelog" label).
- Out: rewriting past released sections.

## Acceptance

- Two PRs that each add a fragment merge in either order without a CHANGELOG conflict.
- The release step produces the same Keep a Changelog section (one heading per kind, bullets in merge order) and
  empties `changelog.d/`.
- The CI check fails on a PR with a code change and no fragment (shown failing once).
