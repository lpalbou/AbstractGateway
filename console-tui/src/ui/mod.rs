//! UI root: one shell, two modes (wizard steps / browse tabs) over the
//! same screens — eight of this crate's, then AbstractCore's shared
//! Models (9) and Engines (0) screens from the `abstractcore-console`
//! crate. Screens are plain component functions; durable UI
//! state (form fields, selections) lives in [`UiState`] created at the
//! root so screen remounts on tab switches lose nothing.

/// The About modal (F1 / ?).
pub mod about;
pub mod connection;
pub mod entity_manage;
pub mod models;
pub mod network;
pub mod providers;
pub mod review;
pub mod routes;
pub mod runtimes;
pub mod users;
pub mod util;
pub mod widths;
pub mod workflows;

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::mpsc::Sender;
use std::time::Duration;

use abstracttui::app::{ChoiceOutcome, ChoicePrompt, Modal, Overlays, Toast};
use abstracttui::prelude::*;
use abstracttui::reactive::IntervalHandle;
use abstracttui::widgets::PageHost;

use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::Cmd;
use abstractcore_console::screens::Remote;
use util::{hints, line, span, span_bold};

pub const SCREENS: [&str; 10] = [
    "Connection",
    "Providers",
    "Routes",
    "Users & Entities",
    "Runtimes",
    "Workflows",
    "Review & Test",
    // APPEND ONLY: tests and muscle memory key on the digit order.
    // User-visible label only — SCREEN_IDS keeps the stable "models" id.
    "Resources",
    // AbstractCore's shared screens (crate `abstractcore-console`),
    // served here over the gateway's HTTP mirrors (transport_http.rs):
    // the model catalog / downloads / deletes, and the local engines.
    abstractcore_console::screens::CATALOG_TITLE,
    abstractcore_console::screens::ENGINES_TITLE,
];

/// Screens with semantic weight get NAMES (round-4 P3-2): the bare
/// literals were confined to this module but 3 of them carry meaning
/// a reader had to reconstruct.
pub const SCREEN_CONNECTION: usize = 0;
pub const SCREEN_USERS: usize = 3;
pub const SCREEN_WORKFLOWS: usize = 5;
pub const SCREEN_REVIEW: usize = 6;
pub const SCREEN_MODELS: usize = 7;
/// AbstractCore's shared "Models" screen (page id `catalog`), key `9`.
pub const SCREEN_CATALOG: usize = 8;
/// AbstractCore's shared "Engines" screen (page id `engines`), key `0`.
pub const SCREEN_ENGINES: usize = 9;

/// The digit that jumps to screen `i` in browse mode: 1-9, then 0 for
/// the tenth (PageHost's own number jump covers 1-9 only).
pub fn screen_key(i: usize) -> char {
    if i == 9 {
        '0'
    } else {
        char::from_digit(i as u32 + 1, 10).expect("screens 1-9")
    }
}

/// Stable PageHost page ids, parallel to `SCREENS`. `ui.screen: usize`
/// stays the source of truth (the wizard gate reads indexes); a two-way
/// equality-guarded bridge keeps PageHost's string `active` in lockstep.
pub const SCREEN_IDS: [&str; 10] = [
    "connection",
    "providers",
    "routes",
    "users",
    "runtimes",
    "workflows",
    "review",
    "models",
    // Never "models"/"runtimes": those ids are taken (Resources and the
    // execution planes). The shared contract names these two.
    abstractcore_console::screens::CATALOG_ID,
    abstractcore_console::screens::ENGINES_ID,
];

/// Durable per-screen UI state (Copy: all signals).
#[derive(Clone, Copy)]
pub struct UiState {
    /// true = wizard chrome (gated steps); false = browse tabs.
    pub wizard: Signal<bool>,
    pub screen: Signal<usize>,
    /// The user explicitly chose to continue without a connection.
    pub offline_ok: Signal<bool>,

    pub conn_url: Signal<String>,
    pub conn_token: Signal<String>,
    /// Human description of the token the LAST probe actually sent
    /// ("field (44 chars)", "env ABSTRACTGATEWAY_AUTH_TOKEN (19 chars)",
    /// "none — no Authorization header"). The one answer to "why can't
    /// I authenticate": which secret source was used, never its value.
    pub token_source: Signal<Option<String>>,

    pub profile_sel: Signal<usize>,
    pub provider_sel: Signal<usize>,
    pub route_sel: Signal<usize>,
    pub user_sel: Signal<usize>,
    pub entity_sel: Signal<usize>,
    pub runtime_sel: Signal<usize>,
    pub workflow_sel: Signal<usize>,
    /// Draft versions are hidden by default: a registry that is majority
    /// drafts buries the published set the operator is looking for.
    pub workflow_drafts: Signal<bool>,
    pub run_sel: Signal<usize>,
    pub home_sel: Signal<usize>,
    pub resv_sel: Signal<usize>,

    /// The runtime the operator CHOSE to inspect (click / Enter on the
    /// runtimes table). None = nothing chosen — the detail region shows
    /// a teaching line and NO data loads (operator directive
    /// 2026-07-26: "do NOT eagerly load the runs/sessions; upon
    /// clicking one runtime, then we can see their sessions and
    /// data/cache below").
    pub rt_detail: Signal<Option<crate::store::RuntimeRow>>,
    /// Active detail tab: 0 = Sessions, 1 = Data & cache.
    pub rt_tab: Signal<usize>,
    /// Runtime-knobs disclosure fold state (true = collapsed, the
    /// default; the knobs load fires on first expand).
    pub rt_knobs_folded: Signal<bool>,
    /// Runtimes-panel toolbar state (parity with the web console's
    /// per-tab filter + search + pager). Held in UI state, mirrored into
    /// each tab's loaded payload so a filter change invalidates it.
    pub rt_runs_status: Signal<String>,
    pub rt_runs_query: Signal<String>,
    pub rt_runs_offset: Signal<u32>,
    pub rt_art_modality: Signal<String>,
    pub rt_art_query: Signal<String>,
    pub rt_art_offset: Signal<u32>,
    pub rt_cache_kind: Signal<String>,
    pub rt_cache_query: Signal<String>,
    pub rt_logs_home: Signal<String>,
    pub rt_logs_query: Signal<String>,
    /// Per-tab row selection. home_sel belongs to the CACHE tab alone —
    /// its clamp is sized by the cache row count, so any tab that borrowed
    /// it inherited that ceiling.
    pub rt_art_sel: Signal<usize>,
    pub rt_logs_sel: Signal<usize>,

    /// Models tab: 0 = Loaded (resident models), 1 = Caches (session
    /// prompt caches). Selections are per-table (own row counts).
    pub models_tab: Signal<usize>,
    pub model_sel: Signal<usize>,
    pub cache_sel: Signal<usize>,
    /// The memory itemization's window position, paged by `m`. It lives
    /// HERE and not inside the region because the Models body regenerates
    /// on every ~4s host-state poll — an offset owned by the region would
    /// snap back to the top under the operator's hands. Read modulo the
    /// live line count, so it is always a valid position whatever the
    /// snapshot or the terminal size turns out to be.
    pub models_detail_top: Signal<usize>,

    pub sb_prompt: Signal<String>,
    /// Sandbox picks, stored BY NAME (indices are per-list and die on
    /// reload; names survive tab switches and carry the Providers `t`
    /// prefill). Empty = placeholder (nothing picked).
    pub sb_provider: Signal<String>,
    pub sb_model: Signal<String>,
    /// Hand-typed model id for providers whose discovery failed/empty.
    pub sb_model_custom: Signal<String>,

    /// (user_id, token) from create/rotate — shown once each, in a
    /// QUEUE: a token modal must never be bulldozed by the next token
    /// (a rotated token that was never read is unrecoverable).
    pub token_queue: Signal<Vec<(String, String)>>,
    /// Bumped whenever the shared modal slot closes — wakes the token
    /// queue effect so a waiting token opens after the current modal.
    pub modal_epoch: Signal<u64>,
    /// Open ChoicePrompt count. Prompts are NOT in the modal slot (the
    /// engine gives them their own Modal at the same z), and equal-z
    /// key dispatch belongs to the OLDEST modal — so opening anything
    /// over a live prompt paints on top of an invisible key owner.
    /// Everything that opens a prompt goes through `open_prompt`;
    /// everything that could stack (the token queue) waits on this.
    pub prompt_open: Signal<u32>,
    /// (form_id, outcome) — write completions routed back to open forms.
    /// Single slot is CORRECT under the one-modal invariant
    /// (open_form closes predecessors; the worker is serial; only the
    /// one open form's write posts here) — same argument as
    /// store.discover. Revisit together if modal stacking changes.
    pub write_done: Signal<Option<(u64, Result<String, String>)>>,
}

impl UiState {
    pub fn create(cx: Scope, url: String, token: String) -> UiState {
        UiState {
            wizard: cx.signal(true),
            screen: cx.signal(0),
            offline_ok: cx.signal(false),
            conn_url: cx.signal(url),
            conn_token: cx.signal(token),
            token_source: cx.signal(None),
            profile_sel: cx.signal(0),
            provider_sel: cx.signal(0),
            route_sel: cx.signal(0),
            user_sel: cx.signal(0),
            entity_sel: cx.signal(0),
            runtime_sel: cx.signal(0),
            workflow_sel: cx.signal(0),
            workflow_drafts: cx.signal(false),
            run_sel: cx.signal(0),
            home_sel: cx.signal(0),
            resv_sel: cx.signal(0),
            rt_detail: cx.signal(None),
            rt_tab: cx.signal(0),
            rt_knobs_folded: cx.signal(true),
            rt_runs_status: cx.signal(String::new()),
            rt_runs_query: cx.signal(String::new()),
            rt_runs_offset: cx.signal(0),
            rt_art_modality: cx.signal(String::new()),
            rt_art_query: cx.signal(String::new()),
            rt_art_offset: cx.signal(0),
            rt_cache_kind: cx.signal(String::new()),
            rt_cache_query: cx.signal(String::new()),
            rt_logs_home: cx.signal(String::new()),
            rt_logs_query: cx.signal(String::new()),
            rt_art_sel: cx.signal(0),
            rt_logs_sel: cx.signal(0),
            models_tab: cx.signal(0),
            model_sel: cx.signal(0),
            cache_sel: cx.signal(0),
            models_detail_top: cx.signal(0),
            sb_prompt: cx.signal("Reply with one short sentence: what model are you?".into()),
            sb_provider: cx.signal(String::new()),
            sb_model: cx.signal(String::new()),
            sb_model_custom: cx.signal(String::new()),
            token_queue: cx.signal(Vec::new()),
            modal_epoch: cx.signal(0),
            prompt_open: cx.signal(0),
            write_done: cx.signal(None),
        }
    }
}

/// Cloneable UI context: the command channel, overlay store, quit and
/// the single-modal slot (stacked modals are an engine hazard — one at
/// a time, sequenced).
#[derive(Clone)]
pub struct Ctx {
    pub tx: Sender<Cmd>,
    pub overlays: Overlays,
    pub quitter: abstracttui::app::Quitter,
    pub store: Store,
    pub ui: UiState,
    pub modal: Rc<RefCell<Option<Modal>>>,
    /// The entity-inspector right Drawer's handle (installed once at
    /// root mount; the Users screen toggles it). Passive focus: the
    /// roster keeps the keyboard while the panel is open.
    pub entity_drawer: Rc<RefCell<Option<abstracttui::app::drawer::DrawerHandle>>>,
    /// Env-token presence (never its value) for honest connection copy.
    pub env_token_set: bool,
    /// The health authority's probe launcher (url, token, generation).
    /// Production installs a thread-spawning prober in lib.rs; the
    /// headless harness installs a recorder (or nothing) and calls
    /// `health::settle` directly — no network near tests.
    pub prober: ProberSlot,
    /// AbstractCore's shared Models/Engines screens: their store, their
    /// own worker lane (over `screens_transport`) and their confirms.
    /// Created ONCE in the mount scope with this app's notice signal, so
    /// their outcomes toast and footer through the same lane as ours.
    pub screens: abstractcore_console::screens::ScreensCtx,
    /// The transport behind `screens` — asked for the host label each
    /// time a screen mounts (the gateway it names can change on
    /// reconnect; production is `transport_http::HttpTransport`).
    pub screens_transport: std::sync::Arc<dyn abstractcore_console::ConsoleTransport>,
}

/// The injected probe launcher: (url, token, generation).
pub type Prober = Box<dyn Fn(String, Option<String>, u64)>;
pub type ProberSlot = Rc<RefCell<Option<Prober>>>;

/// Forget everything the shared screens read from the previous gateway
/// (the same law as `Store::reset_domains`: a new gateway or principal
/// never renders under the old one's catalog). The job strip survives —
/// like `Store::download`, it is the only record of a job on the OLD
/// host, and its watch ends by itself.
pub fn reset_screens(s: &abstractcore_console::screens::ScreensStore) {
    use abstractcore_console::screens::Remote;
    s.host.set(Remote::NotAsked);
    s.engines.set(Remote::NotAsked);
    s.catalog.set(Remote::NotAsked);
    s.installed.set(Remote::NotAsked);
    s.engine_filter.set(None);
    s.providers_seen.set(Vec::new());
    s.catalog_sel.set(0);
    s.installed_sel.set(0);
    s.engine_sel.set(0);
    // Late answers from the old gateway die on the generation gate.
    s.catalog_gen.update(|g| *g += 1);
}

impl Ctx {
    /// The shared screens' context for a page mount, labelled with the
    /// gateway host as the transport sees it NOW ("runs on gateway host
    /// …" in the install/delete confirms).
    pub fn screens_for_page(&self) -> abstractcore_console::screens::ScreensCtx {
        let mut s = self.screens.clone();
        s.host_label = std::rc::Rc::from(self.screens_transport.host_label());
        s
    }

    pub fn send(&self, cmd: Cmd) {
        // A dropped worker only happens at quit; ignore then.
        let _ = self.tx.send(cmd);
    }

    pub fn close_modal(&self) {
        if let Some(m) = self.modal.borrow_mut().take() {
            m.close();
        }
    }

    /// Effective connection form values (URL default + env token fallback).
    /// ONE credential resolution for the Probe button AND the health
    /// authority's background probe (they must never disagree — the
    /// normalize_url law applied to credentials). Returns
    /// (normalized url, token-if-any, human source description).
    pub fn effective_credentials_with_source(&self) -> (String, Option<String>, String) {
        let url = normalize_url(&self.ui.conn_url.get_untracked());
        let typed = self.ui.conn_token.get_untracked();
        let typed = typed.trim();
        let (token, source) = if !typed.is_empty() {
            (
                Some(typed.to_string()),
                format!("the field ({} chars)", typed.chars().count()),
            )
        } else {
            match std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN") {
                Ok(t) if !t.trim().is_empty() => {
                    let t = t.trim().to_string();
                    let d = format!(
                        "env ABSTRACTGATEWAY_AUTH_TOKEN ({} chars)",
                        t.chars().count()
                    );
                    (Some(t), d)
                }
                _ => (None, "none — no Authorization header sent".to_string()),
            }
        };
        (url, token, source)
    }

    /// The health authority's view: url + token only.
    pub fn effective_credentials(&self) -> (String, Option<String>) {
        let (url, token, _) = self.effective_credentials_with_source();
        (url, token)
    }

    pub fn connect_now(&self) {
        let (url, token, source) = self.effective_credentials_with_source();
        self.ui.conn_url.set(url.clone());
        let token = token.unwrap_or_default();
        self.ui.token_source.set(Some(source));
        // A probe targets a possibly-different gateway/principal: cached
        // domains are unvouched-for the moment the user asks to connect.
        // Reset here (synchronously, UI-side) so no stale table ever
        // renders under the new header; the worker repeats the reset on
        // its own thread for the auto-probe path (harmless overlap).
        self.reset_domains();
        self.send(Cmd::Connect {
            url,
            token: token.into(),
        });
    }

    /// Forget every cached domain (new gateway / new principal) — the
    /// list lives in ONE place, the store (F1: two hand-maintained
    /// copies each missed the seven newest slots).
    pub fn reset_domains(&self) {
        self.store.reset_domains();
        // The shared Models/Engines screens' reads are gateway data too.
        // (The worker-side reset for the boot auto-probe reaches them via
        // the Probing transition — see install_effects.)
        reset_screens(&self.screens.store);
        // The chosen runtime is REMOTE data (a cloned RuntimeRow) that
        // happens to live in UiState: surviving a gateway/principal
        // reset would make the sessions effect load the OLD plane
        // against the NEW gateway with zero choice made on it, and
        // render the old row's facts under the new header until the
        // self-heal lands (the F1 class in UI clothing).
        self.ui.rt_detail.set(None);
        self.ui.rt_runs_status.set(String::new());
        self.ui.rt_runs_query.set(String::new());
        self.ui.rt_runs_offset.set(0);
        self.ui.rt_art_modality.set(String::new());
        self.ui.rt_art_query.set(String::new());
        self.ui.rt_art_offset.set(0);
        self.ui.rt_cache_kind.set(String::new());
        self.ui.rt_cache_query.set(String::new());
        self.ui.rt_logs_home.set(String::new());
        self.ui.rt_logs_query.set(String::new());
        self.ui.rt_art_sel.set(0);
        self.ui.rt_logs_sel.set(0);
    }

    /// Refresh the domains behind a screen (sets Loading synchronously —
    /// the effect's NotAsked check stays race-free).
    pub fn refresh_screen(&self, screen: usize) {
        let s = &self.store;
        // User action re-arms the health authority's one-auto-retry
        // budgets (a fresh `r` means "try again", including the probe).
        s.conn_retry_spent.set(Vec::new());
        match screen {
            0 => {
                // Connection: the network exposure panel's read (the
                // probe itself stays the Probe button's job).
                if s.conn.with_untracked(crate::store::ConnPhase::is_connected) {
                    s.network.set(Loadable::Loading);
                    self.send(Cmd::LoadNetwork);
                }
            }
            1 => {
                s.profiles.set(Loadable::Loading);
                s.providers.set(Loadable::Loading);
                self.send(Cmd::LoadProfiles);
                self.send(Cmd::LoadProviders);
            }
            2 => {
                s.routes.set(Loadable::Loading);
                s.availability.set(Loadable::Loading);
                self.send(Cmd::LoadRoutes);
                // Weights last: the slowest read on this screen (it
                // talks to the host's LM Studio/Ollama) and nothing
                // else waits on it. A reload is also exactly when a
                // just-finished download must show up.
                self.send(Cmd::LoadAvailability);
                if s.providers.with_untracked(|p| p.ready().is_none()) {
                    s.providers.set(Loadable::Loading);
                    self.send(Cmd::LoadProviders);
                }
            }
            3 => {
                s.users.set(Loadable::Loading);
                s.entities.set(Loadable::Loading);
                // The inspector's detail must honor `r`'s "refreshing
                // live data" promise too (F17): NotAsked here → the
                // selection effect reloads it when fresh entities land.
                s.entity_detail.set(Loadable::NotAsked);
                self.send(Cmd::LoadUsers);
                self.send(Cmd::LoadEntities);
            }
            4 => {
                // ONLY the inventory loads here (operator directive
                // 2026-07-26: no eager runs/sessions). The detail slots
                // reset to NotAsked so the panels that own them (the
                // Sessions/Data tab effects, the knobs disclosure)
                // reload IF the operator has them open — `r` keeps its
                // "refreshing live data" promise without eager-loading
                // anything nobody asked for.
                s.runtimes.set(Loadable::Loading);
                s.runs.set(Loadable::NotAsked);
                s.data_homes.set(Loadable::NotAsked);
                s.runtime_config.set(Loadable::NotAsked);
                self.send(Cmd::LoadRuntimes);
            }
            5 => {
                // The registered workflow registry. Drafts follow the
                // screen's own toggle, so `r` refreshes what is on screen
                // rather than silently changing what it shows.
                s.workflows.set(Loadable::Loading);
                self.send(Cmd::LoadWorkflows {
                    include_drafts: self.ui.workflow_drafts.get_untracked(),
                });
            }
            6 => {
                // The inline sandbox feeds its provider picker from
                // discovery — `r` here means "re-discover providers".
                s.providers.set(Loadable::Loading);
                self.send(Cmd::LoadProviders);
            }
            SCREEN_MODELS => {
                // ONE freshness authority: the host-state poll chain.
                // `r` restarts it under a fresh generation — the old
                // chain's next result fails the gen check and dies, so
                // two chains can never interleave.
                s.host_state.set(Loadable::Loading);
                let gen = s.host_poll_gen.get_untracked() + 1;
                s.host_poll_gen.set(gen);
                self.send(Cmd::PollHostState { gen, first: true });
            }
            // The shared screens own their reads; these are their own
            // `r` verbs (host profile + catalog + installed; a PROBING
            // engines read), reached when the root `r` handles the key.
            SCREEN_CATALOG => self.screens.refresh_catalog(),
            SCREEN_ENGINES => self.screens.refresh_engines(),
            _ => {}
        }
    }
}

/// Modal close handle passed into form builders. The Modal object only
/// exists after `Modal::open` returns, while the builder runs inside it
/// — so builders get a closure over the shared slot instead.
pub type CloserFn = Rc<dyn Fn()>;

/// Open a form modal, closing any previous one (never stack — the
/// engine's same-z stacked-modal hazard). Esc closes; the builder gets
/// a `CloserFn` for its own Save/Cancel buttons. Delegates to the
/// guarded variant with an unfilled guard slot (F11: the two bodies
/// were verbatim twins minus the guard; an empty slot = Esc closes).
/// A preview's share of each axis, as a fraction: two thirds of the
/// terminal, never more (operator 2026-08-20). Big enough that an image
/// mosaic and a log window have room to be worth opening; small enough
/// that the list behind stays legible around it and the modal keeps
/// reading as a LENS over the page rather than a new screen.
const PREVIEW_NUM: i32 = 2;
const PREVIEW_DEN: i32 = 3;

/// A modal sized to a share of the TERMINAL, for the panels whose job is
/// to SHOW content rather than collect it.
///
/// Forms keep their fixed widths on purpose — a text field 200 cells
/// wide is harder to read, not easier. A preview has exactly the
/// opposite property: on an image every extra cell is another pair of
/// mosaic subpixels, and on a log every extra row is another line you
/// did not have to scroll for. So it scales with the terminal, capped at
/// two thirds of each axis.
///
/// The cap is a CAP, not a target: it is deliberately not floored at the
/// fixed size it replaces, because a floor of 96 cells would swallow the
/// cap outright on every terminal under ~146 columns and the proportion
/// would never apply. The engine clamps the request to the viewport and
/// re-clamps on resize, so the result is always on screen.
pub fn preview_size(cx: Scope) -> Size {
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    Size::new(
        (vp.w * PREVIEW_NUM / PREVIEW_DEN).max(1),
        (vp.h * PREVIEW_NUM / PREVIEW_DEN).max(1),
    )
}

pub fn open_form(ctx: &Ctx, cx: Scope, size: Size, build: impl FnOnce(Scope, CloserFn) -> View) {
    open_form_guarded(ctx, cx, size, |mcx, close, _guard| build(mcx, close));
}

/// Open a ChoicePrompt with prompt-tracking: the open count gates the
/// token queue (see `UiState::prompt_open` — same-z modal stacking gives
/// keys to the INVISIBLE oldest layer). Every prompt in the app opens
/// through here.
pub fn open_prompt(
    cx: Scope,
    ui: UiState,
    prompt: abstracttui::app::ChoicePrompt,
    resolve: impl FnOnce(ChoiceOutcome) + 'static,
) {
    ui.prompt_open.update(|n| *n += 1);
    prompt
        .on_resolve(move |outcome| {
            ui.prompt_open.update(|n| *n = n.saturating_sub(1));
            resolve(outcome);
        })
        .open(cx);
}

/// ONE danger confirm (F9: ten verbatim copies): message → one
/// danger-tinted option → one keep option. The safety property every
/// site used to re-implement by convention — danger confirms DEFAULT
/// to keep — is structural here (`initial("keep")`), impossible to
/// forget on the next destructive verb.
pub(crate) fn confirm_danger(
    cx: Scope,
    ui: UiState,
    message: String,
    danger_label: &str,
    keep_label: &str,
    on_confirm: impl FnOnce() + 'static,
) {
    open_prompt(
        cx,
        ui,
        ChoicePrompt::new(message)
            .option_with(abstracttui::app::ChoiceOption::new("go", danger_label).danger(true))
            .option("keep", keep_label)
            .initial("keep"),
        move |outcome| {
            if let ChoiceOutcome::Answered(a) = outcome {
                if a.selected.iter().any(|s| s == "go") {
                    on_confirm();
                }
            }
        },
    );
}

/// Humanize a raw engine startup notice for the operator footer (D3,
/// cycle-1 UX): the zero-collapse diagnostic ("layout: fixed-size child
/// #0 LayoutId(Key { index: 22, … })") rendered verbatim in warn ink on
/// reachable first-run states and read as a crash log. Translate the
/// layout class into a plain sentence; the raw pointer stays behind the
/// debug env flag. Non-layout notices pass through unchanged.
pub fn humanize_engine_notice(raw: &str) -> String {
    if raw.trim_start().starts_with("layout:") {
        if std::env::var("ABSTRACTGATEWAY_CONSOLE_DEBUG").is_ok() {
            return raw.to_string();
        }
        // Non-debug: SUPPRESSED entirely (empty). The engine's startup
        // notice registry is append-only and never clears (REG-1/NEW-1),
        // so a single transient over-demand — e.g. one frame at a tight
        // size before the operator enlarges the window — would pin a
        // permanent "display degraded" banner for the whole session.
        // Operators cannot act on "a panel over-demanded space" anyway;
        // the debug flag surfaces it for developers.
        return String::new();
    }
    raw.to_string()
}

/// The dirty-form Esc warning — one string, compared verbatim by the
/// disarm effects so a REAL error is never cleared by accident.
pub const ESC_WARNING: &str = "unsaved changes — press Esc again to discard";

/// THE dirty-Esc contract, one implementation (F4: four verbatim copies
/// drifted-in-waiting): the first Esc on a dirty form WARNS through the
/// form's message slot and arms; the second discards; ANY edit after
/// the warning DISARMS it (a warning shown minutes ago must not make a
/// later Esc silently destructive) and clears only the warning text,
/// never a real error.
///
/// `dirty` runs UNTRACKED comparisons against the form's initial
/// values; `track` performs TRACKED reads of every editable signal so
/// the disarm effect re-runs on edits.
pub(crate) fn install_dirty_guard_with(
    mcx: Scope,
    guard: &GuardSlot,
    dirty: impl Fn() -> bool + 'static,
    track: impl Fn() + 'static,
    esc_armed: Signal<bool>,
    form_error: Signal<Option<String>>,
) {
    *guard.borrow_mut() = Some(Box::new(move || {
        if !dirty() || esc_armed.get_untracked() {
            return false;
        }
        esc_armed.set(true);
        form_error.set(Some(ESC_WARNING.into()));
        true
    }));
    mcx.effect(move || {
        track();
        if esc_armed.get_untracked() {
            esc_armed.set(false);
            if form_error.with_untracked(|e| e.as_deref() == Some(ESC_WARNING)) {
                form_error.set(None);
            }
        }
    });
}

/// String-pairs convenience over [`install_dirty_guard_with`] for forms
/// whose whole state is text fields.
pub(crate) fn install_dirty_guard(
    mcx: Scope,
    guard: &GuardSlot,
    fields: Vec<(Signal<String>, String)>,
    esc_armed: Signal<bool>,
    form_error: Signal<Option<String>>,
) {
    let fields2 = fields.clone();
    install_dirty_guard_with(
        mcx,
        guard,
        move || fields.iter().any(|(s, init)| s.get_untracked() != *init),
        move || {
            for (s, _) in &fields2 {
                let _ = s.get();
            }
        },
        esc_armed,
        form_error,
    );
}

/// write_done routing, one implementation: close on success, verbatim
/// gateway error on failure, in-flight released either way.
pub(crate) fn install_write_done(
    mcx: Scope,
    ctx: &Ctx,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    close: CloserFn,
) {
    let ui = ctx.ui;
    mcx.effect(move || {
        if let Some((fid, outcome)) = ui.write_done.get() {
            if fid == form_id {
                ui.write_done.set(None);
                in_flight.set(false);
                match outcome {
                    Ok(_) => close(),
                    Err(e) => form_error.set(Some(e)),
                }
            }
        }
    });
}

/// The message line every write form shows: the error/warning WINS over
/// the busy line (Save clears form_error before sending, so anything
/// here during flight is the dirty-Esc warning — it must stay visible
/// or the second Esc discards with the warning unseen), then busy,
/// then blank.
pub(crate) fn message_slot(
    theme: Signal<&'static abstracttui::theme::Theme>,
    form_error: Signal<Option<String>>,
    in_flight: Signal<bool>,
) -> View {
    // shrink(0.0): "must stay visible or the second Esc discards with
    // the warning unseen" is a SAFETY property — its survival under
    // height pressure was accidental (placement order), now structural.
    dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
        let t = theme.get().tokens;
        if let Some(e) = form_error.get() {
            return util::line(vec![util::span_bold(format!("✗ {e}"), t.error)]);
        }
        if in_flight.get() {
            return util::line(vec![util::span(
                "⟳ applying… (write + verify via GET)",
                t.info,
            )]);
        }
        util::line(vec![util::span(String::new(), t.text)])
    })
}

/// The Esc guard a form installs into its modal: return true to BLOCK
/// this Esc (the form shows its own "Esc again to discard" warning and
/// arms itself); false lets the modal close. A slot because the guard
/// needs the form's modal-scope signals, which only exist once `build`
/// has run.
pub type GuardSlot = Rc<RefCell<Option<Box<dyn Fn() -> bool>>>>;

/// `open_form` with an Esc guard slot: forms with typed state fill it so
/// one keypress cannot silently destroy a filled form.
pub fn open_form_guarded(
    ctx: &Ctx,
    cx: Scope,
    size: Size,
    build: impl FnOnce(Scope, CloserFn, GuardSlot) -> View,
) {
    ctx.close_modal();
    let viewport = abstracttui::app::use_viewport(cx).get_untracked();
    let slot = ctx.modal.clone();
    let epoch = ctx.ui.modal_epoch;
    let closer: CloserFn = Rc::new(move || {
        if let Some(m) = slot.borrow_mut().take() {
            m.close();
        }
        epoch.update(|e| *e += 1);
    });
    let guard: GuardSlot = Rc::new(RefCell::new(None));
    let guard_esc = guard.clone();
    let c_esc = closer.clone();
    let modal = Modal::open(&ctx.overlays, cx, viewport, size, move |mcx| {
        // THE DIALOG DRESS (operator screenshot 2026-07-25, tui wave13
        // modal-bg-diagnosis): the engine's Modal panel ground is
        // translucent (overlay rgba .45) and borderless BY DESIGN — over
        // this app's dark page it read as a torn/blanked background, not
        // a dialog. One Block here dresses ALL modals at the one
        // chokepoint (open_form delegates): an OPAQUE raised ground +
        // rounded border give the eye the boundary the screenshot
        // lacked; the translucent panel ground survives only as the
        // 1-cell margin around this block (the Modal's own padding).
        let t = use_theme(mcx).get().tokens;
        Element::new()
            .style(LayoutStyle::fill())
            .shortcut(KeyChord::plain(Key::Escape), move |_| {
                if let Some(g) = guard_esc.borrow().as_ref() {
                    if g() {
                        return; // the form warned and armed itself
                    }
                }
                c_esc()
            })
            .child(
                Block::new()
                    .border(BorderKind::Rounded)
                    .fill(t.surface_raised)
                    .layout(LayoutStyle::column().grow(1.0).padding(Edges::all(1)))
                    .child(build(mcx, closer.clone(), guard.clone()))
                    .element(&t)
                    .build(),
            )
            .build()
    });
    *ctx.modal.borrow_mut() = Some(modal);
}

/// The root component.
pub fn root(cx: Scope, ctx: Ctx) -> View {
    let theme = use_theme(cx);
    let ui = ctx.ui;

    install_effects(cx, &ctx);

    let quit = ctx.quitter.clone();
    let ctx_q = ctx.clone();
    let ctx_esc = ctx.clone();
    let ctx_next = ctx.clone();
    let ctx_back = ctx.clone();
    let ctx_next2 = ctx.clone();
    let ctx_back2 = ctx.clone();
    let ctx_refresh = ctx.clone();
    let ctx_about = ctx.clone();
    let ctx_about2 = ctx.clone();

    let mut root_el = Element::new()
        .style(LayoutStyle::column())
        .shortcut(KeyChord::plain(Key::Char('q')), move |_| {
            // q quits from browse; in wizard it is refused WITH A REASON
            // (a swallowed key is a dead-action experience — F3).
            if !ui.wizard.get_untracked() {
                // A Models/Engines job runs ON THE GATEWAY and survives
                // us, but the console is its only live progress view —
                // quitting mid-download is a decision, not a keystroke.
                if ctx_q.screens.store.job_active() {
                    ctx_q.store.notice.set(Some(
                        "a models/engines job is running on the gateway — c on Models/Engines \
                         cancels it (Ctrl+C quits anyway; the gateway keeps running it)"
                            .into(),
                    ));
                    return;
                }
                quit.quit();
            } else {
                ctx_q.store.notice.set(Some(
                    "q quits in browse mode — Ctrl+C quits anywhere".into(),
                ));
            }
        })
        .shortcut(KeyChord::plain(Key::Escape), move |_| {
            wizard_back(&ctx_esc);
        })
        .shortcut(KeyChord::plain(Key::Char(']')), move |_| {
            wizard_next(&ctx_next, cx);
        })
        .shortcut(KeyChord::plain(Key::Char('[')), move |_| {
            wizard_back(&ctx_back);
        })
        // Ctrl chords survive focused text inputs (plain chars are
        // consumed by the field under the caret — ] is dead while the
        // URL input has focus, which at boot is always).
        .shortcut(KeyChord::new(Mods::CTRL, Key::Char('n')), move |_| {
            wizard_next(&ctx_next2, cx);
        })
        .shortcut(KeyChord::new(Mods::CTRL, Key::Char('p')), move |_| {
            wizard_back(&ctx_back2);
        })
        .shortcut(KeyChord::plain(Key::Char('r')), move |_| {
            let s = ui.screen.get_untracked();
            if ctx_refresh
                .store
                .conn
                .with_untracked(ConnPhase::is_connected)
            {
                // Screen 0 has no remote domain to reload — the old
                // "refreshing…" notice over refresh_screen's `_ => {}`
                // was the dead-action class (F3) wearing a live label.
                // (Screen 5 left this list with the inline sandbox: its
                // provider picker is live data.)
                if matches!(s, SCREEN_CONNECTION) {
                    ctx_refresh.store.notice.set(Some(
                        "nothing to refresh here — r reloads live data on screens 2-9 and 0".into(),
                    ));
                    return;
                }
                // The immediate ack: fast domains repaint identically
                // (the probe-incident class) — the notice IS the trace.
                ctx_refresh
                    .store
                    .notice
                    .set(Some(format!("⟳ refreshing {}…", SCREENS[s])));
                ctx_refresh.refresh_screen(s);
            } else if matches!(
                ctx_refresh.store.conn.get_untracked(),
                ConnPhase::Verifying(_)
            ) {
                ctx_refresh.store.notice.set(Some(
                    "verifying the gateway connection — retrying automatically, one moment".into(),
                ));
            } else {
                ctx_refresh.store.notice.set(Some(
                    "not connected — probe on the Connection screen first (r refreshes live data)"
                        .into(),
                ));
            }
        })
        .shortcut(KeyChord::new(Mods::CTRL, Key::Char('l')), |_| {
            abstracttui::app::request_full_redraw();
        })
        // About: F1 anywhere (function keys survive focused text fields),
        // `?` wherever no text field holds the caret.
        .shortcut(KeyChord::plain(Key::F(1)), move |_| about::open(&ctx_about, cx))
        .shortcut(KeyChord::plain(Key::Char('?')), move |_| about::open(&ctx_about2, cx));
    // Digit keys at the root. Wizard: a REFUSAL with a reason, so a
    // swallowed digit never reads as a dead app (F3). Browse: PageHost
    // owns digit jumps (its number_jump surface), but its shortcut rides
    // the FOCUSED path — on a page where nothing holds focus (a table
    // still loading) the digit reaches this root handler instead, which
    // then performs the jump itself. `0` is always ours: PageHost's
    // number surface is 1-9, and the tenth screen (Engines) needs a key.
    for i in 0..SCREENS.len() {
        let ctx_i = ctx.clone();
        let key = screen_key(i);
        root_el = root_el.shortcut(KeyChord::plain(Key::Char(key)), move |_| {
            if ctx_i.ui.wizard.get_untracked() {
                ctx_i.store.notice.set(Some(
                    "digit jumps work in browse mode — walk the wizard with Ctrl+N and Finish on the Review step"
                        .into(),
                ));
            } else if ctx_i.ui.screen.get_untracked() != i {
                ctx_i.ui.screen.set(i);
            }
        });
    }

    // §1 bridge: ui.screen (usize, the wizard gate's truth) ⇄ PageHost's
    // string `active`. Both effects are equality-guarded — one hop, no
    // oscillation.
    let active =
        cx.signal(SCREEN_IDS[ui.screen.get_untracked().min(SCREEN_IDS.len() - 1)].to_string());
    cx.effect(move || {
        let id = SCREEN_IDS[ui.screen.get().min(SCREEN_IDS.len() - 1)];
        if active.with_untracked(|a| a != id) {
            active.set(id.to_string());
        }
    });
    cx.effect(move || {
        let pos = active.with(|a| SCREEN_IDS.iter().position(|s| s == a));
        if let Some(i) = pos {
            if ui.screen.get_untracked() != i {
                ui.screen.set(i);
            }
        }
    });

    // §2/§3: ONE PageHost carries the tab bar + page region — the
    // hand-rolled bar, its draw-mirroring mouse hit-test and the browse
    // digit loop are deleted (the drift class PageHost's single-plan
    // design kills). Free navigation is ARMED in browse and fully
    // DISARMED in wizard (empty chord sets + number_jump(false)): the
    // gate logic stays app-side in wizard_next/wizard_back, which keep
    // writing ui.screen. The host rebuilds when the MODE flips (this
    // region reads ui.wizard); page state survives in UiState.
    let host_ctx = ctx.clone();
    let host = dyn_view_scoped(LayoutStyle::default().grow(1.0), move |hcx| {
        let wizard_now = ui.wizard.get();
        let (prev_chords, next_chords) = if wizard_now {
            (Vec::new(), Vec::new())
        } else {
            (
                vec![KeyChord::new(Mods::CTRL, Key::Char('p'))],
                vec![KeyChord::new(Mods::CTRL, Key::Char('n'))],
            )
        };
        let c0 = host_ctx.clone();
        let c1 = host_ctx.clone();
        let c2 = host_ctx.clone();
        let c3 = host_ctx.clone();
        let c4 = host_ctx.clone();
        let c5 = host_ctx.clone();
        let c6 = host_ctx.clone();
        let c7 = host_ctx.clone();
        let c8 = host_ctx.clone();
        let c9 = host_ctx.clone();
        PageHost::new()
            .page(SCREEN_IDS[0], "1 Connection", move |gcx| {
                connection::view(gcx, &c0, &theme.get().tokens)
            })
            .page(SCREEN_IDS[1], "2 Providers", move |gcx| {
                providers::view(gcx, &c1, &theme.get().tokens)
            })
            .page(SCREEN_IDS[2], "3 Routes", move |gcx| {
                routes::view(gcx, &c2, &theme.get().tokens)
            })
            .page(SCREEN_IDS[3], "4 Users & Entities", move |gcx| {
                users::view(gcx, &c3, &theme.get().tokens)
            })
            .page(SCREEN_IDS[4], "5 Runtimes", move |gcx| {
                runtimes::view(gcx, &c4, &theme.get().tokens)
            })
            .page(SCREEN_IDS[5], "6 Workflows", move |gcx| {
                workflows::view(gcx, &c5, &theme.get().tokens)
            })
            .page(SCREEN_IDS[6], "7 Review & Test", move |gcx| {
                review::view(gcx, &c6, &theme.get().tokens)
            })
            .page(SCREEN_IDS[7], "8 Resources", move |gcx| {
                models::view(gcx, &c7, &theme.get().tokens)
            })
            // AbstractCore's screens, inherited — not re-implemented.
            .page(SCREEN_IDS[8], "9 Models", move |gcx| {
                abstractcore_console::screens::catalog(gcx, &c8.screens_for_page())
            })
            .page(SCREEN_IDS[9], "0 Engines", move |gcx| {
                abstractcore_console::screens::engines(gcx, &c9.screens_for_page())
            })
            .active(active)
            .number_jump(!wizard_now)
            .chords(&prev_chords, &next_chords)
            .view(hcx)
    });

    // §5 (the drawer opportunity): the entity inspector installs ONCE at
    // root as a right-edge PASSIVE drawer — glanceable reference beside
    // the live roster (decisions stay Modals; this is a reader). Closed
    // = disposed; the content rebuilds per open reading the store, so
    // loaded detail survives close/reopen for free.
    {
        use abstracttui::app::drawer::{Drawer, DrawerEdge, DrawerFocus, DrawerSize};
        let drawer_ctx = ctx.clone();
        let handle = Drawer::new(DrawerEdge::Right)
            .size(DrawerSize::Percent(0.42))
            .focus(DrawerFocus::Passive)
            .title("Entity inspector")
            // Instant mode: a config console wants snap, and the
            // headless suite renders no wall-time animation frames.
            .motion(std::time::Duration::ZERO)
            .overlays(&ctx.overlays)
            .install(cx, move |dcx| {
                entity_manage::inspector_view(dcx, &drawer_ctx, theme)
            });
        *ctx.entity_drawer.borrow_mut() = Some(handle);
        // Leaving the Users screen closes the inspector — a stale panel
        // over an unrelated page would be a lying surface.
        let drawer_slot = ctx.entity_drawer.clone();
        cx.effect(move || {
            if ui.screen.get() != SCREEN_USERS {
                if let Some(h) = drawer_slot.borrow().as_ref() {
                    if h.is_open() {
                        h.close();
                    }
                }
            }
        });
    }

    root_el
        .child(header(cx, &ctx, theme))
        // One blank line between the title bar and the tab bar
        // (operator ask 2026-07-24: the header must never butt directly
        // against the components below). Pinned like the header — a
        // separator that vanishes under pressure separates nothing.
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            line(vec![span(String::new(), theme.get().tokens.text)])
        }))
        .child(host)
        // Per-step GOAL line (P1-B, cycle-1 UX): the wizard GATED but
        // never GUIDED. WIZARD MODE ONLY — and TRULY zero-height
        // otherwise (REG-1: an empty pinned line still reserved its row
        // and pushed the connection screen over 24 rows at the macOS
        // default 80x24). The connection screen is self-teaching (probe
        // report + intro), so it gets no goal row — that is the tightest
        // screen and the one that broke. dyn_view_scoped with a natural
        // height: the returned child is h(0) when there is no goal, so
        // the row genuinely vanishes.
        .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), {
            let ui = ctx.ui;
            move |_| {
                let t = theme.get().tokens;
                let screen = ui.screen.get();
                let goal = if ui.wizard.get() {
                    wizard_goal(screen)
                } else {
                    ""
                };
                if goal.is_empty() {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                Element::new()
                    .style(LayoutStyle::line(1).shrink(0.0))
                    .child(line(vec![
                        span(" Step goal: ", t.accent),
                        span(goal, t.text_muted),
                    ]))
                    .build()
            }
        }))
        .child(footer(cx, &ctx, theme))
        .build()
}

/// The one-line first-run goal for each wizard step (P1-B): what THIS
/// step is for, and — crucially — when it can be skipped. Kept ≤ ~85
/// chars (NEW-3) so the operative tail survives at 80-100 cols. The
/// connection screen is self-teaching and gets none (REG-1: it is the
/// tightest screen; the goal row is what pushed it over at 80x24).
fn wizard_goal(screen: usize) -> &'static str {
    match screen {
        1 => "make one provider usable — a adds a key; e edits or overrides env rows; t tests.",
        2 => {
            "optional — the engine picks models by default; override only to pin a provider/model."
        }
        SCREEN_USERS => {
            "mint a token per app/person that connects (a); skip if the admin token is enough."
        }
        4 => "nothing to configure — storage inventory; glance and continue.",
        SCREEN_WORKFLOWS => {
            "nothing to configure — the registered workflows; e exports, d/D delete."
        }
        SCREEN_REVIEW => {
            "run one real test (Enter in the prompt) to prove a provider, then Finish."
        }
        SCREEN_MODELS => {
            "nothing to configure — live models, memory & caches; Finish lives on Review."
        }
        SCREEN_CATALOG => {
            "optional — w downloads a model that fits this gateway host; f shows only those."
        }
        SCREEN_ENGINES => {
            "optional — i installs a local engine (Ollama, MLX…) on the gateway host, after a confirm."
        }
        _ => "",
    }
}

fn install_effects(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let ui = ctx.ui;

    // The centralized connection authority (health.rs): transport
    // failures anywhere trigger ONE background probe that settles the
    // story every surface tells.
    crate::health::install(cx, ctx);

    // Screen-entry data loading: when connected and a screen's domains
    // were never asked, ask. Loading is set synchronously in
    // refresh_screen, so this cannot double-send.
    {
        let ctx = ctx.clone();
        cx.effect(move || {
            let screen = ui.screen.get();
            if !store.conn.with(ConnPhase::is_connected) {
                return;
            }
            // A screen needs loading when ANY of its domains is still
            // NotAsked (refresh_screen sets Loading synchronously, so a
            // re-run cannot double-send). Each `na` read is tracked, so
            // the effect re-runs when a domain returns to NotAsked.
            let needs = match screen {
                1 => {
                    matches!(store.profiles.get(), Loadable::NotAsked)
                        || matches!(store.providers.get(), Loadable::NotAsked)
                }
                2 => matches!(store.routes.get(), Loadable::NotAsked),
                3 => {
                    matches!(store.users.get(), Loadable::NotAsked)
                        || matches!(store.entities.get(), Loadable::NotAsked)
                }
                4 => {
                    // ONLY the inventory: runs / data_homes /
                    // runtime_config are owned by their panel effects
                    // (choose-to-load, Data-tab mount, knobs expand —
                    // operator directive 2026-07-26: nothing eager).
                    // Keying entry-needs on any of them while
                    // refresh_screen leaves them NotAsked would re-send
                    // LoadRuntimes on every effect re-run (a storm).
                    matches!(store.runtimes.get(), Loadable::NotAsked)
                }
                5 => matches!(store.workflows.get(), Loadable::NotAsked),
                // The Review screen's inline sandbox needs the provider
                // catalog (a browse digit-jump straight to 7 must not
                // land on an empty picker).
                6 => matches!(store.providers.get(), Loadable::NotAsked),
                // The shared screens: whatever the (re)connection reset
                // to NotAsked. Their own mount effect asks too, but it
                // runs once per mount — possibly before the connection
                // existed — so the connected transition asks again.
                SCREEN_CATALOG => {
                    let s = ctx.screens.store;
                    s.host.with(Remote::is_not_asked)
                        || s.catalog.with(Remote::is_not_asked)
                        || s.installed.with(Remote::is_not_asked)
                }
                SCREEN_ENGINES => {
                    let s = ctx.screens.store;
                    s.engines.with(Remote::is_not_asked) || s.host.with(Remote::is_not_asked)
                }
                // Deliberately NOT keyed here: host-state freshness is
                // owned end-to-end by the poll-lifecycle effect below
                // (a second trigger lane would race it into double
                // chains). `_ => false` covers SCREEN_MODELS.
                _ => false,
            };
            if needs {
                match screen {
                    // Only what was never asked (ensure_*), never a full
                    // refresh: entering must not re-probe the engines.
                    SCREEN_CATALOG => ctx.screens.ensure_catalog(),
                    SCREEN_ENGINES => ctx.screens.ensure_engines(),
                    _ => ctx.refresh_screen(screen),
                }
            }
        });
    }

    // Host-state poll lifecycle: the SLOW /host/state probe polls ~4s
    // apart and ONLY while the Models tab is on screen. ONE authority
    // for entry/exit: entering (or the connection landing while on the
    // tab) starts a fresh chain — with the Loading state + busy label
    // only when nothing is held yet, silently otherwise; leaving bumps
    // the generation so the live chain's next result dies on the
    // UI-thread gate in the worker's PollHostState arm. A gateway
    // reset re-enters through the conn transition this effect tracks
    // (Probing = off, Connected = on again), so a new host never
    // renders under the old host's numbers.
    {
        let ctx = ctx.clone();
        let was_on = Rc::new(std::cell::Cell::new(false));
        cx.effect(move || {
            let on = ui.screen.get() == SCREEN_MODELS && store.conn.with(ConnPhase::is_connected);
            if on && !was_on.get() {
                let first = matches!(store.host_state.get_untracked(), Loadable::NotAsked);
                if first {
                    store.host_state.set(Loadable::Loading);
                }
                let gen = store.host_poll_gen.get_untracked() + 1;
                store.host_poll_gen.set(gen);
                ctx.send(Cmd::PollHostState { gen, first });
            }
            if !on && was_on.get() {
                store.host_poll_gen.update(|g| *g += 1);
            }
            was_on.set(on);
        });
    }

    // The worker's reset for a probe (boot auto-probe included) posts
    // `Probing` with `Store::reset_domains`, which cannot reach the
    // shared screens' store — so the transition resets them here. ONE
    // edge: entering Probing.
    {
        let screens = ctx.screens.store;
        let was_probing = Rc::new(std::cell::Cell::new(false));
        cx.effect(move || {
            let probing = store.conn.with(|c| matches!(c, ConnPhase::Probing));
            if probing && !was_probing.get() {
                reset_screens(&screens);
            }
            was_probing.set(probing);
        });
    }

    // Screen switches retire the footer notice — a stale toast line
    // must not outlive the context it was about.
    {
        let last_screen = std::rc::Rc::new(std::cell::Cell::new(usize::MAX));
        cx.effect(move || {
            let s = ui.screen.get();
            if last_screen.get() != usize::MAX && last_screen.get() != s {
                store.notice.set(None);
            }
            last_screen.set(s);
        });
    }

    // Busy ticker: exists only while ops are in flight (zero idle cost).
    {
        let ticker: Rc<RefCell<Option<IntervalHandle>>> = Rc::new(RefCell::new(None));
        cx.effect(move || {
            let any = store.busy.with(|b| !b.is_empty());
            let mut slot = ticker.borrow_mut();
            match (any, slot.is_some()) {
                (true, false) => {
                    *slot = Some(abstracttui::reactive::interval(
                        cx,
                        Duration::from_millis(500),
                        move || store.tick.update(|t| *t += 1),
                    ));
                }
                (false, true) => {
                    if let Some(h) = slot.take() {
                        h.cancel();
                    }
                }
                _ => {}
            }
        });
    }

    // Notices → toast (and the footer mirrors the latest one).
    {
        let overlays = ctx.overlays.clone();
        cx.effect(move || {
            if let Some(n) = store.notice.get() {
                let viewport = abstracttui::app::use_viewport(cx).get_untracked();
                Toast::show(
                    &overlays,
                    cx,
                    viewport,
                    util::ellipsize(&n, (viewport.w as usize).saturating_sub(6).max(20)),
                    Duration::from_secs(4),
                );
            }
        });
    }

    // Token-once modals: create-user / rotate-token responses queue up
    // and show ONE AT A TIME — never bulldozing an unread token modal
    // (the previous single-slot design could destroy an uncopied token).
    {
        let ctx = ctx.clone();
        cx.effect(move || {
            let _ = ui.modal_epoch.get(); // re-check on every modal close
            let queue_len = ui.token_queue.with(Vec::len);
            if queue_len == 0 {
                return;
            }
            if ctx.modal.borrow().is_some() {
                return; // wait for the open modal to close (epoch wakes us)
            }
            if ui.prompt_open.get() > 0 {
                // A ChoicePrompt is up (danger confirms). Opening the
                // token modal now would stack two same-z modals: ours
                // PAINTS on top while the engine routes keys to the
                // OLDEST — an invisible prompt eating arrows + Enter.
                // The prompt's wrapped resolver re-wakes this effect.
                return;
            }
            let Some((user, token)) = ui.token_queue.with(|q| q.first().cloned()) else {
                return;
            };
            ui.token_queue.update(|q| {
                q.remove(0);
            });
            users::open_token_modal(cx, &ctx, user, token);
        });
    }
}

fn wizard_next(ctx: &Ctx, cx: Scope) {
    if !ctx.ui.wizard.get_untracked() {
        // Browse: ] is simply next tab — a refused step SAYS why (F3).
        if ctx.ui.screen.get_untracked() + 1 >= SCREENS.len() {
            ctx.store
                .notice
                .set(Some("already on the last screen".into()));
            return;
        }
        ctx.ui
            .screen
            .update(|s| *s = (*s + 1).min(SCREENS.len() - 1));
        return;
    }
    let screen = ctx.ui.screen.get_untracked();
    let connected = ctx.store.conn.with_untracked(ConnPhase::is_connected);
    if screen == 0 && !connected && !ctx.ui.offline_ok.get_untracked() {
        // The §3 gate: can't leave connection until ping succeeds or the
        // user explicitly chooses offline/draft mode.
        let ui = ctx.ui;
        open_prompt(
            cx,
            ui,
            ChoicePrompt::new("The gateway is not connected. Configuration screens need a live gateway to read and write.")
                .option("stay", "Stay and fix the connection")
                .option_detail(
                    "offline",
                    "Continue offline (browse only)",
                    "screens will show honest unreachable states; writes will fail",
                ),
            move |outcome| {
                if let ChoiceOutcome::Answered(a) = outcome {
                    if a.selected.iter().any(|s| s == "offline") {
                        ui.offline_ok.set(true);
                        ui.screen.update(|s| *s = (*s + 1).min(SCREENS.len() - 1));
                    }
                }
            },
        );
        return;
    }
    if screen + 1 < SCREENS.len() {
        ctx.ui.screen.set(screen + 1);
    } else {
        ctx.store.notice.set(Some(
            "already on the last step — Finish on the Review step switches to browse mode".into(),
        ));
    }
}

fn wizard_back(ctx: &Ctx) {
    if ctx.ui.screen.get_untracked() == 0 {
        // Esc/[ at the first screen is a no-op — say so instead of
        // swallowing the key (F3).
        ctx.store
            .notice
            .set(Some("already on the first screen".into()));
        return;
    }
    ctx.ui.screen.update(|s| *s = s.saturating_sub(1));
}

fn header(_cx: Scope, ctx: &Ctx, theme: Signal<&'static abstracttui::theme::Theme>) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    // shrink(0.0): the title bar is CHROME — without the pin, a page
    // whose content minimum over-demands height (users screen with
    // loaded rosters) makes the root column flex-shrink this fixed row
    // to ZERO and the tab bar paints at row 0 (operator screenshot
    // 2026-07-24; engine finding 0240's class at the root level).
    dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
        let t = theme.get().tokens;
        let conn = store.conn.get();
        let mode = if ui.wizard.get() { "wizard" } else { "browse" };
        let (dot, dot_ink, label) = match &conn {
            ConnPhase::NotConnected => ("○", t.text_muted, "not connected".to_string()),
            ConnPhase::Probing => ("◌", t.info, "probing…".to_string()),
            ConnPhase::Verifying(id) => (
                "◌",
                t.warn,
                format!("{}@{} — verifying connection…", id.user_id, id.tenant_id),
            ),
            ConnPhase::Connected(id) => (
                "●",
                t.ok,
                format!(
                    "{}@{} ({}){}",
                    id.user_id,
                    id.tenant_id,
                    id.auth_mode,
                    if id.admin { " admin" } else { "" }
                ),
            ),
            ConnPhase::Unauthorized(_) => ("●", t.error, "unauthorized".to_string()),
            ConnPhase::Forbidden(_) => ("●", t.error, "forbidden".to_string()),
            ConnPhase::NotGateway(_, _) => ("●", t.warn, "not a gateway?".to_string()),
            ConnPhase::Unreachable(_) => ("○", t.error, "unreachable".to_string()),
        };
        // Middle-ellipsize long URLs (adversary round-3): the line
        // truncates LAST-SPAN-FIRST, so a long remote-gateway URL used
        // to push the connection dot + identity off the right edge —
        // the least dynamic span was evicting the most important one.
        let url = {
            let u = ui.conn_url.get();
            let max = 42usize;
            if u.chars().count() > max {
                let head: String = u.chars().take(max / 2 - 1).collect();
                let tail: String = {
                    let cs: Vec<char> = u.chars().collect();
                    cs[cs.len() - (max / 2 - 1)..].iter().collect()
                };
                format!("{head}…{tail}")
            } else {
                u
            }
        };
        line(vec![
            span_bold(" AbstractGateway Console ", t.accent),
            span(format!("· {mode} "), t.text_muted),
            span(format!("· {url} "), t.text_muted),
            span(format!("{dot} "), dot_ink),
            span(label, t.text),
        ])
    })
}

fn footer(_cx: Scope, ctx: &Ctx, theme: Signal<&'static abstracttui::theme::Theme>) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let engine_notices = abstracttui::app::use_startup_notices(_cx);
    Element::new()
        // Chrome rows: pinned like the header (finding-0240 class) —
        // the hint line disappearing under content pressure would take
        // the app's teachable surface with it.
        .style(LayoutStyle::column().shrink(0.0))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            // Busy strip: in-flight ops with elapsed seconds. Reading
            // tick keeps it live while ops run; idle renders nothing.
            let t = theme.get().tokens;
            let _ = store.tick.get();
            let ops = store.busy.get();
            // Engine startup notices (adversary round-3): the engine's
            // zero-collapse diagnostic (0240 #3) and input-path
            // degradations publish into use_startup_notices — this app
            // never rendered that signal, so the header crush was being
            // NAMED in every debug run into a lane nobody read. App
            // notices win the slot (actionable acks); the engine line
            // shows whenever the app lane is idle.
            if ops.is_empty() {
                let notice = store.notice.get();
                return match notice {
                    Some(n) => line(vec![span(format!(" {n}"), t.text_muted)]),
                    None => {
                        // Only DIAGNOSTIC engine notices surface here
                        // (degradations, zero-collapse warnings). The
                        // capability summary ("caps: truecolor …") is
                        // ambient INFO the engine always publishes —
                        // showing it permanently in warn-amber made it
                        // read as a problem (operator question
                        // 2026-07-25: "what does caps true color
                        // mean?"). Idle stays blank.
                        // Newest notice that HUMANIZES to something
                        // operator-actionable (caps info + suppressed
                        // layout diagnostics render empty and are
                        // skipped — REG-1: layout notices never clear,
                        // so surfacing them permanently is worse than
                        // silence for a non-developer).
                        let shown = engine_notices.with(|v| {
                            v.iter().rev().find_map(|n| {
                                if n.trim_start().starts_with("caps") {
                                    return None;
                                }
                                let h = humanize_engine_notice(n);
                                (!h.trim().is_empty()).then_some(h)
                            })
                        });
                        match shown {
                            Some(en) => line(vec![span(format!(" engine: {en}"), t.warn)]),
                            None => line(vec![span(String::new(), t.text_muted)]),
                        }
                    }
                };
            }
            let mut parts = Vec::new();
            for (i, op) in ops.iter().enumerate() {
                if i > 0 {
                    parts.push(span(" · ", t.text_faint));
                }
                let secs = op.started.elapsed().as_secs();
                let flag = if secs >= 60 {
                    " (still running — model calls can take a while)"
                } else {
                    ""
                };
                parts.push(span(format!(" ⟳ {}… {}s{}", op.label, secs, flag), t.info));
            }
            line(parts)
        }))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let t = theme.get().tokens;
            let wizard = ui.wizard.get();
            let screen = ui.screen.get();
            let mut pairs: Vec<(&str, &str)> = Vec::new();
            // Universal pairs FIRST (adversary round-3): the hint row
            // truncates right-edge-first, and Ctrl+C used to sit last —
            // on wizard data screens (where q is refused) no quit
            // affordance survived at ≤120 cols. Per-screen verbs are
            // the cheaper loss: refusal notices re-teach them.
            if wizard {
                pairs.push(("Ctrl+N/]", "next step"));
                pairs.push(("Ctrl+P/Esc", "back"));
                pairs.push(("Ctrl+C", "quit"));
            } else {
                pairs.push(("1-9,0", "screens"));
                pairs.push(("Ctrl+N/P", "next/prev"));
                pairs.push(("q/Ctrl+C", "quit"));
            }
            pairs.push(("Tab", "focus"));
            match screen {
                1 => {
                    pairs.push(("a", "add connection"));
                    pairs.push(("e", "edit/override"));
                    pairs.push(("d", "delete"));
                    pairs.push(("m", "models"));
                    pairs.push(("t", "test"));
                    pairs.push(("r", "refresh"));
                }
                2 => {
                    pairs.push(("Enter/e", "edit route"));
                    pairs.push(("x", "clear route"));
                    // `d` is "delete" on Connections/Users and
                    // "download" here. The three never share a screen,
                    // the hint row names the verb for the screen you are
                    // on, and the download confirms with the artifact
                    // spelled out before it spends a byte.
                    pairs.push(("w", "download weights"));
                    pairs.push(("r", "refresh"));
                }
                3 => {
                    pairs.push(("a", "add user"));
                    pairs.push(("e", "edit"));
                    pairs.push(("t", "rotate token"));
                    pairs.push(("d", "delete"));
                    pairs.push(("m", "manage entity"));
                    pairs.push(("i", "inspect"));
                    pairs.push(("v", "kept data of deleted users"));
                    pairs.push(("r", "refresh"));
                }
                4 => {
                    pairs.push(("Enter", "inspect runtime"));
                    pairs.push(("f", "filter"));
                    pairs.push(("/", "search"));
                    pairs.push(("n/p", "page"));
                    pairs.push(("o", "open row"));
                    pairs.push(("i", "run detail"));
                    pairs.push(("w", "policy"));
                    pairs.push(("←/→", "inspector tab (when focused)"));
                    pairs.push(("c", "cancel run"));
                    pairs.push(("s", "steer run"));
                    pairs.push(("r", "refresh"));
                }
                // Named arms from here down (the numbered arms above
                // predate the constants): the Workflows/Review pair had
                // drifted one screen left when Workflows was inserted —
                // Review's sandbox hints rendered on the Workflows
                // screen and Review showed none. Pinned by
                // footer_hints_stay_in_lockstep_with_screens.
                SCREEN_WORKFLOWS => {
                    pairs.push(("t", "show/hide drafts"));
                    pairs.push(("e", "export .flow"));
                    pairs.push(("d", "delete version"));
                    pairs.push(("D", "delete every version"));
                    pairs.push(("r", "refresh"));
                }
                SCREEN_REVIEW => {
                    pairs.push(("Enter", "run the test (REAL generation)"));
                    pairs.push(("r", "refresh providers"));
                }
                SCREEN_MODELS => {
                    pairs.push(("u", "unload"));
                    pairs.push(("k", "lock/unlock"));
                    pairs.push(("w", "load (warm up)"));
                    pairs.push(("e", "context estimate"));
                    pairs.push(("c", "clear session caches"));
                    // At 80x24 the memory itemization does not fit beside the
                    // Loaded table, and the table wins the rows — so the verb
                    // that pages the itemization has to be as visible as the
                    // rest of them.
                    pairs.push(("m", "more memory detail"));
                    pairs.push(("r", "refresh"));
                }
                // The shared screens publish their own verbs.
                SCREEN_CATALOG => {
                    pairs.extend_from_slice(abstractcore_console::screens::catalog::HINTS);
                    pairs.push(("r", "refresh"));
                }
                SCREEN_ENGINES => {
                    pairs.extend_from_slice(abstractcore_console::screens::engines::HINTS);
                }
                _ => {}
            }
            hints(&t, &pairs)
        }))
        .build()
}

/// Scheme-default URL normalization, shared by the Probe button and the
/// boot auto-probe (they must never disagree — a raw host that works on
/// the button and fails at boot is a first-run contradiction).
pub fn normalize_url(raw: &str) -> String {
    let u = raw.trim();
    if u.is_empty() {
        "http://127.0.0.1:8080".to_string()
    } else if u.starts_with("http://") || u.starts_with("https://") {
        u.to_string()
    } else {
        format!("http://{u}")
    }
}
