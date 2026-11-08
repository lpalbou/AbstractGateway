
## Progress (2026-11-07, branch `feat/0210-mail-and-fixes`, unreleased)

- **Item 3 done**: `perf.yml` installs on the restored pip cache (`cache-dependency-path: pyproject.toml`, no
  `--no-cache-dir`, pip `--timeout 60 --retries 5`) inside its own `timeout-minutes: 10`; the job bound is 25 so the
  load test keeps its 15. Pinned by `tests/test_perf_workflow_install.py` (red with `--no-cache-dir` back, red with an
  install bound that leaves the test less than 15 minutes). The proof that the install no longer times out is the
  next perf run.
- **Item 4 done**: `test_address_is_never_identity` checks every manifest field (a string holding the host, or the
  port as a value of its own) instead of grepping the serialized manifest;
  `test_the_address_check_finds_an_address_and_ignores_coincidences` proves the check is red on an address written
  into a manifest and green on `…38.248080` and SHA hex.
- **Item 5 done**: `test_base_install_is_remote_light_server` asserts the `abstractavatar` floor `>= 0.1.1` (red at
  0.1.0), and the new `markdown-it-py >= 3.0.0` floor (backlog 1011).
- **Item 6 done**: `kit_release.release_tag` maps the ui-kit version to the oldest abstractuic tag whose
  `ui-kit/package.json` carries it (0.9.15 -> v0.10.2, 0.9.16 -> v0.10.3 on the real checkout); the drift pins rebuild
  the islands bundle from that tag and byte-compare it, and fail with the fix (never skip) when no tag carries the
  version, the kit's packages are not installed, or an `ABSTRACTUIC_SRC` kit is not built. Only a checkout with no
  abstractuic beside it (CI, ci_local) still skips.
- Items 1 and 7: `a86ddab9`, `a6d4b689` and `ce55cc8f` are in this branch's base. Items 2 and 8 and the
  `automation.pause` question were not looked at here.
