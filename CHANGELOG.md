# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Models page: **Delete** (a trash-bin icon, tooltip "Delete") on every downloaded build. The confirmation states the size from the gateway ("Deletes 351 MB from this computer. Files only — nothing in your runs is touched."), the row shows a busy state, then "Not downloaded" and what was freed. New admin route `POST /api/gateway/models/delete-download` (`model_download_delete_v1`, `dry_run` measures): removes the files with the engine's own mechanism (Ollama delete, Hugging Face / MLX cache folder, one GGUF quant's files), refuses a model that is loaded or locked ("Unload it first"), still downloading, or managed by LM Studio, and writes `model.download_deleted` / `model.download_delete_refused` audit events. Needs AbstractCore with per-quant GGUF delete and the repo-scoped cache delete (branch `round4/2026-10-03`). The terminal console keeps its own delete for now.

### Fixed

- Models page: the filters-in-use label no longer overlaps the "N of M models" count on a narrow screen.

### Changed

- `GET /api/gateway/runs?include_metrics=true` returns each run's totals across its sub-runs (subworkflows, agent sub-runs) as documented, and on the default file store too: `steps`, `llm_calls`, `tool_calls` and `tokens_total` are read from the ledger. Terminal runs are cached by run id and update time.

## [0.12.0] - 2026-10-03

### Added

- Admin controls in Network for the OpenAI-compatible AbstractCore endpoint: enable/disable, copy base URLs, require a dedicated token or allow direct local-network access, and reveal/copy/regenerate the token. Changes apply immediately on the Gateway listener.
- Direct streaming of Core serving routes with request-scoped authentication, admin-only credential access, private token persistence and preserved cloud-provider credential protections. Uses the Runtime serving facade; requires AbstractRuntime 0.8.5 and AbstractCore 2.24.0.

## [0.11.3] - 2026-10-02

- Expose configurable growing-context budgets in automation summaries.
- Deliver full completed automation results to configured recipients, preserving outbox deduplication and separating digests by recipient set. Existing outboxes migrate automatically.

## [0.11.2] - 2026-10-02

### Changed

- Default workflow per app: the gateway ALWAYS resolves a default for every app interface that has at least one available workflow — the shipped bundle first (basic-agent for `abstractcode.agent.v1`, the Assistant orchestrator for `abstractassistant.agent.v1`), else the newest available workflow declaring the interface. "Clients choose" is gone from the console; the first option reads "Gateway default: <workflow>". The Workflows page and the default-workflow endpoints report the resolved default.

### Fixed

- The default-workflow dropdown lists each workflow once: a bundle the tenant catalog also lists (same id, version and entrypoint) is offered under the private registry only, and the scope is shown only when it disambiguates (the console showed two identical "AbstractAssistant Orchestrator 0.0.1" entries).

## [0.11.1] - 2026-10-02

### Removed

- The entity output-token cap: the gateway never sends `max_output_tokens` for an entity (visits,
  summons, chat, its own time), so the model works at its full capacity. The
  `ABSTRACTGATEWAY_ENTITY_MAX_OUTPUT_TOKENS` environment variable is gone, and the chat-open
  request's `max_output_tokens` field is ignored: a request that still sends it opens normally and
  the response carries a one-line `deprecation`.

### Added

- Boot applies the installer's `<data dir>/apps-upgrade.pending` (written when the installer
  upgrades with no gateway running): every installed browser app named there is brought to the
  release's version before the apps start; the file is removed, and a failed update is logged as an
  error and reported in the boot outcome.

### Fixed

- AbstractCode runs never offered `send_email` when Agent email tools were on: the run input schema
  (bundle and workflow-catalog `input_schema` routes) now serves the effective `tools` default, the
  start-node list plus the email tools when the principal's agent email tools are active.

## [0.11.0] - 2026-10-01


### Added

- **Rotate your own token.** `POST /api/gateway/me/token/rotate` (any signed-in user) answers your new
  token once; the old one stops working as soon as it answers, a browser session moves to the new
  token, and your Logs show "Token rotated". Your own Accounts row offers Rotate token (the web
  console uses this route for your row; admins keep `PATCH /admin/users/{id}` for other users).
- Recipient rules in the Email settings (Advanced): "Your agents may send to [Only the Allowed list |
  Anyone not on the Denied list]", an **Always allowed** and an **Always denied** list (addresses
  such as `name@example.com` or domains such as `example.com`, which also cover their subdomains),
  and one sentence: "Denied always wins. Your own address is always allowed. A domain also covers
  its subdomains." Every chip added or removed saves at once ("Saved", or the gateway's reason).
  One renderer, `renderEmailRecipientRules(policy, apiBase)`, draws it for any Email modal.
- `PUT /me/email/policy` takes `always_allow` and `always_deny` (a list given replaces that list;
  an omitted list is kept); `GET /me/email` returns both. `POST /me/email/policy/check` takes
  `to`, `cc` and `bcc` (and the older `addresses`, read as To) and each verdict says which rule
  decided (`source`: `self`, `always_deny`, `always_allow`, `mode`).
- The terminal console shows and edits both lists under Advanced.
- Accounts: **Show archived** (admins) lists archived accounts with an "Archived" chip; their row
  offers Logs and, in the "⋯" menu, **Unarchive** ("<id> is back, inactive: turn Active on to let it
  sign in."). **Archive** sits in the same menu and asks inline first; nothing is deleted.
- Accounts: an entity's **Email** opens the same Email settings as your own (address, mailbox,
  notifications, agent email tools, Advanced), on the entity's own mailbox.
- **Entities have their own mailbox.** An entity's mailbox lives in its home
  (`<runtime>/entities/<name>/email/...`). An admin or the entity's creator sets it up through
  `/api/gateway/accounts/{id}/email...` and `/api/gateway/accounts/{id}/notifications...`, which mirror every
  `/me/email...` and `/me/notifications...` route with the same bodies and answers; a user target, an entity
  you can't manage or an archived entity answers 403 with the reason. The entity's watcher reads its mailbox
  into its own event inbox, its notifications go to its own address through its own account, and its send
  limits and recipient rules are its own. With its Agent email tools switch on, the entity's agents get the
  email tools during visits and send through the entity's account. An archived or suspended entity's watcher
  stops. No route reads an entity's mail. The Accounts row shows the entity's real mailbox state.

### Changed

- **An entity without its own mind thinks with the gateway's text model.** Opening a visit, a
  summon or its own time no longer refuses with "no mind substrate chosen". The mind is the entity's
  own choice when it has one (`substrate.yaml`, set in the console's Manage → Mind & voice or the
  Entity app), else the gateway's text route (the console's text default, including its endpoint and
  reasoning). The only refusal left is a gateway with no text model at all: "This gateway has no
  text model yet: open Setup in the console, choose Use recommended defaults, then try again."
- **`ABSTRACTGATEWAY_ENTITY_CHAT_*` are removed** (`_PROVIDER`, `_MODEL`, `_BASE_URL`,
  `_CONTEXT_WINDOW`, `_SHELF_SIZE`). Setting them has no effect. Entity geometry uses its code
  defaults (context 65536, shelf 50) unless a request asks otherwise.
- **`GET /api/gateway/entities/{name}/substrate`** returns the entity's own choice (`provider`,
  `model`, `thinking`, `speculation`; null when it has none), `gateway_default` (the text route),
  `effective` (what the next visit uses) and `source` (`entity`, `gateway`, or `unset` with a
  `note`). **`PUT`** accepts `{"clear": true}` (back to the gateway default, recorded in the entity's
  history) and `speculation` (MTP: `false` or `{"mode": "native_mtp", "num_draft_tokens": N}`),
  applied to visits and summons.
- **Console, entity Manage → Mind & voice** uses the kit's shared pickers, the same as the apps:
  **Gateway default** or **Custom** provider and model (reasoning and MTP for a custom model), and
  the voice picker with **Gateway default voice**. Changes save themselves.
- Web console: **Providers** and **Engines** are one page. The **Providers** tab lists **Local
  providers** first (one card per local engine: status, Install, Start, Stop, Cancel, Continue,
  **Browse models**, **Learn more**, and the provider's server connection for Ollama, LM Studio and
  vLLM), then **Remote providers** (OpenAI, Anthropic, OpenRouter, Portkey, custom
  OpenAI-compatible, each with its connection state; keys appear as fingerprints only), then the
  **Available Providers** table as before. **Engines** is no longer in the sidebar; a `#engines`
  link opens Providers. The terminal console and the API are unchanged.
- Accounts: the table never scrolls sideways. Each row shows Email, Logs and Workspace (users) or
  Manage (entities), plus a "⋯" menu with only the actions that apply (Rotate token or Workspace,
  Archive). Actions that do not apply are no longer shown greyed out with an explanation under the
  row. Long names and addresses wrap instead of widening the table; when the table does not fit,
  each account becomes a block (name and Active, address and mailbox, runtime, actions).
- **Accounts are archived, never deleted.** `POST /api/gateway/admin/accounts/{id}/archive` and `/unarchive`
  (admin) answer the updated row; a signed-in user archives an entity they created with
  `POST /api/gateway/me/accounts/{id}/archive`. An archived user can't sign in (their token answers 401 and
  their sessions end); an archived entity is suspended and never wakes: the state verb, summon, talk, visits,
  meets, its own-time loop and the self-repair and need-check sweeps all refuse it. Records, runtimes, runs,
  memory and history are kept. Unarchive brings the account back inactive; turn Active on to let it sign in or
  act. Archived accounts are left out of `GET /admin/accounts` unless `include_archived=true` and are never
  listed on `GET /me/accounts`. Rows carry `archived` and `archived_at`; row actions are `email, logs,
  workspace, rotate, manage, archive, unarchive, suspend` (`delete` is gone). Logs show "Archived" and
  "Unarchived" with who did it.
- **`DELETE /api/gateway/admin/users/{id}` and `POST /api/gateway/admin/runtime-reservations/{id}/purge`
  answer 410** with "Accounts are archived, never deleted: use Archive (POST
  /api/gateway/admin/accounts/{id}/archive). Runs and history are kept." Nothing is changed. Listing and
  transferring retained runtimes stay.
- Sends to To, Cc and Bcc, from agent tools and notifications alike, follow the precedence: your own
  address is allowed, Always denied refuses ("Not sent: x@xxx.gov is on your Always denied list
  (xxx.gov)."), Always allowed allows, then the mode decides. A notification to your own address is
  therefore always delivered, also when your lists do not name it.
- A policy stored by an earlier version keeps its meaning: an allowlist's entries become the Always
  allowed list, a denylist's entries the Always denied list. The older `{mode, entries}` body is
  still accepted (`entries` replaces the list the mode uses). Needs the AbstractCore version that
  adds the two lists.
- `GET /me/email` policy carries `self_addresses`: the own addresses, shown as a fixed chip ("(your
  address)", never removable) because they are always allowed.
- Advanced reads "Send at most [100] per hour and [1000] per day." on one line with two small number
  fields (wrapping on phones), and "Watch folder" is a sentence-case label like the others.
- Web console, Sandbox: the chat is the AbstractUIC kit's chat (the thread and composer AbstractCode
  uses), with the standard Attach button, drop and paste to attach, attachment chips that say when an
  upload failed and why, hold to dictate (when a transcription route is configured) and a speaker on
  replies (when a voice route is configured). Generated images, audio and video play inline in the
  thread with a link to the raw file. The output modes, system prompt, reasoning and MTP settings sit
  above the chat; every mode stays choosable, and one that is not configured says so instead of being
  greyed out. Each mode still uses its existing gateway endpoint.
- Web console: an entity's **Manage** opens as a dialog over the Accounts page instead of replacing
  it (Esc or a click outside closes it and focus goes back to the entity's row; full screen on
  phones). Awake and Personal time are switches; the mind, voice, tools and prompt settings save
  by themselves and say "Saved", or why they were not saved; sleep, emergency freeze and the memory
  index rebuild ask for confirmation inline. Stop and Restore are no longer in Manage: the Accounts
  row's Active switch suspends and resumes an entity.
- The MCP server registry lives in the gateway's root data folder, also when several users sign in
  (one registry for every account, the one agent runs read).
- **Workflow ownership.** The Workflows page groups bundles into **Shared with everyone** and
  **Mine** (what you imported or published), with a Shipped / Imported / From AbstractFlow badge.
  `GET /bundles` items carry `owner` (`gateway` or `user`), `shipped`, `available` and `archived`; imports and
  publishes stamp `metadata.owner` (user, tenant, time). Users can import: the bundle lands in **Mine**.
- **Available to users** (administrators): a switch per shared workflow, also
  `PUT /api/gateway/admin/workflows/{bundle_id}/availability`. Off hides it from users' lists and app pickers,
  refuses their new runs, schedules and automations ("This workflow isn't available to users on this gateway.
  Ask an admin."), and pauses their existing automations on it with that reason; turning it back on does not
  resume them. Admins always see every workflow; an app's default workflow keeps running for everyone.
- **Open** (in AbstractFlow) on every workflow: opens it in the visual editor (AbstractFlow's
  `?bundle=<id>&version=<v>` link). `GET /bundles/{bundle_id}` also returns `source`, `shipped` and `owner`.
- Paused automations show why the gateway paused them (`paused_reason` in `GET /automations`).
- **Workflows are archived, never deleted.** **Archive** / **Unarchive** (and **Show archived**) replace Delete
  on the Workflows page, the terminal console (`d` / `D`) and broken bundle files; `POST /bundles/{bundle_id}/archive`
  and `/unarchive`. An archived workflow leaves the lists and cannot start new runs; the file and every past run stay,
  and existing runs and automations keep resuming. Bundles that ship with the gateway can be neither archived nor
  deleted. `DELETE /bundles/{bundle_id}` now answers `410`.

### Fixed

- **`/api/health` answers at once while the gateway warms up.** While a service was being built (the
  eager warm-up after start, or a user's first request), each health probe held the whole server for
  half a second. With the stack supervisor and five apps polling at the same time, probes waited past
  their 3-second timeout and the apps stopped with "the gateway is not reachable" until the build
  finished (minutes on a fresh checkout). Health now never waits for a build; it answers
  `"warming_up": true` (with `runner.building: true`) until the build is done, and `status` stays
  `healthy`.
- **The console says when the gateway is warming up.** While `/api/health` reports `warming_up`, the
  top bar shows a quiet "Warming up…" pill (tooltip: building the default model client; some pages wait
  until it is ready). It disappears when the warm-up ends.
- Runs now use the base URL set on the text-generation default (`output.text`, stored as `input.text`). Released 0.10.0 saved and showed it, but runs, run summaries, Ask and the sandbox called the provider's built-in address instead (for example LM Studio on `localhost:1234`). The base URL applies to the route's own provider only; an endpoint profile (`endpoint:<id>`) keeps its own address, and nothing changes when the field is empty. Affects 0.10.0; ships in the next release.
- **An attachment never crosses conversations.** A reference to another session's upload — even one
  naming its owner `run_id`, which the start door used to accept — is refused with a typed 400
  `artifact_not_in_session` by `POST /runs/start` and by the host for every in-process caller
  (bridges, entities, automations), so a client bug can no longer send one conversation's screenshot
  to another conversation's model (Mac mini, 2026-10-01: the Code web form resent the previous
  conversation's attachment; fixed there too in code web). Uploads (session-memory-owned or tagged
  `kind: attachment`) are a conversation's own; a run's produced artifacts keep the documented
  `run_id` hand-off; `shared: user` opens an upload to every session of its owner.
  `src/abstractgateway/artifact_scope.py`; tests red on removal.
- **Agents get the email tools the client lists.** The agents' tool lists are built with a user's
  host; only the "Agent email tools" switch rebuilt it, so a mailbox connected, paused or
  disconnected afterwards — or an administrator's capability change — left a run without
  `send_email` while Run settings showed it enabled (Mac mini, 2026-10-01). Connect, disconnect,
  Active and the OAuth finish now reload the caller's host (`tools_reloaded` in their answers), an
  administrator's capability change reloads every built host, and the host re-checks the rule at
  every run start and rebuilds when it moved.
- **A connected mailbox can always send.** Connect stores both legs: a leg the form or the
  discovery left out is the domain's standard one (`imap.`/`smtp.<domain>`, 993/465 SSL) and the
  connect test signs in to both — an unreachable outgoing server refuses the connect with its step
  (`detail.step: smtp`) instead of a "Connected" mailbox whose first send answers "no SMTP (send)
  settings". A mailbox stored without its SMTP leg reads `mailbox.state: receive_only` (with the
  reason) and `send_capable: false` on `GET /me/email`, `/admin/users` and `/admin/accounts`.
- **Session history never crosses sessions — pinned.** `tests/test_gateway_session_history_isolation.py`
  proves a new session starts with no messages from another session of the same user, of another
  user, or after a restart, and that nothing of the other session reaches its run vars.
- Web console: saving or clearing an entity's voice, and giving or ending a work order, did nothing
  (the page read an entity name that was never set).
- Agents can use the tools of registered MCP servers. An admin turns **Enabled for agents** on for a
  server whose connection test succeeded (`POST /api/gateway/admin/mcp/servers/{name}/agents`); its
  tools are then offered to agent runs as `mcp::<server>::<tool>`, listed in `/discovery/tools`
  (grouped per server, so the Code web and Assistant tool pickers show them) and added to a run
  started without a tool list. Each call asks for approval unless the run allows all tools. An
  archived or disabled server offers nothing, and every call checks the registry again. Header
  values stay in the gateway's secret store and are read only when a call runs; they never reach the
  run, the ledger or the prompt. A server that fails `initialize` when a run starts is skipped and
  the run records why (`_runtime.mcp_notes`). A run started by an email from someone else is never
  offered an MCP tool. Needs the AbstractRuntime MCP facade (`mcp_facade`) and AbstractCore's MCP
  clients with `initialize()`.
- **Import .flow** in the web console works again: the upload was sent as JSON instead of a multipart form, so the
  gateway refused every file. The Workflows page also keeps the import and archive result sentence on screen
  after the list reloads.

## [0.10.0] - 2026-10-01

Email settings follow one model across the consoles: your **email address** (where sign-in codes and
notifications go) and your **mailbox** (the connection your agents and automations use) are named
apart, the administrator has one switch, notifications are two switches, and the sign-in page says
what happened to a code request. Users and entities share one **Accounts** table with an Active
switch, activity logs and role-based visibility (a user sees only themself and the entities they
created; admins see all), and the Workflows page says what each bundle does and which app uses it.
The web console's sidebar is grouped, with a **Setup** button. Email send limits default to 100 per
hour and 1000 per day. The consoles work behind Tailscale or another https proxy, and the server
file helpers never serve the gateway's data folder (see Security).

Needs AbstractCore 2.22.0 (mailbox server discovery `abstractcore.comms.email.discover_servers`, the
form defaults `server_defaults` and the 100/1000 send-limit defaults) and AbstractRuntime 0.8.2; the
floors are raised in the base install and in the `apple`, `gpu` and `embeddings` extras. The
terminal console is `abstractgateway-console` 0.13.0; its matching changes are in
[console-tui/CHANGELOG.md](console-tui/CHANGELOG.md).

### Migration notes
- **Email settings model (version 3).** On the first start `capabilities.json` moves to version 3.
  A connected mailbox stays connected and keeps its settings. A user whose own Agent email tools
  switch was on while the tools were not available to them gets a per-user override that keeps
  them off; the audit log records it. Saved notification preferences carry over to the two
  switches (`job_failed` = job failed or automation failed).
- `POST /api/gateway/session/recovery/request` says what happened (`sent: false` with a
  `reason_code`: `no_email_address`, `no_mailbox`, `send_failed` or `too_many_requests`) instead of
  always answering that a code is on its way; show its `message` to the person signing in.
- **Send limits.** Defaults are 100 per hour and 1000 per day. A stored 20 / 100 without a marker
  (the old defaults) follows the new defaults; limits a user set are kept.
- **Entity visibility.** Entities created before this release have no creator and are visible to
  admins only; nothing is rewritten.

### Added
- Entity visibility by role: an admin sees every user and entity; anyone else sees only themself and
  the entities they created. `POST /entities` records `created_by` (`{tenant_id, user_id}`) in the
  new home's manifest; `GET /entities` is filtered and every `/entities/{name}/…` route answers 404
  (the same as a missing entity) for an entity the caller may not see. Entities created before this
  release have no creator and are visible to admins only (nothing is rewritten).
- `GET /api/gateway/me/accounts` (your own row plus the entities you created, the `/admin/accounts`
  row shape) and `GET /api/gateway/me/accounts/{id}/activity`; `/admin/accounts` entity rows carry
  `created_by`.
- `GET /api/gateway/admin/accounts`: users and entities in one list (role, email address, mailbox
  state, runtime, Active, entity state) with each row's actions and, when one cannot apply, the
  reason (an entity has no mailbox, no token to rotate and no delete).
- `PUT /api/gateway/admin/accounts/{id}/active {"active"}`: the Active switch for users and entities.
  Suspending an entity pauses it and switches its door credential off; resuming restores the state
  it had before (kept in the new file `<data_dir>/auth/entity_suspended.json`).
- `GET /api/gateway/admin/accounts/{id}/activity` and `GET /api/gateway/me/activity`: sign-ins,
  token rotations, runs started, automation commands, account changes and email events from the
  audit log (and its rotated files), newest first, read backwards within a fixed budget (well under
  a second on a 13 MB log). The answer says what the log does not record.
- Audit log lines now name who signed in (`signed_in`), the run a start created (`run`), the
  automation and command (`automation`) and the account an administrator changed
  (`account_change`).
- Account Logs: a run started names its workflow and opens in the Observer (`/runs/start` and
  `/runs/schedule` record `run {run_id, workflow, bundle_id, bundle_version, entrypoint}` on their
  audit line; lines written before 0.10.0 say "Run id not recorded (before this version)"), and
  notification events read "Approval needed", "Job failed" or "Test notification".
- `GET /api/gateway/bundles` items carry `source` (`shipped`, `published`, `imported`) and
  `description` (the default entrypoint's).
- `GET /api/gateway/admin/runtime-config`: every default-agent-workflow row carries `interface`,
  `label`, `app`, `help`, `group` and `state` (`clients_choose`, `builtin`, `set`, `broken`) from
  one interface table; `reason` is only set for a broken saved value.
- docs-qa 0.1.2 ships ("Docs Q&A" with a plain description; otherwise the same as 0.1.1, which
  stays installed); the native-loop and docs-qa build scripts write the shipped entrypoint names.
- `POST /api/gateway/me/email/discover {"address"}` finds a mailbox's IMAP and SMTP servers from its
  address (known providers, the domain's autoconfig file, the Thunderbird ISPDB, DNS SRV, MX), lists
  every step it tried, and returns `defaults`, the server fields a mailbox form pre-fills (AbstractCore
  `server_defaults`).
- `PUT /api/gateway/me/email` without `imap` and `smtp` discovers the servers; when none are found it
  answers `400 email_discovery_failed` with `tried`. `username` defaults to the discovered form,
  else the address.
- `PUT /api/gateway/me/email/address {"address"}` sets your own email address on your user record
  (`""` clears it).
- `PUT /api/gateway/me/email/notifications {"job_failed"?, "approval_needed"?}` sets the two
  notification switches.
- `GET /api/gateway/me/email` also returns `email_address` (where your sign-in codes and notifications go),
  `email_available`, `notifications`, `notifications_unavailable_reason`, `oauth_providers` and,
  in `agent_tools`, `on`, `unavailable_reason` and `admin_available`; `registered_address` is
  filled before a mailbox is connected too.
- `GET /api/gateway/admin/email/capabilities` returns each capability's `advanced` flag.
- `PUT /api/gateway/me/email/folder {"folder"}` sets the folder your mailbox is read from (empty =
  INBOX) without reconnecting; `404 email_not_configured` without a mailbox.
- `GET /api/gateway/admin/users` rows carry `email_account.capabilities` (`{value, source}` for
  `email` and `email_agent_tools`), so the consoles can show a per-user override with a Reset.
- `GET /api/gateway/apps` carries `browser_gateway_url`, the address the caller uses (the https
  origin behind a proxy). `gateway_url` stays the address the app servers use.
- `GET /api/gateway/network` carries `browser_url` (the caller's address) and
  `browser_url_listed` (false behind a proxy or tunnel).
- Docs: "Reached through Tailscale (https)" in configuration.md and deployment.md, and the
  https same-origin rule in security.md.

### Changed
- New dependency on Python 3.10: `tomli>=1.1` (`python_version < '3.11'`), used to read a checkout's
  `pyproject.toml` when `GET /api/gateway/bundles` tells shipped workflows from imported ones.
- Email send limits default to 100 per hour and 1000 per day (were 20 and 100; AbstractCore's
  defaults). A new mailbox no longer stores the defaults, so it follows them; limits a user sets
  (`PUT /me/email/limits`, Advanced in the consoles' email settings) are kept across upgrades. An
  unmarked 20 / 100 stored before is the old default and follows the new defaults; any other
  unmarked value is kept (`limits.source` is `legacy`). The one-time import of AbstractCore's local
  account copies its limits only when someone set them.
- `POST /api/gateway/me/notifications/test` answers with `sent`, `reason_code` (`no_mailbox`,
  `mailbox_paused`, `rate_limited`, `queued_behind`, `send_failed`), a ready sentence in `message`
  ("Not sent: hourly limit reached (100 of 100 this hour) — resets at 14:05.") and `limit`. Notices
  held back by a send limit keep the reason and the retry time (`outbox.rate_limited` in
  `GET /me/notifications`). `POST /me/email/test` carries a `message` too.
- `/admin/users` rows and `GET /me/email` carry `email_address` and `mailbox` from one resolver, so
  an administrator's own row matches their email card (it showed "—" / "not connected" when the
  address lived in the gateway's operator setting). The address shown on the email card and in the
  Accounts table is where sign-in codes and notifications go: the registered email address, else
  the account's own connected mailbox address, so a mailbox connected before connecting set the
  address no longer shows "No address" next to "Connected as …".
- Connecting a mailbox sets your email address when it is empty; the user name and display name
  are optional (the discovered login or the address; the stored name or the address's local part).
- The administrator's one switch is **Mailboxes for users** (capability `email`, on by default).
  **Agent email tools for users** (`email_agent_tools`) is now on by default, next to **Sign-in by
  email** under Advanced; each user still switches their own agent email tools on. On the first
  start `capabilities.json` moves to version 3: a user whose own switch was on while the tools were
  not available to them gets a per-user override that keeps them off, recorded in the audit log.
- Notifications are two switches, **Job failed** (an automation failed after its retries) and
  **Approval needed**, both on by default and sent once a mailbox is connected. An automation's
  "Email me the result" and a run's "email me when done" deliver on their own. Saved preferences
  carry over (`job_failed` = job failed or automation failed); the earlier five-event body of
  `PUT /api/gateway/me/notifications` is still accepted.
- `POST /api/gateway/session/recovery/request` answers what happened: `sent` with the masked
  address (`l•••@•••`), `no_email_address` (also for an unknown account) or `too_many_requests`
  with `retry_after_s`; `404 recovery_off` when sign-in by email is off. `purpose` defaults to
  `sign_in`. Rate limits and audit events are unchanged. See docs/email.md for the trade-off.
- A failed mailbox connect names the step: `detail.step` (`imap` or `smtp`) and a message such as
  "Sign-in refused by imap.example.com — check the password." or "Couldn't reach
  smtp.example.com:465."
- `PATCH /api/gateway/admin/users/{user_id}` refuses to deactivate the caller's own account
  (`409 cannot_deactivate_self`, "You can't deactivate your own account.").
- Route summaries, error messages and docs say "email address" or "mailbox"; unavailable switches
  give their reason ("Connect a mailbox first.", "Your admin turned mailboxes off.", "Your admin
  turned agent email tools off.").

### Web console
- The sidebar is grouped: Accounts; Work (Workflows, Runtimes, Apps); Models (Providers, Models,
  Engines, Multimodal); System (Resources, Sandbox, Network). **Setup**, at the bottom of the
  sidebar (administrators), opens the setup guide; the flag button in the top bar is gone.
- **Accounts** (was "Users & Entities") is one table of users and entities: name with a kind chip
  (Admin, User, Entity), email address ("No address" when none), mailbox, runtime, an **Active**
  switch on every row and the actions Email, Logs, Workspace, Rotate, Manage (entities) and Delete.
  Rows are tinted by kind, with a legend. An action that cannot apply is disabled and one line in the
  row says why. Active asks before deactivating a user or suspending an entity and resumes an entity
  in its previous state; your own row cannot be deactivated. Deleting asks in a row under the
  account. The separate entity roster and the per-user Email on/off and Agent tools buttons are
  gone.
- Accounts for someone who is not an administrator: titled "Your account", the same table with
  their own row and the entities they created, without Create user or Email for everyone.
- **Email** on a row opens a dialog: on your own row, your email settings; on another user's row,
  their email address with Save and a read-only mailbox line; on an entity, why it has no mailbox.
  Your email settings open only from there.
- **Logs** on a row opens the account's activity from the audit log, newest first in your local
  time, filtered by sign-ins, runs, automations or email; a run links to the run in the Observer app
  (`/apps/observer/#run/<run_id>`). The footer says in plain words what is recorded, and a mailbox
  connection reads "IMAP · password sign-in", "Google sign-in" or "Microsoft sign-in".
- **Email for everyone** under the table holds the administrator's **Mailboxes for users** switch,
  with **Agent email tools for users** and **Sign-in by email** under Advanced.
- Create user asks for the **Email address** next to the user ID and role; Advanced keeps Runtime
  and Tenant, each with a sentence saying what it is for.
- Email settings are a page of cards: **Your email address** (one editable address field at a time;
  "Not set yet — connecting a mailbox below sets it." with **Set it now** until one is set),
  **Mailbox**, **Notifications** (**Job failed**, **Approval needed**, **Send a test**), **Agent
  email tools** and Advanced. The mailbox tabs are IMAP (first and default), Google, Microsoft. The
  IMAP servers are always visible and filled in as soon as the address has a domain, then replaced by
  what discovery finds unless you edited them. No user name or display name field (a small link
  reveals a login field for providers that need one). Once connected: one status line, the
  mailbox's **Active** switch, Test and Disconnect. "Send a test" always answers with a sentence,
  reset times in your local time. Advanced reads as three sentences (recipients, send limits,
  folder). Switches and limits apply on change.
- The workspace policy disclosure reads **Workspace policy** with one helper line.
- Workflows says what workflows are and lists one row per bundle with its name, what it does,
  version, source and the apps that use it ("No app" when none); expand a row for its versions. A
  manifest version 0.0.0 reads "unversioned". **Drafts** and **Older versions** are switches.
  Delete asks in the row; for a bundle that ships with the gateway it says nothing puts it back at
  restart (only a reinstall does). Every bundle the repository keeps in `flows/bundles/`
  (map-reduce, structured-extract, adversarial-review, the meta-* agents) counts as shipped on a
  checkout deploy.
- "Default workflow per app" uses plain names with a (?) explanation, applies a choice at once,
  and warns only about a broken default; interfaces no app asks for sit under "Other workflow
  types". The **Streamed replies** switch sits under Settings on the same page.
- The setup guide's model cards name the engine that runs a route ("faster-whisper · base"), with
  the download as the small detail. A model AbstractCore could not look for (no Hugging Face cache
  yet on a fresh install) shows "Download needed" with the reason and a Download button, in the
  setup guide and on Multimodal, not "Unknown".
- The sign-in card has one column, one status ("Not signed in", "Signed in as admin", "Token
  refused") and errors under the field that failed ("This token was refused.", "Can't reach the
  gateway at …"). Sign-in by email is one link, **Forgot your token? Email me a sign-in code**: it
  shows "Sending…", then the code step in its place with what happened, a code field (Use code is
  available at 8 digits), **Send a new code** after 30 seconds and **Back to token**.
- **Workflows paused** and **Start at login** are switches instead of Pause/Resume and Turn on/Turn
  off buttons.
- The Runtimes page says what a runtime is: a user's own data plane (runs, flows, sessions and
  memory).
- Copy buttons work over plain http (a LAN or Tailscale address), and say "Copy failed — select and
  copy" when the browser refuses. Ids for commands and the docs assistant are random UUIDs over
  plain http too.
- On touch screens reading text is never below 14 px: the console's small type steps have a floor
  there (they were 11-13 px).
- On phones each account is one flat block and the email settings' cards are flat sections; lists
  no longer scroll inside the page.

### Fixed
- Sign-in by email answers with the real outcome: `no_mailbox` when the account has an email
  address but no mailbox to send the code from (it used to read like "no email address"), and
  `send_failed` when the mail server refused the code or could not be reached (the request waits up
  to 12 s for the send; before, it answered "on its way" before the mail left). The cause stays in
  the audit log, never in the answer.
- The web console's `[hidden]` attribute always wins over a display rule (a hidden "Cancel sign-in"
  on the Google and Microsoft tabs stayed visible), and two regular-expression escapes in the
  console's inline script no longer raise a `SyntaxWarning` at import.
- Checkbox and form labels in the console were 18-20 px bold: the console carried its own copy of
  the sign-in card's styles, and its checkbox rule was reused outside the card. The console now
  uses the kit's sign-in card styles; labels are 14 px, helper text 13 px.
- The Sandbox's system prompt field no longer shows "optional" as a placeholder.
- Resuming a paused `email.received` automation wakes the email worker at once, as creating one
  already did: the watcher takes its new-mail mark within seconds, so mail arriving right after
  the resume triggers the automation (it was taken as history until the worker's next tick, up to
  15 s later). The runner calls the worker after each applied automation command
  (`GatewayRunner.add_automation_command_listener`).
- Installing two apps that need Node.js at the same time, or clicking **Install Node.js** while an
  app install is fetching it, no longer fails one of them. Both installs used to download into one
  shared partial file, and the second failed with `FileNotFoundError`; a retry worked. Now one
  Node.js install runs at a time. The second waits for it and uses the Node.js it installed, so it
  is downloaded only once. Every download also writes its own partial file and renames it into
  place when done.
- Stopping the gateway while it is still starting (Ctrl-C or `SIGTERM` a few seconds after
  `serve`) now waits for the startup. Before, the stop returned at once, and the startup went on to
  start the runner, the email worker and the bridges of a gateway that had already stopped. The
  stop now cancels the startup, which starts nothing more and stops what it built. It waits for
  it, and for the background task that restarts each user's runner, before stopping the service.
  In the same way, apps that were still starting are waited for and stopped. Before, an app could
  start after the others were stopped and outlive the gateway.
- The identity card's moments no longer depend on the clock minute. A state-history line is
  treated as the history copy of a sleep/wake/pause change made through the gateway when a marker
  with the same verb and the same reason follows it within an hour. Before, the rule was "same
  verb in the same minute". A change made across a minute boundary then showed twice, and the
  entity's "asleep at birth" moment disappeared when a sleep through the gateway fell in the same
  minute (backlog 0920).
- Behind `tailscale serve` (or any TLS proxy on the gateway machine that keeps the browser's
  `Host`), signing in to the console at `https://<host>.<tailnet>.ts.net/` no longer fails with
  403 "origin not allowed". An https page asking its own address is accepted when the request
  came over TLS (native, or `X-Forwarded-Proto: https` from a loopback proxy). No
  `allowed_origins` entry is needed. A plain-http Origin never qualifies.
- Apps served at `/apps/<app>/` answer their web app manifest and icons (`manifest.webmanifest`,
  `favicon*`, `icon*`, `apple-touch-icon*`, `icons/<file>`) without an app session, because
  browsers fetch them without cookies. The console no longer logs `manifest.webmanifest 401`.
  These requests are relayed with no cookie and answered only with a manifest or image type,
  with no `Set-Cookie`. Pages, scripts, `sw.js` and `api/` still need the session.

### Security
- The server file helpers (`GET /files/list|search|read|skim`, workspace import and export) never
  serve the gateway data folder or the account's credential folders, the same rule as the run
  workspace browser and every run's tools. The server workspace root defaults to the gateway's
  working directory, which is the data folder under the OS service and contains it after a launch
  from a parent folder: an administrator could read another user's received mail and run ledgers
  through `/files/read`. Administrators never read mail.
- `POST /entities` refuses a name the gateway already holds (409 "That name is taken") when the
  caller's runtime has no home under that name: an entity created by another user in another
  runtime, or a user account's name. Entity names belong to the whole gateway while homes belong to
  a runtime, so such a create used to take over the existing account as the new entity's identity.
  Re-creating your own entity still answers as before. `POST /entities/{name}/validate` reports the
  same refusal.

### Tests
- Two CI timing flakes are now deterministic. The automations test `test_d2_waiting_only_on_a_person`
  expects that an occurrence names its run for a moment before the run exists, and it forces that
  moment. `test_identity_card_composes_a_life` fixes its timestamps instead of reading the clock.

## [0.9.0] - 2026-09-30

The web console works on phones, tablets and any window size. It follows the AbstractFramework
responsive system of `@abstractframework/ui-kit` 0.3.2. It also carries the mail watcher fixes
prepared as 0.8.2, which was not published. Dependencies are unchanged (AbstractCore 2.20.2,
AbstractRuntime 0.8.1, terminal console 0.12.0).

### Changed
- Below 1024 px the console sidebar is a drawer that opens from a **☰** button in the header.
  Escape, a tap outside it, its close button or picking a section closes it, and the page behind
  it is inert while it is open. From 1024 px up the sidebar stays a column.
- The header wraps its top-right buttons under the page title on narrow screens and uses a single
  thin row in phone landscape. The page respects the notch and home-indicator areas
  (`viewport-fit=cover`) and uses the dynamic viewport height, so nothing hides under the mobile
  browser bars.
- Dialogs open as bottom sheets below 768 px wide or 500 px tall, with their buttons always
  visible. A dialog opened while a drawer is open appears above it, and Escape closes the top one
  first. The docs assistant drawer is full width below 768 px.
- On touch screens, buttons and rows are at least 44 px tall, form fields use 16 px text (iOS does
  not zoom into a focused field), and reading text is 14 px.
- The layout uses the framework breakpoints: 480, 768, 1024 and 1440 px wide, plus 500 px tall for
  phone landscape.
- Console themes and kit components are synced from `@abstractframework/ui-kit` 0.3.2, including
  its responsive tokens (`--tap-min`, `--vh-full`, `--safe-*`, `--gutter`, `--font-size-*`).

### Fixed
- Model catalog cards keep their layout in narrow windows and on tablets.
- On phones the console is never wider than the screen.
- The Sandbox composer is reachable at every window size: the transcript gives way first, and the
  chat card scrolls when the composer still does not fit (for example at 1280x800 or 1366x768).
- The action buttons of a users row keep their spacing when the table is shown as cards.
- Mail sent to test a new "When an email arrives" automation right after creating it triggers it.
  The mail watcher reads nothing until an email automation exists and its first read only marks
  where new mail starts; that read came up to a minute after the automation was created (checks
  that read nothing counted as polls, and nothing woke the watcher), and mail arriving in that
  minute was treated as already there. Creating an email automation now wakes the watcher, which
  marks the start within seconds.
- A new (or resumed) "When an email arrives" automation no longer runs on mail that arrived while
  you had no email automation active: the watcher did not read the mailbox in that time, and its
  next read delivered all of it as new mail. That mail is now treated as already there.

## [0.8.1] - 2026-09-30

Needs AbstractCore 2.20.2 and AbstractRuntime 0.8.1 (the floors are raised; the terminal console
stays 0.12.0).

### Fixed
- An "Email me the result" automation whose filter matches its own result email no longer
  re-triggers itself. Notices and recovery codes are sent with `Auto-Submitted: auto-generated`
  and `X-AbstractFramework-Automation`, as is every send an automation makes through your
  account; each such Message-ID is recorded in the outbox, and the watcher never admits your own
  automatic mail (the poll result counts it in `own_automatic`). The trigger also skips automatic
  mail from others by default (`auto_submitted: "skip"`).
- Text-to-speech, transcription and image requests no longer fail when the text default cannot be
  built (for example an MLX route on a light install): media-only requests run without the text
  model.
- The runner no longer ticks the child runs a media route executes in-process; such a child was
  failed as "workflow ... not registered (after 40 consecutive attempts)", sometimes after it had
  succeeded.
- A chat's `send_email` to your own address runs without an approval wait under the default
  policy, and runs and automations use the registered address My email shows (your user email,
  else the connected mailbox's address); an administrator without an email on their user record
  had every self-send parked.
- A run started without `input_data.tools` gets the workflow's default tools plus the email tools
  when your agent email tools are active; an explicit tool list is used as given.
- A capability-default save that moves a route to another provider without naming options drops
  the old engine's speculation request (an LM Studio text route no longer keeps an MLX route's
  `speculation: native_mtp`).
- A live stream no longer misses a run's end when the runner wrote the run's last line just as the
  API process reached the end of the stream file (the open reply never got its closing frame).

### Docs
- Model downloads: the Supertonic 3 examples show its real size (about 401 MB).
- Email: automatic mail and the loop guard, who "you" is for sends without approval, the default
  tool list.

## [0.8.0] - 2026-09-30

AbstractGateway 0.8.0 ships with the terminal console 0.12.0 (see
[console-tui/CHANGELOG.md](console-tui/CHANGELOG.md)). Needs AbstractCore 2.20.0 (the mail library) and
AbstractRuntime 0.8.0 (account binding, event inbox, `email.received@1`); the dependency floors are
`abstractcore>=2.20.0` and `AbstractRuntime>=0.8.0` (also in the `apple` and `gpu` settings), and a host refuses to
build on a runtime without the email seams.

### Added
- **Per-user email: one user, one runtime, one mailbox** ([docs/email.md](docs/email.md)). Every signed-in person
  connects their own mailbox from the web console (Users tab → **My email**), the terminal console (Users → `@`) or
  `PUT /api/gateway/me/email`: IMAP/SMTP with a password or app password, or OAuth2 for Google and Microsoft (device
  code or a browser on the gateway's computer; your own OAuth client, the gateway's, or the built-in one). The
  account is tested before it is stored, stored in the user's own data home with the credentials encrypted
  (AES-256-GCM, key in the OS keychain), and every connection verifies TLS. Failures name the cause and the fix.
- **Nothing email-related is on by default**: the watcher runs only for a user with an email-triggered automation,
  every email notification event is off until the user turns it on, and nothing is seeded at first run.
- **What is available is the administrator's decision**: gateway-wide defaults with per-user overrides for `email`
  (default on), `email_agent_tools` (default off) and `email_recovery` (sign-in by email, gateway-wide, default on):
  `GET/PUT /api/gateway/admin/email/capabilities`, `PUT /api/gateway/admin/users/{id}/email {enabled?, agent_tools?,
  inherit?}`; web console **Email for users** and row buttons, terminal console `x` / `X` and **Email for users
  (admin)**.
- The administrator's gateway account starts, once, from AbstractCore's own local account when the gateway has none;
  the consoles say which account is being edited.
- **Agent email tools** per user, off by default (`PUT /api/gateway/me/email/agent-tools`, web console My email,
  terminal console **Policy, limits & tools**): agents and workflows get list/search/read/folders/send/reply/attachment
  tools only when the administrator made them available, the account is connected and allowed, and the switch is on —
  applied when the toolsets are built (the switch reloads the user's workflows) and again when a tool runs. The run
  binding follows the account, so user-authored send-email actions and notifications work with the switch off.
  `GET /api/gateway/discovery/tools` lists the email tools as enabled only for such a caller; the
  `ABSTRACT_ENABLE_EMAIL_TOOLS` environment gate no longer enables them on the gateway.
- **Recipient policy and send limits** per user (`allowlist` / `denylist` of addresses and domains; 20 per hour,
  100 per day by default, editable), applied to every send: agents, automations, notifications, recovery codes.
- **Mail watcher** per user: read-only, a durable cursor that moves only after the message is stored in the user's
  runtime event inbox, safe across UIDVALIDITY resets, one bad message never blocks the mailbox, capped backoff with
  cause and fix. It runs only while the user has an active `email.received@1` automation and wakes those automations
  on new mail (every 60 s; how often an automation runs is its own trigger setting — hourly by default when it runs a
  model).
- **Email notifications** (`GET/PUT /api/gateway/me/notifications`, `POST /me/notifications/test`): automation results
  and failures (for automations set to email), approval needed, and job finished/failed for runs started with
  `_runtime.notify = {on: [...], channels: ["email"]}`. Sent to your registered address through your own account from
  a durable outbox: queued once, never resent after an interrupted send, retried on temporary refusals, a digest when
  the send limits are reached. Fixed templates.
- **Account recovery by email**: "Forgot your token?" and "Email me a sign-in code" on the sign-in page when some
  account has email (`GET /api/gateway/session/recovery`, `POST …/request`, `POST …/redeem`). Single-use 8-digit codes,
  10-minute expiry, stored as keyed hashes, rate-limited per account and client address, the same answer for every
  account, audited without the code.
- **Administrator switch**: `PUT /api/gateway/admin/users/{id}/email` (web console **Email off / Email on**, terminal
  console `x`) turns email off or on for a user — no watcher, no sending, no notifications, settings kept.
  `GET /admin/users` rows and `GET /admin/users/{id}/email` show the mailbox state, address and last error, never
  mail, recipient lists or credentials. Bring-your-own OAuth clients per provider:
  `GET/PUT /api/gateway/admin/email/oauth-clients[/{provider}]` (secrets sealed, never returned).
- Runs are bound to their user's account at the door (`_runtime.email_account`, set by the gateway; client values
  removed); each runtime gets its durable event inbox and an in-memory credential resolver that answers only for its
  own account. The send-email action workflow is registered on every host.
- Typed audit events: `email.connected`, `email.tested`, `email.disconnected`, `email.capability_changed`,
  `email.cursor_reset`, `email.message_unprocessable`, `email.notification_sent|failed`,
  `email.recovery_code_issued|used|refused`, `email.oauth_client_changed`, `email.legacy_imported`.

### Changed
- `POST /api/gateway/automations` accepts `notify` (`{"channels": ["console", "email"]}`) like an edit, and passes
  `policy.email_allowed_recipients`, `policy.untrusted_input_tools` and the `email.received@1` trigger configuration
  to the runtime, which validates them.
- Tool listings (`/discovery/tools`, `/discovery/capabilities`, skills' tool checks) follow the caller's agent-tools
  state; a disabled email row names the runtime's typed reason (not available to the user — ask your
  administrator, email turned off by an administrator, not connected, agent tools off).
- Maintenance notices (triage, entity repair, backlog runner) are emailed to the administrator's registered address
  through the administrator's own account and the notification outbox.
- The email tool catalog row lists `reply_email`, `search_emails`, `get_email_attachment` and `list_email_folders`.

### Removed
- The email bridge (`ABSTRACT_EMAIL_BRIDGE` and its polling variables) and every `ABSTRACT_EMAIL_*` configuration
  path, including the process manager's email environment overrides. On the first start a gateway that still has them
  imports that account once into the administrator's email settings; afterwards the variables are ignored and each one
  still set is named at startup (and in the administrator's **My email** notices) with the setting that replaced it.
  `ABSTRACT_BACKLOG_EMAIL_TO` / `ABSTRACT_TRIAGE_EMAIL_TO` and the related account variables are ignored.

### Deprecated
- `GET /api/gateway/email/accounts`, `GET /api/gateway/email/messages[/{uid}]` and `POST /api/gateway/email/send` act on
  the calling administrator's own account (message bodies whole; the recipient policy and limits apply to sends) and
  will be removed in a later minor release. Use `/api/gateway/me/email`.

### Security
- **OAuth sign-in cannot be pointed at another host.** `POST /api/gateway/me/email/oauth/start` accepted
  `token_endpoint`, `authorization_endpoint`, `device_authorization_endpoint` and `scopes` from any user, while the
  client it used could be the administrator's (with its secret): a user could make the gateway send that secret, or
  POST, to an internal address. For Google and Microsoft these fields are now refused with
  `403 email_oauth_override_refused` (the answer names the fields), for administrators too, and a `tenant` must be a
  tenant id or domain. Explicit endpoints and scopes are accepted only from an administrator with
  `provider: "custom"`, which always brings its own client id; a user asking for `custom` gets the same typed 403
  (was 400 `email_invalid_settings`). Independently, the gateway's and the built-in OAuth client are only ever used
  against their provider's own endpoints (gate review of framework backlog 0992).
- While an administrator has email off for a user, that user's Connect (`PUT /me/email`), Test
  (`POST /me/email/test`) and OAuth sign-in are refused with `409 email_disabled` before any connection to a host the
  user chose.
- Account recovery checks the per-account and per-client rate limits on the request thread, before any background
  send is started: a flood of requests starts no threads (the answer stays the same for every account).
- The same maintenance notice is emailed at most once per UTC day (the deduplication key used to be "once ever", so
  a condition still true the next day was never reported again).
- Error texts name the real place to add a gateway OAuth client (`PUT /api/gateway/admin/email/oauth-clients/<provider>`)
  instead of a console menu that does not exist; `email.md` is in the documentation site's navigation.

## [0.7.4] - 2026-09-29

Dependencies: AbstractCore 2.19.2 or newer and AbstractRuntime 0.7.3 or newer (also in the `apple` and `gpu`
settings). The terminal console (`abstractgateway-console`, see [console-tui/CHANGELOG.md](console-tui/CHANGELOG.md))
is 0.11.2.

### Changed
- **Speech input is part of the fresh setup.** With AbstractCore 2.19.2 a fresh install seeds `input.voice`
  (faster-whisper `base`) with text, voice and image, and **Use recommended defaults** / *Apply recommended* set it
  too, so transcription runs locally without an OpenAI key. The capability-defaults payload reports
  `seeded: "recommended-v2"`; a store seeded by an earlier release gains the speech-input route once. The
  `apply-recommended` endpoint's `only` selector accepts `stt` for speech input.
- **A loaded image or video model is reused.** After **Load** in the console (or `POST /models/load`), image and
  video requests for that model run on the loaded pipeline instead of loading the model again for each request
  (AbstractRuntime 0.7.3). Measured with FLUX.2 [klein] 4B on a 16 GB NVIDIA card: 17-19 s per image instead of
  54-59 s.

### Fixed
- The setup guide's speech-input card is titled **Transcription** in the web console and the terminal console, and
  the terminal console's Models screen treats speech input as a recommended route (`a` offers **Replace mine too**).
- A fresh image-model load reports `loaded` (`loaded_new: true`) instead of `already_loaded` (AbstractVision 0.3.33).
- Unloading a Diffusers image model releases its memory (AbstractVision 0.3.33).
- The email bridge passes message bodies whole (ADR-0026: no character caps on model inputs). It clamped text and
  HTML bodies at 20,000 characters by default; `ABSTRACT_EMAIL_MAX_BODY_CHARS` / `ABSTRACT_EMAIL_MAX_HTML_CHARS`
  still set an explicit bound when given.

### Security
- **Email connections verify TLS** (framework backlog 0992 WP0). The email bridge opened IMAP with no SSL context,
  which on CPython 3.12 checks neither the certificate nor the host name, so anyone on the network path could read
  the password. It now uses `ssl.create_default_context()` and a bounded connect, and a failed check is refused
  before login with the host, the reason and the fix. The `/api/gateway/email/*` routes and the maintenance
  notifier use AbstractCore's mail tools, which verify TLS from AbstractCore 2.19.2.
- **Automations no longer email anyone the model chooses.** With the default `policy.tool_approval: "auto"`, the
  grant pre-approved `send_email`, so an occurrence that read an inbound email or a web page could mail data to an
  address that text named. From AbstractRuntime 0.7.3 message-sending tools are never in the grant: a
  `send_email` to your registered email runs unattended, any other recipient waits for approval. The gateway
  freezes the registered email (the account's email, or the `operator_email` setting without accounts) into the
  automation's target inputs at creation and on a target revision; a value a client sends is never trusted.
- The email bridge's `imap_password_env_var` is always the name of an environment variable; a value that is not a
  variable name was read as the password itself and is now refused with the fix (never echoed).

## [0.7.3] - 2026-09-29

Dependencies: AbstractCore 2.19.1 or newer and AbstractRuntime 0.7.2 or newer (also in the `apple` and `gpu`
settings). The terminal console (`abstractgateway-console`) is unchanged at 0.11.1.

### Changed
- **Linux + NVIDIA recommendations.** With AbstractCore 2.19.1 the first-run guide and **Use recommended defaults**
  follow its recommendations for NVIDIA hosts: the LM Studio text download is `qwen/qwen3.5-9b@q4_k_m`, and
  `output.image` is FLUX.2 [klein] 4B through Diffusers (`black-forest-labs/FLUX.2-klein-4B`, fits a 16 GB card);
  video stays not available there.
- **Engine installs name only AbstractCore's install settings.** MLX, llama.cpp, Hugging Face and vLLM install
  `abstractcore[apple]` (Apple silicon) or `abstractcore[gpu]` (Linux) at the installed AbstractCore version,
  never `mlx-lm`, `llama-cpp-python`, `vllm` or the deprecated `abstractcore[huggingface]` alone (llama.cpp still
  takes upstream's prebuilt wheel). On an Intel Mac or a Rosetta Python the llama.cpp and Hugging Face rows say the
  engine is not available on this machine with the install settings.
- `abstractgateway models download --help` shows `qwen/qwen3.5-9b@q4_k_m` as its LM Studio example.

### Fixed
- Transcription runs on the configured `input.voice` route (for example local faster-whisper) without an OpenAI key,
  and the Voice screen's transcription model list names each engine's own models (AbstractCore 2.19.1,
  AbstractVoice 0.13.1, AbstractRuntime 0.7.2).

## [0.7.2] - 2026-09-29

No dependency changes: AbstractCore 2.18.0 or newer, as before. With AbstractCore 2.18.1 the first-run guide follows its
recommendations (a Mac too small for the recommended image model shows it as not available here, with the reason). The
terminal console (`abstractgateway-console`, see [console-tui/CHANGELOG.md](console-tui/CHANGELOG.md)) is 0.11.1;
0.11.0 sends no installer sha256, so it cannot start the update of an installer install (the gateway answers "check
again first").

### Changed
- **Updating an AbstractFramework installer install runs the installer.** When the gateway was installed by the
  AbstractFramework installer (its data folder holds the installer's `bootstrap.env`), **Check for Updates** (tray),
  **Check now** (web console) and `u` in the terminal console's F3 compare the installed AbstractFramework release
  with the newest one (the install manifest on the framework repository's `main`), not with the newest gateway on
  PyPI. **Update** runs that commit's `scripts/install.sh`, the script of the one-line install, as
  `/bin/sh install.sh --yes --no-start --no-open --no-modify-path --data-dir <data dir>`: nothing is asked, start
  at login stays as it is, every package moves to the release's tested versions, and the running gateway is not
  stopped. The confirmation names the script's `main` address, the commit, its sha256 and the command, and the
  update runs only the script shown: `installer_sha256` on `POST /host/update/start` is required for an installer
  install (409 "check again first" without it, 409 when it differs from the last check's). Each run executes its
  own new copy of the script in `<data dir>/update/`, and one update runs at a time: a second start while one runs
  is refused before anything is written. The job log
  streams the installer's output; the result lists what moved and offers the restart, says **already up to date**
  when nothing changed, or gives the installer's exit code and where its log is. On Windows the hint shows the
  PowerShell line (the installer stops and restarts the gateway there). The gateway no longer runs
  `uv tool upgrade abstractgateway` for these installs (uv keeps the installer's `==` pin, so it changed nothing).
- **One update rendering for every client.** `GET /host/update` carries `update`: the status, the version line, the
  hint, what an update installs, and the action with its label, command, source and confirmation. The tray, the web
  console (which also shows the update log) and the terminal console show it word for word; `POST
  /host/update/start` answers the same payload as `GET /host/update`. A hand-made `uv tool` install pinned to one
  version says it cannot be upgraded in place and gives the reinstall command.
- The terminal console (`abstractgateway-console`) shows the gateway's update line, hint and confirmation and sends
  back the installer's sha256; with an older gateway it keeps its own rendering.

### Fixed
- **Start at login on macOS no longer fails with `5: Input/output error`.** Replacing a running login item
  (`launchctl bootout`, then `bootstrap`) now waits, up to 15 seconds, until launchd has removed the previous job,
  and retries a `bootstrap` that answers 5 or 37. `service uninstall` waits too.
- **A failed macOS login item leaves one clear state.** When `launchctl bootstrap` still fails, the message says how
  many attempts were made and why (a code other than 5/37 is tried once and said to be not retried), and start at
  login is off: the plist this command wrote is removed from `~/Library/LaunchAgents`, so nothing loads at the next
  login. A gateway that ran as that login item was stopped by `launchctl bootout`; once the cause is fixed,
  `abstractgateway service install` registers the login item and starts it again.
- **An update whose output is not UTF-8 keeps running to its end.** The job reads the command's output with invalid
  bytes replaced (U+FFFD in the log), and if reading fails anyway the gateway stops the command's whole process group
  and waits for it before reporting the failure: the job never leaves "running" while the command runs, so a second
  Update can never start a second installer next to the first.
- **An update that hangs without printing is stopped.** The 30-minute job limit is a watchdog that stops the whole
  command (its process group), not a check made only when the command prints a line.
- **A version the update check cannot read is an error**, never "up to date" (an unreadable AbstractFramework
  release or PyPI version).
- **The tray and the web console no longer stick on a refused update.** When the gateway refuses to start an update
  (the installer changed, one is already running, check again first), the offer is dropped and the next step is a new
  check. The terminal console's `U` shows a failed check's reason instead of "no update is available".
- On Windows, the hint of an installer install gives the PowerShell line to run; it no longer says Update runs the
  installer. A temporary installer file a crashed write left behind (older than an hour) is removed at the next
  update.
- The tray reports an update that installed nothing newer as **Already up to date** instead of "The update didn't
  finish", and a failed update shows the job's reason and the last lines of its log.

## [0.7.1] - 2026-09-28

No dependency changes. The terminal console (`abstractgateway-console`) stays 0.11.0.

### Fixed
- **Terminal apps land on PATH.** The gateway installs Code's terminal app (`abstractcode`) where uv put the
  gateway's own commands (`uv tool dir --bin`, usually `~/.local/bin`, read from the tool's `uv-receipt.toml`). This
  is the folder the installer puts on PATH and builds `abstractcode` and the terminal console into, so there is one
  place whichever of the two installed it. The presence check finds it there even when the gateway's own PATH lacks
  it (a gateway started at login). A copy a gateway before 0.7.1 left in `<data dir>/apps/bin/` is still found and
  offered the update; the next install removes it. A gateway that is not a uv tool install keeps
  `<data dir>/apps/bin/`. `command` is the bare name when the app runs by name.
- **Installing the terminal app never replaces another program.** When the install folder already holds a different
  `abstractcode` (for example the older Python AbstractCode from PyPI, installed with `uv tool`), the install stops
  with `foreign_binary` (409), names the file and says what to do (`uv tool uninstall abstractcode`, or remove it);
  the file is left as it is. The gateway's own copy, or the installer's build, is still updated in place.
- A `uv-receipt.toml` of an unexpected shape (no `[tool]` table, no `entrypoints` list, a relative `install-path`)
  is ignored: the gateway uses `<data dir>/apps/bin/` and starts normally.

## [0.7.0] - 2026-09-28

Requires AbstractRuntime 0.7.0 (the session history window and its public `window_transcript`), AbstractCore 2.18.0
(`engine_missing`, `needs_gpu_limit`), AbstractVoice 0.13.0 (`abstractvoice.engine_runtime`, `voice_openai_api_key`,
now declared directly) and AbstractAgent 0.3.17 (entity visit turns send the runtime's history window). The gateway
refuses to build its workflow host on a runtime without the history window. The terminal console
(`abstractgateway-console`, see [console-tui/CHANGELOG.md](console-tui/CHANGELOG.md)) builds on
`abstractcore-console` 0.4.

### Added

- **Apps are served through the gateway at `/apps/<id>/`** (HTTP streaming, server-sent events, WebSocket), gated by
  the app's gateway session, with cookies isolated per app. An app that announces `X-AbstractFramework-App: <id>;
  mount=1` is served there; `POST /api/gateway/apps/{id}/open` returns `app_path` and builds `app_url` on the
  browser's `origin`; the sign-in handover redirects relatively with `Path=/apps/<id>/` cookies, so apps open from a
  LAN address, a reverse proxy or a tunnel. Requests carrying another origin are refused. See
  [docs/apps.md](docs/apps.md) and the nginx block in [docs/deployment.md](docs/deployment.md).
- **The local gateway pointer** `~/.abstractframework/gateway.json`: `serve` writes it once bound (url, port, data
  directory, `written_by: "serve"`) under an ownership rule (default data directory, or the pointer already names
  this gateway's data directory), so the terminal consoles, the Node apps and the Assistant find this computer's
  gateway. `abstractgateway network status` shows it.
- `GET /config/capability-defaults` warns, in words, when the default text route names an endpoint profile that was
  deleted or disabled.
- `_runtime.session_history` records the history window for every seeded run: `seeded`, `policy`, `max_tokens`,
  `token_estimator`, `replayed_messages`, `replayed_tokens`, `dropped_messages`, `dropped_tokens`,
  `dropped_counts_complete`, `oversize_turn_kept`. Strict seeding (automation and discussion sessions) also adds
  `strict` and `session_kind`.
  `GET /runs/{id}` returns the same receipt as `session_history` (counts only, `null` for a run that was not
  seeded), so a client can say how many earlier messages were not replayed.
- **Start at login from the consoles.** `GET/PUT /api/gateway/host/start-at-login` (admin)
  reports whether this gateway starts at the next login (`enabled`, `state`, the mechanism:
  LaunchAgent, systemd user unit or desktop autostart entry, Windows Run value) and whether it
  can be changed here (`can_change` with a plain `reason`), and turns it on or off without
  starting or stopping the running gateway. The web console has the switch in the Gateway card
  and on the setup guide's last step; the terminal console in F3 and on the Finish step.
- **Cloud voice providers are always listed.** Voice provider listings include `openai` and
  `openai-compatible` marked `needs_key` until a key is configured, in the environment or
  through the Providers screen; both consoles show the state.
- Voice listings pass AbstractVoice's `unavailable_providers` and `unavailable_reason` through,
  and a listing filtered to a cloud provider without a key says where the key goes. Both
  consoles' voice pickers show the reason instead of an empty list.
- The consoles show AbstractCore's `engine_missing` (a route this computer can run whose engine
  is not installed, with the install command) and the `needs_gpu_limit` fit verdict (the exact
  command that raises the GPU memory limit).
- An OpenAI key saved through the Providers screen reaches voice: the voice listings pass it to
  AbstractVoice as `voice_openai_api_key` on every call, and the gateway's runtime is built with
  it for voice generation.
- `restart.port` in `GET /api/gateway/network`: the port a restart binds; the restart route
  reconnects there.
- The web console's Apps page names this browser's address and the `/apps/` path the apps open under; a link
  `#apps?open=<id>&path=<path>` opens an app already signed in (where the proxy sends a signed-out visitor).
- The terminal console finds its gateway like every client: `--gateway-url` (alias `--url`), the legacy
  `ABSTRACTGATEWAY_URL`, then the local gateway pointer, then `http://127.0.0.1:8080`. It reads the pointer the way the
  kit and AbstractCode do: opened without following a symbolic link and without blocking (a FIFO in its place cannot
  hang the console), checked on the open file (regular file, owned by you, not writable by group or others), read from
  the same file with a 64 KiB bound, loopback URLs only. When a gateway found this way stops answering, the console
  follows it to a new port; an address you typed, submitted or probed on the Connection screen is never replaced.
  Give the token with `--token <token>`. It also gained a Network screen, the start-at-login switch and voice/engine
  states (details in its changelog).

### Changed

- **Chat routes that take a client-sent history use the runtime's one history window** (ADR-0026, operator ruling
  2026-09-28): `POST /runs/{id}/chat`, `/backlog/assist`, `/backlog/maintain`, `/backlog/advisor` and
  `/sandbox/generate` replay the newest whole messages up to 50,000 tokens (`fold_history_window`; the question being
  asked is always kept whole) and return the receipt as `history` (`replayed_messages`, `replayed_tokens`,
  `dropped_messages`, `dropped_tokens`, `max_tokens`, ...); run chat also records it in the persisted
  `abstract.chat` event. When older messages are dropped, the oldest kept one carries the runtime's `#TRUNCATION`
  notice. Removed: the 12,000-character cut on every run-chat and backlog-assist message, the advisor's
  12,000-character cut and its "first 40 messages" cap, maintain's 8,000-character
  cut, and the backlog assist/maintain draft and template cuts (500k/600k/220k/120k characters, 180k context).
- **`POST /runs/start` bounds a client-sent `input_data.context.messages` with the same history window** (older
  clients such as the AbstractCode web legacy REPL and older TUIs send their whole transcript there): leading system
  messages are kept, the rest is the newest whole turns up to 50,000 tokens with the `#TRUNCATION` notice when older
  turns are dropped; messages keep their shape (tool and multimodal messages pass through). The receipt is recorded as
  `_runtime.session_history` with `source: "client_context"` (a client-sent value is replaced) and returned by
  `GET /runs/{id}`. Discussion and automation sessions still refuse client messages (400). `POST /runs/schedule`
  applies the same window and receipt to the input its runs start with.
- **Shipped workflows rebuilt without truncation (ADR-0026):** `coding-agent` 0.2.8 and `co-scientist` 0.2.1 are the
  versions a new start gets. The versions 0.6.0 shipped (`coding-agent` 0.2.6, `co-scientist` 0.2.0, `docs-qa` 0.1.0)
  still ship, so automations and catalog records that pin them keep running after the upgrade. The coding agent's gates and fixer now read whole failure lines (the snippet
  cuts are gone) and its verifier and spec judges no longer cap their output at 4,000 tokens; co-scientist embeds the
  rebuilt `deep-plan`, `deep-investigate` and `diagram-render`. The example workflow id in the default-agent hint is
  `coding-agent@0.2.8:coder`.
- **docs-qa 0.1.1: conversation history comes from the run's session.** The question is the
  `prompt` input; each question of a conversation starts with the same `session_id` and `use_session_history`, the
  gateway replays the earlier turns through the runtime's history window and the bundle's LLM call includes them.
  The `question`/`history` inputs and the bundle's "last 12 messages" cap are gone. The web console's and the
  terminal console's docs assistants no longer send a history copy (they sent the last 12 messages): one session per
  conversation, **New conversation** starts a new one, and the drawer says when earlier messages were not replayed.
  Both versions are published into the tenant catalog at boot (clients of 0.6.0 pin 0.1.0); a fresh catalog's default
  is 0.1.1.
- Every browser app is launched with `--port`, `--host`, `--gateway-url` flags (Observer, Code, Entity and Flow join
  Continuum). An installed version older than the flags (Observer 0.1.14, Code 0.5.0, Entity 0.2.2 or earlier) also
  gets the legacy `PORT`/`HOST`/`<APP>_GATEWAY_URL` environment, so it still listens on the port the gateway chose.
- `apps.host` is deprecated: apps always listen on 127.0.0.1 and open through the gateway. Only a loopback value is
  accepted; an older saved `0.0.0.0` is ignored with one warning.
- Run Ask, run summary, backlog assist and the console sandbox share one LLM client resolved like a run's (endpoint
  profiles, the console's provider connections, the Core store). Ask on a run whose provider was `endpoint:<id>`
  failed with "Unknown provider".
- `use_context` is set by the server for automation targets and discussion turns: the automation's context mode
  (independent / growing) is the one history control. Discussion follow-ups that name no model keep the fork's
  provider and model.
- `GET /runs/{id}/input_data` never returns a workspace folder the gateway made inside its data folder (a second
  automation started from it was refused); the refusal for such a folder says to leave the workspace empty.
- **Session history replay is the most recent 50,000 tokens of whole turns.** A run started with
  `use_session_history` gets the session's newest turns that fit 50,000 estimated tokens (AbstractRuntime's history
  window). No message is cut, and there is no message-count or character cap. The old defaults (40 messages,
  24,000 characters, 200-message / 200,000-character ceilings) are removed. Automation (growing) and discussion
  sessions use the same window. The model can use the rest of its context window (operator ruling 2026-09-28,
  ADR-0026).
- **Retired history caps.** `input_data.session_history_max_messages` and `input_data.session_history_max_chars` no
  longer change what is replayed. A run that still sends them lists them in `_runtime.session_history.ignored_inputs`
  and the gateway logs a warning. An explicit `0` no longer disables replay; to start without history, leave out
  `use_session_history`. The environment variables `ABSTRACTGATEWAY_SESSION_HISTORY_MAX_MESSAGES` and
  `ABSTRACTGATEWAY_SESSION_HISTORY_MAX_CHARS` are no longer read and have been removed from the environment registry.
- **Telegram bridge:** `ABSTRACT_TELEGRAM_MAX_HISTORY_MESSAGES` (default 30) is retired. The bridge no longer sends a
  message cap or `_limits.max_history_messages`, and it logs a warning when the variable is still set.
- `restart_required` compares **saved** values only: a gateway started with `--port N` and no
  saved port needs no restart, and a command-line flag counts as overriding the setting only
  when it shadows a saved value that differs.
- The static voice listings decide which local engines are available from AbstractVoice's
  `abstractvoice.engine_runtime` (a Supertonic voice without its runtime is no longer listed),
  and answer 503 with the reason when AbstractVoice lacks that API.
- The web console's model cards show AbstractCore 2.18's `needs_gpu_limit` verdict as **Needs GPU limit** with the
  exact command to run (`sudo sysctl iogpu.wired_limit_mb=<MB>`, click to copy), instead of "Fit unknown", and the
  **Fits this computer** filter keeps those models (AbstractCore's `FITS_FILTER_VERDICTS`), so the recommended model
  for a 128 GiB Mac is listed.
- The console's theme selector and connect control are re-vendored from the released `@abstractframework/ui-kit`
  0.1.14.

### Security

- The OpenAI key used for voice comes from the Providers screen's OpenAI key or from an endpoint profile on OpenAI's
  own endpoint only; the key of an `openai`-family profile that points at another base URL (a proxy, a self-hosted
  server) is no longer sent to OpenAI.
- The gateway writes the pointer through a fresh private temporary file (random name, created exclusively, mode 0600)
  and an atomic rename: a symbolic link planted at the old predictable temporary name, or at the pointer itself, is
  never written through.

### Fixed

- The app proxy never relays a request without `X-Forwarded-Host`. A browser `Host` the proxy cannot forward (Chrome
  accepts names such as `a_b.attacker.com`) is refused with 400 `invalid_host` (HTTP) or a closed WebSocket (1008),
  so an app that checks for a loopback peer and a loopback host (abstractuic app-server kit 0.1.14) never sees a
  remote page as a browser on this machine.
- Model discovery for a saved endpoint profile (`POST /config/provider-endpoint-profiles/discover-models` with
  `profile_id` only) uses the profile's provider family; a saved `openai` profile was listed as `openai-compatible`.
- Retrying an automation command whose first attempt was already applied (for example a `PATCH` revise sent again
  after a lost response) returns the duplicate receipt of the first attempt instead of `409 revision_conflict`.
- A default text route naming a deleted or disabled endpoint profile no longer stops the workflow host from loading
  (every `/automations` and `/runs` read answered 500). The host loads with the default unrouted; a call that uses it
  fails naming the profile.

## [0.6.0] - 2026-09-27

Requires AbstractRuntime 0.6.0 and AbstractCore 2.17.0. The terminal console
`abstractgateway-console` 0.10.0 ships with this release.

### Added

- **Automations.** Run a workflow again and again on a schedule ("every 2 minutes") or on
  request, read every run as a chat turn, and get notified only when a run asks for it or
  fails. See [docs/automations.md](docs/automations.md).
  - `POST /api/gateway/automations` creates an automation from a workflow of this gateway or the
    gateway default agent (`flow_id: "@default"` with an `interface`), a trigger, an
    independent or growing context and a policy; the same `request_id` returns the same
    automation. `title` and `trigger` can come from the workflow's `automation_defaults`.
  - `GET /api/gateway/automations` lists your automations in full pages, with older scheduled
    runs on the last page (`legacy: true`); `GET …/{id}` returns the definition and summary.
  - `PATCH …/{id}` revises title, target, trigger, context or policy; `POST …/{id}/commands`
    pauses, resumes, runs now, stops the current run or archives. Commands the automation's
    state rules out are refused at once (409 `automation_busy`, `invalid_state`,
    `revision_conflict`, `identity_conflict`).
  - `GET …/{id}/occurrences` shows each run with its trigger, the prompt it received, its
    answer, failures, files and pending waits.
  - `GET …/{id}/attention` pages the notifications you have not seen, and `POST …/{id}/seen`
    records, per user, what you have seen.
  - `POST …/{id}/discuss` opens a separate conversation about one run, seeded with the
    automation's conversation up to that run, on a read-only folder; its later turns through
    `POST /api/gateway/runs/start` stay read-only and keep its history.
  - `GET /api/gateway/trigger-sources` lists what can start an automation (`schedule@1`: a
    fixed UTC interval; `manual@1`: only when asked).
  - Tool approval: `policy.tool_approval` is `auto` by default (creating the automation is the
    consent for its target's tools) or `ask` (every tool batch waits for approval).
  - Typed waits: an occurrence's waits say what they wait for (`ask_user`, `tool_approval`
    with the tool calls, `event`), and an answer of the wrong shape for an automation's wait
    is refused with 422.
  - Errors on the automation routes, sign-in failures and malformed JSON included, all have
    the shape `{"detail": {"reason_code", "message", "field"?, "command_id"?}}`.
  - The capabilities document advertises the API under `contracts.common.automations`, and
    `POST /api/gateway/commands` also accepts the six `automation.*` commands for an automation
    id.
  - Automations are durable runs of the gateway's runner: they survive a restart, and a
    restart during a run never starts it twice.
  - `scripts/accept_automations_v1.py` checks the whole feature end to end against a gateway
    it starts itself.
- **Automation attribution in run lists.** `GET /api/gateway/runs` rows carry `session_kind`
  (`chat`, `automation`, `occurrence`, `discussion`), `automation_id`, `role`,
  `occurrence_index` and `legacy`, and accept a `session_kind` filter
  (`session_kind=chat,discussion`). `root_only=true` returns conversation turns, one per
  occurrence (a retried occurrence once), so a growing automation reads as one chat; the
  session history bloc returns the same turns.
- **`automation_defaults` on workflows.** The flow editor routes save, return and remove
  (`null`) a workflow's automation defaults (title, trigger, context mode, inputs), checked on
  save; publishing writes them into the bundle manifest, and `/bundles`, `/bundles/{id}` and
  the shared catalog return them.

- **Web console: video and routes this computer cannot run.**
  - The Models catalog has a **Video** capability filter, and MLX-Gen is labelled "MLX images &
    video". The setup guide's model step has a **Video** card, and "Apply recommended" covers
    text, voice, images and video where this computer can run them.
  - A route AbstractCore recommends but this computer cannot run (for example MLX-Gen images off
    Apple silicon, or the video model on a Mac without enough memory) says why on its
    Multimodal row and gets a *Not available here* card in the setup guide, with no Download.
  - A route already configured with a provider this computer cannot run (AbstractCore's
    `route_unavailable`, for example an MLX image route carried over from a Mac to Linux) shows
    *cannot run here* with the reason on its row, and a warning or a *Cannot run here* card in
    the setup guide.
  - The "Apply recommended" result names the routes nothing recommended can run, the configured
    routes that cannot run, and routes the forced pass removed ("removed … — cannot run on this
    computer: …"); it is not shown as a success while a broken route remains, and offers the
    forced pass ("Replace mine too" / "Clear what cannot run here").
  - Per-user apply: a broken route a user inherits from the gateway store is flagged in the
    report (`route_unavailable` with `inherited: true` and the note "inherited from the gateway
    store (admin)"); only an admin can change it, so no forced pass is offered for it.
- **Terminal console 0.10.0: parity with the web console.** `abstractgateway-console` does what
  the web console does, through the same routes and with the same admin rules: the setup
  guide for a headless first run (Connection → Setup → Engines → Providers → Routes → Models →
  Apps → Review, `Ctrl+G` to jump to any step), a **Setup** screen, **A Apps**, the **F2** docs
  assistant, the **F3** host panel, every sandbox mode, entity summon and spark templates,
  workflow import, and routes this computer cannot run flagged with the reason. Admin-only
  actions are refused with the reason for a non-admin sign-in, and `--token-file PATH` signs in
  with a token read from a file. Install or upgrade with
  `cargo install abstractgateway-console`; see
  [console-tui/CHANGELOG.md](console-tui/CHANGELOG.md) and
  [docs/console.md](docs/console.md#terminal-console-abstractgateway-console).
- **One rule for where this computer's gateway answers.** `abstractgateway models
  loaded|load|unload` and `abstractgateway claim` find the gateway the same way: the running
  gateway's address, else the installed service's, else the port saved in the Network
  setting, else `http://127.0.0.1:8080`. They follow a gateway the installer moved off a busy
  port without `--url`. The `models` verbs gain `--data-dir DIR`, and read this data dir's
  bootstrap admin token for a gateway on this computer when no token is given.

### Changed

- Minimum versions raised to AbstractRuntime 0.6.0 (automations) and AbstractCore 2.17.0
  (capability-default rows report routes this computer cannot run).
- Automation list rows carry `workspace_root` (the automation's folder), so apps can open it
  from the list.
- Automation summaries show the next scheduled run also while an occurrence is running, and
  the occurrence in progress (`current_occurrence`: index, run, attempt, status).
- `GET /api/gateway/runs` turn rows carry `workspace_root`: the folder the run works in (a
  launch-folder override, a discussion's own folder), read from the run index.
- With file-backed stores the gateway builds the run store's session and children indexes at
  startup, so the first chat after a start answers without that scan; the log reports the time
  it took.
- A discussion about an automation run now works in its own folder, where it can write and run
  commands, with the automation's folder mounted read-only beside it (it used to get only the
  automation's folder, read-only). `POST …/discuss` answers `workspace_root` and
  `mounted_workspace`, and later turns of the discussion keep both.
- The web console's theme selector and top-bar controls follow ui-kit 0.1.13.

### Fixed

- An automation's target input keeps only the `_runtime` keys a client may set (`allowed_tools`,
  `provider`, `model`, `thinking`, `speculation`, `stream`); every other `_runtime` key is
  dropped. A client `_runtime.control` used to create an automation whose runs never started.
- A run whose saved state said "paused" while it was still marked running was re-ticked
  thousands of times a second, slowing every other run of the gateway; it is now left alone
  until it is resumed.
- Run commands (`pause`, `cancel`, …) sent to an automation's id are refused with 409
  `invalid_state` and a pointer to the `automation.*` commands.
- A scheduled run listed by `GET /api/gateway/runs` reports `is_scheduled: true`.
- Web console: the catalog's **Use as default** sets exactly the chosen model; the previous text
  route's server address, reasoning effort and options used to stay, pointing a new provider at
  the old server's port.
- Web console: Sandbox generations and the voice test work for accounts whose tenant or user id
  contains `:` (they answered "Run not found"), and Sandbox uploads are filed with the Sandbox's
  own conversation (they went to a second, unused one).
- Web console: the Sandbox image and video lanes use the image or video route when only that is
  set (a fresh install), as generation does, instead of calling them not configured.
- Web console: the setup guide's Computer tile shows this computer's name and its operating
  system.

## [0.5.1] - 2026-09-26

Requires AbstractRuntime 0.5.1, AbstractCore 2.16.1 and AbstractAgent 0.3.15.

### Changed

- Minimum versions raised to AbstractRuntime 0.5.1 (a wait is resumed only once, so a finished
  child can no longer make its parent run it again), AbstractCore 2.16.1 (MLX models receive
  prompts in their own chat template, so tool calls and replies from local MLX models are read
  correctly) and AbstractAgent 0.3.15 (CodeAct and MemAct agents no longer crash, and an agent that
  announces tool calls without making them is asked again instead of stopping).

### Fixed

- A flow whose Agent or subflow node waits on a child could run that child twice when the child
  finished exactly as the runner ticked (twice the time and tokens). The fix is in
  AbstractRuntime 0.5.1, which resumes a wait only once; this gateway change only quiets the
  harmless second attempt (a debug line instead of an error), and a parent the runner cannot
  resume after its child finished is now logged as a warning instead of being skipped silently.

## [0.5.0] - 2026-09-26

Requires AbstractCore 2.16.0, AbstractRuntime 0.5.0 (live replies and the
built-in tool deny rules; the gateway refuses to start on an older runtime and
says so), AbstractAgent 0.3.14 (sub-agents follow the run's live-reply switch)
and AbstractSkill 0.3.0, all installed automatically. The terminal console is
`abstractgateway-console` 0.9.0 (About, the stream-replies knob).

### Added

- **Default agent workflow.** A gateway setting,
  `agents.default_workflow.<interface>`, chooses the workflow that answers
  each agent interface (`abstractcode.agent.v1`,
  `abstractassistant.agent.v1`, ...) when an app picks "Gateway default".
  Set it in the console (Workflows > Default agent workflow, or *Make agent
  default* on an entrypoint), in the terminal console, or with
  `abstractgateway config set agents.default_workflow.<interface>
  bundle[@version]:flow`. Runs started with `flow_id: "@default"` and an
  `interface` use it at every start and are refused, with the reason, when
  it cannot run; `GET /bundles` and `GET /workflow-catalog` say what it is.
  The backlog advisor and the Telegram bridge use it too.
- Every run start answers `resolved_workflow` (the workflow it really runs),
  also kept in the run as `input_data.workflow_selection`.
- **Browse a run's files.** `GET /runs/{run_id}/workspace`,
  `/workspace/files` and `/workspace/content` show the run's folder on the
  gateway computer (with its full path and the computer's name), list it and
  preview any file, for the person who started the run.
- **Skills shelf.** The gateway keeps its own copy of the curated skills
  that ship with AbstractSkill in `<data dir>/skills/registry`, refreshed at
  each start without touching your edits; `skills.shelf` points it at
  another folder (console, terminal console, `abstractgateway config set
  skills.shelf`). `GET /skills` says which shelf is used and why a list is
  empty; `POST /admin/skills/reseed` refreshes the copy on demand.
- **The Assistant opens signed in.** Opening the Assistant from the console
  or the menu bar icon connects it to this gateway and signs it in, through
  a one-time code in a private file (`POST /apps/desktop-handover`).
- `GET /about` (no sign-in): the framework and package versions this
  gateway runs. The tray's About shows them with the project details.
- `abstractgateway serve --no-tray`: no menu bar / tray icon for this run.
- **About in the console.** The console's top bar has an About button: the
  gateway's name and version, AbstractFramework, the author, copyright and
  licence, the project links and contact, and the versions from `GET /about`.
  The terminal console has the same About (`F1` or `?`, and
  `abstractgateway-console --about`).
- **Stream replies by default** in the console (Workflows tab, under the
  default agent workflows) and in the terminal console (Runtimes → Runtime
  knobs): the `agents.streaming_default` switch. A gateway without the
  setting says so.
- **What holds the memory.** The console's Resources tab, the tray menu and
  the Activity window count the memory of every model library in the gateway
  (MLX, llama.cpp, transformers, embeddings), say how the figure was measured
  (Metal or CUDA device counter, or the sum of MLX and llama.cpp), name the
  models that hold memory when none is listed as loaded (or say none is
  attributed), and list the ejects a default-model switch still owes or that
  failed ("Will eject X when the in-flight call ends", "X: eject failed:
  reason"). `GET /host/state` carries them as `residency_diagnostics`.
- The console's Skills shelf block also shows the curated shelf version
  shipped with the gateway and how many skills the shelf holds.
- **Live replies.** A run started with `input_data._runtime.stream: true`
  sends the model's reply as it is written, as `event: llm.delta` and
  `event: llm.delta_end` frames on the run's existing ledger stream (no
  `id:` line, so reconnects resume the ledger exactly as before). A client
  that connects mid-reply first gets the text so far (`snapshot: true`); a
  run's stream also carries its sub-runs' replies; a call that could not
  stream says why (`reason: "unavailable"` with a `detail`); a run stopped
  mid-reply closes its open call (`synthetic: true`), whoever ended it (a
  stop, the kill switch, a run the runner failed). No size cap. Works
  with `serve --no-runner` + `abstractgateway runner` too (through a
  private file per run in `<data dir>/live`, deleted when the run ends).
  `GET /discovery/capabilities` advertises it as `streaming`.
- `agents.streaming_default` (default off): streams interactive
  `POST /runs/start` runs that do not say; never scheduled runs, bridges or
  the entity loop. `abstractgateway config get|set|unset
  agents.streaming_default`, `POST /admin/runtime-config {"agents":
  {"streaming_default": true}}`.

### Changed

- "A caller on this computer" is decided on the browser's address when an
  app on this computer relays the request, so a browser on another computer
  going through an app is no longer treated as local (installs, opening
  folders, the Assistant).
- A run cannot use a folder inside the gateway's data folder as its
  workspace, except the conversation folder the gateway made for it.
- **Built-in deny list.** Credential and configuration folders of the
  gateway's user account (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`,
  `~/.kube`, `~/Library/Keychains`, the `~/.abstract*` app folders) and the
  gateway's data folder are never shown by the workspace browser, and runs'
  file tools are denied them by default (an admin can turn that part off with
  `workspace_builtin_deny`). The runs part is sent as whole-folder rules
  (`workspace_builtin_deny_prefixes`, with the run's own folder as the one
  exception, `workspace_builtin_allow`) that the runtime enforces without
  writing them into the model's prompt. Scheduled runs get them too.
- **Trust proxy: the saved setting now wins over the environment.**
  `ABSTRACTGATEWAY_TRUST_PROXY` used to override the saved `trust_proxy`
  switch; it is now only a fallback when nothing is saved, and the same rule
  decides sign-in lockouts, the audit log's client address and whether a
  caller sits at the gateway computer. Security consequence: a deployment that
  pinned `ABSTRACTGATEWAY_TRUST_PROXY=0` (for example in a container) while its
  settings file says `true` now takes the client address from
  `X-Forwarded-For`. Check `abstractgateway network status`, and turn the saved
  switch off with `abstractgateway network set --trust-proxy off`.
  `ABSTRACTGATEWAY_ALLOWED_ORIGINS` keeps overriding the saved origins.
- Agents started by the Telegram, email and agora bridges, by entity
  summons and by schedules can no longer reach files outside their
  conversation's folder (the gateway's data folder and credential folders
  stay off limits), exactly like agents started from an app.
- Every run start goes through the same workspace rules: `POST
  /runs/schedule` and entity summons now refuse a `workspace_root` that
  `POST /runs/start` refuses (the data folder, folders outside the allowed
  roots), and runs started by the Telegram, email and agora bridges, entity
  summons and schedules get a workspace and the built-in deny rule like any
  other run (a run that named no folder works in its conversation's folder).
- `input_data._runtime.stream` must be `true` or `false`: any other value is
  refused with 400 (on `/runs/start` and `/runs/schedule`).
- A request relayed by an app on this computer is recognised by the app's
  proxy marker, and a forwarded request from another machine is never
  treated as local; the stored reverse-proxy setting wins over the
  environment.
- A settings write that names an unknown setting is refused as a whole and
  saves nothing.

### Fixed

- **Ejecting a model frees its memory.** Ejecting a model from the console or
  tray now frees its memory from the whole gateway process (weights,
  prompt/KV caches, MLX cache); the memory figures show what the process
  really holds, and the Models menu no longer reports "No models loaded"
  while memory is still held. Gateways started with older versions must be
  restarted once to reclaim memory already held.
- The console's accelerator memory meter counts the MLX memory this gateway
  process holds (live buffers plus MLX's cache) when it is larger than the
  system-wide figure, which on macOS does not see MLX memory, and says which
  of the two it shows.
- With nothing listed in memory but memory still held, the console's Models
  table, the tray menu and the Activity window say "Gateway still holds N GB
  (no model listed)" and offer the two ways out: eject the held model, or
  restart the gateway. A model kept in memory by another part of the gateway
  shows as **resident via other holders**; ejecting it frees every holder.
- When the Models and Engines tabs cannot load, their card names AbstractCore
  2.16.0 as the version to install.
- The shipped `deep-research` workflow (now bundle 0.1.8) researches the
  message AbstractCode sends it (`prompt`) when no `request` is given, and
  reports `success`; before, it researched an empty request when started
  from AbstractCode.

## [0.4.3] - 2026-09-25

Requires AbstractCore 2.15.2 and AbstractRuntime 0.4.35 (installed
automatically). The terminal console is unchanged (`abstractgateway-console`
0.8.0).

### Fixed

- **Installing the Assistant works on a new Mac.** Installing the Assistant
  (and engines) into the gateway's Python failed with "File not found:
  …/Library/Application" whenever the data folder's path contained a space,
  as the macOS `Application Support` folder does. The gateway now pins its
  own packages to their current versions in the install command itself.
- Continuum, when the gateway (or the tray, for a global install) starts it,
  gets its port, bind address and gateway URL as launch flags (`--port`,
  `--host`, `--gateway-url`) instead of environment variables. Continuum
  0.3.1's settings file (`~/.abstractcontinuum/settings.json`) takes
  precedence over the environment, so a saved port, host or gateway URL
  could otherwise have replaced the ones the gateway chose. The other four
  apps still receive them in the environment.
- The web console page no longer carries the source code's maintainer
  comments (design notes, review references, dates); it is about 11% smaller
  (1.25 MB to 1.12 MB). The artifact search box shows `YYYY-MM-DD` as its
  date example.
- When the Models and Engines tabs cannot load, their card names AbstractCore
  2.15.1 as the version to install (it said 2.14.0, which lacks the cancel
  attribution the gateway uses).
- The documentation site no longer publishes the backlog: planning notes
  under `docs/backlog/` stay in the repository and are left out of the site
  build. The one API page that cited a backlog item links to it on GitHub.

### Changed

- Dependency floors: `abstractcore>=2.15.2` (also in the `embeddings` extra)
  and `AbstractRuntime>=0.4.35` (also in the `apple` and `gpu` extras).
  AbstractCore 2.15.2's default MLX model is a repository that exists on
  Hugging Face, so a first MLX download no longer fails.

## [0.4.2] - 2026-09-24

Requires AbstractCore 2.15.1 and AbstractRuntime 0.4.34 (installed
automatically). The terminal console is unchanged (`abstractgateway-console`
0.8.0).

### Added

- **The Assistant as an app card.** AbstractAssistant, the desktop menu-bar
  app, appears after the five browser apps (`kind: "desktop"`, id
  `assistant`). **Install** installs `abstractassistant` into the gateway's own
  Python as a job, with every `abstract*` package kept at its current version.
  **Open** (`POST /apps/assistant/launch`) starts it on the gateway's computer,
  or brings a running one to the front; from another computer the route
  answers 409 `not_on_gateway_machine`. `abstractgateway apps install|launch
  assistant` and the tray's **Install Assistant…** do the same. See
  [docs/apps.md](docs/apps.md#the-assistant-a-desktop-app).

### Changed

- **One Install button per app.** Install only installs; the card then shows
  **Open**, and **Open in Terminal** beside it when the terminal app is
  installed. For Code, when a prebuilt terminal app exists for the computer,
  Install installs the browser app and the terminal app as one job with two
  progress rows (`parts`); Cancel stops both, and a failed terminal part keeps
  the browser app. `POST /apps/{id}/install` accepts `with_terminal` (default
  `true`); app rows carry `kind` and `install_parts`. "Install terminal app"
  alone is under **Technical details**.
- **Tray:** **Install X…** runs the same install and no longer opens the app;
  a notification says when it is installed and the menu offers **Open X**.
- The first-run guide's Apps step is titled "Apps that work with this gateway".
- Dependency floors: `abstractcore>=2.15.1` (also in the `embeddings` extra)
  and `AbstractRuntime>=0.4.34` (also in the `apple` and `gpu` extras).

### Fixed

- The download card in the console keeps its file list open while progress
  updates, and **Cancel** asks for confirmation before stopping a download.
- A failed download shows why it ended (`ended_reason`: a dropped connection,
  a Hub error, a restart), and a cancelled one says who cancelled it and when.
  `POST /models/download/{id}/cancel` accepts `{"via": "console"}` and records
  the admin who asked. See [docs/model-downloads.md](docs/model-downloads.md).
- The model catalog's fit tooltip compares the model's needs with the usable
  memory used by the verdict.

## [0.4.1] - 2026-09-24

Requires AbstractCore 2.15.0 and AbstractRuntime 0.4.33 (installed
automatically). The terminal console ships as `abstractgateway-console` 0.8.0
(see `console-tui/CHANGELOG.md`). The `v0.4.0` tag was not published to PyPI;
0.4.1 is the first release with the changes below.

### Upgrade notes

- **Login service:** run `abstractgateway service enable` once on machines
  where the gateway starts at login, then restart it (`abstractgateway service
  install`, or log out and back in). Registrations now start plain
  `abstractgateway serve` so the Network setting applies; `service status`
  reports older registrations as `broken` / *needs repair*.
- **Settings instead of environment variables:** browser origins, trust proxy,
  the apps settings, the backlog folder and the exec runner are runtime
  settings. The matching environment variables still work as a start-time
  fallback (and, for origins and trust proxy, as a pin), and every surface says
  when one is in effect.
- **Default app ports** follow the framework stack map: Observer 3001,
  Continuum 3002, Code 3003, Entity 3004, Flow 3005.
- **User accounts off:** only admin accounts can sign in to the console and
  the browser apps; existing non-admin sessions are ended at their next use.

### Added

- **Network exposure** (`localhost`, `lan`, `internet`): one setting, changed
  from the console's new **Network** tab, the terminal console, the tray or
  `abstractgateway network status|show|set|addresses|restart`
  (`GET/POST /api/gateway/network`, contract `gateway_network_v1`). `lan` and
  `internet` require user accounts; `internet` also requires an explicit
  acknowledgement. Changes apply at the next start, and `serve --host/--port`
  override the setting. See
  [docs/configuration.md](docs/configuration.md#network-exposure-localhost--local-network--internet).
- **Reverse proxy settings:** `allowed_origins` and `trust_proxy`, changed
  from the console (Network → *Advanced: reverse proxy*), the terminal console
  or `abstractgateway network set --allowed-origins … --trust-proxy on|off`,
  applied to the next request without a restart.
- **Browser apps managed by the gateway:** Flow Editor, Code, Observer,
  Continuum and Entity can be installed, started, stopped, updated and opened
  signed in from the console's **Apps** tab, the first-run guide, the tray, the
  API (`/api/gateway/apps`) and `abstractgateway apps …`. The gateway installs
  Node.js when the machine has none, checks every download, supervises the
  apps, and detects apps started outside it. `POST /apps/{id}/open` accepts a
  `path` inside the app. See [docs/apps.md](docs/apps.md).
- **Apps settings** `apps.node`, `apps.ports`, `apps.host`,
  `apps.npm_registry`, `apps.pypi_url`: from the Apps tab, the terminal console
  or `abstractgateway apps config get|set`.
- **Code in the terminal:** "Open in Terminal" opens Code's terminal app on
  the gateway machine, signed in through a one-time code; `abstractgateway
  apps install-tui|tui-command code` do the same from a shell.
- **Engine installs without a terminal:** Ollama and LM Studio install from
  the vendors' signed apps on macOS, llama.cpp from prebuilt wheels, and a
  step that needs the Apple command-line tools or an administrator password
  pauses (`needs_tools`, `needs_admin`) until you continue through the
  operating system's own dialog. New `/api/gateway/engines/*` routes and
  `abstractgateway engines continue|cancel|start|stop`. See
  [docs/engines.md](docs/engines.md).
- **Model downloads with real progress:** bytes, speed, time left and per-file
  rows for Hugging Face, MLX, Ollama, LM Studio and Supertonic; a `stalled`
  state; "Use recommended defaults" as one parent job;
  `POST /models/download/{id}/cancel` and `GET /models/downloads/stream`
  (Server-Sent Events). See [docs/model-downloads.md](docs/model-downloads.md).
- **Model catalog as cards:** one card per model with all its builds, filters
  (search, 4-bit / 8-bit / other, provider, capability, status, fits this
  computer), Hugging Face search, and filters kept in the address.
- **Recommended text model per computer:** on a Mac, AbstractCore picks an MLX
  build by memory; the guide, "Use recommended defaults" and the tray follow
  that pick.
- **Backlog folder and exec runner settings:** a fresh gateway keeps its own
  backlog in `<data dir>/backlog/`; choose another folder with `abstractgateway
  config set triage_repo_root PATH`, the console or Continuum, or for one run
  with `serve --backlog-root PATH` and `--exec-runner on|off`.
  `abstractgateway config get|set|unset` change any runtime setting from a
  terminal. `GET /api/gateway/backlog/status` reports the folder.
- **Tray control centre:** start at login, apps, models (eject, load), network
  mode and addresses, and a console link that signs you in. See
  [docs/tray.md](docs/tray.md).
- **Login service:** `abstractgateway service enable|disable|status` (states
  `on`, `off`, `broken`, `other`), an XDG autostart entry on Linux without a
  systemd user manager, and `--pin-command-line` to keep `--host/--port` on the
  command line.
- **Console:** a full-page first-run guide, engine cards, app cards with one
  action row, a **Technical details** switch, and the header widgets of the
  AbstractFramework UI kit. The create-user dialog follows the user-accounts
  mode.
- `POST /api/gateway/session/claim` reports who minted the link
  (`claim.created_by`: `serve`, `cli` or `tray`).
- `GET /api/gateway/models/installed` rows carry `kind`, `tasks` and
  `tasks_source`.
- `abstractgateway --version`.

### Changed

- Windows login item: a per-user `HKCU\…\Run` value replaces the Startup
  folder shortcut (the shortcut is removed on install).
- Someone at the gateway machine may install engines and apps by default,
  whatever address the gateway listens on; remote callers still need
  `allow_engine_install`.
- Messages that used to ask for an environment variable now name the setting
  or command to use.
- `serve` prints the admin token again on a loopback bind; `serve
  --print-token` / `--no-print-token` control it.

### Fixed

- The saved backlog folder is used by every backlog, report, triage and
  process route and by the exec runner; a folder that disappears answers
  `404` with the reason.
- Model downloads keep working after an MLX model is loaded, and a restart
  from the tray or console no longer carries in-process Hugging Face offline
  flags into the new process.
- A leftover browser app from a gateway that died is stopped on Linux.
- No false "PyTorch was imported" GGUF warning on hosts without Apple silicon
  or llama-cpp-python.
- Engine installer downloads use the per-OS user cache directory.
- `abstractgateway network … --data-dir DIR` uses `DIR`.
- `service uninstall` on Linux works when no unit file exists.
- The consoles explain in words why MTP did not run.

### Security

- With user accounts off, a non-admin account can no longer sign in and change
  the operator's settings (`401 user_accounts_off_admin_only`); creating such
  an account answers `409`.
- The last enabled admin account cannot be deleted, disabled or demoted
  (`409 last_admin`).
- `POST /bundles/{id}/deprecate` and `/undeprecate` apply the shared-registry
  ownership check.
- `lan` and `internet` are refused when the gateway was started with read
  protection off (`ABSTRACTGATEWAY_PROTECT_READ=0`).

## [0.3.0] - 2026-09-23

This release requires AbstractRuntime 0.4.33 and AbstractCore 2.14.0
(installed automatically).

### Added
- **Models and Engines tabs in the web console.** Browse models that fit this
  machine, download or delete them, and see and install local engines
  (Ollama, LM Studio, MLX, llama.cpp, Hugging Face). These are AbstractCore's
  own screens, embedded in the gateway, so the gateway and
  `abstractcore serve` show the same data and the same actions. Download,
  delete and install are admin-only; an install first shows the exact command
  it will run on the gateway host. See [docs/console.md](docs/console.md).
- **The first-run guide uses them.** The engines step lists the real engines
  on this machine with an install button. The model step lists models that
  fit, downloads one, and sets an installed model as the default text model.
- **Routes** (same bodies and payloads as AbstractCore's `/acore/*`):
  `GET /api/gateway/host/profile`, `GET /api/gateway/engines`,
  `GET /api/gateway/engines/{id}`, `POST /api/gateway/engines/{id}/install`,
  `GET /api/gateway/models/catalog`, `GET /api/gateway/models/installed`,
  `POST /api/gateway/models/delete`, `GET /api/gateway/jobs`,
  `GET /api/gateway/jobs/{id}` and `POST /api/gateway/jobs/{id}/cancel`.
  Every POST is admin-only and in the audit log. An AbstractCore older than
  2.14.0 answers 501 with the upgrade command instead of failing.
  See [docs/api.md](docs/api.md#models-and-engines).
- **Commands:** `abstractgateway models list|catalog|search|download|delete|jobs|cancel`
  and `abstractgateway engines status|install|open`, with the same arguments
  and exit codes as `abstractcore models|engines` (0 ok, 1 error, 2 refused).
  They call the running gateway; `--local` runs them in-process instead.
  Job cards in the consoles show these commands.
- **`allow_engine_install`** (runtime config): engine installs from the
  console or API run on the gateway host, so they are on by default only for
  a gateway bound to loopback. Dry runs are always allowed. See
  [docs/configuration.md](docs/configuration.md#allow_engine_install).
- `abstractgateway claim` and `abstractgateway-config claim-url` accept
  `--base-url` as another name for `--url` (the bootstrap installers use it).
- **console-tui (crate `abstractgateway-console` 0.7.0, versioned separately):**
  the terminal console gains screens 9 **Models** and 0 **Engines**, which are
  AbstractCore's shared screens from the `abstractcore-console` crate mounted
  over the gateway's `/api/gateway/models/*`, `/engines/*`, `/host/profile`
  and `/jobs/*` routes. See
  [console-tui/CHANGELOG.md](console-tui/CHANGELOG.md).
- **Zero-configuration first run.** With no auth configured, `abstractgateway serve`
  binds `127.0.0.1`, enables user auth, creates `default/admin`, and prints a
  one-time console sign-in link instead of a token. See
  [docs/first-run.md](docs/first-run.md).
- **One-time sign-in links:** `abstractgateway claim [--open]` and
  `abstractgateway-config claim-url [--open]` mint a single-use, 10-minute link
  (`/console#claim=<code>`); `POST /api/gateway/session/claim` redeems it for an
  admin browser session from a loopback peer only.
- **First-run guide in the web console** (host summary, local engines, default
  model with recommended downloads, apps, CLI equivalents), opened once per
  data folder and reachable later from the **Setup** button.
  `GET /api/gateway/host/first-run` and `POST` (admin) hold its state.
- **`abstractgateway service install|uninstall|status`**: start the gateway at
  login as a macOS LaunchAgent, a Linux systemd user unit, or (experimental) a
  Windows Startup shortcut, with `--dry-run`, free-port selection and a
  persisted port.
- `serve --data-dir`.
- `abstractgateway-config status --json` gains `schema`
  (`gateway_config_status_v1`), `data_dir_source`, `data_dir_reason`,
  `auth_mode`, `auth`, `service`, `claim_pending`, `claims`, `first_run` and
  `serve`; `GET /api/gateway/host/state` gains a `gateway` block with the same
  facts.

### Changed
- **Model downloads run in AbstractCore's job registry.** `POST /models/download`
  and `GET /models/download/{job}` keep their `{ok, job}` envelope and
  behaviour (a queued job reads `running`, a duplicate request joins the
  running job), and the job is also readable at `GET /api/gateway/jobs/{id}`.
  The job now carries AbstractCore's fields as well (`schema`, `job_id`,
  `kind`, `log_tail`, `command`, `cli_equivalent`); `started_at` is an
  ISO-8601 time instead of a Unix timestamp. Jobs started by the
  `abstractcore` CLI on the same machine appear in the job list.
- **Default data folder.** When `ABSTRACTGATEWAY_DATA_DIR` is unset, the gateway
  uses `./runtime` only if it already exists in the working directory, and
  otherwise the per-user data folder (macOS
  `~/Library/Application Support/AbstractGateway`, Linux
  `$XDG_DATA_HOME/abstractgateway`, Windows `%LOCALAPPDATA%\AbstractGateway`).
  The `triage-reports`, `triage-apply`, `backlog-exec-runner` and `data list`
  commands use the same default as `serve` (they previously defaulted to
  `./runtime/gateway`). Set `ABSTRACTGATEWAY_DATA_DIR` to keep any other layout.
- **`serve --host` default.** `127.0.0.1` when no auth setting is present;
  `0.0.0.0` (unchanged) when any auth setting is present.
- **The bootstrap admin token is no longer printed** on loopback starts; it
  stays in `<data dir>/auth/bootstrap-admin-token`. Set
  `ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN=1` to print it.
- **Windows:** the runner's singleton lock uses `msvcrt.locking`, so two
  gateways on one data folder no longer both run workflows.

## [0.2.30] - 2026-09-23

This release requires AbstractRuntime 0.4.32, AbstractAgent 0.3.13 and
AbstractMemory 0.3.0 (installed automatically). It also folds in the
`[0.2.29]` changes below, which were never published separately.

### Added
- **Stop kill switch.** A `cancel` command (the Stop button) cancels the run
  tree and stops the model call that is executing. If a call of the cancelled
  tree is still running after `stop_kill_switch_s` seconds (runtime config key,
  or `ABSTRACTGATEWAY_STOP_KILL_SWITCH_S`; default `10`, `0` disables), the
  gateway kills that inference in process. The gateway process, other runs and
  the HTTP API keep working. Stopped calls are recorded as `cancelled` ledger
  steps with `cancelled_by` / `killed_by`. See
  [docs/configuration.md](docs/configuration.md#stop-and-the-kill-switch).
- **`abstractgateway models loaded|load|unload`.** List, warm and eject models
  on a running gateway from a shell, through the same routes the consoles use
  (`--url`, `--token`, `--provider`, `--model`, `--force` for a locked model).
- **MTP (speculative decoding) controls.** `speculation` is accepted on
  `/runs/start`, `/runs/schedule`, `/sandbox/generate` and as
  `_runtime.speculation` (`false` = Off, a native-MTP object selects a depth).
  The web console and the console TUI edit the Core-owned default
  (`options.speculation` on the text route) with an MTP selector.
- **Desktop tray icon for `abstractgateway serve`** (install the `tray` extra):
  open the console, pause/resume workflows, unload models, and watch memory,
  GPU and recent runs. See [docs/tray.md](docs/tray.md).
- **Pause / resume execution** (`POST /api/gateway/host/pause|resume`, admin;
  `GET /host/runner`). A paused runner still applies commands, so Stop works.
- **Restart and self-update** from the tray or console (`POST /host/restart`,
  `GET /host/update`, `POST /host/update/check|start`), aware of pip, uv, pipx,
  editable and Docker installs.
- **Host views:** `GET /host/metrics/live` (GPU, memory and execution state in
  one call) and `GET /host/runs` (recent runs across every data plane, admin).
- **Workflows tab in both consoles** listing every registered workflow with its
  versions and entrypoints, plus import, export
  (`GET /api/gateway/bundles/{bundle_id}/download`) and delete. Versions that
  cannot be served are listed in `skipped` with the reason.
- **Out-of-the-box workflows.** A fresh install serves `basic-agent`,
  `coding-agent` (`coder` entrypoint), `deep-research`, `co-scientist`,
  `docs-qa`, and the `react-agent` / `codeact-agent` / `memact-agent` native
  loops. See [docs/shipped-workflows.md](docs/shipped-workflows.md).
- **Durable session replay.** `use_session_history` seeds a run's
  `context.messages` from the session's prior turns (with a message cap), and
  `GET` history-bundle / session-bloc endpoints serve replayable transcripts.
- **Summoned entities.** Persistent entities with their own homes, identity,
  memory and lifecycle: `abstractgateway entity create|list|inspect|verify|chat`
  and `/api/gateway/entities/*` (summon with a queue, chat, visits,
  sleep/wake/pause, diary, skills, voice, task inbox, tool policy). See
  [docs/entities.md](docs/entities.md).
- **Run-level skills selection** and skills/MCP inventories for launch surfaces.
- **One seam for AbstractCore-owned configuration** (`core_config.py`); the
  text reasoning effort is editable from the Gateway.
- `inject_guidance` runner command, durable `emit_event` delivery
  (`payload.durable: true`), and a declared environment-variable registry.

### Changed
- **One gateway-owned workspace per session**, not per run (HTTP API and
  Telegram bridge). The system prompt stays byte-stable across turns, so
  prompt caches are reused.
- **Skills selection never widens an explicit tool ceiling.** If a run passes
  `_runtime.allowed_tools`, include `read_skill` yourself when you want the
  skill tool available.
- **Cancellation, turn grounding and agent loops follow AbstractRuntime 0.4.32
  and AbstractAgent 0.3.13:** cancelled ledger steps have status `cancelled`;
  stored user turns may start with a `<runtime_metadata>` grounding envelope;
  tool loops append messages marked `_af_synthetic`. Clients that render
  transcripts should handle all three.
- The fresh-install capability seed belongs to the install (it is not re-applied
  on every boot), and the capability-defaults read reports its provenance.
- `dp-*` workflow ids are renamed `deep-*`.
- The tray menu has a Workflows section; the `desktop_tray` setting was removed
  (the icon is present whenever `serve` runs on a desktop).

### Fixed
- The ledger stream's `event: done` follows the run's terminal save instead of
  an idle timer, and the runner wakes on events instead of polling, so a
  no-tool chat turn finishes as soon as its answer is saved.
- The shipped `basic-agent` bundle (0.0.5) no longer waits 3 s after answering.
- Workflow publish, promote, upload and reload no longer block health checks.
- `POST /prompt_cache/prepare_modules` forwards `thinking`.
- Runs of catalog-published workflows are listed normally.
- An event-entry flow no longer gets a second derived listener (no duplicate
  messages or tool calls).
- A non-object client `context` is kept as sent.
- A configured `ABSTRACTGATEWAY_BACKLOG_CODEX_BIN` counts as an available
  executor.
- Idle file-store deployments no longer burn CPU, and valid credentials are no
  longer caught by the auth lockout.
- Gateway writes of Core-owned configuration keep the fields they did not name.

### Security
- Writes to the shared workflow registry (upload, delete, reload, deprecate,
  publish) require an admin principal; per-user registries are unchanged.
- `POST /models/download` and `POST /config/capability-defaults/apply-recommended`
  require an admin principal.

## [0.2.29] - 2026-08-27

Never published separately; these changes ship in 0.2.30.

### Added
- **`GET /api/gateway/host/state` — one-call host snapshot.** Memory, GPU,
  resident models, and session prompt caches, plus byte totals, in a single
  authenticated read. Every section is independently best-effort: a missing
  facade method or a failed probe nulls that section and names it in
  `degraded` (with a `reasons` map saying why) instead of failing the
  snapshot; the route never returns a 500. `totals.models_resident`
  (additive) counts only rows with `resident: true` so every client can show
  a truthful "N loaded" — `totals.models` counts every known row,
  configured / cached included, and must not be presented as "loaded".
- **`GET /api/gateway/host/metrics/memory`.** Host RAM/process/device memory
  snapshot relayed from the Runtime host facade, with the same
  `supported: false` degraded style as `GET /host/metrics/gpu`. The snapshot
  exposes both `process.rss_bytes` and `device.allocated_bytes`;
  `device.allocated_bytes` is the signal that verifies an in-process unload
  freed device memory, since freed buffers can keep process RSS unchanged.
- **Frozen `model_residency_row_v1` row schema.** `GET /models/loaded` now
  also returns `rows` — normalized records (`runtime_id`, `task`,
  `provider`, `model`, `source`, `resident`, `state`, `pinned`, `default`,
  `size_bytes`, `size_vram_bytes`, `expires_at`, `context_length`,
  `loaded_at`, `last_used_at`, `locked`, `lockable`, `modalities`,
  `calibrated_context_length`, `context_calibrated`, `host_id`, `host_name`,
  `details`) — and `row_schema`, alongside the unchanged raw `models`
  records. Residency truth is provider-first:
  `provider_resident`/`provider_loaded` outrank runtime lease booleans, state
  strings can confirm residency but never deny it, and unknown values stay
  `null`. The schema is additive-tolerant: fields beyond the original 16 are
  optional and `null` when the runtime does not report them. Rows and the
  `GET /host/state` snapshot (its optional top-level `host` block) carry a
  host identity as the aggregation seam for a proposed multi-machine model
  resource pool
  ([backlog 0093](docs/backlog/proposed/0093_multi_machine_model_resource_pool.md)).
- **Model residency locks.** Admin-only `POST /api/gateway/models/lock` and
  `POST /api/gateway/models/unlock` pin a resident model against unload and
  release that pin, selecting the target like unload does (`runtime_id` or
  `provider`+`model`). Lock requires provider-verified residency: a
  configured or merely-warm model refuses with an
  `error: "model_not_resident"` payload (load with `lock: true` instead),
  and unlock always works — even for a since-evicted model — so locks are
  never stranded. `POST /models/unload` answers **HTTP 409** with
  the normalized `model_locked` refusal payload when the target is locked,
  and the unload request gains `"force": true` to unload anyway; every other
  unload outcome stays in-band at 200. Rows report `locked`/`lockable` so
  clients can render lock state and offer the right verb.
- **`GET /api/gateway/models/context_estimate`.** Context/KV memory estimate
  for a `provider`+`model` (optional `context_length` >= 1), relayed from the
  Runtime host facade with in-band `confidence` (`calibrated` | `estimated` |
  `unknown`) and fields such as `predicted_max_context` (the context that
  fits beside the weights), the tri-state `fits_weights` /
  `fits_requested_context` split, and `budget_bytes` (real-ceiling budget;
  basis and reserve stated in `notes`). Advisory only — no load path gates
  on it. Available to any
  authenticated principal; degrades at 200 with
  `code="context_estimate_unavailable"`/`"context_estimate_error"` like the
  other host relays.
- **A Resources surface in both consoles.** The web console gains a
  `Resources` tab and the console-TUI a `Resources` screen (8): memory/GPU
  meters with
  degradation notes, the resident-model table (modality chips/labels from
  the shared `modality_ui` palette, tri-state residency, lock state, context
  facts with calibration), and session prompt caches with per-session clear.
  The web table defaults to provider-verified RESIDENT rows only — the
  section header counts resident rows, and configured / cached rows
  (labeled "configured — not in memory", Estimate only, no Unload/Lock)
  appear behind a "Show configured / cached (N)" toggle; the TUI totals line
  counts resident rows apart from the row total. Default ≠ loaded: a
  configured capability default is never presented as loaded.
  Admins additionally get warm-up (with an optional lock-after-load and a
  live context-estimate hint), lock/unlock, and unload — a locked model's
  409 refusal triggers an explicit force-unload confirmation instead of a
  dead end. Reads render for every authenticated user; mutation controls are
  admin-gated. The web tab polls `/host/state` every 5s while active
  (stale responses are discarded), the TUI every 4s while the screen is
  active.
- **Session prompt-cache enumeration lane.**
  `GET /api/gateway/sessions/prompt_cache?session_id=` lists the prompt
  caches the runtime actually minted, with session/run/workflow/node
  attribution, and admin-only
  `POST /api/gateway/sessions/{session_id}/prompt_cache/clear_all` unloads
  every cache for a session in one call. This lane is recommended over the
  identity-derived per-session lifecycle endpoints, which are unchanged.
- **Discovery contract additions.** `capabilities.contracts.common` gains
  `host_state` and `session_caches` descriptors, and the `model_residency`
  descriptor now names its `row_schema`, lists the `lock`/`unlock`/
  `context_estimate` endpoints, and carries `modality_ui` — the canonical
  modality color map (`{version: 1, colors: {...}}`, one `{color, label}`
  entry per residency task plus an `unknown` fallback) every residency
  client renders with instead of hardcoding its own palette. `modality_ui`
  is a rendering contract and is served even when the runtime facade is
  absent.

### Changed
- **Host and residency reads are user-level.** `GET /models/loaded`,
  `GET /models/context_estimate`, `GET /host/state`, `GET /host/metrics/*`,
  and `GET /sessions/prompt_cache` serve any authenticated principal.
  Mutations — `POST /models/load|unload|lock|unlock|download` and every
  prompt-cache mutation, including the new `clear_all` — remain admin-only,
  and anonymous requests are still rejected.
- Raised the AbstractRuntime dependency floor to `AbstractRuntime>=0.4.31`
  across the base, `apple`, and `gpu` profiles; that release provides the
  host facade methods (memory snapshot, session-cache enumeration) these
  endpoints relay.

## [0.2.28] - 2026-06-14

### Changed
- Raised the Gateway dependency floors to `AbstractRuntime>=0.4.29`, `abstractagent>=0.3.12`, and `abstractcore[embeddings]>=2.13.38` across the base and hardware profiles so published installs consume the released Runtime/Core/Agent contract from this wave.
- Release packaging now ships only the supported Gateway bundles `basic-agent.flow` and `abstractassistant-orchestrator@0.0.0.flow`; local draft bundles under `flows/bundles/` are ignored by default and no longer ride along into sdists, wheels, or Docker source copies.

## [0.2.27] - 2026-06-06

### Added
- Added `POST /api/gateway/runs/{run_id}/images/upscale`, backed by Runtime's durable `AbstractCoreRunFacade.upscale_image(...)` child-run path.
- Added `upscaled_image` media capability/readiness contract entries and `task=image_upscale` Vision provider-model discovery.
- Added `GET /api/gateway/vision/adapters`, backed by Runtime's public discovery facade, so thin clients can query compatible installed adapters for image/video tasks.
- Direct image/video routes now return plural artifact fields (`image_artifacts`, `video_artifacts`) for batch generation while preserving the existing singular compatibility fields.

### Changed
- Raised the Runtime floor to `AbstractRuntime>=0.4.28` across Gateway base, Apple, and GPU profiles so Gateway installs always include the Runtime `read_pdf` / `write_pdf` nodes and their permissive `pypdf` / `reportlab` dependencies.
- Forwarded newer Runtime/Core/Vision request controls such as image/video batch `count` / `n`, `seeds`, ordered `lora_adapters`, video `flow_shift`, and image-upscaler parameters through Gateway direct media routes.
- Raised the `abstractcore[embeddings]` optional profile floor to `>=2.13.37`, matching Runtime's Core floor used by the base, Apple, and GPU Gateway profiles.

### Fixed
- Added Gateway bundle execution coverage for writing a real PDF artifact, reading it back through Runtime's PDF node, and exposing the extracted text through `On Flow End`.
- Bundle-mode VisualFlow execution preserves Runtime structured LLM `data` outputs through data edges and Break Object while leaving `response` as text.
- Bundle-mode structured LLM outputs can now drive `Answer User` and `Switch` nodes through `Break Object` without dropping the parsed data payload.
- Gateway now reuses Runtime's published workspace-path and file-filter helpers, and the published package/HTTP app versions are aligned to `0.2.27` while the base/Apple/GPU dependency floor for `abstractagent` stays on the latest PyPI release line.
- Gateway provider/model resolution now falls back to the service store base directory when embedded hosts expose stores without a full host config object, keeping backlog-assist and other hosted endpoints usable in lightweight service contexts.

## [0.2.26] - 2026-06-03

### Added
- `abstractgateway serve` now auto-ensures the `default/admin` Gateway user and writes the bootstrap browser-login token when user auth is enabled, matching the Docker first-run path for native pip installs.
- Added runtime-scoped Core config storage for Gateway capability defaults:
  Gateway baseline defaults live in `<ABSTRACTGATEWAY_DATA_DIR>/config/abstractcore.json`
  and user runtime overrides live in
  `<ABSTRACTGATEWAY_DATA_DIR>/users/<tenant>/<runtime>/runtime/config/abstractcore.json`.

### Changed
- Gateway Console now presents provider endpoint profiles as provider connections for OpenAI, Anthropic, OpenRouter, Portkey, LM Studio, Ollama, and custom OpenAI-compatible endpoints, with clearer endpoint/key hints and model discovery.
- Gateway configuration docs now distinguish browser user tokens from the legacy server/operator `ABSTRACTGATEWAY_AUTH_TOKEN`.

### Removed
- BREAKING: removed legacy Gateway `config/capability_defaults.json` overlay support. Gateway capability defaults now use only scoped Core config files (`config/abstractcore.json`). Existing overlay files are ignored; recreate those defaults with `abstractgateway-config set-default ...`.

## [0.2.25] - 2026-05-31

### Changed
- Set Gateway container defaults for host-native LM Studio and Ollama endpoints so named provider discovery does not default to `localhost` inside the container.
- Updated Docker deployment docs to use `LMSTUDIO_BASE_URL` for LM Studio and `OPENAI_BASE_URL` for generic OpenAI-compatible endpoints.

### Fixed
- Fixed Gateway Console capability-default model discovery so the Base URL field is forwarded to the provider model catalog before saving.
- Fixed Docker Compose/OpenAI-compatible documentation drift where `OPENAI_COMPATIBLE_BASE_URL` was shown as the primary AbstractCore discovery variable even though AbstractCore uses `OPENAI_BASE_URL`.

## [0.2.24] - 2026-05-31

### Added
- Added `abstractgateway-config bootstrap-admin` to create or recover a file-backed `default/admin` Gateway user for hosted/container user-auth deployments.
- Added a Gateway Docker entrypoint that bootstraps the admin user token into `/data/auth/bootstrap-admin-token` before starting the server.
- Added first-class GHCR tags for `ghcr.io/lpalbou/abstractgateway:<version>`, `latest`, `<version>-gpu`, and `gpu-latest`, while preserving the legacy `abstractgateway-server` tags during transition.

### Changed
- Gateway Docker and Compose defaults now use `/data`, enable hosted user auth, and build release images from the just-published PyPI wheel instead of local source.
- Gateway startup now accepts hosted user-auth deployments without the legacy shared `ABSTRACTGATEWAY_AUTH_TOKEN`.

### Fixed
- Fixed the PyPI/GHCR release path so container images can start cleanly from the published Gateway wheel and still provide an initial admin login token.

## [0.2.23] - 2026-05-31

### Fixed
- Fixed local-source Gateway container builds so the packaged `basic-agent` workflow bundle is present when Hatch builds the wheel inside the release image.

## [0.2.22] - 2026-05-31

### Added
- Added hosted user-principal auth with `GET /api/gateway/me`, admin-only `/api/gateway/admin/users` CRUD, and a file-backed user registry storing bearer-token hashes.
- Added request-scoped Gateway service routing so hosted user-auth mode maps each principal to a separate GatewayService data plane under `<DATA_DIR>/users/<tenant_id>/<runtime_id>/`.
- Added the built-in Gateway Console at `/console` for browser-session sign-in, account/runtime summary, admin user management, token rotation, and per-principal capability default editing.
- Added per-principal capability-default overlays in hosted user-auth mode so users can set provider/model defaults for their own runtime without mutating the global AbstractCore config.
- Added provider endpoint profiles for Gateway-stored OpenAI-compatible or hosted endpoints. Profiles keep API keys server-side, discover endpoint models on demand, and surface as virtual providers in Gateway defaults and Flow node selectors.

### Changed
- Raised dependency floors to `AbstractRuntime>=0.4.26`, `abstractagent>=0.3.10`, and `abstractcore[embeddings]>=2.13.31` so Gateway installs inherit the latest light-profile, media, and provider-profile contracts.

### Fixed
- Fixed the Gateway Console sign-in page so generated inline JavaScript parses correctly, the sign-in form posts to `/api/gateway/session/login`, and signed-out users see only the same-origin Gateway user/token login card.
- Made `abstractgateway.security` export session and middleware helpers lazily so direct `abstractgateway.users` imports are not order-sensitive.
- Kept the base `pip install abstractgateway` remote-light on Linux while relying on the base `AbstractRuntime` install for MCP and remote multimodal routing. Local sentence-transformer embeddings moved behind `abstractgateway[embeddings]`, and Gateway no longer declares direct base `sentence-transformers` or `numpy` dependencies, avoiding PyTorch/NVIDIA CUDA runtime wheels unless an explicit local-engine profile is selected.
- Kept remote/provider-backed embeddings in the base light profile through `embedding.text` routes and remote AbstractCore delegation, while surfacing embedding setup errors instead of reporting a generic missing integration.
- Gateway admin user routes now fail closed when request principal context is absent while Gateway security is enabled.
- Gateway route-family authorization now keeps operator/admin surfaces and server-workspace file helpers admin-only in hosted user-auth mode while regular users remain able to operate within their own runtime data plane.

## [0.2.21] - 2026-05-29

### Added
- Gateway artifact search/import/export endpoints for thin clients, including scoped artifact lookup by run, session, or all stored artifacts with modality, content type, text, and tag filters.
- Capability discovery now advertises artifact search, workspace import, and workspace export descriptors in the shared thin-client contract.

### Changed

- Removed legacy compatibility install extras (`abstractgateway[http]`, `[server]`, `[multimodal]`, `[memory]`, `[voice]`, `[vision]`, `[telegram]`, `[visualflow]`, `[all]`, `[all-apple]`, `[all-gpu]`, `[server-nvidia]`). The supported install surface is now:
  - `pip install abstractgateway`
  - `pip install "abstractgateway[apple]"`
  - `pip install "abstractgateway[gpu]"`
- Raised dependency floors to `AbstractRuntime[multimodal,mcp-worker]>=0.4.25` and `abstractagent>=0.3.9`.
- KG memory readiness now treats a resolvable fresh persistent AbstractMemory store as available, so empty stores return empty query results instead of hiding Flow authoring surfaces.

### Fixed
- Media model-residency discovery now keeps image editing distinct from image generation when Runtime/Core expose task-specific residency state.

## [0.2.20] - 2026-05-26

### Added
- Direct Runtime-backed video generation routes:
  - `POST /api/gateway/runs/{run_id}/videos/generate` for text-to-video
  - `POST /api/gateway/runs/{run_id}/videos/from_image` for image-to-video
- Thin-client capability contracts and readiness metadata now advertise `generated_video` and `image_to_video`, including `provider_models_task` values and `abstract.progress` child-run progress events.
- Model-residency capability reporting now includes video tasks (`text_to_video`, `image_to_video`, and `video_generation`) when Runtime/Core expose them.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.24`.
- Gateway documentation now describes direct video routes, video provider/model catalog tasks, and progress-event handling for long-running media jobs.

## [0.2.19] - 2026-05-26

### Added
- Gateway capability-default routing and configuration helpers so downstream thin clients can discover provider/model defaults without hardcoded fallbacks.
- Run-retention cleanup support for draft and ephemeral Flow runs.

### Changed
- Raised dependency floors to `AbstractRuntime[multimodal,mcp-worker]>=0.4.23` and `abstractagent>=0.3.8`.
- Refined Gateway model-residency and catalog proxy responses around Runtime/Core discovery truth, including the latest MLX-Gen vision and OmniVoice catalog surfaces.
- Refreshed Docker and deployment docs for the new release image tags.

### Fixed
- Removed brittle catalog payload assertions by normalizing Gateway-owned catalog envelopes at the route boundary.

## [0.2.18] - 2026-05-23

### Added
- Catalog and provider discovery routes now include a stable Gateway-owned envelope (`catalog.contract=gateway_catalog_v1`, `catalog.version=1`) plus one canonical `items` array, while preserving legacy lower-layer fields for compatibility.
- Capability discovery now also exposes `common.readiness` (`gateway_surface_readiness_v1`): a compact surface-level summary derived from endpoint descriptors, memory readiness, prompt-cache, media gates, and Runtime/Core truth.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.22`.
- Removed VisualFlow directory mode and fully removed the `abstractflow` package dependency from Gateway. VisualFlow JSON is stored/published via Gateway endpoints and executed as `.flow` WorkflowBundles (bundle mode).

## [0.2.17] - 2026-05-22

### Added
- Gateway now exposes Runtime-backed image editing for thin clients through `POST /api/gateway/runs/{run_id}/images/edit`.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.21`.
- Gateway capability discovery and thin-client contracts now advertise edited-image and generated-music availability, richer voice `tts|stt|listen` contracts, and Runtime-backed model residency truth instead of hard-coded media support flags.
- Direct STT now forwards `prompt`, `response_format`, `temperature`, and source `format` hints through the Runtime transcription surface.
- Release-facing docs now describe the current higher-app surface more precisely, including the stable route/contract layer and the current best-effort catalog payload limitation.

## [0.2.16] - 2026-05-21

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.20` across the base, Apple, and GPU install profiles.
- Gateway's legacy prompt-cache snapshot aliases, `GET /api/gateway/prompt_cache/saved` and `POST /api/gateway/prompt_cache/save|load`, now delegate to Runtime's public host facade instead of using provider-private prompt-cache state directly.
- Local bundle runtimes now keep host-local prompt-cache exports under `<DATA_DIR>/prompt_cache_exports` through Runtime's export root policy.

### Fixed
- Removed the last Gateway-side prompt-cache boundary bypass (`runtime._abstractcore_llm_client`, direct provider-instance access, and provider-private `_prompt_cache_store` / GGUF cache hooks) from the public route surface.
- Removed the stale internal Core catalog proxy module after discovery routing fully moved to Runtime's public discovery facade.

## [0.2.15] - 2026-05-21

### Added
- Added Runtime-backed durable bloc prompt-cache control-plane routes under `/api/gateway/blocs/*`, including KV manifest/list/ensure/load/delete/prune helpers for exact-reuse workflows.
- Added Gateway-owned workspace file helper support plus focused route and contract coverage for durable blocs, model residency, notifier behavior, and Runtime-backed capability discovery.

### Changed
- Raised the Runtime floor to `AbstractRuntime[multimodal,mcp-worker]>=0.4.19` and moved Gateway's public provider/media/tool boundary behind Runtime facades rather than direct package imports.
- Updated Apple/GPU install profiles to cascade through Runtime's aggregate extras and excluded internal `tests/`, `flows/`, and backlog notes from source distributions.
- Expanded the docs and capability contract to cover durable blocs, media/model residency, Runtime-backed email/Telegram helpers, and the current Docker/runtime dependency shape.

### Fixed
- Gateway no longer reads AbstractCore config for LLM helper defaults; provider/model resolution now follows request values, Gateway env, and flow defaults with a clear config error when unset.
- Gateway's operator email, Telegram, and notification paths now use Runtime's AbstractCore host facades, while local file/workspace helpers stay owned by Gateway.
- Capability discovery and prompt-cache readiness reporting now better reflect the actual state of generated-media, voice/audio, and provider-backed cache controls.

## [0.2.14] - 2026-05-19

### Fixed
- Gateway now carries explicit modern OpenAI/httpx/anyio dependency bounds in its base install metadata, preventing Python 3.10 resolver backtracking while preserving the Apple/GPU profile cascade into `[all-apple]` and `[all-gpu]` framework dependencies.

### Changed
- Raised the Runtime floor to `AbstractRuntime>=0.4.14` so Gateway profiles consume Runtime's resolver bounds for AbstractCore provider/tool extras.

## [0.2.13] - 2026-05-19

### Fixed
- Gateway's base install now avoids mixing Core's narrow base media/embeddings extras with Core `[all-apple]` and `[all-gpu]` profile dependencies, while still installing the media, compression, and embeddings dependency set needed by the remote-capable base package.
- Gateway's base media dependency set now uses a Python-3.10-compatible `unstructured` line and bounds `python-pptx` to supported modern releases so document-capable installs do not backtrack into broken legacy setup packages.
- Gateway's base web dependency set now prefers current compatible FastAPI/Uvicorn/Requests/urllib3 releases to keep CI and user installs out of unnecessary resolver backtracking.
- Gateway now applies a compatible setuptools lower bound so Apple/GPU installs satisfy Torch's `<82` constraint without resolving into ancient broken setuptools releases.

### Changed
- Raised the Runtime floor to `AbstractRuntime>=0.4.13` so Gateway profiles consume Runtime's updated multimodal dependency metadata, and raised the Music floor to `abstractmusic>=0.1.2`.

## [0.2.12] - 2026-05-19

### Fixed
- Gateway Apple install profiles now preserve the entrypoint contract by cascading `[all-apple]` through Runtime, Agent, Core, Vision, Voice, Music, and Memory dependencies; GPU profiles continue to cascade `[all-gpu]`.

### Changed
- Gateway's base remote-capable install now includes Core embeddings dependencies alongside remote providers, media, tools, tokens, compression, voice/audio, and vision while preserving the published Core dependency floor.

## [0.2.11] - 2026-05-19

### Fixed
- Gateway voice, TTS, STT, and vision catalog routes now use the AbstractCore capability abstractions as the source of truth for provider and provider-model discovery.
- Direct Gateway TTS and STT routes now dispatch through the AbstractCore capability registry, preserving explicitly selected media providers and models through execution.
- Gateway LLM provider/model discovery can proxy configured AbstractCore Server catalog routes while keeping Flow's existing response contract.

### Changed
- Raised dependency floors to Runtime `>=0.4.12`, Core `>=2.13.15`, Flow `>=0.3.11`, Vision `>=0.3.6`, and Voice `>=0.10.3`.

## [0.2.10] - 2026-05-13

### Fixed
- Gateway capability discovery now builds its embedded capability registry with Gateway-scoped media configuration, keeping discovery contracts aligned with the concrete voice, TTS, STT, and image catalog routes.
- Gateway media catalog proxy calls now avoid forwarding unset optional query params, preventing stale `None` values from breaking downstream capability discovery.

### Changed
- Raised dependency floors to Runtime `>=0.4.11`, Core `>=2.13.14`, Flow `>=0.3.11`, Vision `>=0.3.5`, and Voice `>=0.9.4`.


## [0.2.9] - 2026-05-12

### Added
- Gateway discovery now advertises `/api/gateway/audio/transcriptions/models` for STT catalog lookup.
- Added local and proxied STT model catalog responses backed by AbstractCore/AbstractVoice.

### Fixed
- Gateway capability catalogs now map Gateway-scoped voice and vision env vars into the embedded capability registry, so local Gateway deployments expose configured voice/TTS/STT/image models without requiring duplicate lower-level env names.
- Catalog proxy calls now omit unset optional query params instead of forwarding `None` values.

### Changed
- Raised dependency floors to Runtime `>=0.4.10`, Core `>=2.13.13`, Flow `>=0.3.10`, and Voice `>=0.9.3`.

## [0.2.8] - 2026-05-10

### Added

- Capability discovery now advertises
  `capabilities.contracts.common.runs.input_data` and
  `capabilities.contracts.common.runs.history_bundle` so thin clients can
  feature-detect the run input and RunHistoryBundle endpoints from the shared
  Gateway contract.

## [0.2.7] - 2026-05-10

### Updated

- Bumped abstractagent floor to >=0.3.6 to match the new abstractagent release that requires abstractruntime>=0.4.9.

## [0.2.6] - 2026-05-09

### Fixed

- Raised the AbstractVision floor to `abstractvision>=0.3.4` across Gateway
  install profiles so `abstractgateway[gpu]` and the NVIDIA image inherit the
  stable-diffusion.cpp binding constraint that avoids the broken
  `stable-diffusion-cpp-python==0.4.6` Linux sdist.
- Updated release-facing Docker examples and package metadata from `0.2.5` to
  `0.2.6`.
- Release/CI installs now bypass the restored pip dependency cache for editable
  dependency resolution, avoiding stale package indexes immediately after
  lower-package releases.

## [0.2.5] - 2026-05-09

### Changed

- Promoted the base `abstractgateway` install to the remote-light HTTP/SSE
  server profile. It now includes Runtime multimodal support, AbstractAgent,
  AbstractCore remote/media/tools/tokens/compression/vision/voice/audio,
  AbstractVision, AbstractVoice, AbstractFlow compatibility,
  AbstractMemory/LanceDB KG support, FastAPI, multipart uploads, and Uvicorn.
- Raised Runtime and Agent floors to `AbstractRuntime>=0.4.9` and
  `abstractagent>=0.3.6`.
- Simplified install guidance around `abstractgateway`, `abstractgateway[apple]`,
  and `abstractgateway[gpu]`. The older `http`, `server`, `multimodal`,
  `memory`, `voice`, `vision`, `all`, and `server-nvidia` extras remain as
  compatibility aliases.
- The NVIDIA Docker image now installs `abstractgateway[gpu]`; `server-nvidia`
  remains only as a compatibility alias.

## [0.2.4] - 2026-05-08

### Added

- Explicit install profiles for the Gateway package: minimal base,
  `http`, `multimodal`, `server`, `memory`, `apple`, `gpu`, `all-apple`,
  `all-gpu`, and `server-nvidia`.
- `abstractgateway-config` plus `abstractgateway config` for operator status and
  private `.env` bootstrap without taking ownership of AbstractCore provider
  configuration.
- Gateway memory store resolver for AbstractMemory-backed LanceDB, SQLite, and
  in-memory stores, including `/kg/query` store metadata.
- Core catalog proxy endpoints for thin clients:
  `GET /api/gateway/voice/voices`,
  `GET /api/gateway/audio/speech/models`, and
  `GET /api/gateway/vision/provider_models`.
- Added a `server-nvidia` extra plus an experimental CUDA/PyTorch-based
  `abstractgateway-server-nvidia` Docker image recipe for full NVIDIA machines.
- Release and manual GHCR image workflows now publish the light default server
  image and attempt an experimental best-effort NVIDIA full image.

### Changed

- Base installs are now intentionally minimal again:
  `AbstractRuntime>=0.4.8` only.
- Server and multimodal profiles now use the aligned Runtime/Core/Voice/Vision
  floors: `AbstractRuntime>=0.4.8`, `abstractcore>=2.13.12`,
  `abstractvision>=0.3.3`, and `abstractvoice>=0.9.2`.
- Server, native Apple, native GPU, and NVIDIA profiles now require
  `abstractagent>=0.3.5`, so Gateway-hosted agent nodes resolve against the
  same Core/Runtime baseline as Gateway itself.
- Release tests now reset Gateway's process-global service between cases and
  pass explicit provider/model overrides for ledger summary/chat generation
  tests.
- Native Python hardware profiles are full deployment aggregates:
  `abstractgateway[apple]` and `abstractgateway[all-apple]` install the
  Apple-local stack and all relevant non-NVIDIA framework capabilities, while
  `abstractgateway[gpu]` and `abstractgateway[all-gpu]` install the matching
  local GPU stack.
- Gateway-owned runtime handoff now seeds `_runtime.prompt_cache`,
  `_runtime.max_attachment_bytes`, and `_runtime.workflow_bundles_dir` from
  Gateway configuration.
- Gateway LLM helper defaults now resolve through the same deployment cascade as
  runtime execution instead of hardcoded local model fallbacks.
- Docker Compose local builds can override `ABSTRACTGATEWAY_EXTRAS`; the
  default examples use port `8080`, and an NVIDIA compose overlay is available
  for GPU hosts.
- The default Docker server image now composes `abstractgateway[server,memory]`
  so KG workflows and `/kg/query` have the AbstractMemory/LanceDB store package
  available without making memory a base-package dependency.
- The `memory` profile now depends on `AbstractMemory[lancedb]>=0.2.6`.

### Fixed

- `memory_kg_*` effects and `/kg/query` no longer assume LanceDB directly;
  in-memory stores work, SQLite structured queries work when the installed
  AbstractMemory build exposes `SQLiteTripleStore`, and semantic queries fail
  clearly when the selected store has no vector/search capability.
- Dynamic voice/audio/vision catalog discovery now delegates to the AbstractCore
  server catalog boundary when configured, with bounded static fallback when it
  is not.
- Observer/chat/backlog/discovery helpers now return a clear provider/model
  configuration error when no request, Gateway env, or AbstractCore default is
  available.

### Notes

- The default Docker image remains the release-grade light, portable image for
  `linux/amd64` and `linux/arm64`. The NVIDIA image is `linux/amd64` only and
  is experimental/best-effort because vLLM/Torch/Diffusers dependency
  resolution is much heavier than the default server profile and still needs a
  CUDA host smoke gate before production positioning.
- There is no practical MLX Docker image target for Apple Silicon today: MLX
  depends on Apple's Metal stack and Docker Desktop runs Linux containers
  without Metal/MPS device access. Apple local inference should stay native on
  macOS, not containerized; the Gateway container can point at Docker Model
  Runner, native LM Studio, `mlx_lm.server`, or Ollama OpenAI-compatible
  endpoints via `model-runner.docker.internal` or `host.docker.internal`.

## [0.2.3] - 2026-05-08

### Added

- Versioned thin-client capability contracts for Gateway common features, AbstractFlow editor/runtime support, AbstractAssistant media/cache controls, and AbstractCode-facing prompt-cache controls.
- AbstractFlow gateway-first editor contract validation, including VisualFlow CRUD/publish/start/observe coverage and a bundled flow input-schema endpoint.
- Gateway-owned session prompt-cache lifecycle routes:
  - `GET /api/gateway/sessions/{session_id}/prompt_cache/status`
  - `POST /api/gateway/sessions/{session_id}/prompt_cache/prepare`
  - `POST /api/gateway/sessions/{session_id}/prompt_cache/rebuild`
  - `POST /api/gateway/sessions/{session_id}/prompt_cache/clear`
- Generated-media contract fields in capability discovery, including direct-vs-workflow generated-image availability.
- Direct generated-image route, `POST /api/gateway/runs/{run_id}/images/generate`, backed by Runtime/Core image output selectors, artifact storage, and `abstract.media.image.generated` ledger events.
- Backlog completion ledger for the capability contract, Flow editor contract, session prompt-cache lifecycle, and generated-media gateway contract.

### Changed

- Capability discovery now truthfully reports provider-level and session-level prompt-cache controls, plus direct Gateway voice/audio/image endpoints where configured.
- API, configuration, deployment, Docker, README, FAQ, and LLM ingestion docs now describe generated images as both workflow-backed and directly available through the Gateway route when a Runtime/Core image backend is installed and configured.
- Docker/Compose release examples now point at the `0.2.3` server image.

### Fixed

- Fixed stale release-facing docs that said Gateway had no direct image-generation endpoint after the direct route landed.
- Fixed an order-dependent test import leak so the full local pytest suite can run cleanly after the AbstractFlow editor contract tests.

### Notes

- Direct image generation still depends on a configured Runtime/Core/AbstractVision-compatible backend; Gateway does not bundle heavy local image engines.
- Session prompt-cache lifecycle is Gateway-owned naming and orchestration over provider/model controls. It is not a provider-independent local KV cache or full CachedSession persistence system.

## [0.2.2] - 2026-05-06

### Added

- MkDocs Material configuration for the documentation site.
- CI docs build job and release docs gate.
- Release workflow deployment to GitHub Pages via `mkdocs gh-deploy`.
- PyPI-backed GHCR server image publishing for `ghcr.io/lpalbou/abstractgateway-server`.
- CI validation build for the local server Docker image recipe.
- Docker server image, Compose profile, and deployment documentation.
- `docs`, `server`, `vision`, and `multimodal` optional dependency extras.
- Discovery metadata for AbstractCore capability plugins (`voice`, `audio`, `vision`, and future `music`).

### Changed

- Version metadata aligned across `pyproject.toml`, package `__version__`, and FastAPI app metadata.
- The server install profile now mirrors the newer AbstractRuntime/Core multimodal stack: `AbstractRuntime[multimodal]>=0.4.6`, `abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]>=2.13.10`, `abstractvision>=0.3.1`, and `abstractvoice>=0.9.0`.
- The server Docker/Compose profile now documents workflow-backed image generation through AbstractVision, direct Gateway TTS/STT through AbstractVoice, and provider-dependent prompt-cache controls.
- Gateway voice/audio endpoints now accept AbstractVoice's newer local/remote backend environment knobs in addition to the existing Gateway-scoped settings.

### Notes

- Release scope is intentionally explicit: TTS and STT have direct Gateway endpoints; generated images are available through Runtime/Core workflows with AbstractVision installed and configured, but Gateway does not yet expose a direct image-generation HTTP endpoint.
- Prompt-cache support is provider-level control-plane support. This release does not add a Gateway-owned CachedSession lifecycle API.
- `flows/bundles/article@dev.flow` was inspected and left untracked. It is a local `dev` bundle generated by the Gateway publisher, not a release artifact.

## [0.2.1] - 2026-02-09

### Changed

- Dependency bumps (see `pyproject.toml`):
  - `AbstractRuntime>=0.4.2` (and `AbstractRuntime[abstractcore]>=0.4.2` for HTTP/voice/telegram/all extras)
  - `abstractagent>=0.3.1`, `abstractvoice>=0.6.3`, `abstractflow>=0.3.7`
  - `abstractcore[media,tools]>=2.11.8` (via `abstractgateway[all]`)
- Documentation refresh for external users:
  - added explicit AbstractFramework ecosystem context
  - updated minimum versions in install snippets to match `pyproject.toml`
  - kept the architecture diagram as the canonical “shape of the system”
- Version metadata alignment:
  - `pyproject.toml`, `src/abstractgateway/__init__.py`, and `src/abstractgateway/app.py` now agree on `0.2.1`

## [0.1.1] - 2026-02-04

### Changed

- Documentation refresh for external users:
  - new FAQ (`docs/faq.md`)
  - clarified quickstart + smoke checks in `README.md`
  - tightened getting started, configuration, security, and API overview docs
  - improved cross-linking in `CONTRIBUTING.md` and `SECURITY.md`
  - refreshed `llms.txt` / `llms-full.txt` for agent ingestion (index + full snapshot)
- Version bump to reflect the documentation release (`0.1.0` → `0.1.1`).

### Notes

- No intentional runtime behavior changes in this release; it is documentation-focused.

## [0.1.0] - 2026-02-03

### Added

- Initial public package for AbstractGateway (`abstractgateway`).
