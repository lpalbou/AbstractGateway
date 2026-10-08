//! Providers screen: ONE unified provider list (web-console parity,
//! operator ruling 2026-07-25: "we have ONE list and option to
//! configure custom endpoints").
//!
//! The gateway's /config/provider-endpoint-profiles payload IS the
//! unified list — the server already merges managed profiles, synthetic
//! env/core rows and auto-probed local servers (the exact list the web
//! console's "Available Providers" table renders). /discovery/providers
//! is picker fuel, demoted here to two footer facts: the gateway
//! default pair and the registered-but-unconfigured backend names.
//!
//! Row actions follow the web's honesty split: managed rows edit/
//! delete; synthetic rows OVERRIDE (e opens the create form prefilled
//! with the same id, so the managed copy shadows the synthetic row
//! server-side). All model/test actions use the provider-name join law
//! (`Profile::provider_name`): bare ids for synthetic rows,
//! endpoint:<id> for managed ones — the prefixed form is "Unknown
//! provider" to the gateway for synthetic rows (live-verified).
//!
//! Secrets discipline: API keys are write-only. Edit forms show
//! "key stored (fingerprint …)" and an explicit clear checkbox — the
//! stored secret is never echoed, never resubmitted.

use abstracttui::prelude::*;
use serde_json::{json, Value};

use super::util::{ellipsize, error_panel, error_panel_hint, line, span, span_bold};
use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Cell, Col, ColW, DataTable, Ink, Row as WRow, Segmented};
use super::Ctx;
use crate::store::{ConnPhase, Loadable, Profile, ProfilesData, ProvidersData};
use crate::worker::Cmd;

/// Known provider families for new endpoint profiles (the gateway
/// accepts any string; these are the documented ones).
const FAMILIES: [&str; 10] = [
    "openai-compatible",
    "openai",
    "anthropic",
    "openrouter",
    "portkey",
    "lmstudio",
    "ollama",
    "vllm",
    "huggingface",
    "mlx",
];

/// How the profile form opens — the web console's three doors.
pub enum ProfileFormMode {
    /// `a` — blank create.
    Create,
    /// `e` on a managed row — PUT to the same id, id fixed.
    Edit(Profile),
    /// `e` on a synthetic row (web parity "Override"): a CREATE
    /// prefilled from the env/core row — same id, so the managed copy
    /// shadows the synthetic row in the server's merged list.
    Override(Profile),
    /// A preset (Remote providers) or a local provider's "Set up
    /// connection": a CREATE prefilled with the family's id, name and
    /// description (console.py `openEndpointModalForFamily` +
    /// `selectProviderPreset`). The String is the dialog title.
    Preset(Profile, String),
}

/// The engines section's code (Local providers).
#[path = "providers_engines.rs"]
pub mod engines;

/// The page's three sections, in the web's order (console.py
/// `tab-providers`): Local providers, Remote providers, Available Providers.
pub const SECTIONS: [&str; 3] = ["Local providers", "Remote providers", "Available Providers"];

/// Each section's note, verbatim from the web page.
pub const SECTION_NOTES: [&str; 3] = [
    "Engines that run models on this computer, and their server connections.",
    "Cloud accounts and OpenAI-compatible servers. Keys stay on the gateway; only fingerprints are shown.",
    "Configured providers available to this Gateway principal. Use their provider ids in Flow nodes and Core capability defaults.",
];

/// The JSON-lane slot holding the shown section (durable across tab
/// switches, forgotten on reconnect like every slot).
const SLOT_SECTION: &str = "ui.providers.section";

/// The section on screen (0 Local, 1 Remote, 2 Available).
pub fn section(store: &crate::store::Store) -> usize {
    match store.json.get(SLOT_SECTION) {
        Loadable::Ready(v) => v.as_u64().unwrap_or(0).min(2) as usize,
        _ => 0,
    }
}

pub fn set_section(store: &crate::store::Store, i: usize) {
    store
        .json
        .set(SLOT_SECTION, Loadable::Ready(json!(i.min(2))));
}

/// The footer's key hints (R15: the engine row's keys, the provider row's,
/// the page's).
pub fn hints(store: &crate::store::Store) -> Vec<(&'static str, &'static str)> {
    let _ = store;
    vec![
        ("↑↓", "rows"),
        ("Tab", "next table"),
        ("Enter", "first action"),
        ("i/s/x/b/c", "engine"),
        ("n/o", "connection"),
        ("k", "Check again"),
        ("p", "Configure"),
        ("a", "Add connection"),
        ("e/d", "Edit/Delete"),
        ("m", "Models"),
        ("t", "Test"),
        ("r", "refresh"),
    ]
}

/// `r` / first look: the profiles + discovery (Remote / Available and the
/// local connections) and the engines.
pub fn refresh(ctx: &Ctx) {
    let s = &ctx.store;
    s.profiles.set(Loadable::Loading);
    s.providers.set(Loadable::Loading);
    ctx.send(Cmd::LoadProfiles);
    ctx.send(Cmd::LoadProviders);
    engines::refresh(ctx);
}

/// The remote families shown as presets (console.py
/// `REMOTE_PROVIDER_FAMILIES` over `ENDPOINT_FAMILIES`):
/// (id, label, default name, summary, description).
pub const REMOTE_PRESETS: [(&str, &str, &str, &str, &str); 5] = [
    (
        "openai",
        "OpenAI",
        "OpenAI",
        "OpenAI API or an OpenAI-compatible OpenAI deployment.",
        "OpenAI account connection for GPT and embedding models.",
    ),
    (
        "anthropic",
        "Anthropic",
        "Anthropic",
        "Anthropic Claude API or a Claude-compatible Anthropic proxy.",
        "Anthropic account connection for Claude models.",
    ),
    (
        "openrouter",
        "OpenRouter",
        "OpenRouter",
        "OpenRouter account connection for multi-provider routing.",
        "OpenRouter connection for hosted model routing.",
    ),
    (
        "portkey",
        "Portkey",
        "Portkey",
        "Portkey gateway connection for governed provider routing.",
        "Portkey connection for gateway-managed provider routing.",
    ),
    (
        "openai-compatible",
        "Custom OpenAI-compatible",
        "Custom endpoint",
        "Any generic /v1 endpoint such as vLLM, llama.cpp, LocalAI, or a private gateway.",
        "Custom OpenAI-compatible endpoint connection.",
    ),
];

/// The local families' preset words (LM Studio, Ollama): (label, name, description).
fn local_family_words(family: &str) -> (&'static str, &'static str, &'static str) {
    match family {
        "lmstudio" => (
            "LM Studio",
            "LM Studio",
            "LM Studio server connection for local or LAN model serving.",
        ),
        "ollama" => (
            "Ollama",
            "Ollama",
            "Ollama server connection for local or LAN model serving.",
        ),
        _ => (
            "Custom OpenAI-compatible",
            "Custom endpoint",
            "Custom OpenAI-compatible endpoint connection.",
        ),
    }
}

/// `endpointKeyText`.
fn key_text(p: &Profile) -> String {
    if p.api_key_set {
        format!(
            "key {}",
            p.api_key_fingerprint
                .as_deref()
                .unwrap_or("")
                .chars()
                .take(8)
                .collect::<String>()
        )
    } else {
        "no key".into()
    }
}

/// A preset's state (`renderProviderPresets`): "Not connected",
/// "Connected · key …" or "N connections".
pub fn preset_status(family: &str, profiles: &[Profile]) -> String {
    let mine: Vec<&Profile> = profiles
        .iter()
        .filter(|p| {
            p.family == family && !engines::LOCAL_CONNECTION_PROFILE_IDS.contains(&p.id.as_str())
        })
        .collect();
    match mine.len() {
        0 => "Not connected".into(),
        1 => format!("Connected · {}", key_text(mine[0])),
        n => format!("{n} connections"),
    }
}

/// A blank profile seeded for a CREATE (presets, local connections).
fn seed(id: &str, family: &str, name: &str, description: &str) -> Profile {
    Profile {
        id: id.to_string(),
        display_name: name.to_string(),
        description: description.to_string(),
        family: family.to_string(),
        base_url: String::new(),
        default_base_url: None,
        api_key_set: false,
        api_key_fingerprint: None,
        scope: String::new(),
        enabled: true,
        synthetic: false,
        allowed_models: Vec::new(),
        provider_id: None,
        source: None,
        discovered_model_count: None,
    }
}

/// Open the connection form for a remote preset (`openEndpointModalForFamily`).
pub fn open_preset(cx: Scope, ctx: &Ctx, family: &str) {
    let Some((id, label, name, _, desc)) = REMOTE_PRESETS.iter().find(|p| p.0 == family) else {
        return;
    };
    let pid = if *id == "openai-compatible" {
        "custom-endpoint"
    } else {
        id
    };
    open_profile_form(
        cx,
        ctx,
        ProfileFormMode::Preset(seed(pid, id, name, desc), format!("Configure {label}")),
    );
}

/// A local provider's "Set up connection" / "Add connection"
/// (`openLocalProviderConnection`).
pub fn open_local_connection(cx: Scope, ctx: &Ctx, engine: &str) {
    let Some((family, fixed, name, desc)) = engines::local_connection(engine) else {
        ctx.store.notice.set(Some(format!(
            "{engine} runs inside the gateway: it has no server connection to set up"
        )));
        return;
    };
    let (label, fname, fdesc) = local_family_words(family);
    let p = match fixed {
        Some(id) => seed(id, family, name, desc),
        None => seed(family, family, fname, fdesc),
    };
    open_profile_form(
        cx,
        ctx,
        ProfileFormMode::Preset(p, format!("Configure {label}")),
    );
}

/// A local provider's connection rows, for `e` (Edit / Override).
fn local_connection_profiles(store: &crate::store::Store, engine: &str) -> Vec<Profile> {
    let Some((family, fixed, _, _)) = engines::local_connection(engine) else {
        return Vec::new();
    };
    store.profiles.with_untracked(|d| {
        d.ready()
            .map(|d| {
                d.profiles
                    .iter()
                    .filter(|p| match fixed {
                        Some(id) => p.id == id,
                        None => p.family == family,
                    })
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    })
}

// ---------------------------------------------------------------------
// R15 (DESIGN-TUI.md §3.7): the web's three sections stacked on one
// scrolling page — Local providers (the engines table: Engine · Status ·
// Connection · Actions, Check again), Remote providers (one row per preset
// with Configure), Available Providers (Name · Provider ID · Type ·
// Models · Status · Actions: Edit / Delete or Override, Models, Test).
// Every action is a click AND a key; one action list per table
// (`engines::engine_actions`, `preset_actions`, `profile_actions`) is the
// single source for the buttons, the keys and the tests.
// ---------------------------------------------------------------------

/// The web page's title and subtitle.
pub const TITLE: &str = "Providers";
pub const SUBTITLE: &str = "Local engines and remote provider connections";

/// A remote preset row's action (the web tile is the button).
pub fn preset_actions(label: &str) -> Vec<Action> {
    vec![Action::label("configure", "Configure")
        .key('p')
        .tooltip(format!("Configure {label}"))]
}

/// An Available Providers row's actions (the web's Edit / Delete, or
/// Override for a row from the environment / core config), then Models and
/// Test (the terminal's model list and sandbox).
pub fn profile_actions(p: &Profile) -> Vec<Action> {
    let mut out = if p.synthetic {
        vec![Action::label("override", "Override")
            .key('e')
            .tooltip(format!(
                "{} comes from {}: Override makes a managed copy",
                p.provider_name(),
                synthetic_origin(p)
            ))]
    } else {
        vec![
            Action::label("edit", "Edit").key('e'),
            Action::label("delete", "Delete").key('d').danger(),
        ]
    };
    out.push(
        Action::label("models", "Models")
            .key('m')
            .tooltip(format!("The models {} serves", p.provider_name())),
    );
    out.push(
        Action::label("test", "Test")
            .key('t')
            .tooltip(format!("Try {} in the sandbox", p.provider_name())),
    );
    out
}

/// The Available Providers head button.
pub const ADD_CONNECTION_TIP: &str = "Add a provider connection";

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    // Deleting the last row must not strand the highlight past the end.
    super::util::clamp_selection(cx, ui.profile_sel, move || {
        store
            .profiles
            .with(|d| d.ready().map(|d| d.profiles.len()).unwrap_or(0))
    });
    let eui = engines::EnginesUi::new(cx, ctx.screens.store.engine_sel);
    engines::install_effects(cx, ctx, eui);
    // The engines' keyed selection follows the legacy index (other paths
    // and tests set it) and writes it back.
    cx.effect(move || {
        let i = eui.sel.get();
        let key = store.json.get(engines::SLOT_ENGINES).ready().and_then(|d| {
            engines::ordered_engines(d)
                .get(i)
                .and_then(|e| e.get("id").and_then(Value::as_str).map(str::to_string))
        });
        if key.is_some() && eui.key_sel.with_untracked(|k| *k != key) {
            eui.key_sel.set(key);
        }
    });
    cx.effect(move || {
        let Some(k) = eui.key_sel.get() else { return };
        let pos = store
            .json
            .get_untracked(engines::SLOT_ENGINES)
            .ready()
            .and_then(|d| {
                engines::ordered_engines(d)
                    .iter()
                    .position(|e| e.get("id").and_then(Value::as_str) == Some(k.as_str()))
            });
        if let Some(p) = pos {
            if eui.sel.get_untracked() != p {
                eui.sel.set(p);
            }
        }
    });
    // First look: read the engines once per connection.
    {
        let ctx_load = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected
                && matches!(
                    store.json.get_untracked(engines::SLOT_ENGINES),
                    Loadable::NotAsked
                )
            {
                engines::refresh(&ctx_load);
            }
        });
    }
    let preset_key = cx.signal(Some(REMOTE_PRESETS[0].0.to_string()));
    let profile_key = cx.signal(Option::<String>::None);
    // The Available Providers keyed selection ↔ the legacy index.
    cx.effect(move || {
        let i = ui.profile_sel.get();
        let k = store.profiles.with(|d| {
            d.ready()
                .and_then(|d| d.profiles.get(i).map(|p| p.id.clone()))
        });
        if k.is_some() && profile_key.with_untracked(|c| *c != k) {
            profile_key.set(k);
        }
    });
    cx.effect(move || {
        let Some(k) = profile_key.get() else { return };
        let pos = store.profiles.with_untracked(|d| {
            d.ready()
                .and_then(|d| d.profiles.iter().position(|p| p.id == k))
        });
        if let Some(p) = pos {
            if ui.profile_sel.get_untracked() != p {
                ui.profile_sel.set(p);
            }
        }
    });

    let keys_ctx = ctx.clone();
    let body_ctx = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .on(abstracttui::ui::Phase::Bubble, move |ectx, ev| {
            if let abstracttui::ui::UiEvent::Key(k) = ev {
                if k.mods.0 != 0 && !matches!(k.key, Key::Char(c) if c.is_ascii_uppercase()) {
                    return;
                }
                if handle_key(cx, &keys_ctx, eui, preset_key, k.key) {
                    ectx.stop_propagation();
                }
            }
        })
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            let t = tt;
            let w = (crate::ui::page_viewport(cx).get().w - 2).max(20);
            super::workflows::page_head(&t, TITLE, SUBTITLE, w, Vec::new())
        }))
        .child(dyn_view_scoped(
            LayoutStyle::column().grow(1.0).basis(Dimension::Cells(0)),
            move |gcx| {
                let t = use_theme(gcx).get().tokens;
                let w = (crate::ui::page_viewport(gcx).get().w - 3).max(20);
                let col = Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(section_head(&t, 0, w))
                    .child(engines::section(gcx, &body_ctx, eui, &t, w))
                    .child(super::w::fill_line(
                        LayoutStyle::line(1).shrink(0.0),
                        vec![],
                        None,
                    ))
                    .child(section_head(&t, 1, w))
                    .child(presets_table(gcx, cx, &body_ctx, &t, preset_key, w))
                    .child(super::w::fill_line(
                        LayoutStyle::line(1).shrink(0.0),
                        vec![],
                        None,
                    ))
                    .child(available_head(gcx, cx, &body_ctx, &t, w))
                    .child(available_table(gcx, cx, &body_ctx, &t, profile_key, w))
                    .child(discovery_footer(
                        &t,
                        &store.providers.get(),
                        &store.profiles.get(),
                        w,
                    ));
                Scroll::new(col.build())
                    .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                    .scrollbar_auto_hide(true)
                    .view(gcx)
            },
        ))
        .build()
}

/// A section's heading (the web's title, the glyph only when safe) and
/// its note.
fn section_head(t: &TokenSet, i: usize, w: i32) -> View {
    Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(super::w::section(t, SECTIONS[i]))
        .child(sentence(t, SECTION_NOTES[i], w, t.text_muted))
        .build()
}

/// Available Providers: its heading, note and Add connection button.
fn available_head(cx: Scope, pcx: Scope, ctx: &Ctx, t: &TokenSet, w: i32) -> View {
    let a = Action::label("add", "Add connection")
        .key('a')
        .tooltip(ADD_CONNECTION_TIP);
    let c = ctx.clone();
    let aw = a.width();
    let btn = button(cx, t, &a, On::Page, true, move || {
        if c.store.conn.with_untracked(ConnPhase::is_connected) {
            open_profile_form(pcx, &c, ProfileFormMode::Create);
        }
    });
    Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(
            Element::new()
                .style(LayoutStyle::row().h(1).shrink(0.0))
                .child(super::w::fill_line(
                    LayoutStyle::default().w((w - aw).max(10)).h(1).shrink(0.0),
                    vec![Ink::new(SECTIONS[2], t.text).bold()],
                    None,
                ))
                .child(btn)
                .build(),
        )
        .child(sentence(t, SECTION_NOTES[2], w, t.text_muted))
        .build()
}

/// One key of the page. ←/→ never arrive here (the shell owns them).
fn handle_key(
    cx: Scope,
    ctx: &Ctx,
    eui: engines::EnginesUi,
    preset_key: Signal<Option<String>>,
    key: Key,
) -> bool {
    let store = ctx.store;
    let Key::Char(ch) = key else { return false };
    match ch {
        'a' => {
            if store.conn.with_untracked(ConnPhase::is_connected) {
                open_profile_form(cx, ctx, ProfileFormMode::Create);
            } else {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            }
        }
        'p' => {
            let fam = preset_key
                .get_untracked()
                .unwrap_or_else(|| REMOTE_PRESETS[0].0.to_string());
            open_preset(cx, ctx, &fam);
        }
        'e' | 'd' | 'm' | 't' => {
            let id = match ch {
                'e' => "edit",
                'd' => "delete",
                'm' => "models",
                _ => "test",
            };
            match selected_profile(ctx) {
                Some(p) => {
                    let id = if id == "edit" && p.synthetic {
                        "override"
                    } else {
                        id
                    };
                    profile_action(cx, ctx, &p.id, id);
                }
                None => store
                    .notice
                    .set(Some("no provider selected — choose a row first".into())),
            }
        }
        'i' | 'u' | 's' | 'x' | 'b' | 'c' | 'k' | 'A' | 'T' | 'n' | 'o' | 'l' | 'w' | 'g' => {
            engines::key(ctx, eui, ch)
        }
        _ => return false,
    }
    true
}

/// Remote providers: one row per preset (Provider · What it is · State ·
/// Configure).
fn presets_table(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    sel: Signal<Option<String>>,
    w: i32,
) -> View {
    let store = ctx.store;
    let profiles: Vec<Profile> = store
        .profiles
        .get()
        .ready()
        .map(|d| d.profiles.clone())
        .unwrap_or_default();
    let narrow = w < 100;
    let mut cols = vec![Col::new("Provider", ColW::Fit { min: 8, max: 26 })];
    if !narrow {
        cols.push(Col::new("What it is", ColW::Flex { weight: 1, min: 16 }));
    }
    cols.push(Col::new("State", ColW::Fit { min: 8, max: 26 }));
    cols.push(Col::new("Actions", ColW::Fit { min: 9, max: 12 }));
    let rows: Vec<WRow> = REMOTE_PRESETS
        .iter()
        .map(|(id, label, _, summary, _)| {
            let state = preset_status(id, &profiles);
            let on = state != "Not connected";
            let mut cells = vec![Cell::Lines(vec![vec![Ink::new(*label, t.text).bold()]])];
            if !narrow {
                cells.push(Cell::text(*summary, t.text_muted));
            }
            cells.push(Cell::text(state, if on { t.ok } else { t.text_muted }));
            cells.push(Cell::Actions(preset_actions(label)));
            WRow::new(*id, cells).note(narrow.then(|| (summary.to_string(), t.text_muted)))
        })
        .collect();
    let (ca, ce) = (ctx.clone(), ctx.clone());
    DataTable::new(cols, rows, sel)
        .width(w)
        .max_rows(20)
        .on_action(move |key, _id| open_preset(pcx, &ca, key))
        .on_activate(move |key| open_preset(pcx, &ce, key))
        .view(cx, t)
}

/// The family's label as the web table prints it (`endpointFamilyInfo`).
pub fn family_label(family: &str) -> String {
    match family {
        "openai" => "OpenAI",
        "anthropic" => "Anthropic",
        "openrouter" => "OpenRouter",
        "portkey" => "Portkey",
        "lmstudio" => "LM Studio",
        "ollama" => "Ollama",
        _ => "Custom OpenAI-compatible",
    }
    .to_string()
}

/// One Available Providers row's cells: [Name, Provider ID, Type, Models,
/// Status] as text (the web table's words).
pub fn available_cells(p: &Profile) -> [String; 5] {
    let models = if !p.allowed_models.is_empty() {
        format!("{} restricted", p.allowed_models.len())
    } else {
        "live discovery".into()
    };
    let scope = if p.scope.is_empty() { "user" } else { &p.scope };
    let status = format!(
        "{} · {scope} · {}",
        if p.enabled { "enabled" } else { "disabled" },
        key_text(p)
    );
    [
        if p.display_name.is_empty() {
            p.id.clone()
        } else {
            p.display_name.clone()
        },
        p.provider_name(),
        family_label(&p.family),
        models,
        status,
    ]
}

fn available_table(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    sel: Signal<Option<String>>,
    w: i32,
) -> View {
    let store = ctx.store;
    let data = store.profiles.get();
    let d = match data {
        Loadable::Ready(d) => d,
        Loadable::Failed(e) => return error_panel(t, &e),
        _ if !store.conn.with(ConnPhase::is_connected) => {
            return sentence(
                t,
                "not connected — probe the gateway on 1 Connection first",
                w,
                t.text_faint,
            )
        }
        _ => return sentence(t, "⟳ reading the provider connections…", w, t.info),
    };
    let narrow = w < 140;
    let mut cols = vec![Col::new("Name", ColW::Fit { min: 6, max: 18 })];
    cols.push(Col::new("Provider ID", ColW::Fit { min: 8, max: 24 }));
    if !narrow {
        cols.push(Col::new("Type", ColW::Fit { min: 4, max: 24 }));
        cols.push(Col::new("Models", ColW::Fit { min: 6, max: 14 }));
    }
    cols.push(Col::new("Status", ColW::Flex { weight: 1, min: 16 }));
    cols.push(Col::new("Actions", ColW::Fit { min: 10, max: 34 }));
    let rows: Vec<WRow> = d
        .profiles
        .iter()
        .map(|p| {
            let c = available_cells(p);
            let endpoint = if !p.base_url.is_empty() {
                p.base_url.clone()
            } else {
                "provider default".into()
            };
            let desc = if p.description.is_empty() {
                "No description".to_string()
            } else {
                p.description.clone()
            };
            let mut cells = vec![
                Cell::Lines(vec![
                    vec![Ink::new(c[0].clone(), t.text).bold()],
                    vec![Ink::new(desc, t.text_muted)],
                ]),
                Cell::Lines(vec![
                    vec![Ink::new(c[1].clone(), t.text)],
                    vec![Ink::new(endpoint, t.text_muted)],
                ]),
            ];
            if !narrow {
                cells.push(Cell::text(c[2].clone(), t.text));
                cells.push(Cell::text(c[3].clone(), t.text_muted));
            }
            cells.push(Cell::text(
                c[4].clone(),
                if p.enabled { t.ok } else { t.text_muted },
            ));
            cells.push(Cell::Actions(profile_actions(p)));
            WRow::new(p.id.clone(), cells)
                .dim(!p.enabled)
                .note(narrow.then(|| (format!("{} · {}", c[2], c[3]), t.text_muted)))
        })
        .collect();
    let (ca, ce) = (ctx.clone(), ctx.clone());
    DataTable::new(cols, rows, sel)
        .width(w)
        .max_rows(40)
        .empty("No available providers configured yet.")
        .on_action(move |key, id| profile_action(pcx, &ca, key, id))
        .on_activate(move |key| {
            let synthetic = ce.store.profiles.with_untracked(|d| {
                d.ready()
                    .and_then(|d| d.profiles.iter().find(|p| p.id == key).map(|p| p.synthetic))
                    .unwrap_or(false)
            });
            profile_action(pcx, &ce, key, if synthetic { "override" } else { "edit" });
        })
        .view(cx, t)
}

/// An Available Providers row action (a click or its key).
fn profile_action(cx: Scope, ctx: &Ctx, key: &str, id: &str) {
    let store = ctx.store;
    let Some(p) = store.profiles.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.profiles.iter().find(|p| p.id == key).cloned())
    }) else {
        return;
    };
    if let Some(pos) = store.profiles.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.profiles.iter().position(|x| x.id == key))
    }) {
        ctx.ui.profile_sel.set(pos);
    }
    match id {
        "edit" if !p.synthetic => open_profile_form(cx, ctx, ProfileFormMode::Edit(p)),
        "override" | "edit" => open_profile_form(cx, ctx, ProfileFormMode::Override(p)),
        "delete" => {
            if p.synthetic {
                store.notice.set(Some(format!(
                    "'{}' comes from {} — only managed profiles delete here; Override creates a managed copy",
                    p.id,
                    synthetic_origin(&p)
                )));
            } else {
                confirm_delete(cx, ctx, p);
            }
        }
        "models" => open_models_modal(cx, ctx, p.provider_name()),
        "test" => super::review::open_sandbox(ctx, Some(p.provider_name())),
        _ => {}
    }
}

/// Where a synthetic row comes from, in operator words.
fn synthetic_origin(p: &Profile) -> String {
    if p.source.as_deref() == Some("reachable-default") {
        let url = if p.base_url.is_empty() {
            p.default_base_url.clone().unwrap_or_default()
        } else {
            p.base_url.clone()
        };
        if url.is_empty() {
            "a detected local server".into()
        } else {
            format!("a detected local server at {url}")
        }
    } else if p.scope == "environment" {
        "environment config".into()
    } else {
        "core config".into()
    }
}

fn selected_profile(ctx: &Ctx) -> Option<Profile> {
    let idx = ctx.ui.profile_sel.get_untracked();
    ctx.store
        .profiles
        .with_untracked(|d| d.ready().and_then(|d| d.profiles.get(idx).cloned()))
}

/// Discovery facts under the one list: the gateway default pair and the
/// registered backends with no connection yet. Degrades independently of
/// the main list — a failed discovery read never blanks the providers.
fn discovery_footer(
    t: &TokenSet,
    providers: &Loadable<ProvidersData>,
    profiles: &Loadable<ProfilesData>,
    w: i32,
) -> View {
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    match providers {
        Loadable::NotAsked => {}
        Loadable::Loading => {
            col = col.child(sentence(t, "⟳ provider discovery…", w, t.info));
        }
        Loadable::Failed(e) => {
            col = col.child(sentence(
                t,
                &format!("provider discovery unavailable — {}", e.message),
                w,
                t.warn,
            ));
        }
        Loadable::Ready(d) => {
            let default = match (&d.default_provider, &d.default_model) {
                (Some(p), Some(m)) => format!("gateway default: {p} / {m}"),
                (Some(p), None) => format!("gateway default provider: {p}"),
                _ => "gateway default: none reported".to_string(),
            };
            col = col.child(sentence(t, &default, w, t.text_muted));
            let free = profiles
                .ready()
                .map(|pf| crate::store::unconfigured_provider_names(pf, d))
                .unwrap_or_default();
            if !free.is_empty() {
                col = col.child(sentence(
                    t,
                    &format!(
                        "not configured yet (Add connection adds one): {}",
                        free.join(", ")
                    ),
                    w,
                    t.text_faint,
                ));
            }
        }
    }
    col.build()
}

/// The web's delete question (console.py `deleteEndpointProfile`).
pub fn delete_question(p: &Profile) -> String {
    format!(
        "Delete {}? Existing workflows that select this virtual provider will stop working until they are remapped.",
        p.provider_name()
    )
}

fn confirm_delete(cx: Scope, ctx: &Ctx, p: Profile) {
    let c = ctx.clone();
    super::w::confirm(
        ctx,
        cx,
        delete_question(&p),
        "Delete endpoint",
        "Cancel",
        move || c.send(Cmd::DeleteProfile { id: p.id }),
    );
}

/// Models drill-in for any provider name (join law: managed profiles
/// answer to endpoint:<id>, synthetic rows to their bare provider id).
pub fn open_models_modal(cx: Scope, ctx: &Ctx, provider: String) {
    let store = ctx.store;
    if store
        .models
        .with_untracked(|m| m.get(&provider).and_then(|l| l.ready()).is_none())
    {
        store.models.update(|m| {
            m.insert(provider.clone(), Loadable::Loading);
        });
        ctx.send(Cmd::LoadModels {
            provider: provider.clone(),
        });
    }
    let p = provider.clone();
    super::w::FormModal::new(format!("Models — {provider}"))
        .size(76, 26)
        .open(ctx, cx, move |mcx, close, _guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let p2 = p.clone();
            let body = dyn_view_scoped(LayoutStyle::column().grow(1.0), move |gcx| {
                let t = use_theme(gcx).get().tokens;
                let entry = store
                    .models
                    .with(|m| m.get(&p2).cloned())
                    .unwrap_or(Loadable::NotAsked);
                match entry {
                    Loadable::NotAsked | Loadable::Loading => {
                        sentence(&t, "⟳ discovering models…", inner_w, t.info)
                    }
                    Loadable::Failed(e) => error_panel_hint(
                        &t,
                        &e,
                        Some("close and reopen this dialog to retry (opening re-reads)"),
                    ),
                    Loadable::Ready(models) if models.is_empty() => sentence(
                        &t,
                        "∅ no models reported (endpoint offline, or nothing loaded)",
                        inner_w,
                        t.text_muted,
                    ),
                    Loadable::Ready(models) => {
                        let mut list = Element::new().style(LayoutStyle::column().shrink(0.0));
                        for m in &models {
                            list = list.child(sentence(&t, m, inner_w - 1, t.text));
                        }
                        Element::new()
                            .style(LayoutStyle::column().grow(1.0))
                            .child(sentence(
                                &t,
                                &format!("{} models", models.len()),
                                inner_w,
                                t.text_muted,
                            ))
                            .child(
                                Scroll::new(list.build())
                                    .layout(LayoutStyle::default().grow(1.0).min_h(2))
                                    .view(gcx),
                            )
                            .build()
                    }
                }
            });
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(body)
                .child(super::w::form::button_row(vec![button(
                    mcx,
                    &t,
                    &Action::label("close", "Close"),
                    On::Raised,
                    true,
                    move || close(),
                )]))
                .build()
        });
}

/// The web endpoint modal's words (console.py `#provider-modal-backdrop`).
pub const FORM_DESCRIPTION: &str = "Gateway stores endpoint details and keys server-side, then exposes this connection as an available provider.";
pub const KEY_PLACEHOLDER: &str = "leave blank to keep existing key";
pub const BASE_URL_PLACEHOLDER: &str = "optional; leave blank for provider default";
pub const DESCRIPTION_PLACEHOLDER: &str =
    "What this provider is for, who owns it, and when to use it.";
pub const VISIBLE_MODELS: &str = "Visible models";
pub const VISIBLE_MODELS_HELP: &str = "Optional. Use Test to preview discovery, then select models only when this provider should expose a fixed allowlist.";
pub const CLEAR_RESTRICTION_TIP: &str = "Serve every model this endpoint exposes (no allowlist)";
pub const TEST_TIP: &str = "Probe the endpoint and list the models it actually serves";
pub const CONFIRM_TIP: &str = "Store this connection server-side and expose it as a provider";
pub const SCOPES: [&str; 2] = ["Gateway-wide", "Only me"];

/// Create / edit / override form (the web's one endpoint modal): Provider
/// type, Who can use it?, Provider ID, Name, Description, Base URL, API
/// key, Clear stored API key, Enabled, Visible models; [Cancel] [↻ Test]
/// [✓ Confirm]. Edit fixes the id and PUTs; Create posts blank; Override
/// posts a create PREFILLED from a synthetic row — same id, so the managed
/// copy shadows the env/core row in the server's merged list.
pub fn open_profile_form(cx: Scope, ctx: &Ctx, mode: ProfileFormMode) {
    let store = ctx.store;
    let (create, existing, prefill, preset_title) = match mode {
        ProfileFormMode::Create => (true, None, None, None),
        ProfileFormMode::Edit(p) => (false, Some(p), None, None),
        ProfileFormMode::Override(p) => (true, None, Some(p), None),
        ProfileFormMode::Preset(p, title) => (true, None, Some(p), Some(title)),
    };
    let can_gateway_scope = store.profiles.with_untracked(|p| {
        p.ready()
            .map(|d| d.can_create_gateway_scope)
            .unwrap_or(false)
    });
    // Reset the shared discover slot so stale results never show.
    store.discover.set(Loadable::NotAsked);
    let seed = existing.clone().or_else(|| prefill.clone());
    // The web's title: "Configure <family>" ("Configure provider" blank).
    let title = match (&preset_title, &seed) {
        (Some(t), _) => t.clone(),
        (None, Some(p)) => format!("Configure {}", family_label(&p.family)),
        (None, None) => "Configure provider".to_string(),
    };
    let lead = REMOTE_PRESETS
        .iter()
        .find(|f| Some(f.0) == seed.as_ref().map(|p| p.family.as_str()))
        .map(|f| f.3.to_string())
        .unwrap_or_else(|| FORM_DESCRIPTION.to_string());
    let ctx2 = ctx.clone();
    super::w::FormModal::new(title)
        .lead(lead)
        .size(100, 40)
        .open(ctx, cx, move |mcx, close, guard, inner_w| {
            let t0 = use_theme(mcx).get().tokens;
            let theme = use_theme(mcx);
            let ex = existing.clone();
            let over = prefill.clone();
            let seed = ex.clone().or_else(|| over.clone());
            // Override seeds the id with the row's PROVIDER id (bare name,
            // web parity): saving under that exact id is what makes the
            // managed profile take the synthetic row's place.
            let id = mcx.signal(
                seed.as_ref()
                    .map(|p| p.provider_id.clone().unwrap_or_else(|| p.id.clone()))
                    .unwrap_or_default(),
            );
            let display = mcx.signal(
                seed.as_ref()
                    .map(|p| p.display_name.clone())
                    .unwrap_or_default(),
            );
            let desc = mcx.signal(
                seed.as_ref()
                    .map(|p| p.description.clone())
                    .unwrap_or_default(),
            );
            let base_url = mcx.signal(
                seed.as_ref()
                    .map(|p| p.base_url.clone())
                    .unwrap_or_default(),
            );
            let api_key = mcx.signal(String::new());
            let clear_key = mcx.signal(false);
            let enabled = mcx.signal(seed.as_ref().map(|p| p.enabled).unwrap_or(true));
            // Model allowlist: empty = live discovery; sent on every save.
            let allowed = mcx.signal(
                seed.as_ref()
                    .map(|p| p.allowed_models.join(", "))
                    .unwrap_or_default(),
            );
            // Provider type: nothing pre-chosen on a blank create.
            let mut families: Vec<String> = FAMILIES.iter().map(|f| f.to_string()).collect();
            if let Some(p) = &seed {
                if !p.family.is_empty() && !families.iter().any(|f| f == &p.family) {
                    families.push(p.family.clone());
                }
            }
            let family_ix = mcx.signal(match &seed {
                Some(p) => families
                    .iter()
                    .position(|f| f == &p.family)
                    .map(|i| i + 1)
                    .unwrap_or(0),
                None => 0,
            });
            // Who can use it? Gateway-wide (admins) | Only me.
            let scope_ix = mcx.signal(match &ex {
                Some(p) if p.scope == "gateway" => 0usize,
                Some(_) => 1usize,
                None => {
                    if can_gateway_scope {
                        0
                    } else {
                        1
                    }
                }
            });
            let form_error = mcx.signal(Option::<String>::None);
            let in_flight = mcx.signal(false);
            let esc_armed = mcx.signal(false);
            let form_id = crate::worker::next_form_id();
            {
                let initial = (
                    id.get_untracked(),
                    display.get_untracked(),
                    desc.get_untracked(),
                    base_url.get_untracked(),
                    enabled.get_untracked(),
                    family_ix.get_untracked(),
                    scope_ix.get_untracked(),
                    allowed.get_untracked(),
                );
                super::install_dirty_guard_with(
                    mcx,
                    &guard,
                    move || {
                        id.get_untracked() != initial.0
                            || display.get_untracked() != initial.1
                            || desc.get_untracked() != initial.2
                            || base_url.get_untracked() != initial.3
                            || enabled.get_untracked() != initial.4
                            || family_ix.get_untracked() != initial.5
                            || scope_ix.get_untracked() != initial.6
                            || allowed.get_untracked() != initial.7
                            || !api_key.get_untracked().is_empty()
                            || clear_key.get_untracked()
                    },
                    || {},
                    esc_armed,
                    form_error,
                );
            }
            super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

            let fam_options: Vec<SelectOption> =
                std::iter::once(SelectOption::new("choose a provider type…"))
                    .chain(families.iter().map(|f| {
                        let l = family_label(f);
                        SelectOption::new(
                            if l == "Custom OpenAI-compatible" && f != "openai-compatible" {
                                f.clone()
                            } else {
                                l
                            },
                        )
                    }))
                    .collect();
            let families2 = families.clone();
            let families3 = families.clone();
            let override_note: Option<String> =
                over.as_ref().filter(|_| preset_title.is_none()).map(|p| {
                    format!(
                        "already usable from {} — saving creates a managed override",
                        synthetic_origin(p)
                    )
                });
            let scope_was_gateway = ex.as_ref().map(|p| p.scope == "gateway").unwrap_or(false);
            let key_note = match &ex {
                Some(p) if p.api_key_set => format!(
                    "A key is stored ({}): leave blank to keep it, type to replace it.",
                    p.api_key_fingerprint
                        .clone()
                        .unwrap_or_else(|| "set".into())
                ),
                _ => "Sent once on save, never shown again.".to_string(),
            };
            let caret = ctx2.ui.caret;
            let fw = (inner_w - 18).clamp(20, 70);
            let input = move |sig: Signal<String>, ph: &str, masked: bool| -> View {
                super::w::caret_tracked(
                    mcx,
                    caret,
                    TextInput::new()
                        .value(sig)
                        .placeholder(ph)
                        .masked(masked)
                        .layout(LayoutStyle::default().w(fw).h(1))
                        .element(mcx, &t0),
                )
                .build()
            };
            let help = move |text: &str| -> View {
                Element::new()
                    .style(LayoutStyle::row().shrink(0.0))
                    .child(
                        Element::new()
                            .style(LayoutStyle::default().w(17).h(1).shrink(0.0))
                            .build(),
                    )
                    .child(sentence(&t0, text, (inner_w - 17).max(10), t0.text_faint))
                    .build()
            };
            let mut col = Element::new().style(LayoutStyle::column().gap(0));
            if let Some(n) = &override_note {
                col = col.child(sentence(&t0, n, inner_w, t0.info));
            }
            let fam_select = Select::new(fam_options)
                .value(family_ix)
                .layout(LayoutStyle::default().w(34).h(1).shrink(0.0))
                .element(mcx, &t0);
            col = col.child(super::w::field_row(
                &t0,
                "Provider type",
                17,
                if create {
                    fam_select.build()
                } else {
                    fam_select.autofocus().build()
                },
            ));
            let mut scope = Segmented::new(SCOPES, None).bind(scope_ix);
            if !can_gateway_scope && !scope_was_gateway {
                scope = scope.disable(0, "Gateway-wide needs admin rights.");
            }
            col = col.child(super::w::field_row(
                &t0,
                "Who can use it?",
                17,
                scope.view(mcx, &t0),
            ));
            col = col.child(super::w::field_row(
                &t0,
                "Provider ID",
                17,
                if create {
                    super::w::caret_tracked(
                        mcx,
                        caret,
                        TextInput::new()
                            .value(id)
                            .placeholder("openai")
                            .layout(LayoutStyle::default().w(fw.min(40)).h(1))
                            .element(mcx, &t0),
                    )
                    .autofocus()
                    .build()
                } else {
                    sentence(&t0, &id.get_untracked(), fw, t0.text_muted)
                },
            ));
            col = col.child(super::w::field_row(
                &t0,
                "Name",
                17,
                input(display, "OpenAI", false),
            ));
            col = col.child(super::w::field_row(
                &t0,
                "Description",
                17,
                input(desc, DESCRIPTION_PLACEHOLDER, false),
            ));
            col = col.child(super::w::field_row(
                &t0,
                "Base URL",
                17,
                input(base_url, BASE_URL_PLACEHOLDER, false),
            ));
            col = col.child(super::w::field_row(
                &t0,
                "API key",
                17,
                input(api_key, KEY_PLACEHOLDER, true),
            ));
            col = col.child(help(&key_note));
            let mut toggles = Element::new().style(LayoutStyle::row().gap(3).h(1).shrink(0.0));
            if !create && ex.as_ref().map(|p| p.api_key_set).unwrap_or(false) {
                toggles = toggles.child(
                    super::w::Toggle::switch("Clear stored API key", clear_key)
                        .on_change(move |v| clear_key.set(v))
                        .view(mcx, &t0),
                );
            }
            toggles = toggles.child(
                super::w::Toggle::switch("Enabled", enabled)
                    .on_change(move |v| enabled.set(v))
                    .view(mcx, &t0),
            );
            col = col.child(super::w::field_row(&t0, "", 17, toggles.build()));
            // Visible models (R15 D1: a named section, never "Advanced").
            col = col.child(super::w::section(&t0, VISIBLE_MODELS));
            col = col.child(sentence(&t0, VISIBLE_MODELS_HELP, inner_w, t0.text_faint));
            let clear =
                Action::label("clear_models", "Clear restriction").tooltip(CLEAR_RESTRICTION_TIP);
            col = col.child(
                Element::new()
                    .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                    .child(
                        super::w::caret_tracked(
                            mcx,
                            caret,
                            TextInput::new()
                                .value(allowed)
                                .placeholder(
                                    "live discovery (no allowlist); or model ids, comma-separated",
                                )
                                .layout(
                                    LayoutStyle::default()
                                        .w((inner_w - clear.width() - 2).max(20))
                                        .h(1),
                                )
                                .element(mcx, &t0),
                        )
                        .build(),
                    )
                    .child(button(mcx, &t0, &clear, On::Raised, true, move || {
                        allowed.set(String::new())
                    }))
                    .build(),
            );
            col = col.child(dyn_view_scoped(
                LayoutStyle::column().shrink(0.0),
                move |gcx| {
                    let t = theme.get().tokens;
                    let d = store.discover.get();
                    let mut el = Element::new()
                        .style(LayoutStyle::column().gap(0))
                        .child(discover_result(&t, &d));
                    // Discovery feeds the allowlist (the web's multi-select).
                    if let Loadable::Ready(o) = &d {
                        if !o.models.is_empty() {
                            let models = o.models.clone();
                            let a = Action::label(
                                "restrict",
                                format!("Restrict to these {} models", models.len()),
                            );
                            el = el.child(button(gcx, &t, &a, On::Raised, true, move || {
                                allowed.set(models.join(", "))
                            }));
                        }
                    }
                    el.build()
                },
            ));
            col = col.child(super::message_slot(theme, form_error, in_flight));
            let ctx_test = ctx2.clone();
            let ex_test = ex.clone();
            let test = move || {
                let fam_ix = family_ix.get_untracked();
                if fam_ix == 0 {
                    form_error.set(Some("Choose a provider type before testing.".into()));
                    return;
                }
                let mut body = json!({
                    "provider_family": families2[fam_ix - 1],
                    "base_url": base_url.get_untracked().trim(),
                });
                if let Some(p) = &ex_test {
                    body["profile_id"] = Value::String(p.id.clone());
                }
                let draft_key = api_key.get_untracked();
                if !draft_key.trim().is_empty() {
                    body["api_key"] = Value::String(draft_key.trim().to_string());
                }
                ctx_test.store.discover.set(Loadable::Loading);
                ctx_test.send(Cmd::DiscoverModels { body: body.into() });
            };
            let ctx_save = ctx2.clone();
            let save = move || {
                if in_flight.get_untracked() {
                    return; // a write is already running
                }
                let idv = id.get_untracked().trim().to_string();
                let fam_ix = family_ix.get_untracked();
                let url = base_url.get_untracked().trim().to_string();
                let key = api_key.get_untracked().trim().to_string();
                let wants_clear = clear_key.get_untracked();
                if create
                    && (idv.is_empty()
                        || !idv.chars().all(|c| {
                            c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_'
                        }))
                {
                    form_error.set(Some(
                        "Provider ID: lowercase letters, digits, - and _ only.".into(),
                    ));
                    return;
                }
                if fam_ix == 0 {
                    form_error.set(Some("Choose a provider type.".into()));
                    return;
                }
                if !key.is_empty() && wants_clear {
                    form_error.set(Some(
                        "Either type a new key or clear the stored one — not both.".into(),
                    ));
                    return;
                }
                if !(url.is_empty() || url.starts_with("http://") || url.starts_with("https://")) {
                    form_error.set(Some("Base URL must start with http:// or https://".into()));
                    return;
                }
                if scope_ix.get_untracked() == 0 && !can_gateway_scope && !scope_was_gateway {
                    form_error.set(Some("Gateway-wide needs admin rights.".into()));
                    return;
                }
                let allowed_list: Vec<String> = {
                    let mut seen = std::collections::BTreeSet::new();
                    allowed
                        .get_untracked()
                        .split([',', ';', '\n'])
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .filter(|s| seen.insert(s.to_string()))
                        .map(str::to_string)
                        .collect()
                };
                let mut body = json!({
                    "display_name": display.get_untracked().trim(),
                    "description": desc.get_untracked().trim(),
                    "provider_family": families3[fam_ix - 1],
                    "base_url": url,
                    "enabled": enabled.get_untracked(),
                    "allowed_models": allowed_list,
                    "scope": if scope_ix.get_untracked() == 0 { "gateway" } else { "user" },
                });
                if create {
                    body["id"] = Value::String(idv.clone());
                }
                if !key.is_empty() {
                    body["api_key"] = Value::String(key);
                }
                if wants_clear {
                    body["clear_api_key"] = Value::Bool(true);
                }
                form_error.set(None);
                in_flight.set(true);
                ctx_save.send(Cmd::SaveProfile {
                    create,
                    id: idv,
                    body: body.into(),
                    form_id: Some(form_id),
                });
            };
            let cancel = {
                let (close, guard) = (close.clone(), guard.clone());
                move || {
                    let handled = guard.borrow().as_ref().map(|g| g()).unwrap_or(false);
                    if !handled {
                        close();
                    }
                }
            };
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    Scroll::new(col.build())
                        .layout(LayoutStyle::default().grow(1.0).min_h(4))
                        .element(mcx, &t0)
                        .build(),
                )
                .child(super::w::form::button_row(vec![
                    button(
                        mcx,
                        &t0,
                        &Action::label("cancel", "Cancel"),
                        On::Raised,
                        true,
                        cancel,
                    ),
                    button(
                        mcx,
                        &t0,
                        &Action::label("test", "↻ Test").tooltip(TEST_TIP),
                        On::Raised,
                        true,
                        test,
                    ),
                    button(
                        mcx,
                        &t0,
                        &Action::label("confirm", "✓ Confirm").tooltip(CONFIRM_TIP),
                        On::Raised,
                        true,
                        save,
                    ),
                ]))
                .build()
        });
}

fn discover_result(t: &TokenSet, d: &Loadable<crate::store::DiscoverOutcome>) -> View {
    match d {
        Loadable::NotAsked => line(vec![span("Not tested yet.", t.text_faint)]),
        Loadable::Loading => line(vec![span("⟳ contacting the endpoint…", t.info)]),
        Loadable::Failed(e) => error_panel(t, e),
        Loadable::Ready(o) => {
            if o.available && o.error.is_none() {
                let sample = o
                    .models
                    .iter()
                    .take(3)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ");
                Element::new()
                    .style(LayoutStyle::column())
                    .child(line(vec![
                        span_bold("✓ reachable — ", t.ok),
                        span(format!("{} models discovered", o.models.len()), t.text),
                    ]))
                    .child(line(vec![span(
                        if sample.is_empty() {
                            "  (endpoint reachable but reported no models)".to_string()
                        } else {
                            format!("  e.g. {}", ellipsize(&sample, 60))
                        },
                        t.text_muted,
                    )]))
                    .build()
            } else {
                Element::new()
                    .style(LayoutStyle::column())
                    .child(line(vec![span_bold("✗ endpoint test failed", t.error)]))
                    .child(line(vec![span(
                        format!(
                            "  {}",
                            o.error.clone().unwrap_or_else(|| "not available".into())
                        ),
                        t.text,
                    )]))
                    .build()
            }
        }
    }
}

/// Shared bits for the routes editor: provider names from discovery.
pub fn provider_names(store: &crate::store::Store) -> Vec<String> {
    store.providers.with_untracked(|p| {
        p.ready()
            .map(|d| d.items.iter().map(|i| i.name.clone()).collect())
            .unwrap_or_default()
    })
}
