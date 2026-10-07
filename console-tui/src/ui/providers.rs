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

use super::util::{ellipsize, error_panel, error_panel_hint, field, line, span, span_bold};
use super::widths;
use super::{open_form, Ctx};
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

/// The footer's key hints for the section on screen.
pub fn hints(store: &crate::store::Store) -> Vec<(&'static str, &'static str)> {
    let mut v = vec![("v", "local/remote/available")];
    match section(store) {
        0 => v.extend([
            ("Enter", "details"),
            ("i", "install"),
            ("s", "start"),
            ("x", "stop"),
            ("b", "browse models"),
            ("c", "cancel install"),
            ("a/e", "connection"),
            ("k", "check again"),
        ]),
        1 => v.extend([("Enter", "configure"), ("r", "refresh")]),
        _ => v.extend([
            ("Enter", "details"),
            ("a", "add connection"),
            ("e", "edit/override"),
            ("d", "delete"),
            ("m", "models"),
            ("t", "test"),
            ("r", "refresh"),
        ]),
    }
    v
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
    let preset_sel = cx.signal(0usize);
    let avail_expanded = cx.signal(Option::<usize>::None);

    let ctx_add = ctx.clone();
    let ctx_edit = ctx.clone();
    let ctx_del = ctx.clone();
    let ctx_models = ctx.clone();
    let ctx_test = ctx.clone();
    let ctx_keys = ctx.clone();
    let ctx_enter = ctx.clone();

    let mut page = Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .shortcut(KeyChord::plain(Key::Char('v')), move |_| {
            set_section(&store, (section(&store) + 1) % 3);
        })
        .shortcut(KeyChord::plain(Key::Char('a')), move |_| {
            if !store.conn.with_untracked(ConnPhase::is_connected) {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
                return;
            }
            match section(&store) {
                0 => match engines::selected(&store, eui) {
                    Some(e) => {
                        let id = e.get("id").and_then(Value::as_str).unwrap_or("").to_string();
                        open_local_connection(cx, &ctx_add, &id);
                    }
                    None => store.notice.set(Some("no engine selected".into())),
                },
                1 => {
                    let i = preset_sel.get_untracked().min(REMOTE_PRESETS.len() - 1);
                    open_preset(cx, &ctx_add, REMOTE_PRESETS[i].0);
                }
                _ => open_profile_form(cx, &ctx_add, ProfileFormMode::Create),
            }
        })
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            if section(&store) == 0 {
                let Some(e) = engines::selected(&store, eui) else {
                    store.notice.set(Some("no engine selected".into()));
                    return;
                };
                let id = e.get("id").and_then(Value::as_str).unwrap_or("").to_string();
                match local_connection_profiles(&store, &id).into_iter().next() {
                    Some(p) if p.synthetic => {
                        open_profile_form(cx, &ctx_edit, ProfileFormMode::Override(p))
                    }
                    Some(p) => open_profile_form(cx, &ctx_edit, ProfileFormMode::Edit(p)),
                    None => store.notice.set(Some(format!(
                        "{id} has no connection yet — a sets one up"
                    ))),
                }
                return;
            }
            edit_selected_profile(cx, &ctx_edit);
        })
        .shortcut(KeyChord::plain(Key::Char('d')), move |_| {
            if section(&store) != 2 {
                store.notice.set(Some(
                    "d deletes a connection on Available Providers (v switches)".into(),
                ));
                return;
            }
            if let Some(p) = selected_profile(&ctx_del) {
                if p.synthetic {
                    store.notice.set(Some(format!(
                        "'{}' comes from {} — only managed profiles delete here; e creates a managed override",
                        p.id,
                        synthetic_origin(&p)
                    )));
                } else {
                    confirm_delete(cx, &ctx_del, p);
                }
            } else {
                store
                    .notice
                    .set(Some("no provider selected — nothing to delete".into()));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
            if let Some(p) = selected_profile(&ctx_models) {
                open_models_modal(cx, &ctx_models, p.provider_name());
            } else {
                store
                    .notice
                    .set(Some("no provider selected — no models to browse".into()));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('t')), move |_| {
            if let Some(p) = selected_profile(&ctx_test) {
                super::review::open_sandbox(&ctx_test, Some(p.provider_name()));
            } else {
                store
                    .notice
                    .set(Some("no provider selected — nothing to test".into()));
            }
        });
    // The engine verbs (Local providers) + the open install question's
    // answers. Elsewhere they say where they live.
    for ch in ['i', 's', 'x', 'b', 'c', 'k', 'A', 'T', 'y', 'u', 'n'] {
        let ctx_k = ctx_keys.clone();
        page = page.shortcut(KeyChord::plain(Key::Char(ch)), move |_| {
            if section(&ctx_k.store) == 0 {
                engines::key(&ctx_k, eui, ch);
            } else if matches!(ch, 'i' | 's' | 'x' | 'b' | 'c' | 'k') {
                ctx_k.store.notice.set(Some(
                    "engine keys work on Local providers — v switches sections".into(),
                ));
            }
        });
    }
    let _ = ctx_enter;
    page.child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
        let t = tt;
        let cur = section(&store);
        let mut spans = vec![span(" ", t.text_faint)];
        for (i, name) in SECTIONS.iter().enumerate() {
            if i > 0 {
                spans.push(span("  │  ", t.text_faint));
            }
            if i == cur {
                spans.push(span_bold(format!("▸ {name}"), t.accent));
            } else {
                spans.push(span(name.to_string(), t.text_muted));
            }
        }
        spans.push(span("   v switches", t.text_faint));
        line(spans)
    }))
    .child(dyn_view_scoped(LayoutStyle::column().grow(1.0), {
        let ctx_body = ctx.clone();
        // ONE keeper for the three sections: switching keeps the keyboard
        // on the page (the section's table takes it over).
        let keeper = super::util::FocusKeeper::new();
        move |gcx| {
            let cur = section(&store);
            let t = tt;
            let width = crate::ui::page_viewport(gcx).get().w - widths::BLOCK_CHROME - 2;
            let mut note = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
            for l in super::util::wrap_text(SECTION_NOTES[cur], width.max(20) as usize) {
                note = note.child(line(vec![span(l, t.text_faint)]));
            }
            let body: View = match cur {
                0 => engines::section(gcx, &ctx_body, eui, &t, keeper.clone()),
                // Forms open on the PAGE scope (cx): a section region rebuilds
                // on every read and would dispose an open form with it.
                1 => presets_section(cx, &ctx_body, &t, preset_sel, keeper.clone()),
                _ => available_section(gcx, &ctx_body, &t, avail_expanded, keeper.clone()),
            };
            Block::new()
                .border(BorderKind::Rounded)
                .title(SECTIONS[cur])
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::hv(1, 0)),
                )
                .child(note.build())
                .child(body)
                .element(&t)
                .build()
        }
    }))
    .build()
}

/// Remote providers: one row per preset with its connection state; Enter
/// (or a) opens the connection form for that family.
fn presets_section(
    page_cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    sel: Signal<usize>,
    keeper: super::util::FocusKeeper,
) -> View {
    let store = ctx.store;
    let tt = *t;
    let ctx_open = ctx.clone();
    dyn_view_scoped(LayoutStyle::column().grow(1.0), move |gcx| {
        let profiles: Vec<Profile> = store
            .profiles
            .get()
            .ready()
            .map(|d| d.profiles.clone())
            .unwrap_or_default();
        let rows: Vec<super::kit::Row> = REMOTE_PRESETS
            .iter()
            .map(|(id, label, _, summary, _)| {
                super::kit::Row::new(vec![
                    label.to_string(),
                    summary.to_string(),
                    preset_status(id, &profiles),
                ])
            })
            .collect();
        let rules = vec![
            widths::ColRule::head("provider", 10),
            widths::ColRule::head("what it is", 20),
            widths::ColRule::head("state", 20),
        ];
        let ctx_a = ctx_open.clone();
        keeper.wire(
            super::kit::WrapTable::new(rules, rows, sel)
                .on_activate(move |i| {
                    let i = i.min(REMOTE_PRESETS.len() - 1);
                    open_preset(page_cx, &ctx_a, REMOTE_PRESETS[i].0);
                })
                .element(gcx, &tt),
        )
    })
}

/// Available Providers: the web table's columns (Name, Provider ID, Type,
/// Models, Status) as wrapping rows; Enter shows a row's description,
/// endpoint and actions.
fn available_section(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    expanded: Signal<Option<usize>>,
    keeper: super::util::FocusKeeper,
) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let _ = cx;
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(dyn_view_scoped(
            LayoutStyle::default().grow(1.0),
            move |gcx| {
                let data = store.profiles.get();
                super::util::loadable_view_kept(
                    &keeper,
                    &tt,
                    &store.conn.get(),
                    || store.tick.get(),
                    &data,
                    |d: &ProfilesData| d.profiles.is_empty(),
                    "No available providers configured yet.",
                    |d| available_table(gcx, &tt, d, ui.profile_sel, expanded, &keeper),
                )
            },
        ))
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            selection_hint(&tt, &store.profiles.get(), ui.profile_sel.get())
        }))
        .child(dyn_view_scoped(
            LayoutStyle::default().shrink(0.0),
            move |_| discovery_footer(&tt, &store.providers.get(), &store.profiles.get()),
        ))
        .build()
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

/// One Available Providers row: [Name, Provider ID, Type, Models, Status]
/// + the detail lines (description, endpoint, origin, actions).
pub fn available_row(p: &Profile) -> super::kit::Row {
    let endpoint = if !p.base_url.is_empty() {
        p.base_url.clone()
    } else {
        "provider default".into()
    };
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
    let desc = if p.description.is_empty() {
        "No description".to_string()
    } else {
        p.description.clone()
    };
    let actions = if p.synthetic {
        format!("{} · e Override · m models · t test", synthetic_origin(p))
    } else {
        "e Edit · d Delete · m models · t test".to_string()
    };
    super::kit::Row::new(vec![
        if p.display_name.is_empty() {
            p.id.clone()
        } else {
            p.display_name.clone()
        },
        p.provider_name(),
        family_label(&p.family),
        models,
        status,
    ])
    .detail(vec![desc, format!("Endpoint {endpoint}"), actions])
    .dim(!p.enabled)
}

fn available_table(
    cx: Scope,
    t: &TokenSet,
    data: &ProfilesData,
    sel: Signal<usize>,
    expanded: Signal<Option<usize>>,
    keeper: &super::util::FocusKeeper,
) -> View {
    let rows: Vec<super::kit::Row> = data.profiles.iter().map(available_row).collect();
    let rules = vec![
        widths::ColRule::head("Name", 10),
        widths::ColRule::tail("Provider ID", 14),
        widths::ColRule::head("Type", 8),
        widths::ColRule::head("Models", 9),
        widths::ColRule::head("Status", 14),
    ];
    keeper.wire(
        super::kit::WrapTable::new(rules, rows, sel)
            .expanded(expanded)
            .empty("No available providers configured yet.")
            .element(cx, t),
    )
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

/// ONE edit entry — shared verbatim by the `e` key and the table's
/// activation (Enter / Space / double-click): the managed-Edit vs
/// synthetic-Override split lives here once and can never drift
/// between the two gestures.
fn edit_selected_profile(cx: Scope, ctx: &Ctx) {
    if let Some(p) = selected_profile(ctx) {
        if p.synthetic {
            open_profile_form(cx, ctx, ProfileFormMode::Override(p));
        } else {
            open_profile_form(cx, ctx, ProfileFormMode::Edit(p));
        }
    } else {
        ctx.store
            .notice
            .set(Some("no provider selected — nothing to edit".into()));
    }
}

/// The per-row action line — what THIS row supports and why (the web
/// shows it as per-row buttons; a TUI says it under the table).
fn selection_hint(t: &TokenSet, data: &Loadable<ProfilesData>, sel: usize) -> View {
    let Some(p) = data.ready().and_then(|d| d.profiles.get(sel)) else {
        return line(vec![span(String::new(), t.text_faint)]);
    };
    let text = if p.synthetic {
        format!(
            "{} — {} · e override → managed copy · m models · t test",
            p.provider_name(),
            synthetic_origin(p)
        )
    } else {
        format!(
            "{} — managed ({} scope) · e edit · d delete · m models · t test",
            p.provider_name(),
            if p.scope.is_empty() { "user" } else { &p.scope }
        )
    };
    let w = (abstracttui::app::current_viewport().w.max(40) - 6) as usize;
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for (i, l) in super::util::wrap_text(&text, w).into_iter().enumerate() {
        col = col.child(line(vec![if i == 0 {
            span_bold(l, t.accent)
        } else {
            span(l, t.text_muted)
        }]));
    }
    col.build()
}

/// Discovery facts under the one list: the gateway default pair and
/// the registered backends with no connection yet (the web shows the
/// latter only as add-cards; here one honest line replaces the whole
/// second table this screen used to carry). Degrades independently of
/// the main list — a failed discovery read never blanks the providers.
fn discovery_footer(
    t: &TokenSet,
    providers: &Loadable<ProvidersData>,
    profiles: &Loadable<ProfilesData>,
) -> View {
    let mut col = Element::new().style(LayoutStyle::column().gap(0));
    match providers {
        Loadable::NotAsked => {}
        Loadable::Loading => {
            col = col.child(line(vec![span("⟳ provider discovery…", t.info)]));
        }
        Loadable::Failed(e) => {
            col = col.child(line(vec![span(
                format!("provider discovery unavailable — {}", e.message),
                t.warn,
            )]));
        }
        Loadable::Ready(d) => {
            let default = match (&d.default_provider, &d.default_model) {
                (Some(p), Some(m)) => format!("gateway default: {p} / {m}"),
                (Some(p), None) => format!("gateway default provider: {p}"),
                _ => "gateway default: none reported".to_string(),
            };
            col = col.child(line(vec![span(default, t.text_muted)]));
            let free = profiles
                .ready()
                .map(|pf| crate::store::unconfigured_provider_names(pf, d))
                .unwrap_or_default();
            if !free.is_empty() {
                // Affordance FIRST: the line right-truncates on narrow
                // terminals, and the teaching must survive over tail
                // names.
                let w = (abstracttui::app::current_viewport().w.max(40) - 6) as usize;
                for l in super::util::wrap_text(
                    &format!("not configured yet (a adds one): {}", free.join(", ")),
                    w,
                ) {
                    col = col.child(line(vec![span(l, t.text_faint)]));
                }
            }
        }
    }
    col.build()
}

fn confirm_delete(cx: Scope, ctx: &Ctx, p: Profile) {
    let ctx = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Delete provider connection '{}'? Workflows routing through endpoint:{} will stop resolving.",
            p.id, p.id
        ),
        "Delete the profile",
        "Keep it",
        move || ctx.send(Cmd::DeleteProfile { id: p.id }),
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
    let title_provider = provider.clone();
    open_form(ctx, cx, Size::new(70, 24), move |mcx, close| {
        let theme = use_theme(mcx);
        let p2 = title_provider.clone();
        Element::new()
            .style(LayoutStyle::column().gap(1))
            .child(dyn_view(LayoutStyle::line(1), {
                let p = p2.clone();
                move || {
                    let t = theme.get().tokens;
                    line(vec![span_bold(format!("Models — {p}"), t.accent)])
                }
            }))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                let p = p2.clone();
                move |gcx| {
                    let t = theme.get().tokens;
                    let entry = store
                        .models
                        .with(|m| m.get(&p).cloned())
                        .unwrap_or(Loadable::NotAsked);
                    match entry {
                        Loadable::NotAsked | Loadable::Loading => {
                            line(vec![span("⟳ discovering models…", t.info)])
                        }
                        Loadable::Failed(e) => error_panel_hint(
                            &t,
                            &e,
                            Some("close and reopen this dialog to retry (opening re-reads)"),
                        ),
                        Loadable::Ready(models) if models.is_empty() => line(vec![span(
                            "∅ no models reported (endpoint offline, or nothing loaded)",
                            t.text_muted,
                        )]),
                        Loadable::Ready(models) => {
                            let count = models.len();
                            Element::new()
                                .style(LayoutStyle::column())
                                .child(line(vec![span(format!("{count} models"), t.text_muted)]))
                                .child(
                                    Scroll::new(
                                        Element::new()
                                            .style(LayoutStyle::column())
                                            .children(
                                                models
                                                    .iter()
                                                    .map(|m| {
                                                        line(vec![span(format!("  {m}"), t.text)])
                                                    })
                                                    .collect::<Vec<_>>(),
                                            )
                                            .build(),
                                    )
                                    .view(gcx),
                                )
                                .build()
                        }
                    }
                }
            }))
            .child(dyn_view_scoped(LayoutStyle::default().h(1).shrink(0.0), {
                move |gcx| {
                    let t = theme.get().tokens;
                    let close = close.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Close (Esc)")
                                .on_click(move || close())
                                .element(gcx, &t)
                                .build(),
                        )
                        .build()
                }
            }))
            .build()
    });
}

/// Create / edit / override form (the web's one endpoint modal).
/// Edit fixes the id and PUTs; Create posts blank; Override posts a
/// create PREFILLED from a synthetic row — same id, so the managed
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

    let ctx2 = ctx.clone();
    // Full-width overlay (R7.2: modals are overlays, Esc closes).
    let vp = crate::ui::page_viewport(cx).get_untracked();
    super::open_form_guarded(ctx, cx, vp, move |mcx, close, guard| {
        let theme = use_theme(mcx);
        // `ex` carries EDIT semantics (static id, stored-key note,
        // clear checkbox, PUT); `seed` only prefills fields — it is
        // the edited profile in edit mode, the synthetic row in
        // override mode, nothing on plain create.
        let ex = existing.clone();
        let over = prefill.clone();
        let seed = ex.clone().or_else(|| over.clone());

        // Form state lives in the modal scope — dies on close.
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
        // Model allowlist (parity with the web console): empty = live
        // discovery; a non-empty list restricts what the profile serves.
        // The web always sends the array, so save always sends it too —
        // clearing the field clears a previously saved restriction.
        let allowed = mcx.signal(
            seed.as_ref()
                .map(|p| p.allowed_models.join(", "))
                .unwrap_or_default(),
        );
        // Family select: placeholder at 0 (the fabricated-selection law:
        // nothing pre-chosen on PLAIN create; edit/override preselect
        // the row's real family).
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
        // Scope: user | gateway (gateway needs admin rights). Editable in
        // both modes — the web sends scope on update and the gateway moves
        // the profile between stores.
        let scope_ix = mcx.signal(match &ex {
            Some(p) if p.scope == "gateway" => 1usize,
            Some(_) => 0usize,
            None => {
                if can_gateway_scope {
                    1
                } else {
                    0
                }
            }
        });
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let esc_armed = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        // Progressive disclosure (P1-C): the happy path to "add a
        // provider key" is id + family + base URL + API key. The six
        // less-common fields live behind ▸ More options — folded on
        // CREATE (first-run adds a key, nothing else), open on EDIT
        // (the operator is deliberately changing an existing profile,
        // so show everything). Field STATE lives in modal-scope signals
        // above, so values survive fold cycles; only the widgets mount
        // on expansion. Signal semantics: true = FOLDED (Disclosure's
        // contract), so folded on create and open on edit.
        let more_folded = mcx.signal(create);

        // Dirty-Esc guard + disarm-on-edit + write_done routing: the ONE
        // shared contract (super::install_* — F4).
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
                move || {
                    let _ = (
                        id.get(),
                        display.get(),
                        desc.get(),
                        base_url.get(),
                        api_key.get(),
                        clear_key.get(),
                        enabled.get(),
                        family_ix.get(),
                        scope_ix.get(),
                        allowed.get(),
                    );
                },
                esc_armed,
                form_error,
            );
        }
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let fam_options: Vec<SelectOption> = std::iter::once(SelectOption::new("choose a family…"))
            .chain(families.iter().map(|f| SelectOption::new(f.clone())))
            .collect();
        let families2 = families.clone();
        let families3 = families.clone();

        let title = match (&ex, &over) {
            _ if preset_title.is_some() => preset_title.clone().unwrap_or_default(),
            (Some(p), _) => format!("Edit profile '{}'", p.id),
            (None, Some(p)) => format!(
                "Override '{}' — create a managed connection",
                p.provider_id.clone().unwrap_or_else(|| p.id.clone())
            ),
            (None, None) => "Add a provider connection".to_string(),
        };
        // The web's override banner, one honest line: the provider
        // already works — this SAVE mints an explicit managed profile
        // that takes precedence over the env/core row.
        let override_note: Option<String> =
            over.as_ref().filter(|_| preset_title.is_none()).map(|p| {
                format!(
                    "already usable from {} — saving creates a managed override",
                    synthetic_origin(p)
                )
            });
        let scope_was_gateway = ex.as_ref().map(|p| p.scope == "gateway").unwrap_or(false);

        let key_note: View = {
            let t = theme.get().tokens;
            match &ex {
                Some(p) if p.api_key_set => line(vec![span(
                    format!(
                        "a key is stored ({}) — leave blank to keep it; type to replace; check clear to remove",
                        p.api_key_fingerprint.clone().unwrap_or_else(|| "set".into())
                    ),
                    t.text_faint,
                )]),
                _ => line(vec![span(
                    "optional — sent once on save, never shown again",
                    t.text_faint,
                )]),
            }
        };

        let t0 = theme.get().tokens;
        let ctx_test = ctx2.clone();
        let ctx_save = ctx2.clone();
        let ex_test = ex.clone();
        let ex_more = ex.clone();
        let close_cancel = close.clone();

        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(title, t0.accent)]))
            .child(match &override_note {
                Some(n) => line(vec![span(n.clone(), t0.info)]),
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            })
            .child(field(
                &t0,
                "id",
                if create {
                    TextInput::new()
                        .value(id)
                        .placeholder("lowercase, digits, - _ (e.g. my-endpoint)")
                        .placeholder_while_focused(true)
                        .layout(LayoutStyle::default().w(40).h(1))
                        .element(mcx, &t0)
                        .autofocus()
                        .build()
                } else {
                    line(vec![span(id.get_untracked(), t0.text_muted)])
                },
            ))
            .child(field(&t0, "family", {
                let sel = Select::new(fam_options)
                    .value(family_ix)
                    .layout(LayoutStyle::default().w(32).h(1).shrink(0.0))
                    .element(mcx, &t0);
                // In edit mode the id row is static text, so family is
                // the first focusable — autofocus it (create autofocuses
                // id). Modal needs a focus target or keys go dead.
                if create {
                    sel.build()
                } else {
                    sel.autofocus().build()
                }
            }))
            .child(field(
                &t0,
                "base URL",
                TextInput::new()
                    .value(base_url)
                    .placeholder("https://host/v1 (or http://127.0.0.1:1234/v1)")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(48).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "API key",
                TextInput::new()
                    .value(api_key)
                    .masked(true)
                    .layout(LayoutStyle::default().w(40).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(&t0, "", key_note))
            // ▸ More options: the six less-common fields (display name,
            // description, allowed models, scope, enabled, clear-key).
            // Folded on create; their state lives in modal-scope signals
            // so values survive fold cycles.
            .child(
                abstracttui::widgets::Disclosure::new(
                    "More options (display name · description · allowed models · scope · enabled)",
                )
                .folded(more_folded)
                .max_body_rows(0)
                .body(move |bcx| {
                    let t = use_theme(bcx).get().tokens;
                    let mut col = Element::new()
                        .style(LayoutStyle::column().gap(0))
                        .child(field(
                            &t,
                            "display name",
                            TextInput::new()
                                .value(display)
                                .placeholder("optional")
                                .layout(LayoutStyle::default().w(40).h(1))
                                .element(bcx, &t)
                                .build(),
                        ))
                        .child(field(
                            &t,
                            "description",
                            TextInput::new()
                                .value(desc)
                                .placeholder("optional")
                                .layout(LayoutStyle::default().w(48).h(1))
                                .element(bcx, &t)
                                .build(),
                        ))
                        .child(field(
                            &t,
                            "allowed models",
                            TextInput::new()
                                .value(allowed)
                                .placeholder("blank = live discovery; or comma-separated model ids")
                                .placeholder_while_focused(true)
                                .layout(LayoutStyle::default().w(48).h(1))
                                .element(bcx, &t)
                                .build(),
                        ))
                        .child(field(
                            &t,
                            "scope",
                            RadioGroup::new(vec![
                                "user (just this login)".to_string(),
                                if can_gateway_scope {
                                    "gateway (everyone on this gateway)".to_string()
                                } else {
                                    "gateway (needs admin — unavailable)".to_string()
                                },
                            ])
                            .selection(scope_ix)
                            .element(bcx, &t)
                            .build(),
                        ));
                    if !create && ex_more.as_ref().map(|p| p.api_key_set).unwrap_or(false) {
                        col = col.child(field(
                            &t,
                            "",
                            Checkbox::new("clear the stored key on save")
                                .checked(clear_key)
                                .element(bcx, &t)
                                .build(),
                        ));
                    }
                    // The profile's on/off, saved with the form's other
                    // fields (form-field switch: Space flips it, Save
                    // writes it with the rest).
                    col.child(field(
                        &t,
                        "",
                        super::switch::Switch::new("Profile in use", enabled)
                            .element(bcx, &t)
                            .build(),
                    ))
                    .build()
                })
                .element(mcx, &t0)
                .build(),
            )
            // Test connection: discover models on the draft (or the saved
            // profile when editing with no draft key/url change).
            .child(field(
                &t0,
                "",
                Button::new("Test connection (discover models)")
                    .on_click(move || {
                        let fam_ix = family_ix.get_untracked();
                        let draft_key = api_key.get_untracked();
                        let url = base_url.get_untracked();
                        if fam_ix == 0 {
                            ctx_test
                                .store
                                .notice
                                .set(Some("choose a family before testing".into()));
                            return;
                        }
                        // Test what the FORM says (family + edited URL),
                        // exactly like the web modal. profile_id rides
                        // along in edit mode so the stored key applies
                        // when no draft key is typed (the gateway merges
                        // body fields over the saved profile; omitting
                        // the family here would test the request-model
                        // default 'openai-compatible', not the profile's).
                        let mut body = json!({
                            "provider_family": families2[fam_ix - 1],
                            "base_url": url.trim(),
                        });
                        if let Some(p) = &ex_test {
                            body["profile_id"] = Value::String(p.id.clone());
                        }
                        if !draft_key.trim().is_empty() {
                            body["api_key"] = Value::String(draft_key.trim().to_string());
                        }
                        ctx_test.store.discover.set(Loadable::Loading);
                        ctx_test.send(Cmd::DiscoverModels { body: body.into() });
                    })
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(4).shrink(0.0),
                move |gcx| {
                    let t = theme.get().tokens;
                    let d = store.discover.get();
                    let mut el = Element::new()
                        .style(LayoutStyle::column().gap(0))
                        .child(discover_result(&t, &d));
                    // Discovery feeds the allowlist, like the web's
                    // multi-select: one keypress restricts the profile
                    // to exactly what the endpoint just reported.
                    if let Loadable::Ready(o) = &d {
                        if !o.models.is_empty() {
                            let models = o.models.clone();
                            let n = models.len();
                            el = el.child(
                                Button::new(format!("Restrict to these {n} models"))
                                    .on_click(move || allowed.set(models.join(", ")))
                                    .element(gcx, &t)
                                    .build(),
                            );
                        }
                    }
                    el.build()
                },
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy_form = in_flight.get();
                    let ctx_save = ctx_save.clone();
                    let close_cancel = close_cancel.clone();
                    let families3 = families3.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new(if create { "Create" } else { "Save" })
                                .disabled(busy_form)
                                .on_click(move || {
                                    // Double-submit guard: a second Save while
                                    // the first write runs would duplicate it.
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let idv = id.get_untracked().trim().to_string();
                                    let fam_ix = family_ix.get_untracked();
                                    let url = base_url.get_untracked().trim().to_string();
                                    let key = api_key.get_untracked().trim().to_string();
                                    let wants_clear = clear_key.get_untracked();
                                    // Local validation mirrors the obvious
                                    // gateway rules; everything else surfaces
                                    // the gateway's own 400 detail verbatim.
                                    if create
                                        && (idv.is_empty()
                                            || !idv.chars().all(|c| {
                                                c.is_ascii_lowercase()
                                                    || c.is_ascii_digit()
                                                    || c == '-'
                                                    || c == '_'
                                            }))
                                    {
                                        form_error.set(Some(
                                            "id: lowercase letters, digits, - and _ only".into(),
                                        ));
                                        return;
                                    }
                                    if fam_ix == 0 {
                                        form_error.set(Some("choose a provider family".into()));
                                        return;
                                    }
                                    if !key.is_empty() && wants_clear {
                                        form_error.set(Some(
                                        "either type a new key or clear the stored one — not both"
                                            .into(),
                                    ));
                                        return;
                                    }
                                    if !(url.is_empty()
                                        || url.starts_with("http://")
                                        || url.starts_with("https://"))
                                    {
                                        form_error.set(Some(
                                            "base URL must start with http:// or https://".into(),
                                        ));
                                        return;
                                    }
                                    // Escalating a user profile to gateway
                                    // scope needs admin; a profile ALREADY
                                    // at gateway scope resends it harmlessly.
                                    if scope_ix.get_untracked() == 1
                                        && !can_gateway_scope
                                        && !scope_was_gateway
                                    {
                                        form_error
                                            .set(Some("gateway scope needs admin rights".into()));
                                        return;
                                    }
                                    // Allowlist: parsed from the field, sent on
                                    // EVERY save (web parity) — an emptied field
                                    // clears a stored restriction; the gateway
                                    // treats a missing field as keep-existing,
                                    // which would make clearing impossible.
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
                                        // Scope rides every save — the gateway
                                        // moves the profile between stores when
                                        // it changes (web sends it too).
                                        "scope": if scope_ix.get_untracked() == 1 {
                                            "gateway"
                                        } else {
                                            "user"
                                        },
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
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_cancel())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

fn discover_result(t: &TokenSet, d: &Loadable<crate::store::DiscoverOutcome>) -> View {
    match d {
        Loadable::NotAsked => line(vec![span("test: not run yet", t.text_faint)]),
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
