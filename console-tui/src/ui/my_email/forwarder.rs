//! My email → Notifications → **Push forwarder** (admins; framework backlog
//! 1049; web: `console_push.py`), beside Devices: where this gateway sends the
//! alerts for watches and iPhones, gateway-wide. One row from
//! `GET /push/settings` — "Built-in forwarder" | "Off" | the URL, and
//! "set by the launch flag for this run" when `--push-relay` wins — then an
//! **Edit** that offers the three choices (Built-in forwarder | Off | Your own
//! forwarder + its address) and one Save (`PUT /push/settings {mode, url?}`).
//! The status line says what the forwarder answered (its apps) or why it
//! could not be reached; the setting is saved either way.

use abstracttui::prelude::*;
use serde_json::{json, Value};

use super::{cycle, field, gap, helper, input, line, span, span_bold, Page};
use crate::store::email::email_error_text;
use crate::store::json::WriteState;
use crate::store::Loadable;
use crate::ui::Ctx;
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

/// The JSON lane's read of `GET /push/settings` (this page's own key).
pub const K_FORWARDER: &str = "my_email.push_forwarder";
/// The outcome of the Save.
pub const K_FORWARDER_WRITE: &str = "my_email.push_forwarder.write";
const FORWARDER_PATH: &str = "/push/settings";
pub const FORWARDER: &str = "Push forwarder";
pub const FORWARDER_HELP: &str = "For the whole gateway (admins): where alerts for watches and iPhones go. Nothing to set unless you run your own forwarder.";
pub const BUILT_IN: &str = "Built-in forwarder";
pub const NONE_BUILT_IN: &str = "none in this release yet, so no alerts are sent";
pub const FLAG_WINS: &str = "set by the launch flag for this run";
pub const URL_HELP: &str =
    "https://, or http:// for a forwarder on this computer (e.g. http://127.0.0.1:8095).";
/// The three choices, in the web's order.
pub const FORWARDER_MODES: &[(&str, &str)] = &[
    ("default", BUILT_IN),
    ("off", "Off"),
    ("url", "Your own forwarder"),
];

/// The gateway's `forwarder` (`GET|PUT /push/settings`).
#[derive(Clone, Debug, PartialEq)]
pub struct Forwarder {
    pub mode: String,
    pub url: String,
    pub effective: String,
    pub source: String,
}

fn st(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

/// The forwarder of an answer, or None when the answer is not one (a seam fault).
pub fn forwarder(v: &Value) -> Option<Forwarder> {
    let f = v.get("forwarder")?;
    let (mode, source) = (st(f, "mode"), st(f, "source"));
    if !["default", "off", "url"].contains(&mode.as_str())
        || !["flag", "setting", "packaged"].contains(&source.as_str())
    {
        return None;
    }
    Some(Forwarder {
        mode,
        url: st(f, "url"),
        effective: st(f, "effective"),
        source,
    })
}

impl Forwarder {
    /// (the state in words, the note after it) — the web's row.
    pub fn words(&self) -> (String, String) {
        if self.source == "flag" {
            let v = if self.effective.is_empty() {
                "Off".to_string()
            } else {
                self.effective.clone()
            };
            return (v, FLAG_WINS.to_string());
        }
        match self.mode.as_str() {
            "off" => ("Off".into(), String::new()),
            "url" => (self.url.clone(), String::new()),
            _ => (
                BUILT_IN.into(),
                if self.effective.is_empty() {
                    NONE_BUILT_IN.into()
                } else {
                    String::new()
                },
            ),
        }
    }

    /// While the flag wins: what the saved choice is and when it applies.
    pub fn later(&self) -> Option<String> {
        if self.source != "flag" {
            return None;
        }
        let saved = match self.mode.as_str() {
            "url" => self.url.as_str(),
            "off" => "Off",
            _ => BUILT_IN,
        };
        Some(format!(
            "Your saved choice ({saved}) applies when the gateway starts without the flag."
        ))
    }
}

/// The Save's sentence: the forwarder's answer to the one `/v1/info` probe.
pub fn probe_words(v: &Value) -> String {
    let Some(probe) = v.get("probe").filter(|p| p.is_object()) else {
        return "Saved.".into();
    };
    if probe.get("reachable").and_then(Value::as_bool) == Some(true) {
        let apps: Vec<&str> = probe
            .get("topics")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        return if apps.is_empty() {
            "Saved. The forwarder answered.".into()
        } else {
            format!(
                "Saved. The forwarder answered: it pushes to {}.",
                apps.join(", ")
            )
        };
    }
    format!(
        "Saved, but the forwarder could not be reached: {}",
        st(probe, "reason")
    )
}

/// `{mode}` or `{mode, url}` (the address trimmed; the gateway validates it).
pub fn put_body(mode: &str, url: &str) -> Value {
    if mode == "url" {
        json!({"mode": mode, "url": url.trim()})
    } else {
        json!({ "mode": mode })
    }
}

fn error_text(e: &crate::api::ApiError) -> String {
    // The gateway's 422 says what is wrong with the address in one sentence.
    if let Some(d) = e
        .body
        .as_ref()
        .and_then(|b| b.get("detail"))
        .and_then(Value::as_str)
    {
        return d.to_string();
    }
    if matches!(e.kind, crate::api::ApiErrorKind::Http(_)) && !e.message.is_empty() {
        return e.message.clone();
    }
    email_error_text(e)
}

/// The card's own state (modal scope: survives every re-render).
#[derive(Clone, Copy)]
pub(super) struct FwdForm {
    editing: Signal<bool>,
    mode: Signal<String>,
    url: Signal<String>,
    busy: Signal<bool>,
    status: Signal<Option<Result<String, String>>>,
}

impl FwdForm {
    pub(super) fn new(cx: Scope) -> FwdForm {
        FwdForm {
            editing: cx.signal(false),
            mode: cx.signal("default".to_string()),
            url: cx.signal(String::new()),
            busy: cx.signal(false),
            status: cx.signal(None),
        }
    }
}

fn is_admin(ctx: &Ctx) -> bool {
    ctx.store.conn.with_untracked(|c| c.is_admin())
}

/// Admins: read the setting when the page opens, and land the Save.
pub(super) fn install(mcx: Scope, ctx: &Ctx, p: Page) {
    if !is_admin(ctx) {
        return;
    }
    let f = p.fwd;
    ctx.store.json.set_write(K_FORWARDER_WRITE, None);
    ctx.send(Cmd::Json(JsonCmd::get(K_FORWARDER, FORWARDER_PATH)));
    let store = ctx.store;
    mcx.effect(move || {
        let out = match store.json.write(K_FORWARDER_WRITE) {
            Some(WriteState::Done(v)) => Ok(v),
            Some(WriteState::Failed(e)) => Err(error_text(&e)),
            _ => return,
        };
        store.json.set_write(K_FORWARDER_WRITE, None);
        f.busy.set(false);
        match out {
            Ok(v) => {
                f.editing.set(false);
                f.status.set(Some(Ok(probe_words(&v))));
            }
            Err(e) => f.status.set(Some(Err(e))),
        }
    });
}

fn open_edit(ctx: &Ctx, f: FwdForm) {
    if let Loadable::Ready(v) = ctx.store.json.get_untracked(K_FORWARDER) {
        if let Some(fw) = forwarder(&v) {
            f.mode.set(fw.mode.clone());
            f.url.set(fw.url.clone());
        }
    }
    f.status.set(None);
    f.editing.set(true);
}

fn save(ctx: &Ctx, f: FwdForm) {
    if f.busy.get_untracked() {
        return;
    }
    let mode = f.mode.get_untracked();
    let url = f.url.get_untracked();
    if mode == "url" && url.trim().is_empty() {
        f.status.set(Some(Err(
            "Give the forwarder's address, e.g. https://push.example.org.".into(),
        )));
        return;
    }
    f.status.set(None);
    f.busy.set(true);
    ctx.store
        .json
        .set_write(K_FORWARDER_WRITE, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: K_FORWARDER_WRITE.into(),
        method: "PUT".into(),
        path: FORWARDER_PATH.into(),
        body: put_body(&mode, &url),
        // The Save probes the forwarder once (up to 5 s).
        slow: true,
        label: "Push forwarder".into(),
        reload: vec![(K_FORWARDER.into(), FORWARDER_PATH.into())],
        journal: true,
    }));
}

/// The card (admins only): its sentence, the row (or the Edit form), the status line.
pub(super) fn section(_cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    if !is_admin(ctx) {
        return Element::new().style(LayoutStyle::default().h(0)).build();
    }
    let t0 = *t;
    let f = p.fwd;
    let store = ctx.store;
    let ctx_body = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(vec![span_bold(FORWARDER.to_string(), t0.text)]))
        .child(helper(&t0, FORWARDER_HELP, p.wrap_w))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |bcx| {
                let t = t0;
                let fw = match store.json.get(K_FORWARDER) {
                    Loadable::Ready(v) => match forwarder(&v) {
                        Some(fw) => fw,
                        None => {
                            return helper(
                                &t,
                                "  The gateway's answer has no forwarder (gateway seam: GET /push/settings).",
                                p.wrap_w,
                            )
                        }
                    },
                    Loadable::Failed(e) => {
                        return helper(&t, &format!("  Could not read it: {}", error_text(&e)), p.wrap_w)
                    }
                    _ => return line(vec![span("  \u{25cc} Reading...", t.info)]),
                };
                if f.editing.get() {
                    return edit_form(bcx, &ctx_body, &t, p);
                }
                let (value, note) = fw.words();
                let mut spans = vec![span(format!("  {value}"), t.text)];
                if !note.is_empty() {
                    spans.push(span(format!(" \u{b7} {note}"), t.text_muted));
                }
                let ctx_edit = ctx_body.clone();
                let mut col = Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(line(spans));
                if let Some(later) = fw.later() {
                    col = col.child(helper(&t, &format!("  {later}"), p.wrap_w));
                }
                col.child(
                    Element::new()
                        .style(LayoutStyle::row().h(1).shrink(0.0))
                        .child(Element::new().style(LayoutStyle::default().w(2).h(1).shrink(0.0)).build())
                        .child(
                            Button::new("Edit")
                                .on_click(move || open_edit(&ctx_edit, f))
                                .element(bcx, &t)
                                .build(),
                        )
                        .build(),
                )
                .build()
            },
        ))
        .child(dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
            let t = t0;
            match f.status.get() {
                Some(Ok(s)) => helper(&t, &format!("  {s}"), p.wrap_w),
                Some(Err(e)) => {
                    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                    for l in crate::ui::util::wrap_text(&e, p.wrap_w.saturating_sub(2).max(10)) {
                        col = col.child(line(vec![span(format!("  {l}"), t.error)]));
                    }
                    col.build()
                }
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        }))
        .child(gap())
        .build()
}

/// Edit: the three choices, the address for "Your own forwarder", Save / Cancel.
fn edit_form(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let f = p.fwd;
    let w = (p.wrap_w as i32 - 22).clamp(16, 48);
    let ctx_save = ctx.clone();
    let ctx_submit = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(cycle(
            cx,
            &t0,
            "Send alerts through",
            f.mode,
            FORWARDER_MODES,
            |_| {},
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |ucx| {
                let t = t0;
                if f.mode.get() != "url" {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                let ctx_submit = ctx_submit.clone();
                Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(field(
                        &t,
                        "Its address",
                        input(f.url, w, false)
                            .on_submit(move |_| save(&ctx_submit, f))
                            .element(ucx, &t)
                            .build(),
                    ))
                    .child(helper(&t, URL_HELP, p.wrap_w))
                    .build()
            },
        ))
        .child(gap())
        .child(dyn_view_scoped(
            LayoutStyle::row().gap(2).h(1).shrink(0.0),
            move |bcx| {
                let t = t0;
                let ctx_save = ctx_save.clone();
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new(if f.busy.get() { "Saving…" } else { "Save" })
                            .on_click(move || save(&ctx_save, f))
                            .element(bcx, &t)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel")
                            .on_click(move || {
                                if !f.busy.get_untracked() {
                                    f.editing.set(false);
                                    f.status.set(None);
                                }
                            })
                            .element(bcx, &t)
                            .build(),
                    )
                    .build()
            },
        ))
        .build()
}
