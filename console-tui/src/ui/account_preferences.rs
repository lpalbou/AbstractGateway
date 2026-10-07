//! An account's client preferences (round 14, R14.2 → R14.3 TUI parity):
//! the web console's Accounts row "Preferences" action and its modal,
//! word for word (R14 PREFERENCES API — FINAL, R14-W2).
//!
//! `GET/PUT /accounts/{me | <id> | <tenant>:<id>}/preferences`. ONE
//! preference today: the default workflow PER APP (`default_workflow`,
//! a map interface → value | null). Each app row offers the gateway's
//! default first (`gateway_default_label`, verbatim: "Gateway default
//! (<name>)"), then every `choices[].label` verbatim; a pick applies at
//! once (one PUT of that interface only); "Saved." or "Not saved.
//! <message>" beside the row; a broken choice shows the gateway's `reason`.
//! Who may change it is the gateway's answer (`can_edit`; the row's
//! `actions.preferences`).

use abstracttui::prelude::*;
use serde_json::{json, Value};

use super::Ctx;
use crate::store::accounts::AccountRow;
use crate::store::json::WriteState;
use crate::store::Loadable;
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

/// The modal's title.
pub fn title(id: &str) -> String {
    format!("Preferences — {id}")
}

/// The modal's lead sentence.
pub fn lead(id: &str) -> String {
    format!(
        "The workflow each app runs for {id} unless a conversation picks another. Gateway default follows the admin's Default workflow per app."
    )
}

/// The row action's tooltip (the web's kit tooltip).
pub fn tip(id: &str) -> String {
    format!("Default workflows of {id}")
}

pub const SAVED: &str = "Saved.";
pub const READING: &str = "Reading the preferences...";

/// The route of a row (the web's `accountPreferencesPath`): your own row
/// is `me`; another tenant's user `<tenant>:<id>`; else the id.
pub fn path(r: &AccountRow) -> String {
    if r.own {
        return "/accounts/me/preferences".into();
    }
    let key = if r.tenant_id != "default" && !r.is_entity() {
        format!("{}:{}", r.tenant_id, r.id)
    } else {
        r.id.clone()
    };
    format!("/accounts/{}/preferences", crate::api::urlencode(&key))
}

/// One app row of the answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PrefApp {
    pub interface: String,
    pub label: String,
    pub help: Option<String>,
    /// The account's own choice (None = the gateway default).
    pub value: Option<String>,
    /// "default" | "set" | "broken".
    pub state: String,
    pub reason: Option<String>,
    pub gateway_default_label: String,
    /// `gateway_default.available == false` → its reason.
    pub gateway_default_unavailable: Option<String>,
    /// (value, label) in the gateway's order.
    pub choices: Vec<(String, String)>,
}

impl PrefApp {
    /// The options as the web's select lists them: the gateway default
    /// (None), a saved choice that no longer runs, then every choice.
    pub fn options(&self) -> Vec<(Option<String>, String)> {
        let mut out = vec![(None, self.gateway_default_label.clone())];
        if let Some(v) = &self.value {
            if !self.choices.iter().any(|(c, _)| c == v) {
                out.push((Some(v.clone()), format!("{v} (no longer runs)")));
            }
        }
        out.extend(
            self.choices
                .iter()
                .map(|(v, l)| (Some(v.clone()), l.clone())),
        );
        out
    }

    /// The label of the current option.
    pub fn current_label(&self) -> String {
        let opts = self.options();
        opts.iter()
            .find(|(v, _)| *v == self.value)
            .map(|(_, l)| l.clone())
            .unwrap_or_else(|| self.gateway_default_label.clone())
    }

    /// The state line under the row (the web's): a broken choice's reason,
    /// or why the gateway default is unavailable when it applies.
    pub fn state_line(&self) -> Option<String> {
        if self.state == "broken" {
            return self.reason.clone().filter(|r| !r.is_empty());
        }
        if self.value.is_none() {
            return self.gateway_default_unavailable.clone();
        }
        None
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Prefs {
    pub can_edit: bool,
    pub apps: Vec<PrefApp>,
}

fn st(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).map(str::to_string)
}

/// The GET/PUT answer, checked like the web (`accountPreferencesApp`):
/// a missing field fails loudly with the web's seam sentence.
pub fn parse(v: &Value) -> Result<Prefs, String> {
    let apps = v
        .get("apps")
        .and_then(Value::as_array)
        .ok_or("GET /accounts/{id}/preferences answered without apps (R14 preferences seam).")?;
    let mut out = Vec::new();
    for a in apps {
        for k in ["interface", "label", "gateway_default_label", "state"] {
            if st(a, k).filter(|s| !s.is_empty()).is_none() {
                return Err(format!(
                    "GET /accounts/{{id}}/preferences app row has no {k} (R14 preferences seam)."
                ));
            }
        }
        let iface = st(a, "interface").unwrap_or_default();
        let choices = a.get("choices").and_then(Value::as_array).ok_or_else(|| {
            format!("GET /accounts/{{id}}/preferences app {iface} has no choices (R14 preferences seam).")
        })?;
        let gd = a.get("gateway_default");
        out.push(PrefApp {
            interface: iface,
            label: st(a, "label").unwrap_or_default(),
            help: st(a, "help").filter(|h| !h.is_empty()),
            value: st(a, "value").filter(|v| !v.is_empty()),
            state: st(a, "state").unwrap_or_default(),
            reason: st(a, "reason"),
            gateway_default_label: st(a, "gateway_default_label").unwrap_or_default(),
            gateway_default_unavailable: gd
                .filter(|g| g.get("available").and_then(Value::as_bool) == Some(false))
                .and_then(|g| st(g, "reason"))
                .filter(|r| !r.is_empty()),
            choices: choices
                .iter()
                .filter_map(|c| {
                    let value = c.get("value").map(|v| match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })?;
                    let label = st(c, "label")
                        .filter(|l| !l.is_empty())
                        .or_else(|| st(c, "name").filter(|l| !l.is_empty()))
                        .unwrap_or_else(|| value.clone());
                    Some((value, label))
                })
                .collect(),
        });
    }
    Ok(Prefs {
        can_edit: v.get("can_edit").and_then(Value::as_bool) != Some(false),
        apps: out,
    })
}

/// The PUT body of one pick: that interface only (unnamed ones keep).
pub fn put_body(iface: &str, value: Option<&str>) -> Value {
    json!({"default_workflow": {iface: value}})
}

/// "Not saved. <the gateway's message>".
pub fn refusal(e: &crate::api::ApiError) -> String {
    format!("Not saved. {}", super::workspace_chooser::error_sentence(e))
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Tone {
    Ok,
    Error,
}

#[derive(Clone, Copy)]
struct St {
    /// (interface, text, tone) beside each row.
    notes: Signal<Vec<(String, String, Tone)>>,
    /// The interface of the write in flight.
    pending: Signal<Option<String>>,
}

/// `p` / the ⊜ button on an Accounts row: the row's Preferences (R15: one
/// form modal — each app a Select, every commit one PUT, "Saved." /
/// "Not saved. …" under the row).
pub fn open(cx: Scope, ctx: &Ctx, row: AccountRow) {
    let slot = format!("prefs.{}.{}", row.tenant_id, row.id);
    let wk = format!("{slot}.write");
    let p = path(&row);
    ctx.store.json.set(&slot, Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get(&slot, p.clone())));
    let c = ctx.clone();
    let id = row.id.clone();
    super::w::FormModal::new(title(&id))
        .lead(lead(&id))
        .size(96, 30)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let st = St {
                notes: mcx.signal(Vec::new()),
                pending: mcx.signal(None),
            };
            {
                let store = c.store;
                let wk = wk.clone();
                mcx.effect(move || {
                    let wst = store.json.write(&wk);
                    let Some(iface) = st.pending.get_untracked() else {
                        return;
                    };
                    let (text, tone) = match wst {
                        Some(WriteState::Done(_)) => (SAVED.to_string(), Tone::Ok),
                        Some(WriteState::Failed(e)) => (refusal(&e), Tone::Error),
                        _ => return,
                    };
                    st.notes.update(|n| {
                        n.retain(|(k, _, _)| *k != iface);
                        n.push((iface.clone(), text, tone));
                    });
                    st.pending.set(None);
                    store.json.set_write(&wk, None);
                });
            }
            let (c2, slot2, wk2, p2, id2) =
                (c.clone(), slot.clone(), wk.clone(), p.clone(), id.clone());
            let t = use_theme(mcx).get().tokens;
            let close2 = close.clone();
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                // The rows scroll (a short terminal never crushes a Select).
                .child(
                    abstracttui::widgets::Scroll::new(dyn_view_scoped(
                        LayoutStyle::column().shrink(0.0),
                        move |gcx| body(gcx, &c2, &slot2, &wk2, &p2, &id2, st, w),
                    ))
                    .axes(false, true)
                    .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                    .scrollbar_auto_hide(true)
                    .view(mcx),
                )
                .child(super::w::form::button_row(vec![super::w::action::button(
                    mcx,
                    &t,
                    &super::w::Action::label("close", "Close"),
                    super::w::action::On::Raised,
                    true,
                    move || close2(),
                )]))
                .build()
        });
}

#[allow(clippy::too_many_arguments)]
fn body(
    cx: Scope,
    ctx: &Ctx,
    slot: &str,
    wk: &str,
    path: &str,
    id: &str,
    st: St,
    width: i32,
) -> View {
    use super::w::form::sentence;
    let t = use_theme(cx).get().tokens;
    let prefs = match ctx.store.json.get(slot) {
        Loadable::Ready(v) => parse(&v),
        Loadable::Failed(e) => Err(super::workspace_chooser::error_sentence(&e)),
        _ => return sentence(&t, READING, width, t.text_muted),
    };
    let prefs = match prefs {
        Ok(p) => p,
        Err(e) => {
            return sentence(
                &t,
                &format!("Could not read the preferences of {id}: {e}"),
                width,
                t.error,
            )
        }
    };
    let label_w = prefs
        .apps
        .iter()
        .map(|a| abstracttui::text::width(&a.label) + 2)
        .max()
        .unwrap_or(10)
        .min(width / 2);
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    for (ai, app) in prefs.apps.iter().enumerate() {
        let opts = app.options();
        let cur = opts.iter().position(|(v, _)| *v == app.value).unwrap_or(0);
        let chosen = cx.signal(cur);
        let select = if prefs.can_edit {
            let c = ctx.clone();
            let app2 = app.clone();
            let opts2 = opts.clone();
            let (wk, path) = (wk.to_string(), path.to_string());
            let el = abstracttui::app::select::Select::new(
                opts.iter()
                    .map(|(_, l)| abstracttui::app::select::SelectOption::new(l.clone()))
                    .collect(),
            )
            .value(chosen)
            .on_change(move |i| {
                let Some((value, _)) = opts2.get(i) else { return };
                if *value == app2.value || st.pending.with_untracked(Option::is_some) {
                    return;
                }
                st.pending.set(Some(app2.interface.clone()));
                st.notes.update(|n| n.retain(|(k, _, _)| *k != app2.interface));
                c.store.json.set_write(&wk, Some(WriteState::Pending));
                let slot = wk.trim_end_matches(".write").to_string();
                c.send(Cmd::Json(JsonCmd::Send {
                    key: wk.clone(),
                    method: "PUT".into(),
                    path: path.clone(),
                    body: put_body(&app2.interface, value.as_deref()),
                    slow: false,
                    label: format!("{} — {}", app2.label, "Default workflow"),
                    reload: vec![(slot, path.clone())],
                    journal: false,
                }));
            })
            .element(cx, &t);
            // The first Select takes the keyboard when the modal opens.
            if ai == 0 { el.autofocus() } else { el }.build()
        } else {
            sentence(&t, &app.current_label(), width - label_w, t.text)
        };
        let label = super::w::paint::fill_line(
            LayoutStyle::default()
                .width(Dimension::Cells(label_w))
                .height(Dimension::Cells(1))
                .shrink(0.0),
            vec![super::w::Ink::new(&app.label, t.text)],
            None,
        );
        let label = super::w::tip::with_tip(
            cx,
            Element::new()
                .style(LayoutStyle::default().width(Dimension::Cells(label_w)).height(Dimension::Cells(1)).shrink(0.0))
                .child(label),
            app.help.clone().unwrap_or_default(),
        )
        .build();
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
                .child(label)
                .child(
                    Element::new()
                        .style(LayoutStyle::default().width(Dimension::Cells((width - label_w).min(56))).height(Dimension::Cells(1)))
                        .child(select)
                        .build(),
                )
                .build(),
        );
        if let Some(h) = &app.help {
            col = col.child(sentence(&t, h, width, t.text_faint));
        }
        if let Some(sl) = app.state_line() {
            col = col.child(sentence(&t, &sl, width, t.warn));
        }
        // The row's live state line (its own region: an outcome never
        // rebuilds the Selects, so focus stays where the keyboard is).
        {
            let iface = app.interface.clone();
            col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                let t = abstracttui::app::current_theme().tokens;
                if st.pending.get().as_deref() == Some(iface.as_str()) {
                    return sentence(&t, "Saving…", width, t.text_muted);
                }
                let note = st.notes.with(|n| {
                    n.iter()
                        .find(|(k, _, _)| *k == iface)
                        .map(|(_, t, tone)| (t.clone(), *tone))
                });
                match note {
                    Some((text, tone)) => {
                        sentence(&t, &text, width, if tone == Tone::Ok { t.ok } else { t.error })
                    }
                    None => Element::new().style(LayoutStyle::default().h(0)).build(),
                }
            }));
        }
        col = col.child(Element::new().style(LayoutStyle::line(1).shrink(0.0)).build());
    }
    col.build()
}




#[cfg(test)]
mod tests {
    use super::*;

    fn fx(name: &str) -> Value {
        let path = format!(
            "{}/tests/fixtures/r14w3_{name}.json",
            env!("CARGO_MANIFEST_DIR")
        );
        serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
    }

    #[test]
    fn the_answer_parses_with_the_gateway_default_first() {
        let p = parse(&fx("prefs_alice_set")).unwrap();
        assert!(p.can_edit);
        let code = &p.apps[0];
        assert_eq!(code.label, "AbstractCode — chat agent");
        assert_eq!(code.value.as_deref(), Some("codeact-agent:codeact"));
        assert_eq!(code.current_label(), "CodeAct agent");
        let opts = code.options();
        assert_eq!(opts[0], (None, "Gateway default (Basic agent)".to_string()));
        assert_eq!(opts.len(), code.choices.len() + 1);
        let asst = &p.apps[1];
        assert_eq!(asst.value, None);
        assert_eq!(asst.current_label(), asst.gateway_default_label);
    }

    #[test]
    fn a_choice_that_no_longer_runs_is_listed_and_broken_says_why() {
        let mut v = fx("prefs_alice_set");
        v["apps"][0]["value"] = json!("gone:flow");
        v["apps"][0]["state"] = json!("broken");
        v["apps"][0]["reason"] = json!("The workflow gone:flow is not on this gateway.");
        let p = parse(&v).unwrap();
        assert_eq!(p.apps[0].options()[1].1, "gone:flow (no longer runs)");
        assert_eq!(
            p.apps[0].state_line().as_deref(),
            Some("The workflow gone:flow is not on this gateway.")
        );
    }

    #[test]
    fn a_partial_answer_fails_loudly() {
        let mut v = fx("prefs_alice_set");
        v["apps"][0]
            .as_object_mut()
            .unwrap()
            .remove("gateway_default_label");
        assert_eq!(
            parse(&v).unwrap_err(),
            "GET /accounts/{id}/preferences app row has no gateway_default_label (R14 preferences seam)."
        );
        assert!(parse(&json!({"ok": true}))
            .unwrap_err()
            .contains("answered without apps"));
    }

    #[test]
    fn words_and_bodies() {
        assert_eq!(title("alice"), "Preferences — alice");
        assert_eq!(tip("alice"), "Default workflows of alice");
        assert_eq!(
            put_body("abstractcode.agent.v1", None),
            json!({"default_workflow": {"abstractcode.agent.v1": null}})
        );
        let body = fx("prefs_alice_refused");
        let e = crate::api::ApiError {
            kind: crate::api::ApiErrorKind::Http(400),
            message: body["detail"].to_string(),
            body: Some(body["detail"].clone()),
            timed_out: false,
        };
        assert_eq!(
            refusal(&e),
            "Not saved. default_workflow.abstractcode.agent.v1 = 'nope:zzz' refused: workflow bundle 'nope' is not on this gateway."
        );
    }
}
