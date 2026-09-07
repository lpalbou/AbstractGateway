//! Per-entity configuration + state controls — the TUI half of the web
//! console's Manage drawer (parity audit items a-1..a-8).
//!
//! Scope law: entity CREATION, summoning and visits stay deliberately
//! out (rituals, not configuration). What lives here is exactly what an
//! operator changes on an existing entity: state (wake/sleep/pause),
//! mind substrate, voice triple, work order, own-time grant + loop,
//! re-embed, verify.

use abstracttui::prelude::*;
use abstracttui::widgets::{ColWidth, Column, SubmitPolicy, Table};
use serde_json::{json, Value};

use super::util::{field, line, span, span_bold};
use super::{open_form, Ctx};
use crate::store::{EntityDetail, EntityRow, Loadable};
use crate::worker::Cmd;

/// The action menu for one entity. Fires the detail load first so the
/// sub-forms open warm (four quick GETs on the worker).
pub fn open_manage_menu(cx: Scope, ctx: &Ctx, entity: EntityRow) {
    let store = ctx.store;
    // (Re)load the snapshot for THIS entity unless it is already warm.
    let warm = store
        .entity_detail
        .with_untracked(|d| d.ready().map(|d| d.name == entity.name).unwrap_or(false));
    if !warm {
        store.entity_detail.set(Loadable::Loading);
        ctx.send(Cmd::LoadEntityDetail {
            name: entity.name.clone(),
        });
    }

    let ctx2 = ctx.clone();
    let name = entity.name.clone();
    let state_now = entity.state.clone();
    super::open_prompt(
        cx,
        ctx.ui,
        abstracttui::app::ChoicePrompt::new(format!(
            "Manage entity '{}' (currently {}) — creation/summon/visits stay outside this console.",
            name, state_now
        ))
        .option("state", "State — wake / sleep / pause")
        .option("substrate", "Mind substrate (provider / model)")
        .option("voice", "Voice (provider / model / voice)")
        .option("work", "Work order (set / clear)")
        .option("owntime", "Own time (grant + loop)")
        .option("tools", "Tool policy (per-phase grants)")
        .option("prompt", "Prompt overlay (edit layers)")
        .option("candidates", "Candidates review (sleep consolidation)")
        .option_detail(
            "reembed",
            "Re-embed the home (repair)",
            "vector-index rewrite; takes the home lease — not advised unless repairing",
        )
        .option("verify", "Verify chain / spark / manifest")
        .initial("state"),
        move |outcome| {
            if let abstracttui::app::ChoiceOutcome::Answered(a) = outcome {
                let pick = a.selected.first().cloned().unwrap_or_default();
                match pick.as_str() {
                    "state" => open_state_modal(cx, &ctx2, entity.clone()),
                    "substrate" => open_substrate_form(cx, &ctx2, entity.name.clone()),
                    "voice" => open_voice_form(cx, &ctx2, entity.name.clone()),
                    "work" => open_work_order_form(cx, &ctx2, entity.name.clone()),
                    "owntime" => open_own_time_modal(cx, &ctx2, entity.name.clone()),
                    "tools" => open_tool_policy_form(cx, &ctx2, entity.name.clone()),
                    "prompt" => open_prompt_editor(cx, &ctx2, entity.name.clone()),
                    "candidates" => open_candidates_modal(cx, &ctx2, entity.name.clone()),
                    "reembed" => open_reembed_form(cx, &ctx2, entity.name.clone()),
                    "verify" => ctx2.send(Cmd::EntityVerify {
                        name: entity.name.clone(),
                    }),
                    _ => {}
                }
            }
        },
    );
}

/// The selected entity's manage snapshot when it matches `name`.
fn detail_for(store: &crate::store::Store, name: &str) -> Option<EntityDetail> {
    store
        .entity_detail
        .with_untracked(|d| d.ready().filter(|d| d.name == name).cloned())
}

/// The right-drawer entity inspector: the full manage snapshot with
/// ROOM (the inline strip this replaces had three rows) — glanceable
/// beside the live roster (DrawerFocus::Passive keeps the keyboard on
/// the tables). Pure read; actions stay on `m`'s manage menu.
pub fn inspector_view(
    _cx: Scope,
    ctx: &Ctx,
    theme: Signal<&'static abstracttui::theme::Theme>,
) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    dyn_view_scoped(LayoutStyle::default().grow(1.0), move |gcx| {
        let t = theme.get().tokens;
        let idx = ui.entity_sel.get();
        let row = store
            .entities
            .with(|d| d.ready().and_then(|d| d.get(idx).cloned()));
        let Some(row) = row else {
            return line(vec![span("no entity selected", t.text_muted)]);
        };
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(row.name.clone(), t.accent)]))
            .child(line(vec![
                span(format!("state: {}", row.state), t.text),
                span(
                    format!(
                        "  ·  mode: {}",
                        row.mode.clone().unwrap_or_else(|| "—".into())
                    ),
                    t.text_muted,
                ),
            ]));
        if let Some(h) = &row.handle {
            col = col.child(line(vec![span(format!("handle: {h}"), t.text_muted)]));
        }
        col = col.child(line(vec![span(
            format!(
                "drives — questions: {}  problems: {}  interests: {}",
                row.open_questions
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "—".into()),
                row.open_problems
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "—".into()),
                row.open_interests
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "—".into()),
            ),
            t.text_muted,
        )]));
        col = col.child(line(vec![span(String::new(), t.text)]));
        let name = row.name.clone();
        match store.entity_detail.get() {
            Loadable::Ready(d) if d.name == name => {
                col = col
                    .child(line(vec![
                        span_bold("mind      ".to_string(), t.text),
                        span(
                            d.substrate
                                .as_ref()
                                .map(|(p, m)| format!("{p} / {m}"))
                                .unwrap_or_else(|| "unset".into()),
                            t.text,
                        ),
                        span(
                            if d.substrate_source.is_empty() {
                                String::new()
                            } else {
                                format!("  ({})", d.substrate_source)
                            },
                            t.text_faint,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("voice     ".to_string(), t.text),
                        span(
                            d.voice
                                .as_ref()
                                .map(|(p, m, v)| format!("{p} / {m} / {v}"))
                                .unwrap_or_else(|| "unset".into()),
                            t.text,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("effective ".to_string(), t.text),
                        span(
                            d.voice_effective.clone().unwrap_or_else(|| "—".into()),
                            t.text_muted,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("work order".to_string(), t.text),
                        span(
                            format!(" {}", d.work_order.clone().unwrap_or_else(|| "none".into())),
                            t.text,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("own time  ".to_string(), t.text),
                        span(
                            format!(
                                "loop {}{}",
                                match d.loop_running {
                                    Some(true) => "running",
                                    Some(false) => "stopped",
                                    None => "unreported",
                                },
                                d.loop_phase
                                    .as_ref()
                                    .map(|p| format!(" (phase {p})"))
                                    .unwrap_or_default()
                            ),
                            t.text,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("grant     ".to_string(), t.text),
                        span(
                            format!(" {}", d.grant.clone().unwrap_or_else(|| "none".into())),
                            t.text_muted,
                        ),
                    ]));
            }
            Loadable::Failed(e) => {
                col = col.child(line(vec![span(
                    format!("detail read failed — {}", e.message),
                    t.error,
                )]));
            }
            _ => {
                col = col.child(line(vec![span(
                    format!("⟳ reading {name}'s configuration…"),
                    t.info,
                )]));
            }
        }
        col = col
            .child(line(vec![span(String::new(), t.text)]))
            .child(line(vec![span(
            "m = manage actions · i closes this panel · creation/summon stay outside this console",
            t.text_faint,
        )]));
        Scroll::new(col.build()).view(gcx)
    })
}

// ---------------------------------------------------------------------
// State: awake / asleep (+dream) / paused
// ---------------------------------------------------------------------

fn open_state_modal(cx: Scope, ctx: &Ctx, entity: EntityRow) {
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(72, 15), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let name = entity.name.clone();
        // 0 awake · 1 asleep · 2 asleep+dream · 3 paused
        let pick = mcx.signal(match entity.state.as_str() {
            "asleep" => 1usize,
            "paused" => 3usize,
            _ => 0usize,
        });
        let reason = mcx.signal(String::new());
        let ctx_apply = ctx2.clone();
        let close_apply = close.clone();
        let close_cancel = close.clone();
        let name_apply = name.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Entity state — {name} (now: {})", entity.state),
                t0.accent,
            )]))
            .child(line(vec![span(
                "state is the operator's INTENT; the loop/visit actuality settles behind it",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "target",
                RadioGroup::new(vec![
                    "awake".to_string(),
                    "asleep".to_string(),
                    "asleep + dream pass".to_string(),
                    "paused (kill switch — never auto-clears)".to_string(),
                ])
                .selection(pick)
                .element(mcx, &t0)
                .autofocus()
                .build(),
            ))
            .child(field(
                &t0,
                "reason",
                TextInput::new()
                    .value(reason)
                    .placeholder("optional — recorded with your principal")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(52).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(line(vec![span(String::new(), t0.text)]))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Apply")
                            .on_click(move || {
                                let p = pick.get_untracked();
                                let state = match p {
                                    1 | 2 => "asleep",
                                    3 => "paused",
                                    _ => "awake",
                                };
                                let mut body = json!({ "state": state });
                                if p == 2 {
                                    body["dream"] = Value::Bool(true);
                                }
                                let r = reason.get_untracked().trim().to_string();
                                if !r.is_empty() {
                                    body["reason"] = Value::String(r);
                                }
                                ctx_apply.send(Cmd::EntityState {
                                    name: name_apply.clone(),
                                    body: body.into(),
                                });
                                // Outcome lands as toast + journal entry
                                // (write → cognition verify).
                                close_apply();
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

// ---------------------------------------------------------------------
// Substrate: provider / model
// ---------------------------------------------------------------------

fn open_substrate_form(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    super::open_form_guarded(ctx, cx, Size::new(72, 16), move |mcx, close, guard| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let detail = detail_for(&store, &name);
        let (p0, m0) = detail
            .as_ref()
            .and_then(|d| d.substrate.clone())
            .unwrap_or_default();
        let provider = mcx.signal(p0.clone());
        let model = mcx.signal(m0.clone());
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let esc_armed = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_dirty_guard(
            mcx,
            &guard,
            vec![(provider, p0.clone()), (model, m0.clone())],
            esc_armed,
            form_error,
        );
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let ctx_save = ctx2.clone();
        let name_save = name.clone();
        let close_cancel = close.clone();
        let source = detail
            .as_ref()
            .map(|d| d.substrate_source.clone())
            .unwrap_or_default();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Mind substrate — {name}"),
                t0.accent,
            )]))
            .child(line(vec![span(
                match &detail {
                    Some(_) => format!(
                        "current: {} (source: {})",
                        if p0.is_empty() {
                            "unset".to_string()
                        } else {
                            format!("{p0} / {m0}")
                        },
                        if source.is_empty() { "?" } else { &source }
                    ),
                    None => "current values still loading — what you type wins".to_string(),
                },
                t0.text_muted,
            )]))
            .child(line(vec![span(
                "one substrate per entity: visits AND the own-time loop resolve this pair",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "provider",
                TextInput::new()
                    .value(provider)
                    .placeholder("e.g. lmstudio, endpoint:ovh-provider")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(44).h(1))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(field(
                &t0,
                "model",
                TextInput::new()
                    .value(model)
                    .placeholder("e.g. ornith-1.0-35b, gpt-oss-120b")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(44).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy = in_flight.get();
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    let close_b = close_cancel.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Save")
                                .disabled(busy)
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let p = provider.get_untracked().trim().to_string();
                                    let m = model.get_untracked().trim().to_string();
                                    if p.is_empty() || m.is_empty() {
                                        form_error.set(Some(
                                            "both provider and model are required — the gateway refuses substrate-less entities".into(),
                                        ));
                                        return;
                                    }
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::SaveEntitySubstrate {
                                        name: n.clone(),
                                        body: json!({ "provider": p, "model": m }).into(),
                                        form_id: Some(form_id),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_b())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

// ---------------------------------------------------------------------
// Voice: provider / model / voice (+ clear)
// ---------------------------------------------------------------------

fn open_voice_form(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    super::open_form_guarded(ctx, cx, Size::new(74, 18), move |mcx, close, guard| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let detail = detail_for(&store, &name);
        let (p0, m0, v0) = detail
            .as_ref()
            .and_then(|d| d.voice.clone())
            .unwrap_or_default();
        let provider = mcx.signal(p0.clone());
        let model = mcx.signal(m0.clone());
        let voice = mcx.signal(v0.clone());
        let clear = mcx.signal(false);
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let esc_armed = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_dirty_guard(
            mcx,
            &guard,
            vec![
                (provider, p0.clone()),
                (model, m0.clone()),
                (voice, v0.clone()),
            ],
            esc_armed,
            form_error,
        );
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let effective = detail
            .as_ref()
            .and_then(|d| d.voice_effective.clone())
            .unwrap_or_else(|| "unknown".into());
        let ctx_save = ctx2.clone();
        let name_save = name.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(format!("Voice — {name}"), t0.accent)]))
            .child(line(vec![span(
                format!("applies now: {effective}"),
                t0.text_muted,
            )]))
            .child(line(vec![span(
                "the FULL triple is required together — a bare voice id leaks across providers",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "provider",
                TextInput::new()
                    .value(provider)
                    .placeholder("e.g. supertonic, openai")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(40).h(1))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(field(
                &t0,
                "model",
                TextInput::new()
                    .value(model)
                    .placeholder("e.g. supertonic-3, gpt-4o-mini-tts")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(40).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "voice",
                TextInput::new()
                    .value(voice)
                    .placeholder("e.g. M3, alloy — pickable in Routes → output.voice")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(40).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "",
                Checkbox::new("clear the set voice (fall back to gateway default)")
                    .checked(clear)
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy = in_flight.get();
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    let close_b = close_cancel.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Save")
                                .disabled(busy)
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let body = if clear.get_untracked() {
                                        json!({ "clear": true })
                                    } else {
                                        let p = provider.get_untracked().trim().to_string();
                                        let m = model.get_untracked().trim().to_string();
                                        let v = voice.get_untracked().trim().to_string();
                                        if p.is_empty() || m.is_empty() {
                                            form_error.set(Some(
                                                "provider and model are required (or check clear)"
                                                    .into(),
                                            ));
                                            return;
                                        }
                                        let mut b = json!({ "provider": p, "model": m });
                                        if !v.is_empty() {
                                            b["voice"] = Value::String(v);
                                        }
                                        b
                                    };
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::SaveEntityVoice {
                                        name: n.clone(),
                                        body: body.into(),
                                        form_id: Some(form_id),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_b())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

// ---------------------------------------------------------------------
// Work order: set / clear
// ---------------------------------------------------------------------

fn open_work_order_form(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    super::open_form_guarded(ctx, cx, Size::new(74, 14), move |mcx, close, guard| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let detail = detail_for(&store, &name);
        let o0 = detail
            .as_ref()
            .and_then(|d| d.work_order.clone())
            .unwrap_or_default();
        let order = mcx.signal(o0.clone());
        let clear = mcx.signal(false);
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let esc_armed = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_dirty_guard(
            mcx,
            &guard,
            vec![(order, o0.clone())],
            esc_armed,
            form_error,
        );
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let ctx_save = ctx2.clone();
        let name_save = name.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Work order — {name}"),
                t0.accent,
            )]))
            .child(line(vec![span(
                "what the entity should work on during its own time",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "order",
                TextInput::new()
                    .value(order)
                    .placeholder("blank + clear checked = remove the order")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(56).h(1))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(field(
                &t0,
                "",
                Checkbox::new("clear the work order")
                    .checked(clear)
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy = in_flight.get();
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    let close_b = close_cancel.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Save")
                                .disabled(busy)
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let body = if clear.get_untracked() {
                                        json!({ "clear": true })
                                    } else {
                                        let o = order.get_untracked().trim().to_string();
                                        if o.is_empty() {
                                            form_error.set(Some(
                                                "type an order, or check clear to remove it".into(),
                                            ));
                                            return;
                                        }
                                        json!({ "order": o })
                                    };
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::SaveEntityWorkOrder {
                                        name: n.clone(),
                                        body: body.into(),
                                        form_id: Some(form_id),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_b())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

// ---------------------------------------------------------------------
// Own time: personal grant + loop start/stop/freeze
// ---------------------------------------------------------------------

fn open_own_time_modal(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    let screen_cx = cx;
    open_form(ctx, cx, Size::new(76, 20), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let tick_s = mcx.signal("20".to_string());
        let ticks_day = mcx.signal("8".to_string());
        let rest_min = mcx.signal("30".to_string());
        let name2 = name.clone();
        let ctx3 = ctx2.clone();
        let close2 = close.clone();

        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Own time — {name}"),
                t0.accent,
            )]))
            // Live status from the manage snapshot (grant + loop).
            .child(dyn_view(LayoutStyle::default().h(2).shrink(0.0), {
                let n = name.clone();
                move || {
                    let t = theme.get().tokens;
                    match store.entity_detail.get() {
                        Loadable::Ready(d) if d.name == n => Element::new()
                            .style(LayoutStyle::column())
                            .child(line(vec![span(
                                format!(
                                    "loop: {}{}",
                                    match d.loop_running {
                                        Some(true) => "running",
                                        Some(false) => "stopped",
                                        None => "unreported",
                                    },
                                    d.loop_phase
                                        .as_ref()
                                        .map(|p| format!(" (phase {p})"))
                                        .unwrap_or_default()
                                ),
                                t.text,
                            )]))
                            .child(line(vec![span(
                                format!(
                                    "grant: {}",
                                    d.grant.clone().unwrap_or_else(|| "none reported".into())
                                ),
                                t.text_muted,
                            )]))
                            .build(),
                        Loadable::Failed(e) => line(vec![span(
                            format!("status read failed: {}", e.message),
                            t.error,
                        )]),
                        _ => line(vec![span("⟳ reading own-time status…", t.info)]),
                    }
                }
            }))
            .child(line(vec![span(
                "OWN TIME IS OFF BY DEFAULT — an unattended loop spends real tokens",
                t0.warn,
            )]))
            .child(field(
                &t0,
                "tick seconds",
                TextInput::new()
                    .value(tick_s)
                    .layout(LayoutStyle::default().w(10).h(1))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(field(
                &t0,
                "ticks per day",
                TextInput::new()
                    .value(ticks_day)
                    .layout(LayoutStyle::default().w(10).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "rest minutes",
                TextInput::new()
                    .value(rest_min)
                    .layout(LayoutStyle::default().w(10).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(line(vec![span(String::new(), t0.text)]))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let n_grant = name2.clone();
                    let n_off = name2.clone();
                    let n_start = name2.clone();
                    let n_stop = name2.clone();
                    let ctx_grant = ctx3.clone();
                    let ctx_off = ctx3.clone();
                    let ctx_start = ctx3.clone();
                    let ctx_stop = ctx3.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(1))
                        .child(
                            Button::new("Grant (timer)")
                                .on_click(move || {
                                    ctx_grant.send(Cmd::SavePersonalGrant {
                                        name: n_grant.clone(),
                                        body: json!({ "mode": "timer" }).into(),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Disable grant")
                                .on_click(move || {
                                    ctx_off.send(Cmd::SavePersonalGrant {
                                        name: n_off.clone(),
                                        body: json!({ "mode": "disabled" }).into(),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Start loop")
                                .on_click(move || {
                                    // Refuse garbage with the reason instead
                                    // of silently substituting the default
                                    // ("20abc" starting a loop at 20 is the
                                    // what-you-typed-is-not-what-applies
                                    // class). Blank = the default, stated.
                                    let parse = |s: Signal<String>, d: f64, label: &str| {
                                        let raw = s.get_untracked().trim().to_string();
                                        if raw.is_empty() {
                                            return Ok(d);
                                        }
                                        raw.parse::<f64>().map_err(|_| {
                                            format!("{label} is not a number: '{raw}'")
                                        })
                                    };
                                    let vals = (|| -> Result<(f64, f64, f64), String> {
                                        Ok((
                                            parse(tick_s, 20.0, "tick seconds")?,
                                            parse(ticks_day, 8.0, "ticks per day")?,
                                            parse(rest_min, 30.0, "rest minutes")?,
                                        ))
                                    })();
                                    let (tick, ticks, rest) = match vals {
                                        Ok(v) => v,
                                        Err(e) => {
                                            ctx_start.store.notice.set(Some(e));
                                            return;
                                        }
                                    };
                                    ctx_start.send(Cmd::EntityLoop {
                                        name: n_start.clone(),
                                        start: true,
                                        body: json!({
                                            "tick_seconds": tick,
                                            "ticks_per_day": ticks as u64,
                                            "rest_minutes": rest,
                                        }).into(),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Stop (graceful)")
                                .on_click(move || {
                                    ctx_stop.send(Cmd::EntityLoop {
                                        name: n_stop.clone(),
                                        start: false,
                                        body: json!({ "mode": "graceful",
                                                      "reason": "operator stop via console-tui" }).into(),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let n_freeze = name.clone();
                    let ctx_freeze = ctx2.clone();
                    let close_f = close2.clone();
                    let close_done = close2.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Emergency freeze")
                                .on_click(move || {
                                    // Danger path: close this modal, then
                                    // confirm on the screen scope (the
                                    // prompt-over-modal stacking hazard).
                                    let n = n_freeze.clone();
                                    let c = ctx_freeze.clone();
                                    close_f();
                                    super::confirm_danger(
                                        screen_cx,
                                        c.ui,
                                        format!(
                                            "FREEZE '{n}'? A hard stop — no reflection pass; the look-back debt rides to the next open."
                                        ),
                                        "Freeze now",
                                        "Keep it running",
                                        {
                                            let c2 = c.clone();
                                            move || {
                                                c2.send(Cmd::EntityLoop {
                                                    name: n,
                                                    start: false,
                                                    body: json!({ "mode": "freeze",
                                                        "reason": "operator emergency freeze via console-tui" }).into(),
                                                });
                                            }
                                        },
                                    );
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Done (Esc)")
                                .on_click(move || close_done())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

// ---------------------------------------------------------------------
// Re-embed (operator repair — danger-gated)
// ---------------------------------------------------------------------

fn open_reembed_form(cx: Scope, ctx: &Ctx, name: String) {
    let ctx2 = ctx.clone();
    let screen_cx = cx;
    open_form(ctx, cx, Size::new(76, 14), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let model = mcx.signal(String::new());
        let reason = mcx.signal(String::new());
        let name2 = name.clone();
        let ctx3 = ctx2.clone();
        let close2 = close.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Re-embed home — {name}"),
                t0.accent,
            )]))
            .child(line(vec![span(
                "vectors are a derived index over engraved text; re-embedding rewrites it",
                t0.text_muted,
            )]))
            .child(line(vec![span(
                "all-or-nothing, takes the home lease, shifts semantic neighborhoods — repair only",
                t0.warn,
            )]))
            .child(field(
                &t0,
                "embedding model",
                TextInput::new()
                    .value(model)
                    .placeholder("e.g. text-embedding-qwen3-embedding-0.6b")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(48).h(1))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(field(
                &t0,
                "reason",
                TextInput::new()
                    .value(reason)
                    .placeholder("why this repair is needed (journaled)")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(48).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(line(vec![span(String::new(), t0.text)]))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Re-embed…")
                            .on_click(move || {
                                let m = model.get_untracked().trim().to_string();
                                let r = reason.get_untracked().trim().to_string();
                                if m.is_empty() {
                                    ctx3.store.notice.set(Some(
                                        "type the embedding model id first".into(),
                                    ));
                                    return;
                                }
                                let n = name2.clone();
                                let c = ctx3.clone();
                                close2();
                                super::confirm_danger(
                                    screen_cx,
                                    c.ui,
                                    format!(
                                        "Re-embed '{n}' with {m}? The whole vector index is rebuilt (slow; the entity's home is leased for the duration)."
                                    ),
                                    "Re-embed now",
                                    "Cancel",
                                    {
                                        let c2 = c.clone();
                                        move || {
                                            let mut body = json!({ "embedding_model": m });
                                            if !r.is_empty() {
                                                body["reason"] = Value::String(r);
                                            }
                                            c2.send(Cmd::EntityReembed {
                                                name: n,
                                                body: body.into(),
                                                form_id: None,
                                            });
                                        }
                                    },
                                );
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

// ---------------------------------------------------------------------
// Tool policy: per-phase grants (the web's capability matrix)
// ---------------------------------------------------------------------

/// Fixed slot budget for phase MultiSelects: signals must live in the
/// MODAL scope (region scopes die on re-render), but the phase list
/// arrives async — so a stable slot array is filled once on Ready.
const PHASE_SLOTS: usize = 6;

fn open_tool_policy_form(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    store.entity_policy.set(Loadable::Loading);
    ctx.send(Cmd::LoadToolPolicy { name: name.clone() });

    let ctx2 = ctx.clone();
    let screen_cx = cx;
    open_form(ctx, cx, Size::new(84, 26), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let phase_sigs: std::rc::Rc<Vec<Signal<Vec<String>>>> =
            std::rc::Rc::new((0..PHASE_SLOTS).map(|_| mcx.signal(Vec::new())).collect());
        let filled = mcx.signal(false);
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();

        // One-shot fill: grants land in the slots when the read arrives.
        {
            let phase_sigs = phase_sigs.clone();
            let n = name.clone();
            mcx.effect(move || {
                if filled.get() {
                    return;
                }
                if let Loadable::Ready(d) = store.entity_policy.get() {
                    if d.entity != n {
                        return;
                    }
                    for (i, (_, tools, _)) in d.phases.iter().take(PHASE_SLOTS).enumerate() {
                        phase_sigs[i].set(tools.clone());
                    }
                    filled.set(true);
                }
            });
        }

        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let ctx_save = ctx2.clone();
        let name_save = name.clone();
        let name_title = name.clone();
        let close_cancel = close.clone();
        let close_for_prompt = close.clone();
        let phase_sigs_save = phase_sigs.clone();
        let phase_sigs_render = phase_sigs.clone();

        // Focusable+autofocus content root (engine 0230): the editors
        // build their widgets inside regenerating regions where
        // autofocus is the 0220 panic hazard — without a focus target
        // in the modal tree, EVERY key but Tab (Esc included) is dead.
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Tool policy — {name_title} (per-phase grants)"),
                t0.accent,
            )]))
            .child(line(vec![span(
                "an emptied phase asks: reset to the framework default, or an explicit deny-all",
                t0.text_faint,
            )]))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                let n = name.clone();
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_policy.get() {
                        Loadable::Ready(d) if d.entity == n => {
                            if !filled.get() {
                                // The fill effect runs this same frame.
                                return line(vec![span("⟳ preparing grants…", t.info)]);
                            }
                            let opts: Vec<SelectOption> = d
                                .all_tools
                                .iter()
                                .map(|tname| SelectOption::keyed(tname.clone(), tname.clone()))
                                .collect();
                            let mut col = Element::new().style(LayoutStyle::column().gap(0));
                            for (i, (phase, _, source)) in
                                d.phases.iter().take(PHASE_SLOTS).enumerate()
                            {
                                col = col.child(field(
                                    &t,
                                    phase,
                                    Element::new()
                                        .style(LayoutStyle::row().gap(1))
                                        .child(
                                            MultiSelect::new(opts.clone())
                                                .values(phase_sigs_render[i])
                                                .placeholder("no tools (empty grant on save)")
                                                .layout(
                                                    LayoutStyle::default().w(44).h(1).shrink(0.0),
                                                )
                                                .element(gcx, &t)
                                                .build(),
                                        )
                                        .child(line(vec![span(
                                            format!("({source})"),
                                            t.text_faint,
                                        )]))
                                        .build(),
                                ));
                            }
                            if d.phases.len() > PHASE_SLOTS {
                                col = col.child(line(vec![span(
                                    format!(
                                        "{} more phase(s) not editable here — use the web console",
                                        d.phases.len() - PHASE_SLOTS
                                    ),
                                    t.warn,
                                )]));
                            }
                            col.build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(
                            &t,
                            &e,
                            Some("close and reopen this dialog to retry (opening re-reads)"),
                        ),
                        _ => line(vec![span("⟳ loading tool policy…", t.info)]),
                    }
                }
            }))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy = in_flight.get() || !filled.get();
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    let close_b = close_cancel.clone();
                    let close_p = close_for_prompt.clone();
                    let sigs = phase_sigs_save.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Save grants")
                                .disabled(busy)
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let Some(d) = ctx_s.store.entity_policy.with_untracked(|p| {
                                        p.ready().filter(|d| d.entity == n).cloned()
                                    }) else {
                                        form_error.set(Some("policy not loaded yet".into()));
                                        return;
                                    };
                                    // Only CHANGED phases ride the write
                                    // (web parity: readMatrix sends deltas).
                                    let mut policy = serde_json::Map::new();
                                    let mut emptied: Vec<String> = Vec::new();
                                    for (i, (phase, orig, _)) in
                                        d.phases.iter().take(PHASE_SLOTS).enumerate()
                                    {
                                        let now = sigs[i].get_untracked();
                                        if &now == orig {
                                            continue;
                                        }
                                        if now.is_empty() {
                                            emptied.push(phase.clone());
                                        }
                                        policy.insert(
                                            phase.clone(),
                                            Value::Array(
                                                now.iter()
                                                    .map(|s| Value::String(s.clone()))
                                                    .collect(),
                                            ),
                                        );
                                    }
                                    if policy.is_empty() {
                                        form_error.set(Some("no changes to save".into()));
                                        return;
                                    }
                                    if emptied.is_empty() {
                                        form_error.set(None);
                                        in_flight.set(true);
                                        ctx_s.send(Cmd::SaveToolPolicy {
                                            name: n.clone(),
                                            body: json!({ "policy": Value::Object(policy) }).into(),
                                            form_id: Some(form_id),
                                        });
                                        return;
                                    }
                                    // Emptied phases are ambiguous — the
                                    // operator PICKS the consequence (web
                                    // parity), on the screen scope (never
                                    // a prompt over a modal).
                                    let c = ctx_s.clone();
                                    let n2 = n.clone();
                                    close_p();
                                    confirm_emptied_phases(screen_cx, &c, n2, policy, emptied);
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_b())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

/// The emptied-phase choice: reset-to-default (null) vs explicit
/// deny-all ([]) — the web's exact semantics, named consequences.
fn confirm_emptied_phases(
    cx: Scope,
    ctx: &Ctx,
    name: String,
    mut policy: serde_json::Map<String, Value>,
    emptied: Vec<String>,
) {
    let ctx2 = ctx.clone();
    let list = emptied.join(", ");
    super::open_prompt(
        cx,
        ctx.ui,
        abstracttui::app::ChoicePrompt::new(format!(
            "Phases with every tool removed ({list}) — what did you mean?"
        ))
        .option_detail(
            "reset",
            "Reset them to the framework default",
            "the evolving default grant applies — this does NOT deny tools",
        )
        .option_with(
            abstracttui::app::ChoiceOption::new("deny", "Deny ALL tools in those phases")
                .danger(true),
        )
        .option("keep", "Cancel — keep the current grants")
        .initial("reset"),
        move |outcome| {
            if let abstracttui::app::ChoiceOutcome::Answered(a) = outcome {
                let pick = a.selected.first().cloned().unwrap_or_default();
                match pick.as_str() {
                    "reset" => {
                        for p in &emptied {
                            policy.insert(p.clone(), Value::Null);
                        }
                    }
                    "deny" => { /* keep the explicit [] already in the map */ }
                    _ => return,
                }
                ctx2.send(Cmd::SaveToolPolicy {
                    name: name.clone(),
                    body: json!({ "policy": Value::Object(policy.clone()) }).into(),
                    form_id: None,
                });
            }
        },
    );
}

// ---------------------------------------------------------------------
// Prompt overlay editor (per-layer TextAreas; Enter inserts newlines)
// ---------------------------------------------------------------------

/// Fixed layer-slot budget (same rationale as PHASE_SLOTS: TextArea
/// states must live in the modal scope, the layer list arrives async).
const LAYER_SLOTS: usize = 4;

fn open_prompt_editor(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    store.entity_prompt.set(Loadable::Loading);
    ctx.send(Cmd::LoadEntityPrompt { name: name.clone() });
    let ctx2 = ctx.clone();
    let name_title = name.clone();
    open_form(ctx, cx, Size::new(92, 30), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let states: std::rc::Rc<Vec<TextAreaState>> =
            std::rc::Rc::new((0..LAYER_SLOTS).map(|_| TextAreaState::new(mcx)).collect());
        let filled = mcx.signal(false);
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();

        // One-shot fill from the read.
        {
            let states = states.clone();
            let n = name.clone();
            mcx.effect(move || {
                if filled.get() {
                    return;
                }
                if let Loadable::Ready(d) = store.entity_prompt.get() {
                    if d.entity != n {
                        return;
                    }
                    for (i, (_, text)) in d.layers.iter().take(LAYER_SLOTS).enumerate() {
                        states[i].set_text(text.clone());
                    }
                    filled.set(true);
                }
            });
        }

        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let ctx_save = ctx2.clone();
        let name_save = name.clone();
        let n_render = name.clone();
        let close_b = close.clone();
        let states_render = states.clone();
        let states_save = states.clone();

        // Focusable+autofocus content root — see the tool-policy twin
        // (engine 0230: no focus in the modal tree = dead keys).
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Prompt overlay — {name_title}"),
                t0.accent,
            )]))
            .child(line(vec![span(
                "per-layer overlay text — Enter inserts a newline; Tab moves between layers; Save writes ALL layers",
                t0.text_faint,
            )]))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_prompt.get() {
                        Loadable::Ready(d) if d.entity == n_render => {
                            if !filled.get() {
                                return line(vec![span("⟳ preparing layers…", t.info)]);
                            }
                            if d.layers.is_empty() {
                                return line(vec![span(
                                    "no overlay layers reported — the framework prelude applies unmodified",
                                    t.text_muted,
                                )]);
                            }
                            let mut col = Element::new().style(LayoutStyle::column().gap(0));
                            for (i, (layer, _)) in
                                d.layers.iter().take(LAYER_SLOTS).enumerate()
                            {
                                col = col
                                    .child(line(vec![span_bold(
                                        format!("── {layer} ──"),
                                        t.accent,
                                    )]))
                                    .child(
                                        TextArea::new()
                                            .state(&states_render[i])
                                            .submit_policy(SubmitPolicy::EnterInserts)
                                            .rows(3, 8)
                                            .layout(LayoutStyle::default().shrink(0.0))
                                            .element(gcx, &t)
                                            .build(),
                                    );
                            }
                            if d.layers.len() > LAYER_SLOTS {
                                col = col.child(line(vec![span(
                                    format!(
                                        "{} more layer(s) not shown — use the web console",
                                        d.layers.len() - LAYER_SLOTS
                                    ),
                                    t.warn,
                                )]));
                            }
                            col.build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(&t, &e, Some("close and reopen this dialog to retry (opening re-reads)")),
                        _ => line(vec![span("⟳ loading overlay…", t.info)]),
                    }
                }
            }))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy = in_flight.get() || !filled.get();
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    let close_c = close_b.clone();
                    let states_s = states_save.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Save overlay")
                                .disabled(busy)
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let Some(d) = ctx_s.store.entity_prompt.with_untracked(|p| {
                                        p.ready().filter(|d| d.entity == n).cloned()
                                    }) else {
                                        form_error.set(Some("overlay not loaded yet".into()));
                                        return;
                                    };
                                    // ALL layers ride (web parity — the
                                    // form is the whole overlay truth).
                                    let mut overlay = serde_json::Map::new();
                                    for (i, (layer, _)) in
                                        d.layers.iter().take(LAYER_SLOTS).enumerate()
                                    {
                                        overlay.insert(
                                            layer.clone(),
                                            Value::String(states_s[i].text()),
                                        );
                                    }
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::SaveEntityPrompt {
                                        name: n.clone(),
                                        body: json!({ "overlay": Value::Object(overlay) }).into(),
                                        form_id: Some(form_id),
                                    });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
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

// ---------------------------------------------------------------------
// Candidates review (sleep consolidation → waking evidence disposes)
// ---------------------------------------------------------------------

fn open_candidates_modal(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    store.entity_candidates.set(Loadable::Loading);
    ctx.send(Cmd::LoadCandidates { name: name.clone() });
    let ctx2 = ctx.clone();
    let name_title = name.clone();
    open_form(ctx, cx, Size::new(90, 24), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let sel = mcx.signal(0usize);
        let reason = mcx.signal(String::new());
        let n = name.clone();
        let n_act = name.clone();
        let ctx3 = ctx2.clone();
        let close_b = close.clone();
        super::util::clamp_selection(mcx, sel, move || {
            store
                .entity_candidates
                .with(|d| d.ready().map(|(_, rows)| rows.len()).unwrap_or(0))
        });
        // Focusable+autofocus content root: the table (the modal's only
        // focusable) arrives AFTER the async load — without this, keys
        // are dead during the loading window (engine 0230 class).
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Candidates — {name_title} (sleep proposes; waking evidence disposes)"),
                t0.accent,
            )]))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                let n = n.clone();
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_candidates.get() {
                        Loadable::Ready((e, rows)) if e == n => {
                            if rows.is_empty() {
                                return line(vec![span(
                                    "no candidates awaiting review",
                                    t.text_muted,
                                )]);
                            }
                            let table_rows: Vec<Vec<String>> = rows
                                .iter()
                                // Uncapped: the title column is Flex, so
                                // it already grows with the drawer; a
                                // 60-char pre-cut only hid the end of a
                                // title the drawer had room for.
                                .map(|r| vec![r.kind.clone(), r.title.clone()])
                                .collect();
                            Element::new()
                                .style(LayoutStyle::column().gap(0))
                                .child(
                                    Table::new(vec![
                                        Column::new("kind", ColWidth::Cells(10)),
                                        Column::new("title", ColWidth::Flex(1.0)),
                                    ])
                                    .rows(table_rows)
                                    .selection(sel)
                                    .layout(LayoutStyle::default().grow(1.0))
                                    .element(gcx, &t)
                                    .autofocus()
                                    .build(),
                                )
                                .child(dyn_view(LayoutStyle::default().h(3).shrink(0.0), {
                                    let n2 = n.clone();
                                    move || {
                                        let t = theme.get().tokens;
                                        let digest = store.entity_candidates.with(|d| {
                                            d.ready()
                                                .filter(|(e, _)| *e == n2)
                                                .and_then(|(_, rows)| {
                                                    rows.get(sel.get()).map(|r| r.digest.clone())
                                                })
                                                .unwrap_or_default()
                                        });
                                        line(vec![span(
                                            super::util::ellipsize(&digest, 250),
                                            t.text_muted,
                                        )])
                                    }
                                }))
                                .build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(
                            &t,
                            &e,
                            Some("press the Reload button below to retry"),
                        ),
                        _ => line(vec![span("⟳ loading candidates…", t.info)]),
                    }
                }
            }))
            .child(field(
                &t0,
                "reason",
                TextInput::new()
                    .value(reason)
                    .placeholder("required — recorded with the act")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(56).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let act = |promote: bool| {
                        let ctx_a = ctx3.clone();
                        let n2 = n_act.clone();
                        move || {
                            let r = reason.get_untracked().trim().to_string();
                            if r.is_empty() {
                                ctx_a.store.notice.set(Some(
                                    "type the reason first — candidate acts are journaled".into(),
                                ));
                                return;
                            }
                            let row = ctx_a.store.entity_candidates.with_untracked(|d| {
                                d.ready()
                                    .filter(|(e, _)| *e == n2)
                                    .and_then(|(_, rows)| rows.get(sel.get_untracked()).cloned())
                            });
                            let Some(row) = row else {
                                ctx_a.store.notice.set(Some("no candidate selected".into()));
                                return;
                            };
                            ctx_a.send(Cmd::CandidateAct {
                                name: n2.clone(),
                                record_id: row.record_id.clone(),
                                promote,
                                reason: r,
                            });
                            reason.set(String::new());
                        }
                    };
                    let ctx_r = ctx3.clone();
                    let n_r = n_act.clone();
                    let close_c = close_b.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Promote (accept)")
                                .on_click(act(true))
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Reject")
                                .on_click(act(false))
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Reload")
                                .on_click(move || {
                                    ctx_r.store.entity_candidates.set(Loadable::Loading);
                                    ctx_r.send(Cmd::LoadCandidates { name: n_r.clone() });
                                })
                                .element(bcx, &t)
                                .build(),
                        )
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

// The shared form plumbing (dirty-Esc guard, write_done routing, the
// message slot) lives in `super` (ui/mod.rs) — one implementation of
// the contract for every write form in the app (F4).
