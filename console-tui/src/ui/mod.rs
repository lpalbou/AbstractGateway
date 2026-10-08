//! UI root: one shell, two modes (wizard steps / browse tabs) over the
//! same screens — eight of this crate's, then AbstractCore's shared
//! Models (9) and Engines (0) screens from the `abstractcore-console`
//! crate. Screens are plain component functions; durable UI
//! state (form fields, selections) lives in [`UiState`] created at the
//! root so screen remounts on tab switches lose nothing.

/// The About modal (F1 / ?).
pub mod about;
/// An account's client preferences (Accounts `p`, round 14).
pub mod account_preferences;
/// The Apps page's settings overlays behind the gears (R8.1).
pub mod app_settings;
/// The Apps screen (browser apps, the desktop Assistant, Node.js).
pub mod apps;
/// The Models page (the web console's catalog, round 7).
pub mod catalog;
pub mod connection;
pub mod docs;
pub mod entity_chat;
pub mod entity_create;
pub mod entity_manage;
/// Gateway host panel (F3) + the paused / restart banner.
pub mod host;
/// Round-7 shared widgets: full-width overlay, wrapping table, inline
/// confirm, key-hint bar (DESIGN.md R7.2 conventions).
pub mod kit;
pub mod models;
/// The caller's own mailbox and notifications (Users screen, `@`).
pub mod my_email;
pub mod network;
/// The OpenAI API page (round 7).
pub mod openai_api;
pub mod providers;
/// The header's memory/compute widget (round 14, R10.3 parity).
pub mod resources_widget;
pub mod review;
pub mod routes;
pub mod runtimes;
pub mod sandbox;
/// Round 15: the app shell (nav rail / strip, clickable header, keys panel).
pub mod shell;
/// Skills & MCP (WORK): the skills shelf and the MCP servers registry.
pub mod skills_mcp;
pub mod switch;
pub mod users;
pub mod util;
/// Round 15: the shared mouse-first widget layer (DESIGN-TUI.md §4).
pub mod w;
/// Setup: the first-run guide's welcome step + the first-run lifecycle.
pub mod welcome;
pub mod widths;
pub mod workflows;
/// The workspace chooser (round 14): "Eligible workspaces" and one
/// account's workspaces, opened from Accounts (`E`, `w`).
pub mod workspace_chooser;

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::mpsc::Sender;
use std::time::Duration;

use abstracttui::app::{ChoiceOutcome, ChoicePrompt, Modal, Overlays};
use abstracttui::prelude::*;
use abstracttui::reactive::IntervalHandle;

use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::Cmd;
use abstractcore_console::screens::Remote;
use util::{line, span, span_bold};

pub const SCREENS: [&str; 16] = [
    "Connection",
    // Local engines + remote connections + the available providers
    // (round 7: the Engines screen merged in, the web's Providers page).
    "Providers",
    // The Routes screen, renamed (DESIGN-v2 §1): which model serves
    // which modality.
    "Multimodal",
    // Users and entities in one table (DESIGN-v2 §2).
    "Accounts",
    "Runtimes",
    "Workflows",
    // The web console's Sandbox page (round 7 rename of "Review & Test":
    // the sandbox workspace, the change journal, the guide's Finish).
    "Sandbox",
    // APPEND ONLY: tests and muscle memory key on the digit order.
    // User-visible label only — SCREEN_IDS keeps the stable "models" id.
    "Resources",
    // AbstractCore's shared screens (crate `abstractcore-console`),
    // served here over the gateway's HTTP mirrors (transport_http.rs):
    // the model catalog / downloads / deletes, and the local engines.
    abstractcore_console::screens::CATALOG_TITLE,
    // Kept as an id only (round 7): the local engines live on Providers;
    // this screen is in no navigation list.
    abstractcore_console::screens::ENGINES_TITLE,
    // The web console's Apps tab (ui/apps.rs).
    "Apps",
    // The first-run guide's welcome step.
    "Setup",
    // Who can reach the gateway (ui/network.rs).
    "Network",
    // The OpenAI-compatible API at /v1 (ui/openai_api.rs, round 7).
    "OpenAI API",
    // The About card (ui/about.rs, round 7: a page at the bottom too).
    "About",
    // Skills agents can load and the MCP tool servers (ui/skills_mcp.rs).
    "Skills & MCP",
    // Round 14: no Workspaces screen — workspaces live on Accounts
    // (ui/workspace_chooser.rs); the old `W` key opens Accounts.
];

/// Screens with semantic weight get NAMES (round-4 P3-2): the bare
/// literals were confined to this module but 3 of them carry meaning
/// a reader had to reconstruct.
pub const SCREEN_CONNECTION: usize = 0;
pub const SCREEN_PROVIDERS: usize = 1;
pub const SCREEN_ROUTES: usize = 2;
pub const SCREEN_USERS: usize = 3;
pub const SCREEN_WORKFLOWS: usize = 5;
/// The Sandbox page (stable id "review": it also carries the guide's
/// Finish and the change journal).
pub const SCREEN_REVIEW: usize = 6;
pub const SCREEN_MODELS: usize = 7;
/// AbstractCore's shared "Models" screen (page id `catalog`).
pub const SCREEN_CATALOG: usize = 8;
/// AbstractCore's shared "Engines" screen (page id `engines`): in no
/// navigation list since round 7 (the local engines are on Providers).
pub const SCREEN_ENGINES: usize = 9;
/// The Apps screen (browser apps, Assistant, Node.js). The first-run
/// wizard references it BY THIS NAME; the number may move.
pub const SCREEN_APPS: usize = 10;
/// The setup guide's welcome step (page id `setup`).
pub const SCREEN_WELCOME: usize = 11;
/// The Network screen (who can reach the gateway).
pub const SCREEN_NETWORK: usize = 12;
/// The OpenAI API page (round 7).
pub const SCREEN_OPENAI: usize = 13;
/// The About page (round 7).
pub const SCREEN_ABOUT: usize = 14;
/// The Skills & MCP page (round 7, R7-W1).
pub const SCREEN_SKILLS: usize = 15;
/// The key the round-8 Workspaces screen used: since round 14 the
/// workspaces live on Accounts ("Eligible workspaces", a row's `w`), and
/// this key opens Accounts (muscle memory keeps working).
pub const WORKSPACES_KEY: char = 'W';
/// The screen list in the order it is SHOWN — the web console's sidebar
/// (console.py `shell_nav`): Connection (the terminal's sign-in) above the
/// groups, then ACCOUNTS (Accounts), WORK (Workflows, Skills & MCP,
/// Runtimes, Apps), MODELS (Providers, OpenAI API, Models, Multimodal),
/// SYSTEM (Resources, Sandbox, Network), then Setup and About at the
/// bottom. The `SCREEN_*` indexes stay stable ids (the wizard, tests and
/// the refresh table key on them); this is only the display/jump order.
pub const NAV_ORDER: [usize; 15] = [
    SCREEN_CONNECTION,
    SCREEN_USERS,
    SCREEN_WORKFLOWS,
    SCREEN_SKILLS,
    SCREEN_RUNTIMES,
    SCREEN_APPS,
    SCREEN_PROVIDERS,
    SCREEN_OPENAI,
    SCREEN_CATALOG,
    SCREEN_ROUTES,
    SCREEN_MODELS,
    SCREEN_REVIEW,
    SCREEN_NETWORK,
    SCREEN_WELCOME,
    SCREEN_ABOUT,
];

/// The Runtimes screen (execution planes).
pub const SCREEN_RUNTIMES: usize = 4;

/// The sidebar groups (the web console's `shell_nav` captions), each with
/// its screens in order. Connection sits above them; Setup and About
/// below them (no group).
pub const NAV_GROUPS: [(&str, &[usize]); 4] = [
    ("ACCOUNTS", &[SCREEN_USERS]),
    (
        "WORK",
        &[
            SCREEN_WORKFLOWS,
            SCREEN_SKILLS,
            SCREEN_RUNTIMES,
            SCREEN_APPS,
        ],
    ),
    (
        "MODELS",
        &[
            SCREEN_PROVIDERS,
            SCREEN_OPENAI,
            SCREEN_CATALOG,
            SCREEN_ROUTES,
        ],
    ),
    ("SYSTEM", &[SCREEN_MODELS, SCREEN_REVIEW, SCREEN_NETWORK]),
];

/// The group caption of screen `i` (None: Connection, Setup, About).
pub fn nav_group(i: usize) -> Option<&'static str> {
    NAV_GROUPS
        .iter()
        .find(|(_, members)| members.contains(&i))
        .map(|(name, _)| *name)
}

/// Position of screen `i` in [`NAV_ORDER`].
pub fn nav_pos(i: usize) -> usize {
    NAV_ORDER.iter().position(|s| *s == i).unwrap_or(0)
}

/// The keys listed in the footer for the screen jumps.
pub const SCREEN_KEYS_HINT: &str = "1-9,0,H,T,N,S,I";

/// The first-run wizard, in the web guide's order (`console.py`
/// `FIRST_RUN_STEPS` = welcome → engines → model → apps → done), mapped
/// onto this console's screens. Connection comes first because the
/// terminal must sign in; the engines step is Providers (its local
/// engines, round 7 — the web engines step sends cloud users to the same
/// page's remote providers); the model step is Routes (the recommended
/// plan, Apply, Download all) then the Models catalog ("or pick any model
/// that fits"); the Sandbox page carries Finish.
/// The web guide gates NO step (every step is optional, Next is always
/// enabled); the only gate here is the sign-in one on Connection.
pub const WIZARD_STEPS: [usize; 7] = [
    SCREEN_CONNECTION,
    SCREEN_WELCOME,
    SCREEN_PROVIDERS,
    SCREEN_ROUTES,
    SCREEN_CATALOG,
    SCREEN_APPS,
    SCREEN_REVIEW,
];

/// The key that jumps to screen `i` in browse mode: FIXED per screen
/// (never positional — a screen arriving later must not renumber the
/// others): 1-9 then 0 down the list, then shifted LETTERS once the
/// digits are spent, chosen so no screen's own uppercase verb (C D F L Q
/// R U) collides: `H` Resources (host), `T` Sandbox (try), `N` Network,
/// `S` Setup, `I` About (info).
pub fn screen_key(i: usize) -> Option<char> {
    match i {
        SCREEN_CONNECTION => Some('1'),
        SCREEN_USERS => Some('2'),
        SCREEN_WORKFLOWS => Some('3'),
        SCREEN_SKILLS => Some('4'),
        SCREEN_RUNTIMES => Some('5'),
        SCREEN_APPS => Some('6'),
        SCREEN_PROVIDERS => Some('7'),
        SCREEN_OPENAI => Some('8'),
        SCREEN_CATALOG => Some('9'),
        SCREEN_ROUTES => Some('0'),
        SCREEN_MODELS => Some('H'),
        SCREEN_REVIEW => Some('T'),
        SCREEN_NETWORK => Some('N'),
        SCREEN_WELCOME => Some('S'),
        SCREEN_ABOUT => Some('I'),
        _ => None,
    }
}

/// The tab title of screen `i`: its jump key and its name.
pub fn screen_title(i: usize) -> String {
    match screen_key(i) {
        Some(k) => format!("{k} {}", SCREENS[i]),
        None => SCREENS[i].to_string(),
    }
}

/// Stable PageHost page ids, parallel to `SCREENS`. `ui.screen: usize`
/// stays the source of truth (the wizard gate reads indexes); a two-way
/// equality-guarded bridge keeps PageHost's string `active` in lockstep.
pub const SCREEN_IDS: [&str; 16] = [
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
    "apps",
    "setup",
    "network",
    "openai",
    "about",
    "skills",
];

/// Durable per-screen UI state (Copy: all signals).
#[derive(Clone, Copy)]
pub struct UiState {
    /// true = wizard chrome (gated steps); false = browse tabs.
    pub wizard: Signal<bool>,
    pub screen: Signal<usize>,
    /// The user explicitly chose to continue without a connection.
    pub offline_ok: Signal<bool>,
    /// A connection was established this session: from then on the
    /// Connection screen's URL field never takes the caret by itself (a
    /// lost connection must leave `r` a re-probe, not a typed letter).
    pub was_connected: Signal<bool>,
    /// `--wizard` / `--browse` was given: the first-run read never
    /// changes the mode.
    pub mode_forced: Signal<bool>,
    /// The first-run boot decision was taken (once per session).
    pub first_run_decided: Signal<bool>,
    /// (form id, outcome) of a Finish / Skip setup in flight.
    pub first_run_pending: Signal<Option<(u64, String)>>,
    /// Why the last Finish / Skip setup did not close the guide.
    pub first_run_error: Signal<Option<String>>,

    pub conn_url: Signal<String>,
    /// Where `conn_url` came from (`pointer::UrlSource`): only a URL from
    /// the gateway pointer or the default follows the pointer when a
    /// connection fails.
    pub url_source: Signal<crate::pointer::UrlSource>,
    /// The home the pointer is read under (None = never follow; the boot
    /// sets it from the real HOME, tests from a scratch dir).
    pub pointer_home: Signal<Option<std::path::PathBuf>>,
    /// The last pointer notice shown (a bad file is said once).
    pub pointer_notice: Signal<Option<String>>,
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
    /// The Accounts table's selection (users + entities, DESIGN-v2 §2).
    pub account_sel: Signal<usize>,
    /// The Logs view's filter chip (index into ACTIVITY_FILTERS).
    pub activity_filter: Signal<usize>,
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
    /// Round 15: the page-level text field holding the caret (the shell's
    /// ←/→ go to it instead of switching screens) — `w::caret_tracked`.
    pub caret: Signal<Option<u64>>,
    /// Round 15 (A2): the focused control's name + tooltip, which the
    /// status bar leads with.
    pub focus_line: Signal<Option<String>>,
    /// Round 15: the page region's size (viewport minus the nav rail,
    /// header, strip and status rows) — `page_viewport(cx)`.
    pub page_vp: Signal<Size>,
    /// Round 15: Accounts selection by row key + its table window.
    pub acc_key: Signal<Option<String>>,
    pub acc_top: Signal<usize>,
    /// Round 15: Apps selection by app id + its table window.
    pub apps_key: Signal<Option<String>>,
    pub apps_top: Signal<usize>,
}

impl UiState {
    pub fn create(cx: Scope, url: String, token: String) -> UiState {
        UiState {
            wizard: cx.signal(true),
            screen: cx.signal(0),
            offline_ok: cx.signal(false),
            was_connected: cx.signal(false),
            mode_forced: cx.signal(false),
            first_run_decided: cx.signal(false),
            first_run_pending: cx.signal(None),
            first_run_error: cx.signal(None),
            conn_url: cx.signal(url),
            url_source: cx.signal(crate::pointer::UrlSource::Flag),
            pointer_home: cx.signal(None),
            pointer_notice: cx.signal(None),
            conn_token: cx.signal(token),
            token_source: cx.signal(None),
            profile_sel: cx.signal(0),
            provider_sel: cx.signal(0),
            route_sel: cx.signal(0),
            user_sel: cx.signal(0),
            account_sel: cx.signal(0),
            activity_filter: cx.signal(0),
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
            caret: cx.signal(None),
            focus_line: cx.signal(None),
            page_vp: cx.signal(Size::new(80, 20)),
            acc_key: cx.signal(None),
            acc_top: cx.signal(0),
            apps_key: cx.signal(None),
            apps_top: cx.signal(0),
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
    /// `Some(reason)`: no screen in front of the person (SSH, or Linux
    /// without a display) — never run a URL opener; show the link.
    pub no_display: Option<String>,
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
    // Per-gateway reads of the optional verbs (abstractcore-console 0.3).
    s.text_default.set(Remote::NotAsked);
    s.feed.set(Remote::NotAsked);
    s.plans.set(Default::default());
    s.hub.set(None);
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
                _ => (None, connection::NO_TOKEN_SENT.to_string()),
            }
        };
        (url, token, source)
    }

    /// The health authority's view: url + token only.
    pub fn effective_credentials(&self) -> (String, Option<String>) {
        let (url, token, _) = self.effective_credentials_with_source();
        (url, token)
    }

    /// Connect because the PERSON asked (Enter in the Connection form,
    /// Probe): the address shown is now theirs, so a failed connection never
    /// swaps it for the pointer's or the default address (their token would
    /// follow it there).
    pub fn connect_typed(&self) {
        self.ui.url_source.set(crate::pointer::UrlSource::Typed);
        self.connect_now();
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
            // Connection (its Network summary line) and the Network
            // screen: the network read (the probe itself stays the
            // Probe button's job).
            0 | SCREEN_NETWORK => {
                if s.conn.with_untracked(crate::store::ConnPhase::is_connected) {
                    s.network.set(Loadable::Loading);
                    self.send(Cmd::LoadNetwork);
                }
            }
            // Providers: profiles + discovery + the local engines.
            SCREEN_PROVIDERS => providers::refresh(self),
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
                // The users registry is admin-only (`/admin/users`): a
                // non-admin is told so on screen, never sent for a 403.
                if !s.conn.with_untracked(ConnPhase::is_known_non_admin) {
                    s.users.set(Loadable::Loading);
                    self.send(Cmd::LoadUsers);
                }
                // The one table (DESIGN-v2 §2): users + entities for an
                // admin; you + the entities you created for a non-admin
                // (`/me/accounts`, the gateway's RBAC).
                s.accounts.set(Loadable::Loading);
                self.send(Cmd::load_accounts_for(s));
                s.entities.set(Loadable::Loading);
                // The inspector's detail must honor `r`'s "refreshing
                // live data" promise too (F17): NotAsked here → the
                // selection effect reloads it when fresh entities land.
                s.entity_detail.set(Loadable::NotAsked);
                self.send(Cmd::LoadEntities);
                // The command sandbox state line under the head (R12.1).
                if s.conn.with_untracked(crate::store::ConnPhase::is_connected) {
                    s.workspace_policy.set(Loadable::Loading);
                    self.send(Cmd::LoadWorkspacePolicy);
                }
            }
            // The whole Runtimes screen is admin-only (`/admin/runtimes`;
            // the web hides the tab): nothing to load for a non-admin.
            4 if s.conn.with_untracked(ConnPhase::is_known_non_admin) => {}
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
                self.send(Cmd::load_runtimes_for(s));
            }
            // The Workflows page reads its list and its defaults itself.
            5 => workflows::refresh(self),
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
            SCREEN_CATALOG => catalog::refresh(self),
            SCREEN_ENGINES => self.screens.refresh_engines(),
            SCREEN_APPS => {
                // Rows stay on screen while re-checking (web "Check
                // again"); a first read shows the loading state.
                if s.apps.overview.with_untracked(|o| o.ready().is_none()) {
                    s.apps.overview.set(Loadable::Loading);
                }
                self.send(Cmd::LoadApps { latest: true });
            }
            SCREEN_WELCOME => welcome::refresh(self),
            SCREEN_OPENAI => openai_api::refresh(self),
            SCREEN_ABOUT => about::refresh(self),
            SCREEN_SKILLS => skills_mcp::refresh(self),
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
    w::tip::hide_all();
    // THE SCREEN DOES NOT SHOW THROUGH A DIALOG (review 2, 80x24): the
    // engine's choice dialog is frameless on a translucent scrim, so the
    // screen around and under it read as part of the dialog. An opaque
    // backdrop layer just under the modal band covers the screen while
    // the prompt is open (removed when it resolves, whatever the answer).
    let backdrop = cx.use_context::<abstracttui::app::Overlays>().map(|ov| {
        let vp = abstracttui::app::use_viewport(cx).get_untracked();
        let ground = abstracttui::app::current_theme().tokens.bg;
        ov.layer_draw(
            abstracttui::app::MODAL_Z - 1,
            abstracttui::base::Rect::from_size(vp),
            move |canvas, rect| {
                canvas.fill_styled(rect, ' ', &abstracttui::render::Style::new().bg(ground));
            },
        )
    });
    prompt
        .on_resolve(move |outcome| {
            if let Some(b) = &backdrop {
                b.remove();
            }
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
    // R15 F1: the one confirm widget — `[danger] [keep]` buttons.
    w::Confirm::danger(message, danger_label, keep_label).open(cx, ui, on_confirm);
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
    // R15 F2 ruling: closing a form with unsaved edits (Esc, ✕, Close)
    // asks "Discard changes?" — [Discard] closes, [Keep editing] (and Esc)
    // return to the form. The form being built is the one that asks.
    let _ = (track, esc_armed, form_error);
    let Some((close, ui)) = FORM_BUILDING.with(|f| f.borrow().clone()) else {
        return;
    };
    *guard.borrow_mut() = Some(Box::new(move || {
        if !dirty() {
            return false;
        }
        let close = close.clone();
        w::Confirm::danger(DISCARD_QUESTION, "Discard", "Keep editing")
            .open(mcx, ui, move || close());
        true
    }));
}

/// The question a form with unsaved edits asks before it closes.
pub const DISCARD_QUESTION: &str = "Discard changes?";

thread_local! {
    /// The form `open_form_guarded` is building (its closer and the UI
    /// state): the dirty guard installed during the build closes it.
    static FORM_BUILDING: RefCell<Option<(CloserFn, UiState)>> = const { RefCell::new(None) };
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
    w::tip::hide_all();
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
    let building = (closer.clone(), ctx.ui);
    let modal = Modal::open(&ctx.overlays, cx, viewport, size, move |mcx| {
        FORM_BUILDING.with(|f| *f.borrow_mut() = Some(building));
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
        let body = build(mcx, closer.clone(), guard.clone());
        FORM_BUILDING.with(|f| *f.borrow_mut() = None);
        Element::new()
            .style(LayoutStyle::fill())
            .shortcut(KeyChord::plain(Key::Escape), move |_| {
                if let Some(g) = guard_esc.borrow().as_ref() {
                    if g() {
                        return; // the form asked "Discard changes?"
                    }
                }
                c_esc()
            })
            .child(
                Block::new()
                    .border(BorderKind::Rounded)
                    .fill(t.surface_raised)
                    .layout(LayoutStyle::column().grow(1.0).padding(Edges::all(1)))
                    .child(body)
                    .element(&t)
                    .build(),
            )
            .build()
    });
    *ctx.modal.borrow_mut() = Some(modal);
}

/// The root component.
/// Say a pointer notice once (a bad file is not repeated on every re-read).
pub fn pointer_notice(ctx: &Ctx, warning: Option<String>) {
    if let Some(w) = warning {
        if ctx
            .ui
            .pointer_notice
            .with_untracked(|p| p.as_deref() != Some(w.as_str()))
        {
            ctx.ui.pointer_notice.set(Some(w.clone()));
            ctx.store.notice.set(Some(w));
        }
    }
}

/// A connection that never reached the gateway, on a URL that came from the
/// gateway pointer or the default: re-read the pointer and, when it names
/// another address (a gateway restarted on a new port), connect there.
fn install_pointer_follow(cx: Scope, ctx: &Ctx) {
    let ctx = ctx.clone();
    let was_unreachable = Rc::new(std::cell::Cell::new(false));
    cx.effect(move || {
        let unreachable = ctx
            .store
            .conn
            .with(|c| matches!(c, ConnPhase::Unreachable(_)));
        let entered = unreachable && !was_unreachable.get();
        was_unreachable.set(unreachable);
        if !entered || !ctx.ui.url_source.get_untracked().follows_pointer() {
            return;
        }
        let Some(home) = ctx.ui.pointer_home.get_untracked() else {
            return;
        };
        let current = ctx.ui.conn_url.get_untracked();
        if let Some(next) =
            crate::pointer::follow_pointer(&current, Some(&home), crate::pointer::current_uid())
        {
            pointer_notice(&ctx, next.warning.clone());
            ctx.ui.url_source.set(next.source);
            ctx.ui.conn_url.set(next.url.clone());
            if next.warning.is_none() {
                ctx.store.notice.set(Some(format!(
                    "{current} did not answer; the gateway pointer names {} — connecting there",
                    next.url
                )));
            }
            ctx.connect_now();
        }
    });
}

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
    let ctx_left = ctx.clone();
    let ctx_refresh = ctx.clone();
    let ctx_about = ctx.clone();
    let ctx_about2 = ctx.clone();
    let ctx_guide = ctx.clone();
    let ctx_host = ctx.clone();
    let ctx_docs = ctx.clone();

    let mut root_el = Element::new()
        .style(LayoutStyle::column())
        .shortcut(KeyChord::plain(Key::Char('q')), move |_| {
            // q quits from browse; in wizard it is refused WITH A REASON
            // (a swallowed key is a dead-action experience — F3).
            if !ui.wizard.get_untracked() {
                // A download or an engine install runs ON THE GATEWAY and
                // survives us, but the console is its only live progress
                // view — quitting mid-download is a decision, not a keystroke.
                if ctx_q.screens.store.job_running()
                    || providers::engines::engine_job_running(&ctx_q.store)
                    || catalog::download_running()
                {
                    ctx_q.store.notice.set(Some(
                        "a models/engines job is running on the gateway — c on Models or Providers \
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
        // ←/→ (R15 §2.2): previous/next screen on EVERY screen, at the
        // CAPTURE phase of the shell root — before any page widget (the
        // engine's Scroll swallows Left/Right unconditionally, which is
        // what killed them on Apps and Network). Exceptions: a page-level
        // text field holding the caret (`ui.caret`); overlays (modals,
        // popups, drawers) are their own trees and never reach here.
        .on(abstracttui::ui::Phase::Capture, move |ectx, ev| {
            if let abstracttui::ui::UiEvent::Key(k) = ev {
                if k.mods.0 != 0 || !matches!(k.key, Key::Left | Key::Right) {
                    return;
                }
                if ctx_left.ui.caret.get_untracked().is_some() {
                    return;
                }
                ectx.stop_propagation();
                arrow_tab(&ctx_left, if k.key == Key::Left { -1 } else { 1 });
            }
        })
        .shortcut(KeyChord::new(Mods::CTRL, Key::Char('t')), |_| {
            w::theme::flip()
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
                // On Connection the live datum IS the connection: `r`
                // re-probes it (the Re-probe button's path).
                if matches!(s, SCREEN_CONNECTION) {
                    let (url, _) = ctx_refresh.effective_credentials();
                    ctx_refresh
                        .store
                        .notice
                        .set(Some(format!("⟳ re-probing {url}…")));
                    ctx_refresh.connect_now();
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
            } else if matches!(ctx_refresh.store.conn.get_untracked(), ConnPhase::Probing) {
                ctx_refresh
                    .store
                    .notice
                    .set(Some("already probing the gateway — one moment".into()));
            } else {
                // Not connected (never probed, unreachable, refused): `r`
                // means "try again" — re-probe with the same URL and token
                // the Probe button uses. A gateway that came back (after a
                // quit, a restart, a reboot) is one keypress away instead
                // of a trip to the Connection screen.
                let (url, _) = ctx_refresh.effective_credentials();
                ctx_refresh
                    .store
                    .notice
                    .set(Some(format!("⟳ not connected — probing {url} again…")));
                ctx_refresh.connect_now();
            }
        })
        .shortcut(KeyChord::new(Mods::CTRL, Key::Char('l')), |_| {
            abstracttui::app::request_full_redraw();
        })
        // About: F1 anywhere (function keys survive focused text fields),
        // `?` wherever no text field holds the caret.
        // The setup guide: Ctrl+G reopens it from browse, leaves or skips
        // it from the wizard (a Ctrl chord: works inside text fields).
        .shortcut(KeyChord::new(Mods::CTRL, Key::Char('g')), move |_| {
            welcome::guide_key(&ctx_guide, cx)
        })
        .shortcut(KeyChord::plain(Key::F(1)), move |_| {
            about::open(&ctx_about, cx)
        })
        // `?` (R15 D5): the keys panel; About stays on F1 and the I page.
        .shortcut(KeyChord::plain(Key::Char('?')), move |_| {
            shell::open_keys(&ctx_about2, cx)
        })
        // Gateway host panel (pause/resume, restart, quit, update): F3
        // anywhere — a function key survives focused text fields.
        .shortcut(KeyChord::plain(host::OPEN_KEY), move |_| {
            host::open(&ctx_host, cx)
        })
        // Docs assistant (the web top bar's ✦ drawer): F2 anywhere — a
        // function key, so it works with the caret in a text field.
        .shortcut(KeyChord::plain(Key::F(2)), move |_| {
            docs::open(&ctx_docs, cx)
        });
    // Screen keys (1-9, 0, N, R, S) at the root — the ONE jump surface. Wizard:
    // a REFUSAL with a reason, so a swallowed digit never reads as a dead
    // app (F3). Browse: the jump. PageHost's own number_jump is off: it
    // re-anchors focus on the host root even when the digit names the
    // screen already shown, and that left the screen's keys dead (the
    // page is not on the root→focus path) — review 2 pty proof, `8` then
    // `w` on Resources. A same-screen key here changes nothing.
    for i in 0..SCREENS.len() {
        let Some(key) = screen_key(i) else { continue };
        let ctx_i = ctx.clone();
        root_el = root_el.shortcut(KeyChord::plain(Key::Char(key)), move |_| {
            if ctx_i.ui.wizard.get_untracked() {
                ctx_i.store.notice.set(Some(format!(
                    "screen jumps ({SCREEN_KEYS_HINT}) work in browse mode — in the guide Ctrl+N walks, Ctrl+G jumps to a step or leaves"
                )));
            } else if ctx_i.ui.screen.get_untracked() != i {
                ctx_i.ui.screen.set(i);
            }
        });
    }
    // R14.3: the old Workspaces key opens Accounts (where workspaces live).
    {
        let ctx_w = ctx.clone();
        root_el = root_el.shortcut(KeyChord::plain(Key::Char(WORKSPACES_KEY)), move |_| {
            if ctx_w.ui.wizard.get_untracked() {
                ctx_w.store.notice.set(Some(format!(
                    "screen jumps ({SCREEN_KEYS_HINT}) work in browse mode — in the guide Ctrl+N walks, Ctrl+G jumps to a step or leaves"
                )));
            } else if ctx_w.ui.screen.get_untracked() != SCREEN_USERS {
                ctx_w.ui.screen.set(SCREEN_USERS);
            }
        });
    }

    // R15 shell (DESIGN-TUI §2.1): our own navigation (rail on wide
    // terminals, strip otherwise) writing `ui.screen`, and ONE mounted
    // page in a generation scope (PageHost's semantics: the outgoing
    // page's scope dies on switch; durable state lives in UiState).
    w::tip::install(ui.focus_line, ctx.overlays.clone());
    w::tip::install_notice(ctx.store.notice);
    install_page_viewport(ui.page_vp);
    {
        let vp = abstracttui::app::use_viewport(cx);
        // Synchronously first: pages built in this same mount read it.
        ui.page_vp.set(shell::page_size(vp.get_untracked(), 0));
        let store_b = ctx.store;
        cx.effect(move || {
            let v = vp.get();
            let paused = store_b.conn.with(ConnPhase::is_connected)
                && store_b.op.runner.with(|r| {
                    r.ready()
                        .and_then(crate::store::operator::paused_banner_text)
                        .is_some()
                });
            let banner = i32::from(paused) + i32::from(store_b.op.lifecycle.with(Option::is_some));
            let want = shell::page_size(v, banner);
            if ui.page_vp.get_untracked() != want {
                ui.page_vp.set(want);
            }
        });
        // Engines has no page since round 7: anything naming it lands on Providers.
        cx.effect(move || {
            if ui.screen.get() == SCREEN_ENGINES {
                ui.screen.set(SCREEN_PROVIDERS);
            }
        });
        // A screen switch releases any caret claim (the field died with its page).
        cx.effect(move || {
            let _ = ui.screen.get();
            ui.caret.set(None);
            ui.focus_line.set(None);
        });
    }
    // R15 §2.6: success sentences as toasts.
    {
        let ctx_t = ctx.clone();
        cx.effect(move || {
            if let Some(msg) = ctx_t.store.toast.get() {
                ctx_t.store.toast.set(None);
                w::toast(&ctx_t, cx, msg);
            }
        });
    }
    let host_ctx = ctx.clone();
    let vp_host = abstracttui::app::use_viewport(cx);
    let host = dyn_view_scoped(LayoutStyle::row().grow(1.0), move |hcx| {
        let v = vp_host.get();
        let t = theme.get().tokens;
        let wide = shell::wide(v);
        let c = host_ctx.clone();
        // PageHost's page-region style (row direction: the page stretches
        // to the region's full height).
        let page = dyn_view_scoped(
            LayoutStyle::default()
                .width(Dimension::Percent(1.0))
                .grow(1.0),
            move |gcx| {
                let i = c.ui.screen.get();
                let t = theme.get().tokens;
                screen_view(gcx, &c, i.min(SCREENS.len() - 1), &t)
            },
        );
        let c2 = host_ctx.clone();
        if wide {
            let rail = dyn_view_scoped(
                LayoutStyle::column()
                    .width(Dimension::Cells(shell::RAIL_W))
                    .shrink(0.0),
                move |rcx| {
                    let t = theme.get().tokens;
                    let _ = c2.store.conn.get();
                    shell::rail(rcx, &c2, &t, v.h - 2)
                },
            );
            let border = t.border;
            Element::new()
                .style(LayoutStyle::row().grow(1.0))
                .child(rail)
                .child(
                    Element::new()
                        .style(
                            LayoutStyle::default()
                                .width(Dimension::Cells(1))
                                .shrink(0.0),
                        )
                        .draw(move |canvas, rect| {
                            for y in rect.y..rect.y + rect.h {
                                canvas.print_styled(
                                    abstracttui::base::Point::new(rect.x, y),
                                    "│",
                                    &abstracttui::render::Style::new().fg(border),
                                );
                            }
                        })
                        .build(),
                )
                .child(page)
                .build()
        } else {
            let strip = dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), move |scx| {
                let t = theme.get().tokens;
                let _ = c2.store.conn.get();
                shell::strip(scx, &c2, &t, v.w)
            });
            let _ = hcx;
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(strip)
                .child(page)
                .build()
        }
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
        docs::install(cx, &ctx); // R15 seam: docs drawer (DESIGN §3.16)
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
        .child({
            let c = ctx.clone();
            let vp = abstracttui::app::use_viewport(cx);
            dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), move |hcx| {
                let t = theme.get().tokens;
                let _ = (
                    c.ui.wizard.get(),
                    c.store.host_state.with(|_| ()),
                    c.store.host_widget_error.get(),
                );
                shell::header(hcx, &c, &t, vp.get())
            })
        })
        // "Workflows are paused" (web: every tab) + the restart/quit
        // watcher line; zero rows when neither applies.
        .child(host::banner(&ctx, theme))
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
                // "Step N of M" (the web guide's kicker) when the screen
                // is one of the guide's steps.
                let step = WIZARD_STEPS
                    .iter()
                    .position(|s| *s == screen)
                    .map(|i| format!(" Step {}/{} ", i + 1, WIZARD_STEPS.len()))
                    .unwrap_or_else(|| " Step goal: ".to_string());
                Element::new()
                    .style(LayoutStyle::line(1).shrink(0.0))
                    .child(line(vec![span(step, t.accent), span(goal, t.text_muted)]))
                    .build()
            }
        }))
        .child(footer(cx, &ctx, theme))
        .build()
}

thread_local! {
    static PAGE_VP: std::cell::Cell<Option<Signal<Size>>> = const { std::cell::Cell::new(None) };
}

/// Install the page-size signal (`ui::root` does it once).
pub fn install_page_viewport(sig: Signal<Size>) {
    PAGE_VP.with(|p| p.set(Some(sig)));
}

/// The page region's size (R15): the viewport minus the nav rail or strip,
/// the header and the status rows. Screens size their content from this,
/// never from the raw viewport. Falls back to the viewport outside a root.
pub fn page_viewport(cx: Scope) -> Signal<Size> {
    PAGE_VP
        .with(|p| p.get())
        .filter(|s| s.is_alive())
        .unwrap_or_else(|| abstracttui::app::use_viewport(cx))
}

/// The builder of screen `i`'s page (the PageHost pages, in any order).
fn screen_view(gcx: Scope, c: &Ctx, i: usize, t: &TokenSet) -> View {
    match i {
        SCREEN_CONNECTION => connection::view(gcx, c, t),
        SCREEN_PROVIDERS => providers::view(gcx, c, t),
        SCREEN_ROUTES => routes::view(gcx, c, t),
        SCREEN_USERS => users::view(gcx, c, t),
        SCREEN_RUNTIMES => runtimes::view(gcx, c, t),
        SCREEN_WORKFLOWS => workflows::view(gcx, c, t),
        SCREEN_REVIEW => review::view(gcx, c, t),
        SCREEN_MODELS => models::view(gcx, c, t),
        // AbstractCore's screens, inherited — not re-implemented.
        SCREEN_CATALOG => catalog::view(gcx, c, t),
        SCREEN_ENGINES => abstractcore_console::screens::engines(gcx, &c.screens_for_page()),
        SCREEN_APPS => apps::view(gcx, c, t),
        SCREEN_WELCOME => welcome::view(gcx, c, t),
        SCREEN_NETWORK => network::view(gcx, c, t),
        SCREEN_OPENAI => openai_api::view(gcx, c, t),
        SCREEN_ABOUT => about::page(gcx, c, t),
        SCREEN_SKILLS => skills_mcp::screen(c, gcx),
        _ => unreachable!("screen {i} has no page"),
    }
}

/// The group line above the tabs: each group caption with its keys
/// (`ACCOUNTS 2 · WORK 3-5 · MODELS 6-9 · SYSTEM 0 N R · S Setup`), the
/// current screen's group in the accent colour, the rest muted.
pub fn group_line(t: &TokenSet, screen: usize) -> View {
    let current = nav_group(screen);
    let mut spans = vec![span(" ".to_string(), t.text_faint)];
    for (gi, (name, members)) in NAV_GROUPS.iter().enumerate() {
        if gi > 0 {
            spans.push(span(" · ".to_string(), t.text_faint));
        }
        let keys: Vec<char> = members.iter().filter_map(|m| screen_key(*m)).collect();
        let digits = keys.iter().all(|k| k.is_ascii_digit() && *k != '0');
        // A range only for CONSECUTIVE digits ("3 5 6" stays a list).
        let consecutive = keys.windows(2).all(|w| (w[1] as u32) == (w[0] as u32) + 1);
        let keys = if digits && consecutive && keys.len() > 2 {
            format!("{}-{}", keys[0], keys[keys.len() - 1])
        } else {
            keys.iter()
                .map(char::to_string)
                .collect::<Vec<_>>()
                .join(" ")
        };
        if current == Some(*name) {
            spans.push(span_bold(name.to_string(), t.accent));
        } else {
            spans.push(span(name.to_string(), t.text_muted));
        }
        spans.push(span(format!(" {keys}"), t.text_faint));
    }
    // Setup and About sit below the groups (the web sidebar's foot and
    // its top-bar About).
    for screen_i in [SCREEN_WELCOME, SCREEN_ABOUT] {
        spans.push(span(" · ".to_string(), t.text_faint));
        let label = screen_title(screen_i);
        if screen == screen_i {
            spans.push(span_bold(label, t.accent));
        } else {
            spans.push(span(label, t.text_muted));
        }
    }
    line(spans)
}

/// The one-line first-run goal for each wizard step (P1-B): what THIS
/// step is for, and — crucially — when it can be skipped. Kept ≤ ~85
/// chars (NEW-3) so the operative tail survives at 80-100 cols. The
/// connection screen is self-teaching and gets none (REG-1: it is the
/// tightest screen; the goal row is what pushed it over at 80x24).
fn wizard_goal(screen: usize) -> &'static str {
    match screen {
        SCREEN_PROVIDERS => "optional — install a local engine, or add a cloud provider's key.",
        2 => "your default model — a applies the recommended set; D downloads all of it.",
        SCREEN_USERS => {
            "a creates a user (their token is shown once); skip if the admin token is enough."
        }
        4 => "nothing to configure — storage inventory; glance and continue.",
        SCREEN_WORKFLOWS => {
            "optional — → to the default workflow per app, Enter picks one; x exports, d archives."
        }
        SCREEN_REVIEW => "optionally run one real test (Tab to the prompt, Enter), then Finish.",
        SCREEN_WELCOME => {
            "this computer at a glance — every step is optional; Ctrl+G jumps to a step or leaves."
        }
        SCREEN_MODELS => {
            "nothing to configure — live models, memory & caches; Finish lives on Sandbox."
        }
        SCREEN_CATALOG => {
            "optional — w downloads a model that fits this computer; f shows only those."
        }
        SCREEN_APPS => {
            "optional — i installs a browser app; o opens it signed in (a one-time link)."
        }
        _ => "",
    }
}

fn install_effects(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let ui = ctx.ui;

    cx.effect(move || {
        if store.conn.with(ConnPhase::is_connected) && !ui.was_connected.get_untracked() {
            ui.was_connected.set(true);
        }
    });

    // The centralized connection authority (health.rs): transport
    // failures anywhere trigger ONE background probe that settles the
    // story every surface tells.
    crate::health::install(cx, ctx);

    // First run: read the state at connect, take the boot decision,
    // route Finish / Skip outcomes (ui/welcome.rs).
    welcome::install(cx, ctx);
    // The paused-banner poll (/host/runner, 15 s while connected).
    host::install(cx, ctx);
    install_pointer_follow(cx, ctx);

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
                    // The accounts table loads for everyone (a non-admin's
                    // is `/me/accounts`); the users registry is admin-only.
                    (matches!(store.users.get(), Loadable::NotAsked)
                        && !store.conn.with(ConnPhase::is_known_non_admin))
                        || matches!(store.accounts.get(), Loadable::NotAsked)
                        || matches!(store.entities.get(), Loadable::NotAsked)
                }
                4 if store.conn.with(ConnPhase::is_known_non_admin) => false,
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
                // The Models page reads on its own (catalog.rs: on mount
                // and when a reconnect clears its slots).
                SCREEN_ENGINES => {
                    let s = ctx.screens.store;
                    s.engines.with(Remote::is_not_asked) || s.host.with(Remote::is_not_asked)
                }
                SCREEN_APPS => matches!(store.apps.overview.get(), Loadable::NotAsked),
                SCREEN_WELCOME => matches!(store.welcome.get(), Loadable::NotAsked),
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

    // R10.3: the header widget's refresh — every few seconds while signed
    // in, OFF the Resources page (its own chain refreshes the same
    // snapshot there). One quiet read per tick; a failure keeps the last
    // snapshot, marked stale.
    {
        let ctx_w = ctx.clone();
        let ticker: Rc<RefCell<Option<IntervalHandle>>> = Rc::new(RefCell::new(None));
        cx.effect(move || {
            let on = store.conn.with(ConnPhase::is_connected) && ui.screen.get() != SCREEN_MODELS;
            let mut slot = ticker.borrow_mut();
            match (on, slot.is_some()) {
                (true, false) => {
                    ctx_w.send(Cmd::RefreshHostWidget);
                    let c = ctx_w.clone();
                    *slot = Some(abstracttui::reactive::interval(
                        cx,
                        resources_widget::POLL,
                        move || c.send(Cmd::RefreshHostWidget),
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

    // Notices render in ONE place: the footer's status line (below).
    // They used to ALSO pop an engine Toast, which rests on row 1 and
    // slides in over row 0 — drawing over the header's connection status
    // and the paused/restart banner (REVIEW-1 minor). The footer line is
    // never covered and keeps the notice while an operation is busy.

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

/// Left/Right (backlog 0984): the previous/next global tab, wrapping at
/// both ends — the Ctrl+P/Ctrl+N cycle on the arrows. These are ROOT
/// shortcuts, so they fire only for an arrow nobody under the focus used:
/// a focused text field moves its caret, a radio group or tabs bar moves
/// its choice, a focused scroll pane scrolls (routing law: handlers
/// before shortcuts). The guide gates its order: there the arrows refuse
/// with the keys that walk it.
fn arrow_tab(ctx: &Ctx, dir: isize) {
    if ctx.ui.wizard.get_untracked() {
        ctx.store.notice.set(Some(
            "←/→ switch screens in browse mode — in the guide Ctrl+N walks, Ctrl+G jumps to a step or leaves"
                .into(),
        ));
        return;
    }
    let n = NAV_ORDER.len() as isize;
    let cur = nav_pos(ctx.ui.screen.get_untracked()) as isize;
    ctx.ui
        .screen
        .set(NAV_ORDER[(cur + dir).rem_euclid(n) as usize]);
}

fn wizard_next(ctx: &Ctx, cx: Scope) {
    if !ctx.ui.wizard.get_untracked() {
        // Browse: ] is simply next tab — a refused step SAYS why (F3).
        let pos = nav_pos(ctx.ui.screen.get_untracked());
        if pos + 1 >= NAV_ORDER.len() {
            ctx.store
                .notice
                .set(Some("already on the last screen".into()));
            return;
        }
        ctx.ui.screen.set(NAV_ORDER[pos + 1]);
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
                        if let Some(next) = wizard_step_after(SCREEN_CONNECTION) {
                            ui.screen.set(next);
                        }
                    }
                }
            },
        );
        return;
    }
    match wizard_step_after(screen) {
        Some(next) => ctx.ui.screen.set(next),
        None => ctx.store.notice.set(Some(
            "already on the last step — Finish (or Skip setup) records it and switches to browse mode"
                .into(),
        )),
    }
}

/// The guide step after `screen` (`None` on the last one). A screen off
/// the guide's path (reached in browse, then Ctrl+G) resumes at the
/// first guide step after it in screen order, else at the welcome step.
pub fn wizard_step_after(screen: usize) -> Option<usize> {
    match WIZARD_STEPS.iter().position(|s| *s == screen) {
        Some(i) => WIZARD_STEPS.get(i + 1).copied(),
        None => Some(SCREEN_WELCOME),
    }
}

/// The guide step before `screen` (`None` on the first one).
pub fn wizard_step_before(screen: usize) -> Option<usize> {
    match WIZARD_STEPS.iter().position(|s| *s == screen) {
        Some(0) => None,
        Some(i) => Some(WIZARD_STEPS[i - 1]),
        None => Some(SCREEN_WELCOME),
    }
}

fn wizard_back(ctx: &Ctx) {
    let screen = ctx.ui.screen.get_untracked();
    let prev = if ctx.ui.wizard.get_untracked() {
        wizard_step_before(screen)
    } else {
        nav_pos(screen).checked_sub(1).map(|p| NAV_ORDER[p])
    };
    match prev {
        Some(p) => ctx.ui.screen.set(p),
        // Esc/[ at the first screen is a no-op — say so instead of
        // swallowing the key (F3).
        None => ctx
            .store
            .notice
            .set(Some("already on the first screen".into())),
    }
}

fn footer(_cx: Scope, ctx: &Ctx, theme: Signal<&'static abstracttui::theme::Theme>) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let engine_notices = abstracttui::app::use_startup_notices(_cx);
    let vp_footer = abstracttui::app::use_viewport(_cx);
    let ctx_hints = ctx.clone();
    Element::new()
        // Chrome rows: pinned like the header (finding-0240 class) —
        // the hint line disappearing under content pressure would take
        // the app's teachable surface with it.
        .style(LayoutStyle::column().shrink(0.0))
        .child(dyn_view_scoped(
            LayoutStyle::line(1).shrink(0.0),
            move |fcx| {
                // R15 §2.7: ONE status row — the notice / busy lane on the left,
                // the hints that apply on the right.
                let notice_spans: Vec<util::SpanSpec> = (|| {
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
                    let notice = store.notice.get();
                    if ops.is_empty() {
                        return match notice {
                            Some(n) => vec![span(format!(" {n}"), t.text_muted)],
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
                                    Some(en) => vec![span(format!(" engine: {en}"), t.warn)],
                                    None => vec![span(String::new(), t.text_muted)],
                                }
                            }
                        };
                    }
                    let mut parts = Vec::new();
                    // The latest notice leads (it is the ack of what the operator
                    // just did); the busy ops follow it on the same line.
                    if let Some(n) = notice {
                        parts.push(span(format!(" {n}"), t.text_muted));
                        parts.push(span(" ·", t.text_faint));
                    }
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
                    parts
                })();
                let width = vp_footer.get().w;
                let wizard = ui.wizard.get();
                let _ = ui.screen.get();
                let _ = store.conn.get();
                let _ = store.acc.tab.get();
                let owned = screen_hint_pairs(&ctx_hints);
                let _ = wizard;
                let mut pairs: Vec<(&str, &str)> = Vec::new();
                // A FOCUSED control names itself first (A2: the R9 kit rule,
                // tooltip on keyboard focus — the status bar carries it too).
                let focus = ui.focus_line.get();
                if let Some(f) = focus.as_deref() {
                    pairs.push((f, ""));
                    pairs.push(("Enter", "press"));
                    pairs.push(("Tab", "next"));
                }
                for (k, v) in &owned {
                    pairs.push((k.as_str(), v.as_str()));
                }
                // The key-hint bar (R7.2): wraps whole pairs onto a second
                // line instead of cutting the row's tail.
                // R15 §2.7: ONE status row.
                let t = theme.get().tokens;
                let nw: i32 = notice_spans
                    .iter()
                    .map(|s| abstracttui::text::width(&s.0))
                    .sum();
                // A notice is a sentence: it gets the row (never cut while it
                // fits); the hints keep what is left.
                let nw = nw.min(width - 12);
                let mut row = Element::new().style(LayoutStyle::row().height(Dimension::Cells(1)));
                if nw > 0 {
                    row = row.child(util::line_styled(
                        LayoutStyle::default()
                            .width(Dimension::Cells(nw + 1))
                            .height(Dimension::Cells(1))
                            .shrink(0.0),
                        notice_spans,
                    ));
                }
                // The header has no room for the ☾/☼ switch under 90 columns:
                // it sits at the status bar's right end there, one click away.
                let theme_here = width < 90;
                let reserve = if theme_here { 4 } else { 0 };
                row = row.child(
                    Element::new()
                        .style(LayoutStyle::default().grow(1.0).height(Dimension::Cells(1)))
                        .child(kit::footer_hint_bar(
                            &t,
                            &pairs,
                            (width - nw - 1 - reserve).max(10),
                            1,
                        ))
                        .build(),
                );
                if theme_here {
                    row = row.child(shell::theme_button(fcx, &t));
                }
                row.build()
            },
        ))
        .build()
}

/// Every key hint of the current screen + the global keys, in the order
/// the status bar shows them (the screen's own keys lead). The `?` keys
/// panel lists all of them; the status bar shows what fits.
pub fn screen_hint_pairs(ctx: &Ctx) -> Vec<(String, String)> {
    let store = ctx.store;
    let ui = ctx.ui;
    let ctx_hints = ctx.clone();
    let screens_caps = ctx.screens.caps;
    let screens_access = ctx.screens.store.access;
    let wizard = ui.wizard.get();
    let screen = ui.screen.get();
    // A principal known NOT to be an admin does not see the admin
    // verbs (the web hides the same controls); pressing one still
    // answers with the reason.
    let non_admin = store.conn.with(ConnPhase::is_known_non_admin);
    let mut pairs: Vec<(&str, &str)> = Vec::new();
    // THE SCREEN'S OWN KEYS LEAD (review 2, 80x24): the row
    // truncates right-edge-first, and with the universal pairs
    // first an 80-column footer showed no screen verb at all.
    // Quit follows them (and the guide's Ctrl+N stays first in
    // the wizard); the rest of the universal keys come after.
    let mut globals: Vec<(&str, &str)> = Vec::new();
    if wizard {
        globals.push(("Ctrl+N/]", "next step"));
        globals.push(("Ctrl+C", "quit"));
        globals.push(("Ctrl+P/Esc", "back"));
    } else {
        globals.push(("q/Ctrl+C", "quit"));
        globals.push((SCREEN_KEYS_HINT, "screens"));
        globals.push(("←/→ Ctrl+P/N", "prev/next"));
    }
    globals.push(("Tab", "focus"));
    match screen {
        SCREEN_CONNECTION => pairs.extend(connection::hints(&ctx_hints)), // R15 arm
        SCREEN_PROVIDERS => pairs.extend(providers::hints(&store)),
        2 => {
            pairs.push(("Enter/e", "edit route"));
            pairs.push(("x", "clear route"));
            // `d` is "delete" on Connections/Users and
            // "download" nowhere: weights are `w`, the whole
            // recommended set `D`, each behind a confirm. (The
            // plan line on the screen teaches p / D / a too, for
            // rows too narrow to reach them here.)
            pairs.push(("w", "download weights"));
            pairs.push(("a", "apply recommended"));
            pairs.push(("D", "download all"));
            pairs.push(("C", "cancel download all"));
            pairs.push(("p", "recommended plan"));
            pairs.push(("r", "refresh"));
        }
        SCREEN_USERS => pairs.extend(users::hints(&ctx_hints)),
        SCREEN_RUNTIMES => pairs.extend(runtimes::hints(&ctx_hints)), // R15 arm
        // Named arms from here down (the numbered arms above
        // predate the constants): the Workflows/Review pair had
        // drifted one screen left when Workflows was inserted —
        // Review's sandbox hints rendered on the Workflows
        // screen and Review showed none. Pinned by
        // footer_hints_stay_in_lockstep_with_screens.
        SCREEN_WORKFLOWS => pairs.extend(workflows::hints(&ctx_hints)),
        SCREEN_SKILLS => pairs.extend(skills_mcp::hints(&ctx_hints)),
        SCREEN_REVIEW => {
            pairs.push(("Tab→prompt, Enter", "run the test (REAL generation)"));
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
        SCREEN_CATALOG => pairs.extend(catalog::hints(non_admin)), // R15 arm
        SCREEN_ENGINES => {
            pairs.extend(abstractcore_console::screens::engines::hints(
                screens_caps,
                &screens_access.get(),
            ));
        }
        SCREEN_APPS => pairs.extend_from_slice(apps::HINTS),
        SCREEN_WELCOME => pairs.extend(welcome::hints(&ctx_hints)), // R15 arm
        SCREEN_NETWORK => pairs.extend(network::hints()), // R15 arm
        SCREEN_OPENAI => pairs.extend(openai_api::hints(non_admin)),
        SCREEN_ABOUT => pairs.extend(about::hints()), // R15 arm
        _ => {}
    }
    let admin_keys: &[&str] = match screen {
        SCREEN_ROUTES => routes::ADMIN_KEYS,
        SCREEN_USERS => users::ADMIN_KEYS,
        SCREEN_WORKFLOWS => workflows::ADMIN_KEYS,
        SCREEN_SKILLS => skills_mcp::ADMIN_KEYS,
        SCREEN_MODELS => models::ADMIN_KEYS,
        SCREEN_WELCOME => &["a", "D"],
        SCREEN_CATALOG => catalog::ADMIN_KEYS,
        _ => &[],
    };
    let (screen_pairs, gated) = util::admin_hint_pairs(pairs, admin_keys, non_admin);
    let mut pairs: Vec<(&str, &str)> = Vec::new();
    if wizard {
        pairs.push(globals.remove(0)); // Ctrl+N: the guide's walk
    }
    pairs.extend(screen_pairs);
    if let Some(keys) = gated.as_deref() {
        pairs.push((keys, "admin only"));
    }
    // R15 D5: the keys panel lists everything the status row cannot.
    pairs.push(("?", "keys"));
    pairs.extend(globals);
    // The setup guide's chord LAST: every screen has it, so it
    // yields to the screen's own verbs when the row truncates
    // (the Setup step and the goal line teach it too). The guide
    // is an admin surface (its writes are admin routes; the web
    // hides "Setup guide" for a non-admin).
    if wizard {
        pairs.push(("Ctrl+G", "steps/leave guide"));
    } else if !non_admin {
        pairs.push(("Ctrl+G", "setup guide"));
    }
    // LAST: the row truncates right-edge-first, so the host panel
    // key shows wherever the screen's own verbs leave room.
    pairs.push((host::OPEN_KEY_LABEL, "gateway host"));
    pairs
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

/// Who may change the gateway host from AbstractCore's Models/Engines
/// screens (9 and 0), from the connection: the web console's rule, admins
/// only. A connection being re-verified keeps its principal (no flicker).
/// The ReadOnly reason ends every refusal those screens give ("only an
/// admin can download models — signed in as ana, not an admin").
pub fn screens_access(conn: &ConnPhase) -> abstractcore_console::screens::Access {
    use abstractcore_console::screens::Access;
    match conn {
        ConnPhase::Connected(id) | ConnPhase::Verifying(id) if id.admin => Access::Admin,
        ConnPhase::Connected(id) | ConnPhase::Verifying(id) => {
            Access::ReadOnly(format!("signed in as {}, not an admin", id.user_id))
        }
        _ => Access::ReadOnly("not signed in to a gateway".into()),
    }
}

/// [`screens_access`] as a signal kept current from `store.conn` — what
/// `ScreensCtx::new` takes (lib.rs and the headless tests share it).
pub fn screens_access_signal(
    cx: Scope,
    store: Store,
) -> Signal<abstractcore_console::screens::Access> {
    let access = cx.signal(store.conn.with_untracked(screens_access));
    cx.effect(move || {
        let next = store.conn.with(screens_access);
        if access.with_untracked(|a| *a != next) {
            access.set(next);
        }
    });
    access
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

#[cfg(test)]
mod nav_tests {
    use super::*;

    /// R7.2: the web console's sidebar (console.py `shell_nav`) — Connection
    /// above the groups, ACCOUNTS (Accounts), WORK (Workflows, Runtimes,
    /// Apps; Skills & MCP joins as `4`), MODELS (Providers, OpenAI API,
    /// Models, Multimodal), SYSTEM (Resources, Sandbox, Network), then
    /// Setup and About. Engines is merged into Providers (no tab). Every
    /// listed screen appears once; keys are fixed per screen.
    #[test]
    fn screen_list_order_keys_and_groups() {
        let shown: Vec<String> = NAV_ORDER.iter().map(|i| screen_title(*i)).collect();
        assert_eq!(
            shown,
            [
                "1 Connection",
                "2 Accounts",
                "3 Workflows",
                "4 Skills & MCP",
                "5 Runtimes",
                "6 Apps",
                "7 Providers",
                "8 OpenAI API",
                "9 Models",
                "0 Multimodal",
                "H Resources",
                "T Sandbox",
                "N Network",
                "S Setup",
                "I About",
            ]
        );
        let mut all = NAV_ORDER.to_vec();
        all.sort_unstable();
        let mut expected: Vec<usize> = (0..SCREENS.len())
            .filter(|i| *i != SCREEN_ENGINES)
            .collect();
        expected.sort_unstable();
        assert_eq!(
            all, expected,
            "every screen but Engines (merged into Providers)"
        );
        assert_eq!(screen_key(SCREEN_ENGINES), None);
        assert_eq!(nav_group(SCREEN_USERS), Some("ACCOUNTS"));
        // R14.3: no Workspaces entry (it lives on Accounts); ACCOUNTS is Accounts alone.
        assert_eq!(NAV_GROUPS[0], ("ACCOUNTS", &[SCREEN_USERS][..]));
        assert!(!SCREENS.contains(&"Workspaces"));
        assert_eq!(nav_group(SCREEN_APPS), Some("WORK"));
        assert_eq!(nav_group(SCREEN_OPENAI), Some("MODELS"));
        assert_eq!(nav_group(SCREEN_ROUTES), Some("MODELS"));
        assert_eq!(nav_group(SCREEN_REVIEW), Some("SYSTEM"));
        assert_eq!(nav_group(SCREEN_NETWORK), Some("SYSTEM"));
        assert_eq!(nav_group(SCREEN_CONNECTION), None);
        assert_eq!(nav_group(SCREEN_WELCOME), None);
        assert_eq!(nav_group(SCREEN_ABOUT), None);
        // The guide: welcome → engines (Providers) → model → apps → done.
        assert_eq!(
            WIZARD_STEPS,
            [
                SCREEN_CONNECTION,
                SCREEN_WELCOME,
                SCREEN_PROVIDERS,
                SCREEN_ROUTES,
                SCREEN_CATALOG,
                SCREEN_APPS,
                SCREEN_REVIEW,
            ]
        );
    }

    #[test]
    fn group_line_lists_the_work_keys() {
        abstracttui::app::set_theme_by_id("abstract-dark");
        let keys: Vec<char> = NAV_GROUPS[1]
            .1
            .iter()
            .filter_map(|m| screen_key(*m))
            .collect();
        assert_eq!(keys, ['3', '4', '5', '6']);
    }
}
