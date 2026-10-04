//! About — the shared About card (ui-kit `AfAbout`, round 5 R5.4): this
//! console's name and version, the AbstractFramework and AbstractGateway
//! versions from the connected gateway's public `GET /about`, the links
//! (Website, Source, Docs, Issues, Feedback, Contact) and ONE
//! author/licence line. No package list.
//!
//! Two doors, one renderer ([`card_rows`]): the About PAGE at the bottom of
//! the screen list (key `I`, round 7 — the web sidebar's IA) and the About
//! overlay (F1 / `?`, anywhere, the web top bar's About button). A gateway
//! that cannot answer is said in the AbstractGateway row, never a blank.

use abstracttui::prelude::*;
use serde_json::Value;

use super::util::{line, span, span_bold};
use super::{open_form, Ctx};
use crate::identity::{about_card_rows, about_version_facts, this_app};
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The two version facts for the current state: `connected` = the console
/// holds a verified connection; `about` = the `GET /about` slot.
pub fn version_facts(connected: bool, about: &Loadable<Value>) -> Vec<(String, String)> {
    match about {
        Loadable::Ready(v) => about_version_facts(Some(v), None),
        Loadable::Failed(e) => about_version_facts(None, Some(&e.to_string())),
        Loadable::Loading => reading(),
        Loadable::NotAsked if connected => reading(),
        Loadable::NotAsked => about_version_facts(None, None),
    }
}

fn reading() -> Vec<(String, String)> {
    vec![
        (
            "AbstractFramework".into(),
            "reading GET /api/gateway/about…".into(),
        ),
        (
            "AbstractGateway".into(),
            "reading GET /api/gateway/about…".into(),
        ),
    ]
}

/// Every row of the card, in order (label: value, or a bare line).
pub fn card_rows(connected: bool, about: &Loadable<Value>) -> Vec<(String, String)> {
    about_card_rows(&this_app(), &version_facts(connected, about))
}

/// (Re)read `GET /about` when connected; NotAsked otherwise.
fn load(ctx: &Ctx) {
    let store = ctx.store;
    if store.conn.get_untracked().is_connected() {
        store.about.set(Loadable::Loading);
        ctx.send(Cmd::LoadAbout);
    } else {
        store.about.set(Loadable::NotAsked);
    }
}

/// `r` on the About page.
pub fn refresh(ctx: &Ctx) {
    load(ctx);
}

/// The label column's width (the longest label, "AbstractFramework", + 1).
const LABEL_W: usize = 18;

/// The card's lines (reactive): the name line bold, labels muted, every
/// value WRAPPED to the width (a long link is never cut).
fn card_view(
    store: crate::store::Store,
    theme: Signal<&'static abstracttui::theme::Theme>,
    width: impl Fn() -> i32 + 'static,
) -> View {
    dyn_view(LayoutStyle::column().gap(0).grow(1.0), move || {
        let t = theme.get().tokens;
        let w = width().max(LABEL_W as i32 + 10) as usize;
        let rows = card_rows(store.conn.get().is_connected(), &store.about.get());
        let mut views: Vec<View> = Vec::new();
        for (i, (k, v)) in rows.into_iter().enumerate() {
            if k.is_empty() {
                for (j, l) in super::util::wrap_text(&v, w).into_iter().enumerate() {
                    views.push(if i == 0 && j == 0 {
                        line(vec![span_bold(l, t.text)])
                    } else {
                        line(vec![span(l, t.text_muted)])
                    });
                }
                continue;
            }
            let ink = if v.starts_with("unavailable") || v == "not connected" {
                t.warn
            } else {
                t.text
            };
            for (j, l) in super::util::wrap_text(&v, w - LABEL_W)
                .into_iter()
                .enumerate()
            {
                let label = if j == 0 {
                    format!("{k:<LABEL_W$}")
                } else {
                    " ".repeat(LABEL_W)
                };
                views.push(line(vec![span(label, t.text_muted), span(l, ink)]));
            }
        }
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .children(views)
            .build()
    })
}

/// The About page (bottom of the screen list).
pub fn page(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    // Read once per connection (and on `r`).
    {
        let ctx_load = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected
                && store
                    .about
                    .with_untracked(|a| matches!(a, Loadable::NotAsked))
            {
                load(&ctx_load);
            }
        });
    }
    let theme = use_theme(cx);
    let vp = abstracttui::app::use_viewport(cx);
    // The page block's border + padding take 4 cells.
    Block::new()
        .border(BorderKind::Rounded)
        .title("About")
        .fill(t.surface)
        .layout(
            LayoutStyle::column()
                .gap(0)
                .grow(1.0)
                .padding(Edges::hv(1, 0))
                .clip(),
        )
        .child(
            Element::new()
                .focusable()
                .autofocus()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .child(card_view(store, theme, move || vp.get().w - 4))
                .build(),
        )
        .element(t)
        .build()
}

/// Open the About overlay (F1 / `?`); a connected console (re)reads
/// `GET /about`.
pub fn open(ctx: &Ctx, cx: Scope) {
    load(ctx);
    let store = ctx.store;
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    let size = Size::new(vp.w.clamp(1, 96), 16.min(vp.h - 2).max(1));
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        // The overlay's chrome (margin, border, padding) takes 6 cells.
        let t0 = theme.get().tokens;
        let close_btn = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("About", t0.accent)]))
            .child(card_view(store, theme, move || size.w - 6))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Close (Esc)")
                            .on_click(move || close_btn())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}
