# Changelog fragments

Each pull request that changes `src/` adds ONE file here instead of editing `CHANGELOG.md`:
`changelog.d/<pr-number-or-slug>.md` (for example `changelog.d/model-host.md`). A new file never
conflicts with another branch; the `[Unreleased]` section did on every merge.

A fragment is Keep a Changelog markdown: one or more of `### Added`, `### Changed`, `### Deprecated`,
`### Removed`, `### Fixed`, `### Security`, each followed by its entries, written as before:

```markdown
### Fixed

- **A stop signal stops the gateway in under a second** (`shutdown_drain.py`). One open stream held a
  SIGTERM for the whole graceful-shutdown timeout ...
```

- CI (`changelog` job): a pull request that changes `src/` without adding a fragment fails, and so does a
  malformed fragment. `python scripts/changelog.py check` runs the same check locally.
- `python scripts/changelog.py preview` prints what the next release section will say.
- Release: `python scripts/changelog.py assemble X.Y.Z` writes `## [X.Y.Z] - <date>` from every fragment
  (sections in Keep a Changelog order) and deletes the fragments; commit that with the version bump.
