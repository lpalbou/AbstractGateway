### Fixed

- **The mailbox form's outgoing server defaults to port 465 with SSL** (993 for incoming) when discovery finds
  nothing, and switching Security to STARTTLS moves a standard port to 587 (143), never a port typed by hand.
### Changed

- **Ollama on Linux installs for the account running the gateway, never with an administrator** (operator
  2026-10-29): its official `ollama-linux-<arch>.tar.zst` (SHA-256 checked) is unpacked into `~/.local` and served by
  a systemd user service on port 11434 (its program path quoted, so a home directory with a space works; a detached `ollama serve` without a user bus). The card no longer says
  "Install (administrator)"; without `zstd` it says why, with no administrator step.
### Fixed

- **A gateway that fails to start now exits non-zero**: the exit belt kept exiting 0 when a non-daemon thread was
  alive, so a port-in-use start under systemd (`Restart=on-failure`) was never retried (OVH 2026-10-28). The off-loop
  pools are stopped before the belt, and the belt exits with the pending code (3 for uvicorn's startup failure).
- **The bootstrap admin token is no longer printed into a service log** (systemd's journal, a launchd agent's log):
  a loopback gateway under a service manager names the token file only. systemd is recognised when fd 2 IS the
  `JOURNAL_STREAM` (its `device:inode`), so a unit's child whose stderr goes elsewhere (a CI job, a shell started
  from a unit) still prints the token.
- **A voice can be the default only once its model is downloaded** (operator ruling 2026-10-29; backlog 0955
  closed as rejected and replaced by this rule).
  - Setting the default voice to a local model whose weights are not on disk is refused when it is chosen: 409
    with the sentence ("The voice model supertonic-3 (401 MB) is not downloaded on this computer yet, so it cannot
    be the default voice…") as a plain `detail`, and the download action under `error`.
  - The console's capability dialog shows that sentence with a **Download (401 MB)** button. Nothing downloads
    before the click.
  - Synthesis never checks, and the voice lists already offer only downloaded voices.
- **A gateway lists only its own apps** (operator P0, 2026-10-29). A second gateway on the same machine (port
  8443 next to 8080) listed the other gateway's running apps as "Started outside the gateway" and opened them,
  so the app asked to sign in to the OTHER gateway with a token ("Invalid Gateway user token"). An app found on a
  usual port is now left out when its page names another gateway (`gateway_url`) or its command line runs another
  data dir's app support; this gateway starts its own copy on a free port and opens it signed in (the one-time
  hand-over). Apps of the dev stack that name no gateway are listed as before.

### Added

- **The console's Hugging Face search pages, counts, and sits beside the catalog** (operator QA on the release-test
  server).
  - Below the Hugging Face cards, a line says how many results are shown and whether more exist.
    **Show more results** appends the next page.
  - An exact handle (`Qwen/Qwen3.5-9B`) lists that repo first. When the Hub has no such repo, the line says so.
  - A catalog search of three characters or more also asks Hugging Face. Its results come under
    **From Hugging Face**, below the catalog's cards, and Hugging Face mode reuses that answer.
  - `GET /api/gateway/models/catalog` gains `hub_page` and `hub_page_size`, passed through only when given.
  - Needs AbstractCore with backlog 0864 (`hub.search`) and AbstractRuntime with its facade passthrough.
- **A health incident record**: when `/api/health` moves to "degraded" (a failed boot, stopped background workers, a
  degraded runner), one record is written to `<data dir>/incidents/health-<UTC stamp>.json` with the reasons in words
  and the health body (one per transition, at most one a minute). Nothing on disk said when or why a gateway turned
  bad before (OVH 2026-10-29).

- **Gateway instances: several self-contained gateways on one machine** (operator, 2026-10-29).
  `abstractgateway --instance NAME <command>` (or `ABSTRACTGATEWAY_INSTANCE`) selects a named instance with its
  own data folder (`-NAME`), its own AbstractCore config (`<data folder>/core-config`), its own default port
  (8090 + slot), its own app port block (derived from a slot registered once in
  `~/.config/abstractgateway/instances.json`, so two instances never collide), its own login service
  (`abstractgateway-NAME.service`, `ai.abstractframework.gateway.NAME`; the unit runs
  `abstractgateway --instance NAME serve`), and its name in the console (tab title, sidebar) and in
  `GET /about` (`instance`). `abstractgateway-config` takes `--instance` too. The default instance is unchanged.

### Fixed

- **A voice can be the default only once its model is downloaded** (operator ruling 2026-10-29; backlog 0955
  closed as rejected and replaced by this rule).
  - Setting the default voice to a local model whose weights are not on disk is refused when it is chosen: 409
    with the sentence ("The voice model supertonic-3 (401 MB) is not downloaded on this computer yet, so it cannot
    be the default voice…") as a plain `detail`, and the download action under `error`.
  - The console's capability dialog shows that sentence with a **Download (401 MB)** button. Nothing downloads
    before the click.
  - Synthesis never checks, and the voice lists already offer only downloaded voices.
- **A gateway lists only its own apps** (operator P0, 2026-10-29). A second gateway on the same machine (port
  8443 next to 8080) listed the other gateway's running apps as "Started outside the gateway" and opened them,
  so the app asked to sign in to the OTHER gateway with a token ("Invalid Gateway user token"). An app found on a
  usual port is now left out when its page names another gateway (`gateway_url`) or its command line runs another
  data dir's app support; this gateway starts its own copy on a free port and opens it signed in (the one-time
  hand-over). Apps of the dev stack that name no gateway are listed as before.
- **The Models page says what a model's "cache" is and who keeps it in memory.** A GGUF row reads
  "~5.4 GiB + ~2.0 GiB KV" with the loaded context in the Context column and, in the tooltip, that
  the KV cache was allocated for that context when the model loaded (an estimate from the file;
  it does not grow with use), then the session prompt caches. It used to read "+ 8.3 GiB cache"
  with no context on a freshly restarted server (AbstractCore over-counted hybrid models' KV).
  Rows carry `kv_cache_bytes`, `kv_cache_estimated` and `held_by`; the residency cell names the
  holders in words ("held by the runtime of account default/bob (loaded)"), and every account's
  runtime client is labelled so ("kept in memory: ... still in use by runtime client
  70a5b844f350" is gone, ADR 0043). Needs AbstractRuntime with `residency_owner_label` /
  `held_by` and AbstractCore 2.31.1 for the KV figures; older ones show what they report.
