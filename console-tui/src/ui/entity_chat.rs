//! Talk to an entity + its identity card — the web console's Manage →
//! Talk subtab (`entityChatOpen` / `entityChatSend` / `entityChatClose`)
//! and Manage → Overview (`loadEntityOverview`, the `/card` read).
//!
//! Talk is the hosted chat VISIT: Open (prelude + memory; may yield the
//! own-time loop) → turns (each a real LLM round trip, answered as one
//! JSON document — the web does not stream this route either) → Close
//! (the reflection pass). The console holds ONE visit at a time, like
//! the web page; the transcript is local, like the web's.

use abstracttui::prelude::*;

use super::entity_manage::{wb, SubForm};
use super::util::{line, span, span_bold, wrap_text};
use super::Ctx;
use crate::api::entities::{ChatLine, ChatState};
use crate::store::Loadable;
use crate::worker::entities::EntityCmd;
use crate::worker::Cmd;

const TALK_W: i32 = 96;

/// The identity card of `name` (pure read — knowing someone never
/// fakes their memory usage), inline in Manage's Overview tab.
pub(crate) fn card_body(mcx: Scope, ctx: &Ctx, name: String) -> View {
    let store = ctx.store;
    store.entity_card.set(Loadable::Loading);
    ctx.send(Cmd::Entity(EntityCmd::LoadCard { name: name.clone() }));
    let ctx2 = ctx.clone();
    {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let n_render = name.clone();
        let n_reload = name.clone();
        let ctx_r = ctx2.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(dyn_view_scoped(
                LayoutStyle::column().shrink(0.0),
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_card.get() {
                        Loadable::Ready(c) if c.name == n_render => {
                            let mut col = Element::new().style(LayoutStyle::column().gap(0));
                            for (k, v) in &c.rows {
                                col = col.child(line(vec![
                                    span_bold(format!("{k:<28}"), t.text_muted),
                                    span(v.clone(), t.text),
                                ]));
                            }
                            if c.rows.is_empty() {
                                col = col.child(line(vec![span(
                                    "the card carries none of the overview fields",
                                    t.text_muted,
                                )]));
                            }
                            if !c.moments.is_empty() {
                                col = col.child(line(vec![span_bold(
                                    "Recent moments".to_string(),
                                    t.text_muted,
                                )]));
                                for (at, what) in &c.moments {
                                    col = col.child(line(vec![
                                        span(format!("{at:<18}"), t.text_faint),
                                        span(what.clone(), t.text),
                                    ]));
                                }
                            }
                            let _ = gcx;
                            col.build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(
                            &t,
                            &e,
                            Some("Reload retries (opening re-reads)"),
                        ),
                        _ => line(vec![span(format!("⟳ reading {n_render}'s card…"), t.info)]),
                    }
                },
            ))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(wb(mcx, &t0, SubForm::Card, "reload", move || {
                        ctx_r.store.entity_card.set(Loadable::Loading);
                        ctx_r.send(Cmd::Entity(EntityCmd::LoadCard {
                            name: n_reload.clone(),
                        }));
                    }))
                    .build(),
            )
            .build()
    }
}

/// Whether the console may open Talk on `name` now: one live visit at a
/// time — a visit held open with ANOTHER entity must be closed first
/// (silently dropping it would leave a live server session nobody can
/// see; the next loop start would 409 against the invisible visit).
pub fn talk_refusal(chat: &ChatState, name: &str) -> Option<String> {
    match &chat.chat_id {
        Some(id) if chat.entity != name => Some(format!(
            "a visit with {} is still open ({id}) — close it first (Talk on {} → Close visit)",
            chat.entity, chat.entity
        )),
        _ => None,
    }
}

/// Talk with `name` (`c` on the roster, or the manage menu).
pub fn open_talk_modal(cx: Scope, ctx: &Ctx, name: String) {
    let store = ctx.store;
    if let Some(why) = store.entity_chat.with_untracked(|c| talk_refusal(c, &name)) {
        store.notice.set(Some(why));
        return;
    }
    // A new entity gets a fresh panel; the same entity resumes its visit
    // (closing the dialog never abandons a live visit).
    store.entity_chat.update(|c| {
        if c.entity != name {
            *c = ChatState::fresh(&name);
        }
    });
    let n = name.clone();
    let ctx2 = ctx.clone();
    super::w::FormModal::new(format!("Talk — {name}"))
        .size(TALK_W, 32)
        .open(ctx, cx, move |mcx, close, _guard, _w| {
            talk_body(mcx, &ctx2, n, Some(close))
        });
}

/// Whether the Talk panel may start: one live visit at a time. `None` =
/// may talk; `Some(why)` = say why (Manage's Talk tab shows it inline).
pub(crate) fn talk_prepare(ctx: &Ctx, name: &str) -> Option<String> {
    let store = ctx.store;
    if let Some(why) = store.entity_chat.with_untracked(|c| talk_refusal(c, name)) {
        return Some(why);
    }
    store.entity_chat.update(|c| {
        if c.entity != name {
            *c = ChatState::fresh(name);
        }
    });
    None
}

/// The Talk panel's body (the standalone panel, or inline in Manage's
/// Talk tab — `close` = the standalone panel's Close).
pub(crate) fn talk_body(
    mcx: Scope,
    ctx: &Ctx,
    name: String,
    close: Option<super::CloserFn>,
) -> View {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    let inline = close.is_none();
    {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let input = mcx.signal(String::new());
        let follow = mcx.signal(true);
        let ctx_send = ctx2.clone();
        let ctx_btn = ctx2.clone();
        let close_b = close.clone();
        let n_send = name.clone();
        let n_btn = name.clone();
        let send = std::rc::Rc::new(move || {
            let text = input.get_untracked().trim().to_string();
            let chat = store.entity_chat.get_untracked();
            let Some(chat_id) = chat.chat_id.clone() else {
                store
                    .notice
                    .set(Some("open the visit first (Open visit)".into()));
                return;
            };
            if text.is_empty() || chat.busy {
                return;
            }
            store.entity_chat.update(|c| {
                c.lines.push(ChatLine {
                    who: "you".into(),
                    text: text.clone(),
                });
                c.status = "thinking…".into();
                c.busy = true;
            });
            input.set(String::new());
            ctx_send.send(Cmd::Entity(EntityCmd::ChatTurn {
                name: n_send.clone(),
                chat_id,
                text,
            }));
        });
        let send_submit = send.clone();
        let send_btn = send.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span(
                "a hosted visit: Open (prelude + memory) → say something (Enter sends) → Close runs the reflection pass",
                t0.text_faint,
            )]))
            .child(dyn_view_scoped(
                if inline {
                    LayoutStyle::column().shrink(0.0)
                } else {
                    LayoutStyle::default().grow(1.0).min_h(4)
                },
                move |gcx| {
                let t = theme.get().tokens;
                let chat = store.entity_chat.get();
                let width = (TALK_W - 8).max(20) as usize;
                let mut rows: Vec<View> = Vec::new();
                if chat.lines.is_empty() {
                    rows.push(line(vec![span(
                        if chat.chat_id.is_some() {
                            "the visit is open — say something"
                        } else {
                            "no visit open — Open visit starts one"
                        },
                        t.text_muted,
                    )]));
                }
                for l in &chat.lines {
                    let you = l.who == "you";
                    rows.push(line(vec![span_bold(
                        l.who.clone(),
                        if you { t.info } else { t.accent },
                    )]));
                    for w in wrap_text(&l.text, width) {
                        rows.push(line(vec![span(format!("  {w}"), t.text)]));
                    }
                }
                if inline {
                    // Inline in Manage (whose body scrolls): the newest
                    // ten lines, no scroll inside a scroll.
                    let skip = rows.len().saturating_sub(10);
                    return Element::new()
                        .style(LayoutStyle::column().gap(0).shrink(0.0))
                        .children(rows.into_iter().skip(skip).collect::<Vec<_>>())
                        .build();
                }
                Scroll::new(
                    Element::new()
                        .style(LayoutStyle::column().gap(0))
                        .children(rows)
                        .build(),
                )
                .follow_tail(follow)
                .layout(LayoutStyle::default().grow(1.0))
                .element(gcx, &t)
                .build()
            }))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                let chat = store.entity_chat.get();
                let ink = if chat.busy { t.info } else { t.text_muted };
                line(vec![span(
                    if chat.busy && !chat.status.is_empty() {
                        format!("⟳ {}", chat.status)
                    } else {
                        chat.status.clone()
                    },
                    ink,
                )])
            }))
            .child({
                let el = TextInput::new()
                    .value(input)
                    .placeholder("say something to the entity (Enter sends)")
                    .placeholder_while_focused(true)
                    .on_submit(move |_| send_submit())
                    .layout(if inline {
                        LayoutStyle::default().w((TALK_W - 26).max(20)).h(1)
                    } else {
                        LayoutStyle::default().grow(1.0).h(1)
                    })
                    .element(mcx, &t0);
                // The standalone panel puts the caret here; inline in Manage
                // the tab bar keeps the focus.
                let el = if inline { el.build() } else { el.autofocus().build() };
                if inline {
                    super::util::field(&t0, "you ›", el)
                } else {
                    Element::new()
                        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                        .child(line(vec![span("you ›", t0.text_muted)]))
                        .child(el)
                        .build()
                }
            })
            // Which verbs apply now (the web hides the others; the
            // terminal keeps ONE static row so focus never drops).
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                let chat = store.entity_chat.get();
                let hint = if chat.busy {
                    "a request is in flight — wait for its answer"
                } else if chat.chat_id.is_some() {
                    if inline {
                        "visit open: Send (or Enter in the input) · Close visit runs the reflection · closing Manage keeps the visit open"
                    } else {
                        "visit open: Send (or Enter in the input) · Close visit runs the reflection · Esc hides this panel, the visit stays open"
                    }
                } else if inline {
                    "no visit: Open visit starts one"
                } else {
                    "no visit: Open visit starts one · Esc closes this panel"
                };
                line(vec![span(hint, t.text_faint)])
            }))
            .child({
                let ctx_o = ctx_btn.clone();
                let ctx_c = ctx_btn.clone();
                let n_o = n_btn.clone();
                let n_c = n_btn.clone();
                let send_b = send_btn.clone();
                let close_x = close_b.clone();
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        wb(mcx, &t0, SubForm::Talk, "open", move || {
                                let chat = store.entity_chat.get_untracked();
                                if chat.busy {
                                    return;
                                }
                                if let Some(id) = &chat.chat_id {
                                    store.entity_chat.update(|c| {
                                        c.status = format!("the visit is already open ({id})")
                                    });
                                    return;
                                }
                                ctx_o.store.entity_chat.update(|c| c.busy = true);
                                ctx_o.send(Cmd::Entity(EntityCmd::ChatOpen { name: n_o.clone() }));
                            }),
                    )
                    .child(
                        wb(mcx, &t0, SubForm::Talk, "send", move || send_b()),
                    )
                    .child(
                        wb(mcx, &t0, SubForm::Talk, "close_visit", move || {
                                let chat = store.entity_chat.get_untracked();
                                if chat.busy {
                                    return;
                                }
                                let Some(chat_id) = chat.chat_id.clone() else {
                                    store
                                        .entity_chat
                                        .update(|c| c.status = "no visit is open".into());
                                    return;
                                };
                                ctx_c.store.entity_chat.update(|c| {
                                    c.busy = true;
                                    c.status = "closing (reflection pass)…".into();
                                });
                                ctx_c.send(Cmd::Entity(EntityCmd::ChatClose {
                                    name: n_c.clone(),
                                    chat_id,
                                }));
                            }),
                    )
                    .children(close_x.map(|c| {
                        wb(mcx, &t0, SubForm::Talk, "close", move || c())
                    }))
                    .build()
            })
            .build()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn talk_refuses_a_second_entity_while_a_visit_is_open() {
        let mut c = ChatState::fresh("Castor");
        assert_eq!(talk_refusal(&c, "Pollux"), None, "no visit open: free");
        c.chat_id = Some("chat_1".into());
        assert_eq!(talk_refusal(&c, "Castor"), None, "same entity resumes");
        let why = talk_refusal(&c, "Pollux").expect("refused");
        assert!(
            why.contains("a visit with Castor is still open (chat_1)"),
            "{why}"
        );
    }
}
