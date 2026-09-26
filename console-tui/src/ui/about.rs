//! The About modal (F1, `?`, or the Connection screen's About button): this
//! console's identity from the vendored AbstractFramework descriptor, then
//! the connected gateway's versions from its public `GET /about`, formatted
//! by [`crate::identity::gateway_version_rows`] (the twin of every other
//! app's About). A gateway that cannot answer is ONE visible row, never a
//! blank.

use abstracttui::prelude::*;
use serde_json::Value;

use super::util::{line, span, span_bold};
use super::{open_form, Ctx};
use crate::identity::{about_rows, gateway_version_rows, this_app};
use crate::store::Loadable;
use crate::worker::Cmd;

/// The gateway rows for the modal's current state: `connected` = the
/// console holds a verified connection; `about` = the `GET /about` slot.
pub fn gateway_rows(connected: bool, about: &Loadable<Value>) -> Vec<(String, String)> {
    match about {
        Loadable::Ready(v) => gateway_version_rows(Some(v), None),
        Loadable::Failed(e) => gateway_version_rows(None, Some(&e.to_string())),
        Loadable::Loading => vec![("Gateway".into(), "reading GET /api/gateway/about…".into())],
        Loadable::NotAsked if connected => {
            vec![("Gateway".into(), "reading GET /api/gateway/about…".into())]
        }
        Loadable::NotAsked => gateway_version_rows(
            None,
            Some("not connected to a gateway — probe on the Connection screen"),
        ),
    }
}

/// Every line the modal shows, in order (label: value, or a bare line).
pub fn modal_rows(connected: bool, about: &Loadable<Value>) -> Vec<(String, String)> {
    about_rows(&this_app(), &gateway_rows(connected, about))
}

/// Open the About modal; a connected console (re)reads `GET /about`.
pub fn open(ctx: &Ctx, cx: Scope) {
    let store = ctx.store;
    let connected = store.conn.get_untracked().is_connected();
    if connected {
        store.about.set(Loadable::Loading);
        ctx.send(Cmd::LoadAbout);
    } else {
        store.about.set(Loadable::NotAsked);
    }
    // The gateway lists every installed abstract* package: take the
    // terminal's height (the rows are few dozen at most).
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    let size = Size::new(vp.w.min(104).max(1), (vp.h - 2).min(46).max(1));
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let close_btn = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("About", t0.accent)]))
            .child(dyn_view(
                LayoutStyle::column().gap(0).grow(1.0),
                move || {
                    let t = theme.get().tokens;
                    let rows = modal_rows(store.conn.get().is_connected(), &store.about.get());
                    let views: Vec<View> = rows
                        .into_iter()
                        .map(|(k, v)| {
                            if k.is_empty() {
                                line(vec![span(v, t.text)])
                            } else {
                                let ink = if v.starts_with("unavailable") {
                                    t.warn
                                } else {
                                    t.text
                                };
                                line(vec![span(format!("{k}: "), t.text_muted), span(v, ink)])
                            }
                        })
                        .collect();
                    Element::new()
                        .style(LayoutStyle::column().gap(0))
                        .children(views)
                        .build()
                },
            ))
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
