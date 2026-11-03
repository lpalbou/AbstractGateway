### Fixed

- **The container docs list the image tags that exist.** `README.md`, `docs/deployment.md` and
  `docs/getting-started.md` named `0.13.x` tags; they now give the tags every release publishes on GHCR:
  `<version>` and `latest` (the light image, linux/amd64 and linux/arm64) and `<version>-gpu` and `gpu-latest`
  (the experimental NVIDIA image, linux/amd64 only, built best-effort, so a release can ship without it).
- **The setup-code help says where the six words are.** The console's "Set up your gateway" form and the
  onboarding answer no longer say the installer printed the code (it does only when it did not open a browser,
  or with `--server`): they name `abstractgateway claim --code` on the gateway's machine, which always shows
  it. `docs/first-run.md` says when the installer's summary shows it and that `serve` never prints it.
