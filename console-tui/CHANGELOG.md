# Changelog — abstractgateway-console

## Unreleased — the Resources screen: live host residency (2026-08-28)

- **The Routes weights line no longer warns a fully configured host
  (2026-09-06).** It read `recommended models: 2 of 3 present · missing:
  lmstudio qwen/qwen3.5-9b@4bit` in warn amber on a host whose every route was
  answered — the operator had routed text generation at their own model, so the
  starter kit's build was absent and would stay absent. A warning whose only
  cure is installing the model you chose against is noise, and noise on the
  healthy path teaches an operator to skip the line that matters. The gateway
  now decides which recommended models belong to an *unanswered* route
  (`recommended.gaps` on `/models/availability`, shared with the web console);
  this line reads them, says `1 route with no model yet · input.text ·
  recommended: lmstudio qwen/qwen3.5-9b@4bit · w downloads the selected route's
  weights`, and paints nothing at all when there are no gaps. `would_download`
  remains the fallback for an older gateway.

- **Byte sizes are binary math with BINARY labels** (`B` / `KiB` / `MiB` /
  `GiB` / `TiB`, one decimal). `human_bytes` always divided by 1024; calling
  the result `GB` made this crate disagree with the AbstractGateway web
  console, which really did divide by 1e9 — one 89,986,353,824-byte GGUF read
  `83.8 GB` here and `89.99 GB` there, and a 128 GiB machine's RAM read
  `128.0 GB` here and `137.44 GB` there. Memory is binary wherever it is
  configured or reported (`sysctl iogpu.wired_limit_mb=110000` is 107.4 GiB),
  so the math stays and the units are corrected. The web console, abstractcode-tui,
  abstractflow and `@abstractframework/monitor-memory` all moved to the same
  spelling in the same wave; a shared six-value assertion set pins them
  together.
- **The Resources screen fits 80x24 again.** The memory strip refused to
  shrink and its natural height took the whole block: at 80x24, 80x26 and
  110x24 the Loaded table had ZERO model rows and was unreachable — neither
  `Tab` nor ten `Down` presses brought it back — and the selected-row detail
  line painted over the bottom border, leaving no closing `╯`. The meters,
  the accelerator's scoped label and its note are now PINNED; when the
  terminal cannot hold both, the table's rows are reserved first and the
  itemization is windowed behind an `↑/↓ N more` affordance that **`m`** pages
  (wrapping); when it can hold both, the itemization renders whole as before.
  The block also clips its children, so no line can ever wear the frame's row
  again. Nothing was removed, shrunk or reordered.

- **A new 8th screen, "Resources" — the "agentic OS" resources view** over
  `GET /api/gateway/host/state`: RAM / device / GPU gauges (ramped
  `Progress` bars, ok → warn → error as they fill; the GPU gauge exists
  only when the probe says supported), the resident-model table, and a
  session-prompt-caches sub-tab with a totals footer. Every snapshot
  section is independently best-effort server-side; `degraded`/`reasons`
  render as muted notes — a half-blind snapshot says so, never
  blank-success. Appended to the lockstep screen arrays (append-only:
  digits and tests key on the order), so digit `8` jumps to it in browse.
- **The table renders row_v1 honestly.** `resident` is TRI-STATE and
  null prints a distinct "unknown", never "no"; the modality column
  carries the gateway's own `modality_ui` labels with a documented
  Badge-tone mapping (Text→Accent, Image→Ok, Video/Voice→Info,
  Music→Warn, 3D/Embedding→Muted — hex colors are for web clients);
  a calibrated context prints its star (`8192*`); the lock marker is
  `⊘` U+2298, not the padlock emoji (the double-width-advance column
  hazard routes.rs documents).
- **Actions**: `u` unload (danger confirm; a gateway **HTTP 409
  model_locked** refusal hands back a second "Force unload?" confirm —
  the 409 is the authority, never the row's possibly-stale `locked`),
  `k` lock/unlock (unlocking a locked model confirms; locking is safe),
  `w` warm up (provider + model form, optional lock-after-load), `e`
  context estimate (confidence + predicted max + first note as a
  notice), `c` clear one session's prompt caches (Caches sub-tab,
  danger confirm). Every mutation follows write → verify-via-GET
  (`/models/loaded` / `/sessions/prompt_cache`) → journal, and splices
  the fresh rows into the held snapshot instead of re-running the slow
  full probe.
- **Polling is generation-gated and tab-scoped.** `/host/state` is a
  SLOW call (GPU probe + residency listing), so a self-rescheduling
  poll chain reads it every ~4s WHILE the screen is active and dies on
  exit (the result and its reschedule are dropped whole when the
  generation moved); steady-state refreshes are silent (no Loading, no
  busy label), a failed poll publishes the honest Failed state and
  STOPS (recovery is `r` or re-entry — never a retry storm), and a
  gateway reset can never paint the old host under the new header.
  Mutations open their busy entry at ENQUEUE, not dequeue — a confirmed
  action shows in the strip immediately even while a silent poll holds
  the serial worker lane.
- **Fixed en route: the footer hint arms had drifted one screen left**
  (the uncommitted Workflows wave) — Review's sandbox hints rendered on
  the Workflows screen and Review showed none. Arms are named constants
  now and a lockstep test pins one distinctive verb per screen.

Gate: `cargo build`, `cargo test` (44 lib + 102 headless, all green —
incl. tri-state rendering, the lock marker, the confirm→Cmd path, the
409 force hand-off, digit-8, and the poll generation), `cargo clippy
--all-targets` clean of new warnings. The tri-state and 409 flows are
pinned so they cannot silently collapse into "no" / plain failure.

### Operator refinements on that screen (2026-08-28)

- **The device meter told the truth about the wrong thing.** On Metal,
  `memory.device.allocated_bytes` is PROCESS-LOCAL: the live host serves
  `allocated_bytes: 0` while a 93 GB GGUF is resident (LM Studio holds
  it, not the gateway). The meter now prefers `host_in_use_bytes /
  wired_limit_bytes` and LABELS it "host-wide", falling back to the
  allocated/total pair labelled "this process only" — a 0 B bar beside a
  full host can no longer be drawn (`DeviceGauge::meter`, pinned against
  the live capture).
- **Every resident line offers the lock verb**, sweep/externally-loaded
  rows included: `POST /models/lock` now ADOPTS them, so `lockable: null`
  is an unknown the gateway answers, never a refusal the client invents.
  `locked` outranks residency (a locked-but-evicted row keeps its
  Unlock); a row the host says it is NOT holding gets no lock and no
  unload, with the reason named. `store::lock_action` is the one
  authority the key handler, the row hint and the tests all read.
- **Per-model footprint.** One coalesce, shared by every surface:
  `size_bytes` → `size_vram_bytes` → `est_weights_bytes`. The third is an
  ESTIMATE and is marked `~3.1 GB` against a reported `3.1 GB` (the
  screen title carries the legend); rows carrying only
  `est_weights_bytes` — every externally-loaded model — used to render
  BLANK. `cache_bytes` rides beside it as its own column, never folded
  into the size.
- **A memory breakdown under the meters**: one line per resident model
  (coalesced size + its cache), the session caches, this process's RSS,
  and an honest **unattributed** remainder — `host_in_use − attributed`,
  clamped at 0, OMITTED when the host-wide figure is unknown, and marked
  "at most" when some contributor went unreported. The per-model lines
  cap at six (the table below lists them all); the fixed tail never caps.
- **The warm-up form picks instead of asking the operator to spell.**
  Provider is a Select over the discovered catalog, model a Combobox over
  that provider's models, refreshed when the provider changes and
  reusing the crate's existing catalog machinery; a discovery failure is
  named and keeps an honest free-text lane, and lock-after-load stays.

Gate: `cargo build`, `cargo test` — 49 lib + 107 headless, all green.
The `/host/state` fixtures are pinned from a LIVE capture (allocated 0,
host-in-use 98.5 GB, a sweep row with only an estimate) rather than
written to match the parser.

## Unreleased — the Workflows screen: the registered registry (2026-08-21)

- **A new 6th screen, "Workflows" — every workflow registered on the
  gateway**, mirroring the web console's tab. A row is a BUNDLE, not a
  version ("how many versions does this have" is the question the panel
  exists to answer), and the version cell carries TWO numbers —
  published / draft — because a single total misrepresents a registry
  that is majority drafts minted one-per-authoring-run. The detail
  block lists per-version channel, created-at and entrypoint counts,
  plus the newest version's flows with their interfaces; the default
  bundle is named.
- **Versions the gateway REFUSED to serve get their own block with the
  reason and path.** They are not runnable, so listing them as
  workflows would be a lie; omitting them is the lie the block was
  built to remove — the file is still on disk and still needs a
  decision.
- **Verbs**: `t` toggles draft visibility (hidden by default — a
  majority-drafts registry buries the published set the operator is
  looking for), `e` exports one version's ORIGINAL `.flow` bytes to a
  LOCAL path (the TUI may not be on the gateway host; an existing file
  is refused, never clobbered), `d`/`D` delete one version / every
  version (danger-confirmed, verified by reading the registry back —
  a delete that reports success while the row survives must say so),
  `r` refreshes honoring the drafts toggle so `r` reloads what is on
  screen rather than silently changing what it shows.

## Unreleased — previews take two thirds of the terminal (2026-08-20)

- **The artifact preview and the log tail scale with the TERMINAL**,
  capped at two thirds of each axis, instead of pinning a fixed 96x26 /
  100x26 dialog. On an image every extra cell is another pair of mosaic
  subpixels; on a log every extra row is a line you did not have to
  scroll for; and the header stops eliding the run and session ids that
  made the operator screenshot unreadable. New `ui::preview_size(cx)`.
- The cap is a CAP, not a target, and deliberately carries no floor at
  the old fixed size: a 96-cell floor would swallow the proportion
  outright on every terminal under ~146 columns, and the two thirds
  would never apply. The engine clamps the request to the viewport and
  re-clamps on resize, so the result is always on screen — a small
  terminal simply gets a small preview.
- FORMS keep their fixed widths deliberately: a text field 200 cells
  wide is harder to read, not easier. This applies to the two panels
  whose job is to SHOW content, and to nothing else.
- Pinned geometrically (170x40, 120x36, 240x60 and the log tail), not by
  how much text happens to fit — a proportional size is a geometric
  claim, and a fixture tuned to a knife edge passes for the wrong
  reason. Both tests fail against a fixed size; the frame is found by
  its untitled corner, since every panel's BOTTOM border is untitled and
  the corner alone identifies nothing.

## Unreleased — the preview flash, and denser image mosaics (2026-08-20)

- **Fixed: another artifact's message flashed under this artifact's
  header.** Opening a .jpeg showed a decode error (or the previous
  file's text) for a beat before the image appeared. The worker is ONE
  serial lane and `artifact_text` / `artifact_image` / `log_text` are
  GLOBAL slots, so opening two previews in a row delivers the first
  one's result AFTER the second modal is on screen. Clearing the slots
  at open — which the opener already did, with a comment saying so —
  cannot help: the stale result had not been produced yet.
  `Store::preview_target` is now stamped at open (`artifact:<run>/<id>`
  or `log:<home>/<file>`) and the worker asks `preview_wanted` before
  publishing; a result that lost its race is DROPPED, never painted.
  The log tail carried the identical defect and the identical fix.
- **`abstracttui` 0.3.5 → 0.3.6 — images draw at the terminal's own
  mosaic density.** `Image` pinned half blocks (1x2 subpixels per cell)
  regardless of what the terminal proved; it now follows
  `MosaicMode::auto`, so a UTF-8 + color terminal gets quadrants (2x2)
  and the artifact preview roughly doubles its effective resolution
  (the engine measures 20.5 → 23.5 dB PSNR on a photograph in a 24x32
  pane). Nothing to opt into here: this console pins no mode and runs
  `RunConfig::default()`, so the family resolves per draw.
- On the screenshot that prompted this: its
  `jpeg: progressive JPEG not supported (baseline only)` text exists
  only in abstracttui 0.3.0 — that frame came from a binary built
  before the 0.3.5 bump, and progressive JPEG has decoded since.

Gate: `cargo build`, `cargo test` (37 lib + 93 headless, 130 green),
`cargo clippy --all-targets` clean of new warnings. The race is pinned by
a headless test that opens two artifacts, replays the slow one's result
through the guard (dropped) and then without it (paints) — so the
assertion cannot pass for the wrong reason.

## Unreleased — `*.jpg` filters, on every runtime tab (2026-08-20)

- **The search box speaks a query LANGUAGE now, not a substring.** Typing
  `*.jpg` on the Artifacts tab returned `0 rows`: every filter compared
  the `*` literally, and no row can carry one. The rule, on all four tabs:
  a query with no `*` and no `?` is the case-insensitive SUBSTRING it
  always was (`06-13` still finds a June 13th run); a query carrying
  either wildcard is a case-insensitive GLOB, anchored to the whole value
  and retried against its basename. `*` crosses `/` on purpose — these
  tabs filter a FLAT row list, they do not walk a tree — so `*.jpg`
  finds both `photo.jpg` and `/var/lib/gw/runs/run-1/photo.jpg`. `[` is
  literal.
- Runs and Artifacts filter SERVER-side, Cache and Logs filter here, so
  the matcher exists three times: `src/query.rs`, the gateway's
  `_glob_matches`, and the web console's `makeNeedle`. `Needle` owns the
  lowercase fold and the wildcard classification so a call site cannot
  hand a filter an unfolded query; `filter_homes` and `filter_log_files`
  now take what the user TYPED. The Rust copy is fuzzed against a naive
  recursive matcher, the Python one against `fnmatch`, and the JS one
  against the Python one — 20k cases each, because three hand-rolled
  copies of one rule is exactly the shape that drifts.
- The note lines say WHICH half answered — `q="*.jpg" (glob)` — so a
  zero-row answer is diagnosable from the row itself, and the search
  modal states the rule once above the field.
- Coverage: `src/query.rs` (9), the Cache/Logs filter wiring in
  `src/ui/runtimes.rs` (4), and two headless tests that drive the REAL
  Cache tab and search modal. `cargo test` 129 green (37 lib + 92
  headless), `cargo clippy --all-targets` clean of new warnings.

## Unreleased — engine 0.3.5: JPEG previews that actually open (2026-08-20)

- **`abstracttui` 0.3.0 → 0.3.5.** (Superseded by 0.3.6, above.) 0.3.5 taught `gfx::decode_image` to
  read PROGRESSIVE JPEG (SOF2 — spectral selection, successive
  approximation, EOB runs, restart markers) and single-component
  sequential scans. Progressive is what most editors emit by DEFAULT,
  so the artifact image preview (`Cmd::LoadArtifactImage` → `decode_image`
  → `Image::from_bitmap`) used to answer `cannot decode this image here:
  parse: jpeg: progressive JPEG not supported (baseline only)` on a large
  share of the .jpg files a run actually produces. Those now render.
  Baseline JPEG and PNG decode unchanged.
- Nothing in this console calls the one changed API surface (the
  unsupported-format message text, no longer saying "baseline JPEG") —
  we surface it verbatim, never match on it. No source changes.

Gate: `cargo build`, `cargo test` (23 lib + 90 headless, 113 pass) clean
against 0.3.5. Decode proven directly: a progressive and a baseline JPEG
of the same image both decode on 0.3.5; the progressive one errors on
0.3.0.

## Unreleased — one config language, two doors (2026-08-01, operator ruling)

Harmonized with `abstractcore-console` for the configuration the two
entry points SHARE. This console is the reference and keeps its shape;
these are the gaps and the two places a better shared pattern won.

- **Reasoning reaches the TUI.** The web console gained a reasoning
  select on text routes this wave; this console had none, so the effort
  was invisible and un-editable here. The route editor now carries a
  `not set / minimal / low / medium / high` select on the
  text-generation route only (the web's `isTextGenerationDefault` gate),
  `RouteRow` folds `reasoning`, and `Applies now` names it. Deliberately
  NOT the engine's `ReasoningSelect`: that control is capability-driven
  and renders locked without `ReasoningFacts`, which a route editor does
  not have.
- **Fixed: an emptied field could not be cleared.** The save omitted
  `base_url` and `options` when blank, and the write path preserves
  fields it is not given — so clearing a base URL silently restored the
  stored value. Every field this form OWNS is now sent explicitly, `""`
  and `{}` included; fields with no control here stay unnamed on purpose
  (the web's "send what this modal owns, and nothing else").
- **State vocabulary** moved onto the row model (`state_label()`, shared
  body with the sibling console). `default` became `not configured` —
  "default" read as "a default is set" on a screen about defaults.
- **The locked-row marker is `⊘` U+2298, not `🔒`.** The padlock is
  Emoji=Yes and measures 2 cells, so terminals slide every column to its
  right on the one row that carries it. The sibling console's glyph
  research, and independently the engine's own (`ReasoningSelect`'s lock
  spelling note) — both land on U+2298.
- **The write proves itself with the resulting row** in the sentence
  both consoles now print: `input.text = endpoint:airelay / gpt-5.4 ·
  reasoning low (source: …)`.

Gate: `cargo build`, `cargo test` (14 lib + 77 headless), `cargo clippy
--all-targets` all clean. Live: driven over a pty against :8080 — set a
reasoning default through the gateway, verified in the payload and in
AbstractCore's config file, store restored to its pre-session state.

## 0.5.1 (2026-07-30) — engine 0.3.0: hover ink on, one clipboard writer

- Bumped abstracttui 0.2.23 → 0.3.0 (four releases: 0.2.24 ReasoningSelect
  / ThinkingFold, 0.2.25 platform clipboard, 0.2.26 List accessories,
  0.3.0). The only breaking change is `RunConfig` gaining `hover_ink` and
  `platform_clipboard`, which breaks exhaustive struct literals — both of
  ours already spread `..RunConfig::default()`, so nothing broke. MSRV
  stays 1.87.
- `run_cli` now calls `run_with(RunConfig { hover_ink: true, .. })`,
  arming mouse mode 1003 (motion with no button held). This console is
  the most button-dense of the three (50 `Button`, 36 `TextInput`) and
  its hover visuals existed but never received an event. Pinned by
  `buttons_take_hover_ink_and_release_it`: pointing at a Button re-inks
  its label (fg text → accent) and leaving restores the base paint
  exactly. `Table` has no hover state in the engine, so the 14 inventory
  tables stay inert — expected, not a bug.
- The token modal's notice no longer claims "(OSC 52)". Since 0.3.0 the
  engine takes exactly ONE copy route — OSC 52 when the terminal
  advertises it, otherwise the host clipboard — and labels its own
  notice when neither works. Naming the route was wrong on every
  Terminal.app-class host.
- The headless harness sets `platform_clipboard: false`. Its fixed caps
  leave `osc52_copy` false, which is precisely what arms the host
  fallback; without this a test that clicks "Copy to clipboard" would
  spawn `pbcopy` and overwrite the clipboard of whoever ran the suite.
  The interactive binary keeps the default `true`.
- NOT affected by the 0.2.25/0.2.26 double-write truncation bug: the host
  clipboard fallback did not exist before 0.2.25, so this crate went from
  "no fallback at all" straight to the fixed form. There is no historical
  truncated-token report to look for here.
- Scroll fixes inherited, no code change: the exact-viewport-height offset
  repair (0281), follow-tail wheel, and wheel bubbling at the offset
  boundary. Audited all five `Scroll` sites — none has a `grow`ing sole
  child, so none can be stuck unscrollable. `sandbox_pane_survives_a_
  response_that_shrinks` pins the one pane here whose content genuinely
  shrinks; it never showed the 0281 blank, because the reply is typeset
  as markdown and reflows well under the viewport.

## 0.5.0 (2026-07-26) — Runtimes: choose-to-inspect with Sessions | Data & cache tabs

Operator directive: "do NOT eagerly load the runs/sessions. we only
show the runtimes and upon clicking one runtime, then we can see their
sessions and data/cache below … displayed as tabs."

- NOTHING loads eagerly on the Runtimes screen anymore: entry sends
  ONLY LoadRuntimes; runs / data-homes / runtime-config slots reset to
  NotAsked and are owned by their panel effects. The health authority's
  runs retry slot demoted to verify-only (a static retry cannot know
  the chosen scope).
- Choose-to-inspect: clicking a runtime row (selection change) or
  Enter / double-click on the highlighted one opens the inspector below
  — a real Tabs widget: "Sessions" (the plane's runs, scoped LoadRuns,
  spinner while loading, entity planes explain why empty is normal) and
  "Data & cache" (the plane's data_dir + size facts, plus registered
  data-home stores attributed by longest path-boundary data_dir prefix;
  the default plane also lists shared caches outside every plane;
  purge stays here, dry-run-gated). The old `h` data-homes modal is
  gone — the Data tab replaces it.
- Runtime knobs (gateway-wide config) moved behind a collapsed
  Disclosure; LoadRuntimeConfig fires on first expand only.
- Self-healing choice: a reloaded inventory refreshes the chosen row's
  facts in place and clears the choice if the plane vanished
  (gateway switch / deleted user).

### Same day — adversarial review of the redesign (fable5)

Seven findings against the choose-to-inspect redesign, five fixed, all
seven pinned as headless regressions (73 headless + 14 unit green,
clippy zero):

- P1 FIXED — steer form died under a runs reload: the runs-table
  activation opened the steer modal on the runs REGION's dyn generation
  scope (the `s` key already used the page scope — the two "can never
  drift" paths drifted in scope). Any `store.runs` change while the
  form was open (post-cancel `refresh_runs`, a stale-load correction)
  disposed the modal's signals: the form kept painting but silently
  dropped every keystroke and Send refused with "type the guidance
  first". Both panels' modals/prompts (steer + purge confirm) now
  anchor to the page scope. Pin: `steer_modal_survives_runs_region_rerender`
  (baseline leg proves the drive; round two re-renders under the modal).
- P1 FIXED — a re-probe kept the chosen runtime: `ui.rt_detail` is a
  cloned RuntimeRow (remote data) living in UiState, so it escaped
  `reset_domains`; after a gateway switch the sessions effect loaded
  the OLD plane against the NEW gateway with zero choice made on it
  (and the inspector wore the old row's facts under the new header —
  the F1 stale-domain class in UI clothing). `Ctx::reset_domains` now
  forgets it. Pin: `gateway_reset_forgets_the_chosen_runtime`.
- P1 FIXED — the busy heartbeat stole focus: `loadable_view` took the
  tick as a VALUE, so every caller's dyn tracked it unconditionally and
  every Ready table region regenerated twice a second during any
  in-flight op — each regeneration re-parked the table's `.autofocus()`
  and yanked focus off the tabs bar / wherever the operator had
  reached; on this screen the next arrow key then CHOSE a plane (a
  load nobody asked for). The tick is now lazy (closure), read only by
  the Loading arm's spinner — Ready regions stop regenerating on busy
  ticks, app-wide (users/providers/routes had the same class). Pin:
  `busy_tick_does_not_steal_focus_from_the_tabs_bar`.
- P1 FIXED — 80x24 showed ZERO runtime rows: the inspector's
  `min_h(12)` + two root gap rows starved the inventory block down to
  border+hint — the screen's primary surface (the table you choose
  from) vanished entirely at the macOS default size, while Enter still
  chose an invisible row 0. Floors rebalanced (inventory `min_h(6)`,
  inspector `min_h(10)`, root gap 0): at 80x24 both the inventory
  header+row and the runs header+row render. Pin:
  `runtimes_inspector_fits_at_80x24`.
- P2 FIXED — the teaching line taught a dead gesture: a single click on
  the ALREADY-selected row fires neither on_select (no change) nor
  on_activate (click_count 1) — and row 0 is pre-highlighted, so the
  very first click most operators make did nothing. The line now names
  the working gestures ("click a row, or Enter / double-click the
  highlighted one"); the footer teaches ←/→ for the inspector tabs.
  Pin: `teaching_line_names_working_gestures_for_the_highlighted_row`
  (also documents the engine gesture fact; a Table click-on-selected
  callback would be the engine-side cure).
- P2 FIXED — unmaterialized user planes rendered a blank "data dir:":
  the gateway serves `data_dir: null` for a binding that never
  materialized; the fold's `unwrap_or_default` made the Data tab's
  anchor fact an empty string. Renders an honest dash + reason now.
  Pin: `unmaterialized_plane_data_dir_renders_a_dash`.
- P2 FIXED — home_sel clamp accumulated: `data_panel` installed its
  clamp_selection on the INSPECTOR scope on every entry into the Data
  tab (Tabs re-runs the panel builder per switch; the effect outlived
  the panel) — one identical effect per visit for the inspector's
  lifetime. Installed once at page scope now, reading the chosen row
  itself.
- Documented, not fixed: duplicate plane data_dirs would make
  attribution first-row-wins (the second plane's tab silently empty) —
  not live-reachable today (the gateway serves per-user dirs;
  source-verified), pinned as `home_plane_index_duplicate_dirs_first_wins`
  so a payload change upstream surfaces it. A screen re-entry retries
  a Failed plane load once (fresh `last_requested` per mount) —
  accepted as fresh-look-fresh-ask.

## 0.4.1 (2026-07-26) — loading spinners + honest entity-plane empty states

Operator report (Runtimes screen): "selecting a runtime doesn't get you
the associated sessions? and if there's loading, we should have a
spinwheel."

- ANIMATED loading spinner everywhere: `loadable_view`'s Loading state
  renders the engine Spinner clocked by `store.tick` (the busy
  heartbeat, 500ms) — slow loads (an entity-plane open can take
  seconds) visibly spin instead of freezing on a static glyph.
- Entity-plane runs honesty: the drill-in DOES read the entity's home
  run store (verified live + at the source) — it is genuinely empty
  because entity CONVERSATIONS run through the chat/life lanes, which
  do not create runtime runs (only durable visits and summoned
  workflows do). The empty state now says exactly that instead of a
  bare "no runs" that read as broken. A real per-entity SESSIONS
  listing is a gateway-side follow-up (filed).

## 0.4.0 (2026-07-25) — screen redesigns from operator feedback (3 fable5 lanes)

Four operator UX complaints, redesigned across three parallel adversarial
lanes and reconciled into one tree: Providers becomes ONE unified list,
every editable table opens on double-click, selecting a runtime filters
the runs below it, and the Review & Test sandbox leaves its modal to
become an inline workspace. Minor bump (user-facing screen shape
changes; all functionality preserved). Gate: 13 unit + 64 headless green
(up from 59), clippy zero, live pty smoke ALL GATES PASSED. Three
concurrent lanes edited the shared tree; the merged result compiles and
tests clean with no collision damage.

### Review & Test redesign: the sandbox is INLINE

Operator (screenshot, screen 6): "i genuinely do not understand this
page.. so much empty space and yet using modal... i think this would
warrant a complete redesign." Done — the sandbox left its modal and
became the screen's body, the web console's Sandbox-tab-as-workspace
shape (`console.py` tab-sandbox: full-page transcript + composer;
provider/model selects, textarea prompt, attachments row).

- INLINE sandbox workspace: provider Select + model Combobox (both
  placeholder-first; provider-switch resets the model — the
  fabricated-selection law), a REAL multiline prompt (`TextArea`,
  2-row window, Enter runs / Ctrl+J newline; the modal's single-line
  TextInput undersold a prompt), Generate button, and the outcome
  rendered in the screen's real estate. `open_sandbox_modal` is
  deleted; `open_sandbox(ctx, provider)` navigates instead.
- The FULL response renders (word-wrapped, scrolling, auto-hide bar) —
  the modal ellipsized the paid-for response at 90 chars, an honesty
  debt. Failed outcomes keep the error verbatim + retry teaching; the
  outcome header is PINNED (the D1 fusion class) and always NAMES the
  pair it belongs to, because the result slot now PERSISTS across
  navigation (no reset-on-open; a gateway switch still clears it via
  reset_domains).
- Picker state is durable BY NAME (`ui.sb_provider`/`sb_model`/
  `sb_model_custom`): indices die with a list reload, names survive
  tab switches and carry the Providers-`t` prefill; a saved name
  missing from a fresh non-empty list resolves to the placeholder and
  clears. GOTCHA pinned in code: PageHost pages build EAGERLY at app
  mount, so the derive effects must TRACK the name signals — an
  untracked read runs once with "" and a post-mount prefill never
  lands (the headless flow test only passed by fixture-ordering luck
  until the eyeball pass caught the placeholder).
- Providers `t` now JUMPS here with the provider pinned (one pair-test
  surface, one result slot) instead of opening a duplicate modal
  picker — the modal-over-content shape was the complaint, and a
  second picker copy was the IA reviewer's R1/R2 duplication. The
  pinned pick in the picker is the visible acknowledgment (notices are
  screen-scoped and retire on arrival — a jump notice would die
  unseen).
- The journal ("Changes this session") keeps every verify-after-write
  line verbatim but becomes a compact bottom receipt strip: sized to
  content (empty = 3 rows — it no longer owns half an empty screen),
  height-aware cap (3 rows tight / 10 tall), newest-first, scrolling
  past the cap, block PINNED (shrink 0) so deficit lands on the
  sandbox's yielding response scroll — harness-caught: a crushed
  journal Scroll's absolutely-positioned content BLED rows over the
  Finish button (engine clip leak under crush; avoided structurally,
  never relied on).
- Keys: Enter (in the always-autofocused prompt) is the advertised run
  gesture — the prompt holds focus, so a bare `g` TYPES there; `g`
  still fires from picker/button focus (old teaching honored, no
  longer advertised as primary). Every refusal names its reason
  (disconnected / no provider / no model / empty prompt / already
  running); the synchronous-Loading double-press guard survives the
  move. The screen's focus anchor is the prompt (harness-caught: with
  no focus, dispatch stops at root and screen shortcuts are dead — the
  F2 class).
- `r` on Review now refreshes providers (the picker is live data;
  the "nothing to refresh" refusal shrank to the Connection screen),
  and entering screen 6 loads providers if never asked (a browse
  digit-jump straight to 6 must not land on an empty picker).
- Attachments slot reserved: the chips row + Ctrl+A picker (engine
  0.2.20 `on_paste`/`FilePicker`; see
  abstracttui/reviews/console-tui-attachments-integration-prompt.md)
  land in the marked column position under the prompt — layout
  accommodates without rework; no dead button shipped.
- TextArea engine gotchas pinned in comments: `.layout()` REPLACES the
  grow-to-content band (hand-managed 2-row band + shrink 0), and auto
  flex-basis inherits the widget's Percent(1.0) inner width and
  overpaints the block border (basis 0 + grow is the fix).
- Tests: 64 headless green (5 new/updated review pins — the inline
  flow end-to-end with the full-response needle past the old 90-char
  cut, refusals-name-reasons, the 110x34/80x24 × empty/ready/FAILED
  shrink-pin matrix with a border-fusion guard, the Providers-`t`
  jump); pty smoke step 8 checks inline-sandbox presence (never runs a
  generation). Clippy zero.

### Double-click-to-edit + runtime→runs follow

Two operator complaints, both wiring (the engine's `Table::on_activate`
fires on Enter, Space, and double-click of the already-selected row —
click 1 selects, click 2 activates; abstracttui 0.2.20+):

- Double-click to edit (COMPLAINT A): every editable table now opens
  its row's modal on activation — providers (Edit / synthetic Override),
  routes (editor, per-row editability refusals intact), users (edit
  form), entities (manage menu, the `m` action), runs (steer form —
  the row's one NON-destructive modal; cancel deliberately stays a
  keypress + confirm, never a casual double-click). Key paths (`e`,
  `m`, `s`, Enter) are unchanged: each table's activation calls the
  SAME extracted entry fn as its key (`edit_selected_profile`,
  `edit_selected_user`, `manage_selected_entity`, `steer_selected`,
  routes' existing `edit_selected`), so guards and refusal notices can
  never drift between gestures. Read-only surfaces (runtimes inventory,
  data-homes/reservations modals — destructive-verb tables) get no
  activation.
- Runtime selection drives the runs panel (COMPLAINT B): runs live in
  per-runtime stores — the generic /runs listing carries no runtime
  field (live-verified) and refuses unknown query params, so
  "filtering" means switching ENDPOINTS. Selecting a runtime row now
  loads that plane's runs through the admin drill-in
  (GET /admin/runtimes/{kind}/{tenant}/{runtime}/runs), root-filtered
  client-side (the drill-in lists children and has no root_only param);
  the default-plane row keeps the richer own-runs lane (/runs:
  server-side root_only + paused, and it works for non-admin tokens).
  The runs slot is scope-stamped (`RunsData{scope, rows}` — the
  VoicesData pattern), loaded by a selection effect with the users
  screen's F2 warm-keeper shape (Loading holds while arrowing — no send
  storm; one corrective load when a stale result lands; Failed holds
  per requested scope — no retry loops). The panel names its plane
  ("showing: entity plane: castor"), empty states name WHERE ("no
  top-level runs in castor yet"), and `r` re-pulls the SELECTED plane
  (refresh_screen leaves the slot NotAsked for the owning effect — the
  entity_detail F17 pattern). Honesty gate: cancel/steer refuse on
  foreign planes with the mechanism named — POST /commands appends to
  THIS console's own command inbox (live-verified, no cross-plane
  routing), so a cancel aimed at another plane's run would be accepted
  and then sit unconsumed forever.

Gate: 13 unit + 58 headless green (8 new: activation per table incl.
real SGR double-click gestures, plane follow, no-storm, foreign-plane
refusal), clippy zero. Cross-lane note: the two review-screen tests
were mid-flight in the concurrent sandbox-redesign lane at gate time.

### Providers screen: ONE unified list (web-console parity)

Operator ruling (2026-07-25, with screenshots): "i don't think we should
have 2 lists... we have ONE list and option to configure custom
endpoints" — the web console is the model. Investigation finding: the
web's unification is SERVER-side — `GET /config/provider-endpoint-
profiles` already merges managed profiles, synthetic env/core rows and
auto-probed local servers (console.py `_effective_endpoint_profile_
public_rows`), and the web's one "Available Providers" table renders
exactly that payload; `/discovery/providers` only feeds pickers. The
TUI was rendering the picker payload as a second table.

- ONE list: the Providers screen renders only the profiles payload
  (managed + env/core + auto-detected rows — the same rows the web
  shows), titled "Available providers (a adds a connection)". The
  "Discovered providers (read-only)" table is deleted; its two real
  facts survive as footer lines: the gateway default pair, and
  "not configured yet (a adds one): …" — the registered backends with
  no connection (the web shows those only as add-cards). The web's
  provider-type card row maps to the TUI add-form's family select
  (card click ≡ family pick — no extra step added).
- THE provider-name join law (`Profile::provider_name`, web
  `providerValueForEndpointProfile` parity): synthetic rows answer to
  their BARE provider id, managed profiles to `endpoint:<id>`.
  Live-verified: `/discovery/providers/endpoint:anthropic/models` →
  "Unknown provider" while bare `anthropic` serves 11 models — so the
  old `m`/`t` on synthetic rows (which always sent `endpoint:<id>`)
  were silently broken; both now use the join law. The table's first
  column shows this name (it is what flows/pins reference), and
  `unconfigured_provider_names` uses it to keep the footer line free
  of double-listings (profile `airelay` ↔ discovery `endpoint:airelay`;
  synthetic `anthropic` ↔ bare `anthropic`; live orphan
  `endpoint:unrelated` and not-running `ollama` surface honestly).
- Override (web parity): `e` on a synthetic row no longer refuses — it
  opens the CREATE form prefilled with the row's identity (bare id,
  family, display name, description, base URL) plus the banner
  "already usable from … — saving creates a managed override"; saving
  POSTs under the same id, so the managed copy shadows the env/core row
  in the server's merged list (exactly the web's
  `openEndpointModalFromConfiguredProvider`). `d` on synthetic rows
  still refuses, now teaching the override path.
- Per-row action honesty: a selection-following line under the table
  names what THIS row supports and why ("managed (gateway scope) ·
  e edit · d delete…" vs "from environment config · e override →
  managed copy…") — the TUI stand-in for the web's per-row buttons.
  Columns: provider (join-law name), family (wide), base URL, API key
  (fingerprint, never the secret), models ("N restr" / "N live" from
  `discovered_model_count` / "live"), enabled, origin (managed rows:
  gateway/user scope; synthetic: env/core/auto).
- Store: `Profile` gains `provider_id`, `source`,
  `discovered_model_count`; `synthetic` now also honors the web's
  `managed === false` check; `virtual_provider()` replaced by
  `provider_name()`. All profile CRUD, discover-test, model drilldown,
  restrict-to-N-models and the sandbox `t` lane are preserved.
- Tests: join law + unconfigured fold pinned in store unit tests
  against the live payload shapes (incl. the `endpoint:unrelated`
  orphan class); headless pins for the one-list render, the join-law
  `m` commands, the override form open/save, and no-double-listing.

## 0.3.8 (2026-07-25) — simplification wave, cycle 3 (validation + regression fixes)

Cycle 3's adversarial pass validated cycles 1-2 (profile disclosure sound;
first-run renders whole) and caught regressions cycle 1 introduced —
fixed here. Gate: 59 tests green (9 unit + 50 headless), clippy zero,
live pty smoke.

- REG-1 (P1, self-inflicted): the wizard goal row was reserved even in
  browse mode (empty but 1 row tall), pushing the macOS-default 80x24
  connection screen over 24 rows — crushing the connected-identity badge
  and, because the engine's startup-notice registry is append-only and
  never clears, pinning a PERMANENT "display degraded" footer for the
  whole session. Fixed: the goal row is now truly zero-height outside
  wizard mode, and the connection screen (tightest, self-teaching) gets
  no goal row at all. Layout diagnostics are also suppressed from the
  operator footer entirely (debug-flag only) — a never-clearing banner
  is worse than silence for a signal operators can't act on. Pinned by
  `connection_screen_fits_at_macos_default_80x24`.
- REG-2: the entity-partition line broke plural grammar ("2 entities
  also hold its own access token") — now "2 entities also hold their own
  access tokens" / "1 entity also holds its own access token". Pinned.
- REG-3: the Users footer hint still said "v reservations" (the surface
  was renamed) → "v kept data of deleted users".
- NEW-3: wizard goal-line copy shortened to ≤ ~85 chars so the operative
  tail (the a/t teachings, "skip if…") survives at 80-100 col widths
  instead of truncating.
- NEW-2 (the users/entities empty-state fusing into the block border)
  was a downstream symptom of REG-1's over-demand and no longer
  reproduces once the row is freed — verified clean at 80x24 wizard.

Remaining queue (a future pass, unchanged top pick): the shared
provider+model picker + test kit (IA R1/R2) — now three near-identical
pickers plus two free-text entity forms; folding them is the one
structural simplification left. Then own-time toggle, route-editor
Advanced trim, Reload buttons, reservations placement.

## 0.3.7 (2026-07-25) — simplification wave, cycle 2 (profile form disclosure)

- Profile form: 4-field happy path (id · family · base URL · API key) with
  the six less-common fields (display name, description, allowed models,
  scope, enabled, clear-key) behind a folded "More options" disclosure
  (P1-C). Adding a provider key now costs ~5 focus stops instead of 11.
  Folded on create (first-run adds a key, nothing else); open on edit
  (the operator is deliberately changing an existing profile). Field
  state lives in modal-scope signals so values survive fold cycles; edit
  mode autofocuses `family` (id becomes static text). Pinned by
  `profile_form_folds_advanced_fields_on_create`; the picker+test-kit
  consolidation (IA R1/R2), Sandbox screen (R3), reservations placement
  (R4), and own-time toggle (R5) remain queued for the next pass.
- Deliberately dropped the original P1-C id-derivation-from-family idea:
  a second openai-compatible endpoint would collide on the derived id;
  keeping id explicit in the happy path is safer and still cuts the form.

## 0.3.6 (2026-07-25) — simplification wave, cycle 1 (progressive disclosure)

Operator directive: simplify setup/management with progressive
disclosure, all functionality preserved. Two fable5 adversaries (naive-
operator walk + IA/redundancy map) ran cycle 1 of 3; the highest-impact,
lowest-risk findings landed here. Gate: 56 tests green (9 unit + 47
headless), clippy zero, live pty smoke.

### Fixed — the first-run no longer ends on a broken screen (P1-A)
The root-chrome shrink-collapse class (findings 1020/1030) had siblings
inside blocks that the earlier pins missed:
- Review "Live test" block pinned `shrink(0.0)` with its teaching line
  pinned — it used to crush to zero and fuse with the Run button once
  the journal held one entry (i.e. for the operator who did the wizard
  right).
- Users entities region floored `min_h(1)` — a first-run gateway with
  zero entities used to hide the `∅ no entities` state while the users
  table hoarded blank rows.
- The engine's raw `layout:` zero-collapse diagnostic is humanized in
  the footer ("display degraded — a panel over-demanded space…") — it
  rendered verbatim as a crash-looking log; the raw pointer stays behind
  `ABSTRACTGATEWAY_CONSOLE_DEBUG=1`.
- Regression-pinned: `first_run_screens_survive_tight_height` (100x24,
  0 entities + 1 journal entry), `engine_layout_notice_is_humanized`.

### Added — the wizard now GUIDES, not just gates (P1-B)
Each wizard step carries one muted goal line (wizard mode only, pinned),
answering "what is this step for" and "can I skip it": Providers "make
one provider usable…", Routes "nothing required — the engine picks
models by default", Users "mint a token… skip if the admin token is all
you use", Runtimes "nothing to configure — glance and continue", Review
"run one real test, then Finish". Browse mode shows none.

### Changed — speak operator, not gateway-internals (P2-A)
"Provider endpoint profiles" → "Provider connections"; "New/Delete
provider endpoint profile" → "Add/Delete a provider connection"; scope
"only this principal"/"all principals" → "just this login"/"everyone on
this gateway"; "Multimodal capability routes" → "Routes — which provider
& model serve each input/output"; Runtimes title → "where each user's
and entity's data lives"; "Recent runs (root)" → "(top-level)";
reservations → "Kept data of deleted users — transfer or purge"; the
entity-principal note now says "access token" not "door credential". The
routes `authority:` module name now shows ONLY in the read-only/error
state (where it names the refusing backend), not as ambient healthy
chrome. Entity manage-menu framework words (substrate/own-time/dream/
spark) deliberately kept — consistency across surfaces is the simplicity.

### Changed — zero-keystroke connect (P2-B)
The console auto-probes at boot even without a token: a local dev
gateway with open reads connects with no keystrokes; an auth gateway
shows its 401 panel (which already teaches the fix) immediately instead
of a neutral "not connected". Same normalization + honest token-source
the Probe button uses.

### Deferred to cycle 2 (compose with sandbox/forms work)
Profile-form 4-field disclosure (P1-C), the picker+test-kit
consolidation (IA R1/R2), Sandbox screen + global journal (IA R3),
reservations→Runtimes placement (IA R4), own-time single toggle (IA R5),
Reload buttons on modal error panels. Sandbox-chat attachments (engine
0.2.20) also land there against the refined sandbox shape.

## 0.3.5 (2026-07-25) — engine 0.2.20: modal popup displacement fixed

- Bumped abstracttui 0.2.12 → 0.2.20. This carries the engine fix for
  the operator-reported P1 (finding 1050): Select/Combobox popups
  inside a Modal used to anchor in layer-local coordinates and render
  displaced to the top-left; the engine now captures anchors in screen
  space across the whole popup family. Verified by a new regression pin
  (`select_popup_inside_modal_opens_adjacent_to_its_field`) — the
  column assertion is what would have caught the old displacement.
- (Sandbox-chat file attachments — engine surfaces now available in
  0.2.20 — are deferred to land against the refined sandbox modal from
  the in-flight simplification wave, not integrated twice.)

## 0.3.4 (2026-07-25) — centralized connection authority, transport truth, users/entities partition

(Addendum, same day: migrated to the gateway's c5308 clean-listing
contract the hour it shipped — `UserRow.principal_kind` is the ONE kind
source when the gateway serves it; the roles convention demoted to a
labeled fallback for pre-contract gateways only. Contract-wins-over-
contradictory-roles pinned by test.)

Operator wave: "one centralized state instead of retesting on every
page", "reopening a connection at every request sounds like bad code",
and "users and entities look completely conflated". Four fable5
adversarial reviews (architecture, best-practices, transport forensics,
conflation investigation) — all findings implemented. Gate: 51 tests
green (8 unit + 43 headless), clippy zero, fmt clean, live pty smoke.

### The centralized connection authority (new: src/health.rs)
- ONE rule app-wide: a transport-class failure anywhere (domain read or
  write) no longer speaks for itself — it triggers ONE background health
  probe (own thread, fresh client from the same credential resolution
  the Probe button uses — never through the worker queue, so it cannot
  wait behind a 300s test). `ConnPhase` is the single truth: a new
  `Verifying` phase renders in the header ("admin@default — verifying
  connection…"), every error panel ("network hiccup — verifying…"),
  and the notice lane. Settle decides the one story: Connected → each
  failed domain retried ONCE automatically; down → every panel defers
  to "gateway connection lost — fix it on the Connection screen".
- Bounds are structural: probe only on the Connected→Verifying edge (a
  failing probe cannot re-trigger itself); one probe in flight (the
  phase IS the guard); per-slot one-retry budgets cleared by user
  action (`r`/refresh/reset) so an endpoint that fails while /ping
  answers can never loop; stale settles discarded by generation.
- The probe-error→phase mapping is extracted to ONE function shared
  with the Probe button (`phase_from_probe_error`) — the authority and
  the button can never tell different stories.

### Transport policy (reconciled from two adversarial reviews)
- POOLING RESTORED (the field standard; the operator's instinct was
  right). The measured hazard (uvicorn FINs idle keep-alives at 5.00s;
  macOS RSTs the half-closed socket at +60s; ureq's pool-checkout peek
  propagates that instead of discarding — reproduced verbatim) is cured
  by a bounded GET-ONLY retry on the socket-death class (reset/aborted/
  broken-pipe/unexpected-EOF; never connect-refused, never timeouts,
  NEVER writes — the gateway's write endpoints are not uniformly
  idempotent). This also covers what pooling-off never fixed: responses
  dying mid-body and gateway-bounce resets. Classification unit-tested;
  the ureq peek hole recorded for an upstream filing (backlog 0002).
- New `NotConnected` error kind: "probe on the Connection screen first"
  instead of a network-flavored error with a dead retry hint.
- Modal error panels teach THEIR real recovery ("close and reopen this
  dialog", "press the Reload button", "press g / Run") — the `r` hint
  was dead inside modals.

### Users & Entities partition (operator screenshot; web-console parity)
- The users table shows HUMAN principals only; entity principals
  (roles=["entity"] — the gateway deliberately registers entities as
  authenticated users) are counted in a teaching line ("N entity
  principals also hold a door credential — managed in Entities below,
  never here") exactly like the web console. Partition at the FOLD, so
  selection indices can never desync and rotate/delete cannot target an
  entity principal by construction. Empty-state distinguishes "no human
  users" from an empty registry.
- P1 filed gateway-side (backlog 0089): PATCH/DELETE /admin/users is
  unguarded on entity principals — rotate mints a live entity bearer
  that must not exist; delete opens name-capture of the entity's
  identity. The API lane must refuse; any client can reach it.

### Robustness (round-4 best-practices + transport audits)
- `reset_domains` uses an exhaustive destructure (no `..`): adding a
  Store field now fails compilation until the reset-or-exempt decision
  is made — the stale-data class is structurally impossible.
- The purge dry-run gate now vetoes on body-level `ok:false` (it obeyed
  transport but not the app's own body-over-transport law).
- The entity-detail warm-keeper held an unbounded auto-retry loop
  against a persistently failing read — now budgeted per requested
  name (same row never loops; selection moves still load).
- Worker second-lane design RECORDED (backlog 0001), deliberately not
  built: serial total-order is a correctness feature today.
- Polish: dead identity closure removed; semantic screen constants;
  `suffix_chars` helper; `write_done` single-slot invariant documented;
  harness settle-rule comment + `drain_cmds`; engine ask 1040 filed
  (focused-widget introspection for headless tests).

## 0.3.3 (2026-07-24) — the vanishing title bar (operator screenshots) + chrome pinning wave

Operator report: the header bar sometimes did not render (Users &
Entities with real rosters). Headless-reproduced, root-caused, fixed,
and adversarially verified (fable5, 36/36 matrix: 6 screens x 2 modes x
3 sizes incl. 100x12 — chrome present in every cell).

### Root cause
The title bar was a fixed `line(1)` row in the root column; when a
page's loaded content minimum over-demands height, the engine's flex
negotiation silently shrinks fixed rows to zero (engine finding 0240's
class at the ROOT). The trigger is DATA VOLUME with a largest-remainder
rounding threshold — light-fixture reviews never caught it; the first
real gateway payload did. The header was not even "gone": the tab bar
OVERPAINTED it (zero-height rows still run their draw closures —
the fusion class, filed as engine finding 1030).

### Fixed
- Title bar, the new separator line under it, and the footer's chrome
  rows are pinned `shrink(0.0)` — content yields, chrome never does.
- One blank separator line between the title bar and the tab bar
  (operator ask: the header must never butt against components below).
- Sandbox result slots (screen + modal) pinned: under height pressure
  they crushed to zero and FUSED with the button row (adversary,
  100x16) — the journal's basis(0) scroll absorbs pressure instead.
- `message_slot` (the dirty-Esc warning line) pinned: its survival was
  accidental placement-order luck; a safety line is now structural.
- Header middle-ellipsizes long URLs: the line truncates
  last-span-first, so a long remote-gateway URL used to evict the
  connection dot + identity — the most important spans.
- Footer hints: universal pairs (quit especially) now precede
  per-screen verbs — on wizard data screens at ≤120 cols NO quit
  affordance survived truncation.
- The engine's startup-notice signal (`use_startup_notices`) now
  renders in the footer's idle notice lane — the engine's own
  zero-collapse diagnostic was firing into a signal nobody read.
  (Follow-up 2026-07-25: the engine's ambient capability summary —
  "caps: truecolor …" — is filtered out of that lane; it rendered
  permanently in warn-amber and read as a problem. Only diagnostic
  notices surface; idle stays blank.)

### Tests
- `title_bar_and_separator_survive_content_pressure`: all 6 screens x
  both modes x heavy fixtures at 100x24 — header at row 0, separator at
  row 1, tab bar at row 2.

### Engine findings filed
- 1020 (fixed rows shrink to zero at root — app recipe applied; the
  "line(n) implies shrink(0)" ask WITHDRAWN after arguing compat) and
  1030 (zero-area rects still PAINT — the fusion class; the one real
  engine ask). Reported to the tui seat with the operator's mandate:
  engine layout flexibility across terminal sizes/ratios, verified by a
  fable5 adversarial sweep.

## 0.3.2 (2026-07-24) — round-2 review fold (the wave validated, residue cleared)

A second fable5 adversary validated the 0.3.1 consolidation itself (no
correctness defects — every mechanical edit checked out) and delivered
the residue + next-tier list; all implemented. Gate: 47 tests green
(7 unit + 40 headless), clippy zero, `cargo fmt --check` clean, live
pty smoke ALL GATES PASSED.

### Fixed
- **Empty voice triple renders "unset" (P2-2, proven)**: `EntityDetail::fold`'s
  voice arm now carries the same empty-string filter as its substrate
  twin — the gateway answers `provider: ""` for an unset voice, which
  used to render as a set-looking " / / " triple in the inspector.
  Regression-pinned.
- **`r` no longer lies on screens 0/5**: the root refresh handler used
  to post "⟳ refreshing Connection…" over a no-op; it now refuses with
  the reason (nothing to refresh there — the dead-action class).
- **Sandbox double-press guard**: Generate sets `Loading` synchronously
  (the routes-editor twin's guard) — a rapid double-press could fire
  two real generations.
- **Own-time numeric fields refuse garbage with the reason** instead of
  silently substituting defaults ("20abc" no longer starts a loop at
  20); blank still means the stated default.
- **api.rs error fallback reads strings as strings** (`as_str` before
  `to_string`) — a JSON-string `error` no longer renders with literal
  quotes.

### Consolidated / cleaned
- **Route Save folds onto `picked_pair`** — the inline copy had diverged
  once (`unwrap_or_default()` could PUT `model: ""`); one derivation
  now serves Save, Test, and the readiness line.
- **`entity_path()` in api.rs** replaces 20 `format!("/entities/{}/…")`
  copies.
- **`SaveToolPolicy`'s verify tail** rides `publish_ready` (the one F13
  straggler).
- **`Body::DerefMut` deleted** (zero users; mutation happens on locals
  before wrapping).
- **`cargo fmt` now clean** — the 0.3.1 mechanical edits left 37 fmt
  hunks; formatted once, suite re-verified.

### Tests
- Store fold pins for the four parsers the wave rewrote with zero
  coverage: runs (items|runs alternation + required-key drops),
  data-homes, reservations, candidates.
- `dirty_guard_disarms_on_edit_after_warning` pins the shared guard's
  third clause (edit clears the warning; next Esc re-warns, never
  discards).

### Docs
- CHANGELOG 0.3.1 counts corrected (six fixes, ×11 row folds); README
  engine-findings band 0900–1010; README + test-header coverage claims
  rewritten to say what is actually pinned headless vs proven live;
  the post-write refresh helpers' comment now states the real
  (deliberate) no-busy-bracket choice.

## 0.3.1 (2026-07-24) — consolidation wave (adversarial code-quality review)

One fable5 adversarial reviewer read the whole tree against the app's
own stated laws; 17 findings, all implemented. Net ≈ −600 lines with
zero shipped-behavior change outside the six fixes below. Gate: 39
headless + 2 unit tests green, clippy zero (`--all-targets`), live pty
smoke ALL GATES PASSED against the real gateway.

### Fixed (proven defects)
- **Stale-data reset now covers every slot (F1, P1 — proven)**: the
  reset-on-probe list existed in two hand-maintained copies and BOTH
  missed the seven newest domains (`entity_detail`, `entity_policy`,
  `entity_prompt`, `entity_candidates`, `runs`, `data_homes`,
  `reservations`) — a same-named entity on gateway B rendered gateway
  A's substrate/voice/grant in the drawer. The ONE list now lives in
  `Store::reset_domains()`; both call sites delegate; the reprobe test
  pins the new slots.
- **Entity-detail load stall (F2 — proven)**: moving the roster
  selection while a detail load was in flight skipped the send and
  never re-ran (untracked slot read) — the drawer said "reading …"
  forever. The selection effect now TRACKS the slot; a stale landing
  re-fires the load for the current row (pinned by a new test).
- **Voice-test artifact label read the wrong key (F3)**: the gateway
  ships `{"$artifact": id, …}`; the old read tried `artifact_id` and
  every successful voice test rendered the "audio artifact" fallback.
  `artifact_ref_label()` reads `$artifact` first (unit-tested against
  the live shape).
- **Sandbox modal's dirty-Esc guard was silent (F5)**: the first Esc on
  a typed model id blocked with zero feedback and never disarmed. It
  now warns through the toast and edits disarm it — the standard
  contract.
- **Stranded selections in the two modals (F8)**: `home_sel`/`resv_sel`
  now clamp to their shrinking row sets (transfer/purge removes rows;
  the stranded index used to dead-end the reopened modal).
- **`r` on Users refreshes the inspector too (F17)**: screen refresh
  resets `entity_detail` so the drawer reloads with the fresh roster.

### Consolidated (one implementation per contract)
- **Form plumbing (F4)**: `install_dirty_guard_with` / `install_dirty_guard`
  / `install_write_done` / `message_slot` hoisted to `ui/mod.rs`;
  providers/users/routes ported off their verbatim copies (~230 lines,
  4 independent copies of the arm/disarm contract → 1).
- **Secrets discipline is structural (F7)**: `Secret` and `Body`
  newtypes redact in their own `Debug`; the 281-line manual `Cmd`
  Debug impl is deleted and `Cmd` derives Debug — a new variant can no
  longer forget to redact.
- **Danger confirms (F9)**: `confirm_danger()` — ten verbatim
  ChoicePrompt sites collapsed; the keep-default is structural now.
- **Worker read arms (F6)**: seven hand-rolled Loading→Ready/Failed
  arms folded onto the existing `load()` helper (parsing moved off the
  UI thread as a side effect).
- **Verify publishing (F13)**: `publish_ready()` replaces 12 verbatim
  wake.post tails.
- **Store parsing (F12)**: `str_list()` (×4 sites) and `rows_from()`
  (×11 row folds, keyed by slice for the items|runs alternation).
- **`open_form` delegates to `open_form_guarded` (F11)** with an
  unfilled guard slot.

### Removed (dead code, each verified zero-caller)
- `ConnPhase::identity()`, `ApiError::status()`, `UiState.sb_provider`/
  `sb_model`, `EntityRow.slug`, `Profile.model_count`,
  `RoutesData.source`, `DiscoverOutcome.base_url_configured`/`api_key_set`
  (F10); the broken `scripts/review_refresh_experiment.py` (imported a
  deleted sibling — could never run) + `__pycache__/` gitignored (F14).

### Docs/tests
- Cargo.toml dependency comment updated to the 0.2.12 rationale (F15);
  the tautological drawer assert replaced with the deterministic
  "reading Testor" pin (F16 — the suite's own named anti-pattern).

## 0.3.0 (2026-07-24) — AbstractTUI 0.2.12: PageHost + Drawer adoption

Executed per the engine team's upgrade brief
(`abstracttui/reviews/gateway-console-v2-upgrade-prompt.md`) — the
engine now owns the jobs the shell hand-rolled.

### PageHost: the tab system is the engine's now
- ONE `PageHost` carries the tab bar + page region for all six screens
  (`.page(id, "N Title", view)` ×6, controlled `active`). Deleted
  wholesale: the hand-rolled `screen_bar` (~83 lines) including its
  mouse hit-test that MIRRORED the draw arithmetic (the F4 drift class
  PageHost's single-plan design kills — one plan feeds draw AND click,
  they can never disagree), the `body` match, and the digit loop's
  browse half.
- The wizard/browse split survives exactly as designed: PageHost is
  FREE navigation, armed in browse (`.number_jump(true)` +
  Ctrl+N/P via `.chords()`); in wizard the free surface is fully
  DISARMED (empty chord sets, digits off) and the app-side gate logic
  (`wizard_next`/`wizard_back`, digit refusals WITH reasons) keeps
  writing the screen signal. `ui.screen: usize` stays the source of
  truth; a two-way equality-guarded bridge keeps PageHost's string id
  in lockstep. Pinned by `pagehost_browse_navigation_digits_and_chords`
  (digit jump, EXACTLY-one-step chords, wizard digit refusal).
- Honest trade recorded: the wizard's greyed-future-step look is gone
  (PageHost has one active/idle style) — filed upstream as
  field-gateway 1010 (per-tab locked presentation state); the gate
  itself never weakened.

### Drawer: the entity inspector
- The 3-row inline entity snapshot strip became a RIGHT drawer (`i`
  toggles; passive focus — the roster keeps the keyboard; instant
  motion; auto-closes when leaving the Users screen). Full room for
  the whole manage snapshot: state/mode/handle/drives, mind + source,
  voice set + effective, work order, loop, grant. Decisions stay
  Modals; the inspector is a reader.

### Gate
- abstracttui 0.2.9 → 0.2.12 (zero API breaks, suite green at the
  bump before any migration edit); 38 headless + 1 unit tests green;
  clippy zero; live pty smoke ALL GATES PASSED.

## 0.2.1 (2026-07-24) — the last three entity-config surfaces (write parity complete)

### Tool policy editor (web capability-matrix parity)
- Manage menu → "Tool policy": per-phase grants as MultiSelects fed by
  the live capability matrix (`GET /entities/inventory/capability-matrix`;
  degrades to the union of granted tools if the matrix read fails),
  provenance shown per phase (default / policy-file). Save sends ONLY
  changed phases (`PUT /entities/{name}/tool-policy {policy}` — the
  web's delta law); emptied phases ask the operator to pick the
  consequence: reset-to-default (null) vs explicit deny-all ([]) —
  the web's exact semantics, danger-marked. Live-verified against
  castor's real grants (visit 9 / work 11 / personal 9 / sleep 6).
- Pinned by `entity_tool_policy_editor_saves_changed_phases_only`
  (drives the real MultiSelect popup: Space toggles a working copy,
  Enter COMMITS — Esc discards, a real gotcha the test now teaches).

### Prompt overlay editor (upgraded from the planned viewer)
- The engine HAS a TextArea (a wrong "no multi-line editor" finding was
  filed and retracted same-hour after checking the widget list) —
  Manage menu → "Prompt overlay" edits each layer in a real multi-line
  TextArea (`SubmitPolicy::EnterInserts`: Enter = newline), all layers
  ride the save (`PUT /entities/{name}/prompt {overlay}`), verify-via-GET
  reports per-layer char counts.

### Candidates review (sleep consolidation)
- Manage menu → "Candidates review": the pending-review list
  (kind/title/digest) with Promote / Reject, reason REQUIRED (journaled
  acts), verify-via-GET confirms the row left the queue.

### Engine finding: the dead-keys window (live diagnosis)
- Both new editors shipped with a silent trap: their widgets mount
  ASYNC inside regions (where autofocus is the 0220 panic hazard), so
  the modal had NO focus owner — every key but Tab was dead, Esc
  included, indistinguishable from a frozen app in the pty (process
  alive, zero bytes). Fix: `.focusable().autofocus()` on the modal
  content root (the 0230 pattern); filed upstream as field-gateway 1000
  with two structural asks (implicit layer-root focus fallback, or a
  loud no-focus-owner debug note).

### Gate
- 37 headless + 1 unit tests green; clippy zero; live pty smoke ALL
  GATES PASSED including the two new editors opening against the live
  gateway (real policy/matrix/overlay reads) and clean Esc round-trips.

## 0.2.0 (2026-07-24) — full web-console write parity + interaction honesty wave

Driven by two independent reviews: a web-UI parity audit (every
user-changeable parameter in `console.py` vs the TUI) and an
interaction root-cause review of the operator's "probe does nothing"
incident (verdict: the click always worked — the probe succeeded in
1–2 ms and repainted identical pixels; pure acknowledgment failure).

### Probe acknowledgment (the incident fix)
- Every probe — auto, Enter-in-field, button, mouse — now lands a
  numbered, timestamped, latency-carrying "last probe #N" line, a
  toast, and token-source-on-success. Same-outcome re-probes are
  visibly distinct events. Pinned by `probe_report_line_renders_and_updates`.
- First-run honesty: a connected screen SAYS so above the form
  ("Connected — nothing to change here…"), the button relabels to
  "Re-probe (connected ✓)", and when the env token is in use the
  masked-empty field says so in normal ink (the empty field was the
  universal "not logged in" signal).

### Interaction honesty (review F2–F5)
- Every footer-advertised key that refuses now SAYS why (no selection,
  not connected, wrong mode) — across providers/routes/users/review.
- Navigation dead-ends notice ("already on the first screen", "q quits
  in browse mode", digit jumps in wizard) instead of swallowing keys.
- The screen bar is CLICKABLE in browse mode (digit-key semantics;
  hit-test mirrors the draw arithmetic).
- `r` refresh acks immediately ("⟳ refreshing …") — fast domains
  repaint identically, so the notice is the trace.

### Web parity — Providers
- `allowed_models` allowlist on the profile form (sent on EVERY save so
  clearing works; "Restrict to these N models" fills from a live
  discovery; the table shows "N restr"/"live").
- Scope is editable on update (the gateway moves the profile between
  user/gateway stores); escalation without admin refused locally.
- Test-connection now tests the FORM's family+URL (with `profile_id`
  riding along so the stored key applies) — this also fixed a real
  bug: the old saved-profile test body omitted `provider_family`,
  which the request model DEFAULTS to openai-compatible, so non-OAI
  profiles were tested under the wrong family.

### Web parity — Routes
- `output.voice` editors carry a per-pair voice picker fed by
  `GET /voice/voices?provider&model&compact`, writing into the options
  JSON (one source of truth; picker resolves the saved voice or falls
  to the "provider default" placeholder — fabricated-selection law).
- Every route editor has a Test verb: voice routes synthesize through
  `POST /runs/{session_memory_gateway_console_voicetest_…}/voice/tts`
  (the SAME run id shape the web mints — one shared test plane),
  everything else through `/sandbox/generate` with the route's own
  capability key. Results render inline with elapsed seconds;
  body-level `ok:false`/`error` is failure.

### Web parity — Users & Entities
- User create: advanced `tenant_id` + `runtime_id` bindings (blank =
  gateway defaults, omitted from the body) and the `readonly` role.
- Entity manage menu (`m`): state wake/sleep(+dream)/pause with reason,
  mind substrate, voice triple (+clear), work order (+clear), own-time
  grant + loop start/stop + danger-gated emergency freeze, re-embed
  (danger-gated, dry-run-style confirm), verify chain. All writes are
  write → verify-via-GET → journal; state verifies against
  `/cognition` and honestly reports intent-vs-actuality settling.
- A selection-driven manage snapshot panel under the entities table
  (mind / voice / loop / work order at a glance).
- Runtime reservations modal (`v`): transfer retained planes to a
  living user or purge with data deletion — both danger-confirmed.
- Entity creation/summon/visits stay deliberately out (rituals, not
  configuration); the block title teaches the split.

### Web parity — Runtimes
- Recent root runs table with durable cancel (`c`, danger-confirmed,
  honest "consumed at the next tick boundary" verify note) and steer
  (`s`, inject_guidance with a guidance form). Terminal runs refuse
  with the reason.
- Data-homes browser (`h`) with purge: the worker runs the gateway's
  dry-run FIRST and a dry-run failure vetoes the purge entirely;
  protected homes refuse client-side with the reason.
- Corrected the runtime-knobs note: NO UI exposes that write today
  (the web console has no runtime-config surface at all) — the API is
  the only editor.

### Gate
- 36 headless tests + 1 unit test green; clippy zero on all targets;
  extended live pty smoke green end-to-end against a live gateway
  (probe ack, voice editor + Test verb, entity manage panel + menu,
  reservations modal, recent-runs row, and the full route
  write/verify/clear/restore cycle).

## 0.1.0 (2026-07-23) — initial build, hardened through three same-day adversarial cycles

### Web-console parity — Runtimes tab
- Added the runtime-knobs surface to the Runtimes screen
  (`GET /admin/runtime-config`), rendered read-only with per-knob
  value + provenance (stored / env / default) and availability-honest
  executor rows — the last read-only surface the served web console
  exposes that the TUI lacked. The five web-console tabs (Users &
  Entities, Runtimes, Providers, Multimodal, Sandbox) are now all
  covered; the TUI additionally ships the guided first-run wizard the
  browse-only web console does not. Pinned by
  `runtime_knobs_render_with_provenance`; live-verified in the pty smoke.

### Adversarial cycle 3 — release gate: SHIP, seven paper cuts fixed
- Verdict from the final review: all cycle-2 fixes verified with no new
  P1/P2; seven P3s found and all fixed same-pass: the dirty-Esc warning
  now WINS over the "applying…" line (it could discard unseen during a
  write); the Esc latch DISARMS when the user edits again after the
  warning; the sandbox modal guards its one typed field (model id); the
  changelog collapsed to one version heading; engine finding 0945's
  MODAL_Z citation corrected to popups.rs:37; dead `section()` helper +
  a stale 0.2.8 comment removed; README states the coverage split for
  the profile leg honestly (headless keyboard tests + live API E2E; the
  pty smoke drives the route leg live).
- Final gate on the shipping build: 33 headless + 1 unit tests, clippy
  zero, live API E2E (writes + verify-via-GET + RAII cleanup) green,
  keyboard-driven pty smoke green against the live gateway.

### Adversarial cycle 2 — fixed (1 P1, 3 P2, 5 P3)
- P1: a queued once-token modal could stack over a live ChoicePrompt at
  the same z — the engine gives keys to the OLDEST modal layer, so an
  invisible danger confirm kept the keyboard under a visible token
  modal. All prompts now open through `open_prompt` (an open-count the
  token queue gates on); regression-pinned.
- "Re-pick the provider to retry" was a dead gesture (the Select
  early-returns on same-value commits): discovery-failed rows now carry
  a real "Retry model discovery" button — and the sandbox modal gained
  the same verbatim-error + retry arm it was missing.
- The post-connect hint taught `]`, which types a literal `]` into the
  focused URL field: hints/help/README now lead with Ctrl+N.
- Worker panics release the issuing form (was: wedged on "applying…").
- Esc on a dirty form warns first ("press Esc again to discard") —
  typed keys/URLs/JSON no longer die on one keypress; clean forms still
  close on one Esc.
- Profile/user Save buttons render disabled during in-flight writes
  (parity with the routes editor).
- live_e2e's cleanup guard can now RESTORE a pre-configured route (not
  just clear); a tautological test assert replaced with a real pin.
- Adopted abstracttui 0.2.9 (released mid-build by the engine team).
- Engine findings filed: 0905 (same-value re-commit unobservable),
  0935 (dirty tracking hand-rolled), 0945 (ChoicePrompt same-z
  stacking, no introspection) — field-gateway now carries 13 items.

### Built (initial)
- Six screens over one shell (wizard with gated steps / browse tabs):
  Connection, Providers (endpoint-profile CRUD + test-connection),
  Routes (default-vs-override capability editing), Users & Entities,
  Runtimes, Review & Test (change journal + sandbox generation).
- One worker thread owns all HTTP (ureq); results cross to the UI as
  posted closures. Every write is write → verify-via-GET → journal,
  inside one busy bracket.
- Honest-state rendering everywhere (`Loadable<T>`: not-asked /
  loading / failed-with-kind / ready-empty); gateway `detail` text
  verbatim; 200-with-`ok:false` treated as failure.
- The fabricated-selection law in the route editor: explicit
  default-vs-override mode, placeholder pickers, model reset on
  provider switch, "Applies now:" derived only from server state.
- Secrets discipline: masked inputs, write-only keys (fingerprint
  display, blank-keeps, explicit clear), once-shown tokens in a
  dedicated modal with clipboard copy, secret-redacting `Debug` on
  worker commands (unit-pinned).
- Headless CaptureTerm suite (31 tests) driving the real UI by
  keyboard; ignored live E2E (`tests/live_e2e.rs`) with RAII cleanup;
  keyboard-driven pty smoke (`scripts/pty_smoke.py`) proving the
  definition-of-done against a live gateway with HTTP state asserts
  and restore-on-failure.

### Adversarial cycle 1 — fixed (16 findings, 1 P1 / 7 P2 / 8 P3)
- P1: re-probing a different gateway/token left every cached domain
  rendering as current — probes now reset all domain caches (UI-side +
  worker-side), regression-pinned.
- Token modals queue; a second once-shown token waits for explicit
  dismissal of the first (an unread token is unrecoverable).
- Probe taxonomy: 403 and "answered, but not like a gateway" (port
  squatters) are distinct states; the connection screen names the
  SOURCE of the token each probe sent (field / env / flag / none).
- Quit no longer joins a worker mid-HTTP (up to 300 s hang).
- Width-aware table columns: narrow terminals drop secondary columns,
  never the payload column (80-col test).
- Runtime sizes honor the gateway's `size_note` truncation label
  (render "≥ X", never a floor as a fact).
- Forms carry in-flight state: double-submit guarded, "applying…"
  line, buttons disabled during the write+verify.
- Model-discovery failures render verbatim and retry on provider
  re-pick (untracked cache reads — no retry storms).
- Hand-rolled text rows clip to their rect; table selections clamp
  when rows shrink; the worker survives per-command panics loudly;
  footer notices retire on screen switch; sandbox modal resets its
  result slot; `model_ix` underflow guards.
- Engine findings filed: field-gateway 0900–0990 (10 items).
