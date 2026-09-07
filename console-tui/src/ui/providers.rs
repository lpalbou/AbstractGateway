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
use abstracttui::widgets::Table;
use serde_json::{json, Value};

use super::util::{
    ellipsize, error_panel, error_panel_hint, field, line, loadable_view, span, span_bold,
};
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

    let ctx_add = ctx.clone();
    let ctx_edit = ctx.clone();
    let ctx_del = ctx.clone();
    let ctx_models = ctx.clone();
    let ctx_test = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(1))
        .shortcut(KeyChord::plain(Key::Char('a')), move |_| {
            // Every guard SAYS why it refused (F2: a footer-advertised
            // key that silently does nothing reads as a dead app).
            if store.conn.with_untracked(ConnPhase::is_connected) {
                open_profile_form(cx, &ctx_add, ProfileFormMode::Create);
            } else {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            edit_selected_profile(cx, &ctx_edit);
        })
        .shortcut(KeyChord::plain(Key::Char('d')), move |_| {
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
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Available providers (a adds a connection)")
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                    let ctx_act = ctx.clone();
                    move |gcx| {
                        let data = store.profiles.get();
                        let ctx_act = ctx_act.clone();
                        loadable_view(
                            &tt,
                            &store.conn.get(),
                            || store.tick.get(),
                            &data,
                            |d: &ProfilesData| d.profiles.is_empty(),
                            "no providers yet — press a to add a connection",
                            |d| {
                                unified_table(gcx, &tt, d, ui.profile_sel, move |_| {
                                    // Activation (Enter / Space / double-
                                    // click) = the `e` path: Edit for
                                    // managed rows, Override for
                                    // synthetic ones — one body.
                                    edit_selected_profile(cx, &ctx_act);
                                })
                            },
                        )
                    }
                }))
                // Selected-row action honesty — the TUI stand-in for
                // the web's per-row Edit/Delete vs Override buttons.
                .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    selection_hint(&tt, &store.profiles.get(), ui.profile_sel.get())
                }))
                // Discovery demoted to facts: the default pair + what
                // is registered but has no connection yet.
                .child(dyn_view_scoped(
                    LayoutStyle::default().shrink(0.0),
                    move |_| discovery_footer(&tt, &store.providers.get(), &store.profiles.get()),
                ))
                .element(t)
                .build(),
        )
        .build()
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

/// The ONE provider table (web "Available Providers" parity): every
/// row is a profiles-payload row — managed, env/core-synthetic, or an
/// auto-probed local server. The first column carries the provider
/// NAME rows answer to (the join law), because that string — not the
/// internal profile id — is what flows/pins/routes reference.
fn unified_table(
    cx: Scope,
    t: &TokenSet,
    data: &ProfilesData,
    sel: Signal<usize>,
    on_activate: impl FnMut(usize) + 'static,
) -> View {
    // Width-aware columns: which columns APPEAR is a breakpoint decision
    // (narrow terminals get fewer, honest columns instead of a silently
    // amputated payload column — filed 0900); how wide the survivors are
    // is MEASURED from the rows by `ui::widths`, so a wide terminal
    // prints every base URL whole.
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let wide = vw >= 104;
    let mut rows: Vec<Vec<String>> = data
        .profiles
        .iter()
        .map(|p| {
            let url = if p.base_url.is_empty() {
                p.default_base_url.clone().unwrap_or_default()
            } else {
                p.base_url.clone()
            };
            let mut row = vec![p.provider_name()];
            if wide {
                row.push(p.family.clone());
            }
            // The FULL URL: two endpoints on the same host differ in
            // their path, so a 38-char cap printed one string for both.
            row.push(if url.is_empty() { "—".into() } else { url });
            row.push(if p.api_key_set {
                match &p.api_key_fingerprint {
                    Some(f) => format!("stored ({})", ellipsize(f, 8)),
                    None => "stored".into(),
                }
            } else {
                "none".into()
            });
            if wide {
                // Web-table parity: an allowlist restricts the profile;
                // otherwise it serves live discovery — and synthetic
                // local rows already know their live count.
                row.push(if !p.allowed_models.is_empty() {
                    format!("{} restr", p.allowed_models.len())
                } else {
                    match p.discovered_model_count {
                        Some(n) if n > 0 => format!("{n} live"),
                        _ => "live".into(),
                    }
                });
            }
            row.push(if p.enabled { "yes".into() } else { "NO".into() });
            row.push(origin_label(p));
            row
        })
        .collect();
    // Provider names and base URLs discriminate on their TAIL (`…/v1` vs
    // `…/v1/openai`); the rest print bounded phrases whose floor is their
    // widest word.
    let mut rules = vec![widths::ColRule::tail("provider", 16)];
    if wide {
        rules.push(widths::ColRule::head("family", 12));
    }
    rules.push(widths::ColRule::tail("base URL", 20));
    rules.push(widths::ColRule::head("API key", 14));
    if wide {
        rules.push(widths::ColRule::head("models", 8));
    }
    rules.push(widths::ColRule::head("enabled", 7));
    rules.push(widths::ColRule::head("origin", 8));
    // The screen's bordered block spends one cell on each side.
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    Table::new(cols)
        .rows(rows)
        .selection(sel)
        .on_activate(on_activate)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t)
        .autofocus()
        .build()
}

/// One word for where a row lives: managed rows show their scope
/// (gateway/user — who sees them); synthetic rows show their source
/// (env / core / auto-detected local server).
fn origin_label(p: &Profile) -> String {
    if p.synthetic {
        if p.source.as_deref() == Some("reachable-default") {
            "auto".into()
        } else if p.scope == "environment" {
            "env".into()
        } else {
            "core".into()
        }
    } else if p.scope.is_empty() {
        "managed".into()
    } else {
        p.scope.clone()
    }
}

/// The per-row action line — what THIS row supports and why (the web
/// shows it as per-row buttons; a TUI says it under the table).
fn selection_hint(t: &TokenSet, data: &Loadable<ProfilesData>, sel: usize) -> View {
    let Some(p) = data.ready().and_then(|d| d.profiles.get(sel)) else {
        return line(vec![span(String::new(), t.text_faint)]);
    };
    if p.synthetic {
        line(vec![
            span_bold(p.provider_name(), t.accent),
            span(
                format!(
                    " — {} · e override → managed copy · m models · t test",
                    synthetic_origin(p)
                ),
                t.text_muted,
            ),
        ])
    } else {
        line(vec![
            span_bold(p.provider_name(), t.accent),
            span(
                format!(
                    " — managed ({} scope) · e edit · d delete · m models · t test",
                    if p.scope.is_empty() { "user" } else { &p.scope }
                ),
                t.text_muted,
            ),
        ])
    }
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
                col = col.child(line(vec![span(
                    format!("not configured yet (a adds one): {}", free.join(", ")),
                    t.text_faint,
                )]));
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
    let (create, existing, prefill) = match mode {
        ProfileFormMode::Create => (true, None, None),
        ProfileFormMode::Edit(p) => (false, Some(p), None),
        ProfileFormMode::Override(p) => (true, None, Some(p)),
    };
    let can_gateway_scope = store.profiles.with_untracked(|p| {
        p.ready()
            .map(|d| d.can_create_gateway_scope)
            .unwrap_or(false)
    });

    // Reset the shared discover slot so stale results never show.
    store.discover.set(Loadable::NotAsked);

    let ctx2 = ctx.clone();
    super::open_form_guarded(ctx, cx, Size::new(76, 28), move |mcx, close, guard| {
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
        let override_note: Option<String> = over.as_ref().map(|p| {
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
                    col.child(field(
                        &t,
                        "",
                        Checkbox::new("enabled")
                            .checked(enabled)
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
