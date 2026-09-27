//! Summon (create) an entity + manage spark templates — the web
//! console's "Summon entity" modal (`createEntity`, console.py) and its
//! "Templates" modal (`tplShowEditor` / `tplSave`).
//!
//! The birth is IRREVERSIBLE (no delete; spark v1-for-life), so the flow
//! is the web's exactly: pick a template + name → DRY-RUN validate
//! (`POST /entities/{name}/validate`) → a confirm that names the
//! permanence and carries the dry-run's warnings → `POST /entities` →
//! the optional admin configuration (substrate, per-phase capabilities),
//! each its own journaled write. The confirm is a second stage INSIDE
//! the form (a prompt over a modal is an engine hazard), so Back keeps
//! every typed value.

use abstracttui::prelude::*;
use abstracttui::widgets::Table;
use serde_json::{json, Value};

use super::util::{field, line, span, span_bold, wrap_text};
use super::{widths, Ctx};
use crate::api::entities::{create_body, CreationKit, TemplateRow, ENTITY_THINKING_LEVELS};
use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::entities::EntityCmd;
use crate::worker::Cmd;

/// Phase-row budget of the capability grid (signals must exist in the
/// modal scope before the matrix arrives — same rule as the manage
/// menu's tool-policy editor).
const PHASE_SLOTS: usize = 6;

/// (provider, its text models or the error) — the provider cascade.
type ModelCascade = Option<(String, Result<Vec<String>, String>)>;

const SUMMON_W: i32 = 100;
const SUMMON_H: i32 = 40;
/// Text width inside the summon dialog (modal margin + dress chrome).
const SUMMON_TEXT_W: usize = (SUMMON_W - 6) as usize;

/// True when the connected principal is an admin (tracked read).
pub(crate) fn is_admin(store: &Store) -> bool {
    store
        .conn
        .with(|c| matches!(c, ConnPhase::Connected(id) if id.admin))
}

fn kit_now(store: &Store) -> Option<CreationKit> {
    store.entity_kit.with_untracked(|k| k.ready().cloned())
}

/// Wrapped muted prose lines (never truncated).
fn prose(col: Element, text: &str, width: usize, ink: Rgba) -> Element {
    let mut col = col;
    for l in wrap_text(text, width) {
        col = col.child(line(vec![span(l, ink)]));
    }
    col
}

/// Load (re-read) the creation kit: templates, defaults, providers,
/// default capability grid. Opening re-reads — the web's rule.
fn reload_kit(ctx: &Ctx) {
    ctx.store.entity_kit.set(Loadable::Loading);
    ctx.send(Cmd::Entity(EntityCmd::LoadCreationKit));
}

/// Load phase of the creation kit, mirrored into the form's own signal
/// so regions re-render on the PHASE (not on every cascade update — a
/// region that regenerates drops the keyboard focus of any widget in it).
fn kit_phase(store: &Store) -> u8 {
    store.entity_kit.with(|k| match k {
        Loadable::Ready(_) => 1,
        Loadable::Failed(_) => 2,
        _ => 0,
    })
}

/// The summon form (`n` on the Users & Entities screen).
///
/// FOCUS LAW: every focusable widget lives in a region that re-renders
/// only when its own options change, and the button row is static — a
/// regenerated widget loses the keyboard focus (and with it Esc and the
/// dirty guard), which the first pty drive caught.
pub fn open_summon_form(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    reload_kit(ctx);
    store.entity_check.set(Loadable::NotAsked);
    let ctx2 = ctx.clone();
    super::open_form_guarded(
        ctx,
        cx,
        Size::new(SUMMON_W, SUMMON_H),
        move |mcx, close, guard| {
            let theme = use_theme(mcx);
            let t0 = theme.get().tokens;
            let tpl_ix = mcx.signal(0usize);
            let name = mcx.signal(String::new());
            let advanced = mcx.signal(false);
            let prov_ix = mcx.signal(0usize);
            let model_ix = mcx.signal(0usize);
            let think_ix = mcx.signal(0usize);
            let emb_ix = mcx.signal(0usize);
            let phase_sigs: std::rc::Rc<Vec<Signal<Vec<String>>>> =
                std::rc::Rc::new((0..PHASE_SLOTS).map(|_| mcx.signal(Vec::new())).collect());
            let matrix_filled = mcx.signal(false);
            let phase = mcx.signal(0u8);
            let models = mcx.signal(ModelCascade::None);
            // 0 = the form · 1 = validating (dry-run in flight) · 2 = confirm.
            let stage = mcx.signal(0u8);
            // The exact (name, body) the dry-run judged — the birth
            // creates THAT, never a later edit of the fields.
            let pending = mcx.signal(Option::<(String, Value)>::None);
            let form_error = mcx.signal(Option::<String>::None);
            let in_flight = mcx.signal(false);
            let esc_armed = mcx.signal(false);
            let form_id = crate::worker::next_form_id();

            super::install_dirty_guard_with(
                mcx,
                &guard,
                move || !name.get_untracked().trim().is_empty(),
                move || {
                    let _ = name.get();
                },
                esc_armed,
                form_error,
            );
            super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

            // Mirror the kit's phase + the model cascade into form signals
            // (written only on change).
            mcx.effect(move || {
                let p = kit_phase(&store);
                if phase.get_untracked() != p {
                    phase.set(p);
                }
                let m = store
                    .entity_kit
                    .with(|k| k.ready().and_then(|k| k.models.clone()));
                if models.with_untracked(|cur| *cur != m) {
                    models.set(m);
                }
            });

            // One-shot fill of the capability grid with the defaults.
            {
                let sigs = phase_sigs.clone();
                mcx.effect(move || {
                    if matrix_filled.get() || phase.get() != 1 {
                        return;
                    }
                    if let Some(m) = kit_now(&store).and_then(|k| k.matrix) {
                        for (i, d) in m.defaults.iter().take(PHASE_SLOTS).enumerate() {
                            sigs[i].set(d.clone());
                        }
                    }
                    matrix_filled.set(true);
                });
            }

            // The dry-run verdict: green → the confirm stage; red → the
            // web's refusal sentence, nothing written.
            mcx.effect(move || {
                let chk = store.entity_check.get();
                if stage.get_untracked() != 1 {
                    return;
                }
                let want = pending.with_untracked(|p| p.as_ref().map(|(n, _)| n.clone()));
                match chk {
                    Loadable::Ready(c) if Some(c.name.clone()) == want => {
                        if c.ok {
                            form_error.set(None);
                            stage.set(2);
                        } else {
                            form_error.set(Some(c.refusal()));
                            stage.set(0);
                        }
                    }
                    Loadable::Failed(e) => {
                        form_error.set(Some(format!("validation call failed: {e}")));
                        stage.set(0);
                    }
                    _ => {}
                }
            });

            // Editing what was validated drops back to the form: the
            // confirm must always describe exactly what will be born.
            mcx.effect(move || {
                let _ = (name.get(), tpl_ix.get(), emb_ix.get());
                if stage.get_untracked() == 2 {
                    stage.set(0);
                }
            });

            let ctx_v = ctx2.clone();
            let ctx_s = ctx2.clone();
            let close_c = close.clone();
            let sigs_btn = phase_sigs.clone();
            let sigs_adv = phase_sigs.clone();
            let ctx_adv = ctx2.clone();

            let intro = prose(
                Element::new().style(LayoutStyle::column().gap(0)),
                "Pick a spark template and name it. The name is permanent — there is no delete (spark v1-for-life), so it is validated (dry-run) before anything is written.",
                SUMMON_TEXT_W,
                t0.text_faint,
            )
            .build();

            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(line(vec![span_bold("Summon a new entity", t0.accent)]))
                .child(intro)
                .child(field(
                    &t0,
                    "name",
                    TextInput::new()
                        .value(name)
                        .placeholder("e.g. Castor — permanent")
                        .placeholder_while_focused(true)
                        .layout(LayoutStyle::default().w(40).h(1))
                        .element(mcx, &t0)
                        .autofocus()
                        .build(),
                ))
                // The template picker (re-renders on the kit's phase only).
                .child(dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                    let t = theme.get().tokens;
                    match phase.get() {
                        1 => {
                            let k = kit_now(&store).unwrap_or_default();
                            if k.templates.is_empty() {
                                return line(vec![span(
                                    "no spark templates served by this gateway — nothing to summon from",
                                    t.error,
                                )]);
                            }
                            let opts: Vec<SelectOption> = k
                                .templates
                                .iter()
                                .map(|tp| SelectOption::keyed(tp.id.clone(), tp.name.clone()))
                                .collect();
                            field(
                                &t,
                                "template",
                                Select::new(opts)
                                    .value(tpl_ix)
                                    .layout(LayoutStyle::default().w(40).h(1).shrink(0.0))
                                    .element(gcx, &t)
                                    .build(),
                            )
                        }
                        2 => match store.entity_kit.get_untracked() {
                            Loadable::Failed(e) => super::util::error_panel_hint(
                                &t,
                                &e,
                                Some("close and reopen this dialog to retry (opening re-reads)"),
                            ),
                            _ => line(vec![span("templates unavailable", t.error)]),
                        },
                        _ => line(vec![span("⟳ loading templates…", t.info)]),
                    }
                }))
                // The selected template's description + locked values.
                .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    let t = theme.get().tokens;
                    let _ = phase.get();
                    let ix = tpl_ix.get();
                    let Some(k) = kit_now(&store) else {
                        return Element::new().style(LayoutStyle::default().h(0)).build();
                    };
                    let Some(tp) = k.templates.get(ix) else {
                        return Element::new().style(LayoutStyle::default().h(0)).build();
                    };
                    let mut col = Element::new().style(LayoutStyle::column().gap(0));
                    let desc = tp.description_line();
                    if !desc.is_empty() {
                        col = col.child(line(vec![span(desc, t.text_muted)]));
                    }
                    if !tp.core_values.is_empty() {
                        col = col.child(line(vec![
                            span("core values (locked for life): ", t.text_faint),
                            span(tp.core_values.join(" · "), t.text),
                        ]));
                    }
                    for w in &k.template_warnings {
                        col = col.child(line(vec![span(w.clone(), t.warn)]));
                    }
                    col.build()
                }))
                // Advanced configuration — admin only (the web hides it
                // for everyone else and says why).
                .child(dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                    let t = theme.get().tokens;
                    if !is_admin(&store) {
                        return prose(
                            Element::new().style(LayoutStyle::column().gap(0)),
                            "Advanced configuration (substrate, per-phase capabilities) requires an admin session — entities you create carry the safe framework defaults; an admin can configure them after.",
                            SUMMON_TEXT_W,
                            t.text_faint,
                        )
                        .build();
                    }
                    field(
                        &t,
                        "",
                        Checkbox::new("Advanced configuration (optional — defaults are safe)")
                            .checked(advanced)
                            .element(gcx, &t)
                            .build(),
                    )
                }))
                .child(dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                    let t = theme.get().tokens;
                    if !advanced.get() || !is_admin(&store) {
                        return Element::new().style(LayoutStyle::default().h(0)).build();
                    }
                    if phase.get() != 1 {
                        return line(vec![span("⟳ loading the gateway defaults…", t.info)]);
                    }
                    let k = kit_now(&store).unwrap_or_default();
                    advanced_section(
                        gcx, theme, &t, &ctx_adv, &k, prov_ix, model_ix, think_ix, emb_ix, models,
                        &sigs_adv,
                    )
                }))
                .child(super::message_slot(theme, form_error, in_flight))
                // The confirm stage: the permanence, named, with the
                // dry-run's warnings reviewed BEFORE the birth.
                .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    let t = theme.get().tokens;
                    match stage.get() {
                        1 => line(vec![span(
                            "⟳ validating (dry-run — nothing is written)…",
                            t.info,
                        )]),
                        2 => {
                            let nm = pending
                                .with(|p| p.as_ref().map(|(n, _)| n.clone()))
                                .unwrap_or_default();
                            let warnings = store
                                .entity_check
                                .with(|c| c.ready().map(|c| c.warnings.clone()))
                                .unwrap_or_default();
                            let mut col = Element::new()
                                .style(LayoutStyle::column().gap(0))
                                .child(line(vec![span_bold(format!("Summon {nm}?"), t.warn)]));
                            col = prose(
                                col,
                                &format!("This creates a permanent entity named \"{nm}\". There is no delete — the name and its home are for life. Its spark's core values are locked. Substrate and per-phase capabilities can be changed later."),
                                SUMMON_TEXT_W,
                                t.text,
                            );
                            if !warnings.is_empty() {
                                col = col.child(line(vec![span(
                                    "Validation warnings (review before summoning):",
                                    t.warn,
                                )]));
                                for w in &warnings {
                                    col = prose(col, &format!("• {w}"), SUMMON_TEXT_W, t.warn);
                                }
                            }
                            col = col.child(line(vec![span(
                                "Summon creates it · Back to the form edits (nothing is written until Summon)",
                                t.text_faint,
                            )]));
                            col.build()
                        }
                        _ => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                }))
                // STATIC button row (focus law): each verb guards its own
                // stage instead of the row being rebuilt per stage.
                .child(
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                        .child(
                            Button::new("Validate & create")
                                .on_click(move || {
                                    if in_flight.get_untracked() || stage.get_untracked() == 1 {
                                        return;
                                    }
                                    validate(&ctx_v, tpl_ix, name, emb_ix, stage, pending, form_error)
                                })
                                .element(mcx, &t0)
                                .build(),
                        )
                        .child(
                            Button::new("Summon")
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    if stage.get_untracked() != 2 {
                                        form_error.set(Some(
                                            "Validate first — the dry-run must pass before a summon."
                                                .into(),
                                        ));
                                        return;
                                    }
                                    summon(
                                        &ctx_s, &sigs_btn, pending, advanced, prov_ix, model_ix,
                                        think_ix, form_id, in_flight, form_error,
                                    );
                                })
                                .element(mcx, &t0)
                                .build(),
                        )
                        .child(
                            Button::new("Back to the form")
                                .on_click(move || {
                                    if !in_flight.get_untracked() {
                                        stage.set(0);
                                    }
                                })
                                .element(mcx, &t0)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_c())
                                .element(mcx, &t0)
                                .build(),
                        )
                        .build(),
                )
                .build()
        },
    );
}

/// The Advanced block: substrate (provider → model cascade, reasoning),
/// the birth embedder, the per-phase capability grid. The model picker
/// and the embedder warning live in their own regions, so picking a
/// provider never rebuilds (and unfocuses) the provider picker.
#[allow(clippy::too_many_arguments)]
fn advanced_section(
    gcx: Scope,
    theme: Signal<&'static abstracttui::theme::Theme>,
    t: &TokenSet,
    ctx: &Ctx,
    k: &CreationKit,
    prov_ix: Signal<usize>,
    model_ix: Signal<usize>,
    think_ix: Signal<usize>,
    emb_ix: Signal<usize>,
    models: Signal<ModelCascade>,
    sigs: &std::rc::Rc<Vec<Signal<Vec<String>>>>,
) -> View {
    let providers = k.providers.clone();
    let prov_opts: Vec<SelectOption> =
        std::iter::once(SelectOption::new(k.substrate_default_label()))
            .chain(
                providers
                    .iter()
                    .map(|p| SelectOption::keyed(p.clone(), p.clone())),
            )
            .collect();
    let emb_opts: Vec<SelectOption> =
        std::iter::once(SelectOption::new(k.embedding_default_label()))
            .chain(
                k.embedding_models
                    .iter()
                    .map(|m| SelectOption::keyed(m.clone(), m.clone())),
            )
            .collect();
    let think_opts: Vec<SelectOption> = std::iter::once(SelectOption::new("not set"))
        .chain(ENTITY_THINKING_LEVELS.iter().map(|l| SelectOption::new(*l)))
        .collect();
    let ctx_p = ctx.clone();
    let providers_cb = providers.clone();
    let providers_m = providers.clone();
    let emb_models = k.embedding_models.clone();
    let default_emb = k
        .default_embedding
        .as_ref()
        .map(|(_, m)| m.clone())
        .unwrap_or_default();
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![
            span_bold("Substrate ", t.accent),
            span(
                "the mind: LLM provider & model — leave on Gateway default to inherit",
                t.text_faint,
            ),
        ]))
        .child(field(
            t,
            "provider",
            Select::new(prov_opts)
                .value(prov_ix)
                .on_change(move |ix| {
                    // The law: a provider switch resets the model —
                    // never a fabricated pair.
                    model_ix.set(0);
                    if let Some(p) = ix.checked_sub(1).and_then(|i| providers_cb.get(i)) {
                        ctx_p.send(Cmd::Entity(EntityCmd::LoadCreateModels {
                            provider: p.clone(),
                        }));
                    }
                })
                .layout(LayoutStyle::default().w(48).h(1).shrink(0.0))
                .element(gcx, t)
                .build(),
        ))
        .child(dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |mcx| {
            let t = theme.get().tokens;
            let chosen = prov_ix
                .get()
                .checked_sub(1)
                .and_then(|i| providers_m.get(i).cloned());
            let (opts, disabled, note) = match &chosen {
                None => (vec![SelectOption::new("Gateway default")], true, None),
                Some(p) => match models.get() {
                    Some((mp, Ok(list))) if mp == *p => (
                        std::iter::once(SelectOption::new("Provider default"))
                            .chain(list.iter().map(|m| SelectOption::keyed(m.clone(), m.clone())))
                            .collect(),
                        false,
                        None,
                    ),
                    Some((mp, Err(e))) if mp == *p => (
                        vec![SelectOption::new("Provider default")],
                        false,
                        Some(format!("(models unavailable — set on manage): {e}")),
                    ),
                    _ => (
                        vec![SelectOption::new("Provider default")],
                        false,
                        Some("⟳ loading models…".to_string()),
                    ),
                },
            };
            let mut c = Element::new().style(LayoutStyle::column().gap(0)).child(field(
                &t,
                "model",
                Select::new(opts)
                    .value(model_ix)
                    .disabled(disabled)
                    .layout(LayoutStyle::default().w(48).h(1).shrink(0.0))
                    .element(mcx, &t)
                    .build(),
            ));
            if let Some(n) = note {
                c = c.child(line(vec![span(n, t.text_muted)]));
            }
            c.build()
        }))
        .child(field(
            t,
            "reasoning",
            Select::new(think_opts)
                .value(think_ix)
                .layout(LayoutStyle::default().w(20).h(1).shrink(0.0))
                .element(gcx, t)
                .build(),
        ))
        .child(field(
            t,
            "embedding at birth",
            Select::new(emb_opts)
                .value(emb_ix)
                .layout(LayoutStyle::default().w(48).h(1).shrink(0.0))
                .element(gcx, t)
                .build(),
        ))
        // A non-default birth embedder refuses unless the door serves
        // it — warn on selection (validate catches it before the name
        // burns).
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            let t = theme.get().tokens;
            let chosen = emb_ix.get().checked_sub(1).and_then(|i| emb_models.get(i).cloned());
            match chosen {
                Some(c) if c != default_emb => prose(
                    Element::new().style(LayoutStyle::column().gap(0)),
                    &format!(
                        "#FALLBACK embedding {c} differs from the gateway's resolved embedder ({}) — set it as the gateway embedding route first, or the home refuses vector ops. Validate will catch this before the name is burned.",
                        if default_emb.is_empty() { "none" } else { default_emb.as_str() }
                    ),
                    SUMMON_TEXT_W,
                    t.warn,
                )
                .build(),
                _ => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        }));
    for n in &k.notes {
        col = prose(col, n, SUMMON_TEXT_W, t.text_muted);
    }
    col = col.child(line(vec![
        span_bold("Per-phase capabilities ", t.accent),
        span(
            "which tools each phase may use — defaults shown; change a phase to override",
            t.text_faint,
        ),
    ]));
    match (&k.matrix, &k.matrix_error) {
        (Some(m), _) => {
            let opts: Vec<SelectOption> = m
                .tools
                .iter()
                .map(|(id, label)| SelectOption::keyed(id.clone(), label.clone()))
                .collect();
            for (i, (_, label)) in m.phases.iter().take(PHASE_SLOTS).enumerate() {
                col = col.child(field(
                    t,
                    label,
                    MultiSelect::new(opts.clone())
                        .values(sigs[i])
                        .placeholder("no tools (cleared = framework default)")
                        .layout(LayoutStyle::default().w(60).h(1).shrink(0.0))
                        .element(gcx, t)
                        .build(),
                ));
            }
            if m.phases.len() > PHASE_SLOTS {
                col = col.child(line(vec![span(
                    format!(
                        "{} more phase(s) keep their defaults here — set them after, from the manage menu's tool policy",
                        m.phases.len() - PHASE_SLOTS
                    ),
                    t.warn,
                )]));
            }
        }
        (None, Some(e)) => col = col.child(line(vec![span(e.clone(), t.error)])),
        (None, None) => {}
    }
    col.build()
}

/// Validate: build the web's `createBody`, then the dry-run.
fn validate(
    ctx: &Ctx,
    tpl_ix: Signal<usize>,
    name: Signal<String>,
    emb_ix: Signal<usize>,
    stage: Signal<u8>,
    pending: Signal<Option<(String, Value)>>,
    form_error: Signal<Option<String>>,
) {
    let Some(kit) = kit_now(&ctx.store) else {
        form_error.set(Some("templates not loaded yet".into()));
        return;
    };
    let nm = name.get_untracked().trim().to_string();
    if nm.is_empty() {
        form_error.set(Some("Name is required.".into()));
        return;
    }
    let Some(tp) = kit.templates.get(tpl_ix.get_untracked()) else {
        form_error.set(Some("Pick a template.".into()));
        return;
    };
    // The embedder choice lives under Advanced (admin); a non-admin
    // form never carries one.
    let emb = if is_admin_untracked(&ctx.store) {
        emb_ix
            .get_untracked()
            .checked_sub(1)
            .and_then(|i| kit.embedding_models.get(i).cloned())
            .unwrap_or_default()
    } else {
        String::new()
    };
    let body = create_body(&nm, &tp.spark, &emb);
    form_error.set(None);
    pending.set(Some((nm.clone(), body.clone())));
    ctx.store.entity_check.set(Loadable::Loading);
    stage.set(1);
    ctx.send(Cmd::Entity(EntityCmd::ValidateEntity {
        name: nm,
        body: body.into(),
    }));
}

fn is_admin_untracked(store: &Store) -> bool {
    store
        .conn
        .with_untracked(|c| matches!(c, ConnPhase::Connected(id) if id.admin))
}

/// Summon: the validated body, plus the optional admin configuration.
#[allow(clippy::too_many_arguments)]
fn summon(
    ctx: &Ctx,
    sigs: &std::rc::Rc<Vec<Signal<Vec<String>>>>,
    pending: Signal<Option<(String, Value)>>,
    advanced: Signal<bool>,
    prov_ix: Signal<usize>,
    model_ix: Signal<usize>,
    think_ix: Signal<usize>,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
) {
    let Some((nm, body)) = pending.get_untracked() else {
        form_error.set(Some("validate first".into()));
        return;
    };
    let kit = kit_now(&ctx.store).unwrap_or_default();
    let warnings = ctx
        .store
        .entity_check
        .with_untracked(|c| c.ready().map(|c| c.warnings.clone()))
        .unwrap_or_default();
    let mut substrate = None;
    let mut policy = None;
    let mut extra = String::new();
    if advanced.get_untracked() && is_admin_untracked(&ctx.store) {
        let provider = prov_ix
            .get_untracked()
            .checked_sub(1)
            .and_then(|i| kit.providers.get(i).cloned());
        let model = match (&provider, &kit.models) {
            (Some(p), Some((mp, Ok(models)))) if mp == p => model_ix
                .get_untracked()
                .checked_sub(1)
                .and_then(|i| models.get(i).cloned()),
            _ => None,
        };
        let thinking = think_ix
            .get_untracked()
            .checked_sub(1)
            .map(|i| ENTITY_THINKING_LEVELS[i].to_string());
        match (provider, model) {
            (Some(p), Some(m)) => {
                let mut sub = json!({ "provider": p, "model": m });
                if let Some(th) = thinking {
                    sub["thinking"] = Value::String(th);
                }
                substrate = Some(sub.into());
            }
            (Some(_), None) | (None, Some(_)) => {
                extra.push_str(" (substrate needs BOTH provider and model — skipped)")
            }
            (None, None) => {
                if thinking.is_some() {
                    extra.push_str(
                        " (reasoning effort needs a provider and model chosen — not applied)",
                    );
                }
            }
        }
        if let Some(m) = &kit.matrix {
            let current: Vec<Vec<String>> = (0..m.phases.len().min(PHASE_SLOTS))
                .map(|i| sigs[i].get_untracked())
                .collect();
            let delta = m.delta(&current);
            if !delta.is_empty() {
                policy = Some(json!({ "policy": Value::Object(delta) }).into());
            }
        }
    }
    form_error.set(None);
    in_flight.set(true);
    ctx.send(Cmd::Entity(EntityCmd::CreateEntity {
        name: nm,
        body: body.into(),
        substrate,
        policy,
        warnings,
        extra_note: extra,
        form_id: Some(form_id),
    }));
}

// ---------------------------------------------------------------------
// Spark templates: view / edit / new (versioned)
// ---------------------------------------------------------------------

const TPL_W: i32 = 92;

/// The templates modal (`s` on the Users & Entities screen).
pub fn open_templates_modal(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    reload_kit(ctx);
    let ctx2 = ctx.clone();
    let screen_cx = cx;
    super::open_form(ctx, cx, Size::new(TPL_W, 24), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let sel = mcx.signal(0usize);
        super::util::clamp_selection(mcx, sel, move || {
            store
                .entity_kit
                .with(|k| k.ready().map(|k| k.templates.len()).unwrap_or(0))
        });
        // Version history follows the selection (operator templates).
        {
            let ctx_v = ctx2.clone();
            let asked = mcx.signal(String::new());
            mcx.effect(move || {
                let i = sel.get();
                let target = store.entity_kit.with(|k| {
                    k.ready()
                        .and_then(|k| k.templates.get(i))
                        .filter(|t| t.source == "operator")
                        .map(|t| t.id.clone())
                });
                if let Some(id) = target {
                    if asked.get_untracked() != id {
                        asked.set(id.clone());
                        ctx_v.send(Cmd::Entity(EntityCmd::LoadTemplateVersions { id }));
                    }
                }
            });
        }
        let ctx_b = ctx2.clone();
        let close_b = close.clone();
        let intro = prose(
            Element::new().style(LayoutStyle::column().gap(0)),
            "A template is a reusable blueprint — every save is a new version; the framework default is the floor and can be seeded but not edited. Editing a template never touches a living entity.",
            (TPL_W - 6) as usize,
            t0.text_faint,
        )
        .build();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Spark templates", t0.accent)]))
            .child(intro)
            .child(dyn_view_scoped(
                LayoutStyle::default().grow(1.0).min_h(3),
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_kit.get() {
                        Loadable::Ready(k) if k.templates.is_empty() => line(vec![span(
                            "no templates served by this gateway",
                            t.text_muted,
                        )]),
                        Loadable::Ready(k) => {
                            let vw = TPL_W.min(abstracttui::app::use_viewport(gcx).get().w) - 4;
                            let mut rows: Vec<Vec<String>> = k
                                .templates
                                .iter()
                                .map(|tp| {
                                    vec![
                                        tp.id.clone(),
                                        tp.name.clone(),
                                        tp.source.clone(),
                                        tp.version
                                            .map(|v| format!("v{v}"))
                                            .unwrap_or_else(|| "—".into()),
                                        if tp.editable {
                                            "yes".into()
                                        } else {
                                            "no".into()
                                        },
                                    ]
                                })
                                .collect();
                            let rules = [
                                widths::ColRule::tail("id", 12),
                                widths::ColRule::head("name", 12),
                                widths::ColRule::head("source", 8),
                                widths::ColRule::head("version", 7),
                                widths::ColRule::head("editable", 8),
                            ];
                            let cols = widths::columns(&rules, &mut rows, vw);
                            Table::new(cols)
                                .rows(rows)
                                .selection(sel)
                                .layout(LayoutStyle::default().grow(1.0))
                                .element(gcx, &t)
                                .autofocus()
                                .build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(
                            &t,
                            &e,
                            Some("close and reopen this dialog to retry (opening re-reads)"),
                        ),
                        _ => line(vec![span("⟳ loading templates…", t.info)]),
                    }
                },
            ))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                let i = sel.get();
                let text = store.entity_kit.with(|k| {
                    let k = k.ready()?;
                    let tp = k.templates.get(i)?;
                    match &k.versions {
                        Some((id, Ok(l))) if *id == tp.id => Some(l.clone()),
                        Some((id, Err(e))) if *id == tp.id => {
                            Some(format!("versions unavailable: {e}"))
                        }
                        _ if tp.source == "operator" => Some("⟳ reading versions…".into()),
                        _ => Some(format!("{} — view-only (seed a new id from it)", tp.source)),
                    }
                });
                line(vec![span(text.unwrap_or_default(), t.text_muted)])
            }))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let i = sel.get();
                    let selected: Option<TemplateRow> = store
                        .entity_kit
                        .with(|k| k.ready().and_then(|k| k.templates.get(i).cloned()));
                    let editable = selected.as_ref().map(|t| t.editable).unwrap_or(false);
                    let mk = |label: &str, mode: TplMode, disabled: bool| {
                        let c = ctx_b.clone();
                        let cl = close_b.clone();
                        let sel_tp = selected.clone();
                        Button::new(label)
                            .disabled(disabled)
                            .on_click(move || {
                                let Some(tp) = sel_tp.clone() else {
                                    c.store.notice.set(Some("Pick a template first.".into()));
                                    return;
                                };
                                cl();
                                open_template_editor(screen_cx, &c, tp, mode);
                            })
                            .element(bcx, &t)
                            .build()
                    };
                    let close_c = close_b.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(mk("View", TplMode::View, selected.is_none()))
                        .child(mk("Edit", TplMode::Edit, !editable))
                        .child(mk("New from selected", TplMode::New, selected.is_none()))
                        .child(
                            Button::new("Close (Esc)")
                                .on_click(move || close_c())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TplMode {
    View,
    Edit,
    New,
}

/// The template editor: View (read-only spark), Edit (operator
/// templates; saves a new version), New (seeded from the selected one).
pub fn open_template_editor(cx: Scope, ctx: &Ctx, tp: TemplateRow, mode: TplMode) {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    super::open_form_guarded(ctx, cx, Size::new(TPL_W, 34), move |mcx, close, guard| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let spark_text = serde_json::to_string_pretty(&tp.spark).unwrap_or_else(|_| "{}".into());
        let id = mcx.signal(String::new());
        let name0 = if mode == TplMode::New {
            String::new()
        } else {
            tp.name.clone()
        };
        let desc0 = if mode == TplMode::New {
            String::new()
        } else {
            tp.description.clone()
        };
        let tname = mcx.signal(name0.clone());
        let desc = mcx.signal(desc0.clone());
        let spark = TextAreaState::new(mcx);
        spark.set_text(spark_text.clone());
        let spark_sig = spark.value();
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let esc_armed = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        if mode != TplMode::View {
            let st0 = spark_text.clone();
            super::install_dirty_guard_with(
                mcx,
                &guard,
                move || {
                    !id.get_untracked().is_empty()
                        || tname.get_untracked() != name0
                        || desc.get_untracked() != desc0
                        || spark_sig.get_untracked() != st0
                },
                move || {
                    let _ = (id.get(), tname.get(), desc.get(), spark_sig.get());
                },
                esc_armed,
                form_error,
            );
        }
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        if tp.source == "operator" && mode != TplMode::New {
            ctx2.send(Cmd::Entity(EntityCmd::LoadTemplateVersions {
                id: tp.id.clone(),
            }));
        }
        let title = match mode {
            TplMode::View => format!("Template '{}' — view", tp.id),
            TplMode::Edit => format!("Template '{}' — edit (saves as a new version)", tp.id),
            TplMode::New => format!("New template — seeded from '{}'", tp.id),
        };
        let ctx_save = ctx2.clone();
        let close_c = close.clone();
        let tp_save = tp.clone();
        let tp_id = tp.id.clone();
        let mut col = Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(title, t0.accent)]));
        if mode == TplMode::New {
            col = col.child(field(
                &t0,
                "new template id",
                TextInput::new()
                    .value(id)
                    .placeholder("lowercase-letters-digits-_- (e.g. researcher)")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(48).h(1))
                    .element(mcx, &t0)
                    .build(),
            ));
        }
        if mode == TplMode::View {
            col = col
                .child(line(vec![span(format!("name: {}", tp.name), t0.text)]))
                .child(line(vec![span(
                    format!("description: {}", tp.description),
                    t0.text_muted,
                )]));
            let mut body = Element::new().style(LayoutStyle::column().gap(0));
            for l in spark_text.lines() {
                body = body.child(line(vec![span(l.to_string(), t0.text)]));
            }
            col = col.child(
                Scroll::new(body.build())
                    .layout(LayoutStyle::default().grow(1.0).min_h(4))
                    .element(mcx, &t0)
                    .build(),
            );
        } else {
            col = col
                .child(field(
                    &t0,
                    "display name",
                    TextInput::new()
                        .value(tname)
                        .placeholder("e.g. Researcher")
                        .layout(LayoutStyle::default().w(48).h(1))
                        .element(mcx, &t0)
                        .build(),
                ))
                .child(field(
                    &t0,
                    "description",
                    TextInput::new()
                        .value(desc)
                        .placeholder("what this blueprint is for")
                        .layout(LayoutStyle::default().w(64).h(1))
                        .element(mcx, &t0)
                        .build(),
                ))
                .child(line(vec![span(
                    "spark (JSON — core values are enforced at save; Enter inserts a newline)",
                    t0.text_faint,
                )]))
                .child(
                    TextArea::new()
                        .state(&spark)
                        .submit_policy(abstracttui::widgets::SubmitPolicy::EnterInserts)
                        .rows(8, 18)
                        .layout(LayoutStyle::default().grow(1.0))
                        .element(mcx, &t0)
                        .build(),
                );
        }
        col = col.child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let t = theme.get().tokens;
            let text =
                store
                    .entity_kit
                    .with(|k| match k.ready().and_then(|k| k.versions.clone()) {
                        Some((vid, Ok(l))) if vid == tp_id => l,
                        Some((vid, Err(e))) if vid == tp_id => format!("versions unavailable: {e}"),
                        _ => String::new(),
                    });
            line(vec![span(text, t.text_muted)])
        }));
        col = col.child(super::message_slot(theme, form_error, in_flight));
        col.child(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |bcx| {
                let t = theme.get().tokens;
                let busy = in_flight.get();
                let admin = is_admin(&store);
                let ctx_s = ctx_save.clone();
                let close_x = close_c.clone();
                let tp_s = tp_save.clone();
                let mut row = Element::new().style(LayoutStyle::row().gap(2));
                if mode != TplMode::View {
                    row = row.child(
                        Button::new("Save")
                            .disabled(busy || !admin)
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                let parsed: Result<Value, _> =
                                    serde_json::from_str(&spark_sig.get_untracked());
                                let spark_v = match parsed {
                                    Ok(v) if v.is_object() => v,
                                    Ok(_) => {
                                        form_error.set(Some("Spark must be a JSON object.".into()));
                                        return;
                                    }
                                    Err(e) => {
                                        form_error.set(Some(format!("Spark is not valid JSON: {e}")));
                                        return;
                                    }
                                };
                                let (tid, create, note) = if mode == TplMode::New {
                                    let nid = id.get_untracked().trim().to_string();
                                    if nid.is_empty() {
                                        form_error.set(Some("A new template needs an id.".into()));
                                        return;
                                    }
                                    (nid, true, "created via console")
                                } else {
                                    if tp_s.source != "operator" {
                                        form_error.set(Some("Only operator templates can be edited (the builtin is the floor — use New to seed one).".into()));
                                        return;
                                    }
                                    (tp_s.id.clone(), false, "edited via console")
                                };
                                let body = json!({
                                    "id": tid,
                                    "spark": spark_v,
                                    "name": tname.get_untracked().trim(),
                                    "description": desc.get_untracked().trim(),
                                    "note": note,
                                });
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_s.send(Cmd::Entity(EntityCmd::SaveTemplate {
                                    id: tid,
                                    create,
                                    body: body.into(),
                                    form_id: Some(form_id),
                                }));
                            })
                            .element(bcx, &t)
                            .build(),
                    );
                }
                row = row.child(
                    Button::new(if mode == TplMode::View { "Close (Esc)" } else { "Cancel (Esc)" })
                        .on_click(move || close_x())
                        .element(bcx, &t)
                        .build(),
                );
                if mode != TplMode::View && !admin {
                    row = row.child(line(vec![span(
                        "saving a template is admin-only (it seeds every entity summoned from it)",
                        t.warn,
                    )]));
                }
                row.build()
            },
        ))
        .build()
    });
}
