# 0961-gateway: [BUG] Fresh-Linux release test fixes: apps install the release matrix, network status reads loopback user auth, llama.cpp card

> Package: abstractgateway
> Type: bug
> Status: completed (2026-10-28, 0.17.2)
> Created: 2026-10-28
> Priority: P1
> Labels: apps, release-matrix, network, auth, engines, linux
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (framework root `untracked/release-closing/ovh-test.log`; QA.md items 8 and 10, the llama.cpp card copy)
> Number: 0961 (0957-0960 are filed on the open branch `docs/backlog-linux-server-findings`; 0960 is the card item fixed here)

## Summary

Three defects the fresh-Linux test of the released stack found in the gateway, fixed together.

## What changed

- **QA 8, apps follow the release matrix** (`apps_manager.py`): the console installed npm's newest apps
  (observer 0.10.1, continuum 0.7.3, code 0.13.1, entity 0.9.1, flow 0.12.0) while the installer's release
  (AbstractFramework 0.11.17) pins flow 0.11.0, code 0.13.0, observer 0.10.0, continuum 0.7.2, entity 0.9.0.
  `AppsManager.release_app_versions()` reads `FRAMEWORK_VERSION` from `<data dir>/bootstrap.env` and the
  `npm_apps` of that release's `docs/installers/install-manifest.json` at the framework tag `v<release>`
  (checked: the manifest must name that release). `start_install` without a version and without `update`
  installs that pin (`_release_pinned_version`, said in the job log). Unreadable pins fail the install with the
  reason (never the newest in silence); no release recorded: the newest, said in the log. An explicit version
  and an explicit Update keep their meaning. `GET /apps` reports `release` and each row's `release_version`;
  the existing `update_available` / "Latest x.y.z" shows the newer version.
- **QA 10, `network status`** (`network_exposure.network_status`): `serve` writes the run record before
  `apply_loopback_auth_default` turns user auth on, so the record's posture was "nothing configured" and the
  CLI printed "user auth off, token unset" while every token-less call got 401. A record with no explicit
  posture on a loopback host now reads as user auth on (a non-loopback one stays off). Works for records
  written by older gateways.
- **llama.cpp card** (`engines_install.py`, 0960): names the Linux CUDA build instead of "CPU on Linux".

## Evidence

- `tests/test_gateway_apps_manager.py`: `test_install_follows_the_release_matrix_and_offers_the_newer_version`,
  `test_install_without_a_release_installs_the_newest`,
  `test_an_unreadable_release_matrix_refuses_instead_of_installing_the_newest`: 3 passed; with the previous
  `apps_manager.py`, 3 failed.
- `tests/test_gateway_network_exposure.py::test_status_of_a_loopback_gateway_started_unconfigured_says_user_auth_on`:
  passed; red with the previous `network_exposure.py`.
- Full suite: see the 0.17.2 PR.

## Findings left open

- QA 4 (`/v1` loads a second llama.cpp instance per request; reproduced on Metal: holders 1 -> 2, RSS 11.7 ->
  22.9 -> 33.5 GB over two calls): AbstractCore's server chat route creates a provider per request when its own
  registry has none (`abstractcore/server/app.py`), never the runtime pool's resident instance. Fixed bottom-up
  in AbstractCore 2.31.1, AbstractRuntime, then gateway 0.17.3.
- QA 11 (Continuum's two 503s): Continuum's own hub proxy (`bin/hub_proxy.js`) answers 503
  `hub_seat_unavailable` with no hub key; its UI calls the hub before reading `/api/hub/meta`. Continuum item.
- Code's terminal app installs GitHub's newest `abstractcode` release, not the release's crate pin
  (`AF_CRATE_CODE_CLI`); the fresh-Linux test happened to match (0.12.0).
