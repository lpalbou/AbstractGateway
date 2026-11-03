### Changed

- **The load test reports its latency and CPU limits on pull requests and enforces them nightly.**
  `scripts/perf/ci.sh` gains `--enforce`. Without it an exceeded p99 or CPU-at-rest limit is printed (and
  written to the GitHub job summary as a table) and the script exits 0; with it the run fails, as before.
  A probe that answers anything but 200, or a gateway that does not boot, fails in both modes. The `Perf`
  workflow passes `--enforce` on its nightly and manual runs only, so a slow shared runner no longer turns
  a pull request red. No product code changes.
