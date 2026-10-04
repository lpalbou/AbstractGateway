//! The Apps page's settings overlays (R8.1, round 8): the web console's
//! settings modal behind the gears — "Apps settings" (the Apps toolbar
//! gear: Node.js, ports, npm registry, Node.js download index; the
//! deprecated "Where apps listen" only while it still holds a saved value)
//! and Continuum's settings (the gear on the Continuum card: backlog
//! folder, backlog exec runner, process manager). They replace the two
//! "Advanced" disclosures that sat under the cards.
//!
//! No Save anywhere: a text row applies on Enter (Esc keeps the old
//! value; empty = back to the default / the gateway's own folder), a
//! switch applies at once, and each row says "Saved" or "Not saved: <the
//! gateway's sentence>" beside itself. One write path: `POST
//! /admin/runtime-config` with only that row's key (the web's).

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use serde_json::{json, Value};

use super::kit;
use super::util::{line, span, span_bold};
use super::Ctx;
use crate::store::skills::Tone;
use crate::store::{Loadable, RuntimeConfigData};
use crate::worker::Cmd;

/// Which overlay.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Which {
    /// The Apps toolbar gear.
    Apps,
    /// The gear on the Continuum card.
    Continuum,
}

impl Which {
    pub fn title(self) -> &'static str {
        match self {
            Which::Apps => "Apps settings",
            Which::Continuum => "Continuum settings",
        }
    }
}

/// One settings row.
#[derive(Clone, Debug, PartialEq)]
pub struct SRow {
    /// The runtime-config key the row writes (`apps.node`,
    /// `triage_repo_root`, `backlog_exec_runner`, …).
    pub key: String,
    pub label: String,
    pub help: String,
    /// The value in effect, as shown.
    pub now: String,
    /// The SAVED value the input starts from ("" = not saved).
    pub saved: String,
    pub placeholder: String,
    /// Where the value comes from, in words ("Saved setting", …).
    pub source: String,
    /// Some(on) for a switch row.
    pub switch: Option<bool>,
    /// Why this row can't be changed here (a launch flag), if so.
    pub locked: Option<String>,
    /// A warning line (set aside / not available).
    pub warn: Option<String>,
}

fn apps_source(source: &str) -> &'static str {
    match source {
        "stored" => "Saved setting",
        "env" => "From the environment",
        _ => "Default",
    }
}

/// The rows of `which`, from the config read (the web renders the
/// gateway's registry as listed — never a hardcoded list).
pub fn rows(which: Which, d: &RuntimeConfigData) -> Vec<SRow> {
    match which {
        Which::Apps => d
            .apps
            .iter()
            // The deprecated host only while it still holds a saved value
            // (so an old 0.0.0.0 can be cleared).
            .filter(|a| a.name != "host" || a.source == "stored")
            .map(|a| SRow {
                key: if a.key.is_empty() {
                    format!("apps.{}", a.name)
                } else {
                    a.key.clone()
                },
                label: a.label.clone(),
                help: a.help.clone(),
                now: if a.value.is_empty() {
                    "(none)".into()
                } else {
                    a.value.clone()
                },
                saved: if a.source == "stored" {
                    a.value.clone()
                } else {
                    String::new()
                },
                placeholder: a.placeholder.clone(),
                source: apps_source(&a.source).into(),
                switch: None,
                locked: None,
                warn: (!a.invalid.is_empty()).then(|| format!("Set aside: {}", a.invalid)),
            })
            .collect(),
        Which::Continuum => d
            .backlog
            .iter()
            .map(|b| {
                let locked = (b.source == "flag").then(|| {
                    if b.flag.is_empty() {
                        "Set by a launch flag for this run.".to_string()
                    } else {
                        format!("Set by the launch flag {} for this run.", b.flag)
                    }
                });
                let source = match b.source.as_str() {
                    "flag" => "Launch flag",
                    "stored" => "Saved setting",
                    "env" => "From the environment",
                    _ if b.is_folder() => "The gateway's own folder",
                    _ => "Default",
                }
                .to_string();
                SRow {
                    key: b.key.clone(),
                    label: b.label.clone(),
                    help: b.help.clone(),
                    now: if b.redacted {
                        "(hidden — admin only)".into()
                    } else if b.value.is_empty() {
                        "(none)".into()
                    } else {
                        b.value.clone()
                    },
                    saved: b.saved.clone(),
                    placeholder: b.default_path.clone(),
                    source,
                    switch: (!b.is_folder()).then(|| b.value == "on"),
                    // The folder stays editable under a flag (the saved
                    // value applies at the next start); a switch does not.
                    locked: if b.is_folder() { None } else { locked },
                    warn: (b.available == Some(false))
                        .then(|| format!("Not available: {}", b.reason)),
                }
            })
            .collect(),
    }
}

/// The POST body for one text row (empty = back to the default; the
/// backlog folder sends null = the gateway's own folder).
pub fn text_body(row: &SRow, typed: &str) -> Value {
    let now = typed.trim();
    if row.key == "triage_repo_root" {
        if now.is_empty() {
            return json!({ "triage_repo_root": Value::Null });
        }
        return json!({ "triage_repo_root": now });
    }
    json!({ row.key.clone(): now })
}

/// The overlay's state (lives with the overlay's scope).
#[derive(Clone, Copy)]
struct St {
    sel: Signal<usize>,
    editing: Signal<Option<String>>,
    draft: Signal<String>,
    /// (key, text, tone) beside each row.
    notes: Signal<Vec<(String, String, Tone)>>,
    /// (form id, key) of writes in flight.
    pending: Signal<Vec<(u64, String)>>,
}

fn note_of(st: &St, key: &str) -> Option<(String, Tone)> {
    st.notes.with(|n| {
        n.iter()
            .find(|(k, _, _)| k == key)
            .map(|(_, t, tone)| (t.clone(), *tone))
    })
}

fn set_note(st: &St, key: &str, text: &str, tone: Tone) {
    st.notes.update(|n| {
        n.retain(|(k, _, _)| k != key);
        n.push((key.to_string(), text.to_string(), tone));
    });
}

fn send(ctx: &Ctx, st: &St, key: &str, body: Value) {
    let fid = crate::worker::next_form_id();
    st.pending.update(|p| p.push((fid, key.to_string())));
    set_note(st, key, "Saving...", Tone::Plain);
    ctx.send(Cmd::SaveRuntimeConfig {
        body: body.into(),
        form_id: Some(fid),
    });
}

/// Open the overlay (admin; the config is read first when it never was).
pub fn open(cx: Scope, ctx: &Ctx, which: Which) {
    if !super::util::admin_gate(&ctx.store, "changing the apps settings") {
        return;
    }
    match ctx.store.runtime_config.get_untracked() {
        Loadable::Ready(_) => {}
        Loadable::Failed(e) => {
            ctx.store
                .notice
                .set(Some(format!("Could not read the apps settings: {e}")));
            return;
        }
        Loadable::NotAsked => {
            ctx.store.runtime_config.set(Loadable::Loading);
            ctx.send(Cmd::LoadRuntimeConfig);
            ctx.store
                .notice
                .set(Some("Reading the apps settings... — one moment".into()));
            return;
        }
        Loadable::Loading => {
            ctx.store
                .notice
                .set(Some("Reading the apps settings... — one moment".into()));
            return;
        }
    }
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        which.title(),
        &[("↑/↓", "choose"), ("Enter", "edit"), ("space", "switch")],
        move |mcx, _close, guard| {
            let st = St {
                sel: mcx.signal(0),
                editing: mcx.signal(None),
                draft: mcx.signal(String::new()),
                notes: mcx.signal(Vec::new()),
                pending: mcx.signal(Vec::new()),
            };
            // Each write's outcome beside its row.
            {
                let ui = c.ui;
                mcx.effect(move || {
                    if let Some((fid, out)) = ui.write_done.get() {
                        let key = st.pending.with_untracked(|p| {
                            p.iter().find(|(f, _)| *f == fid).map(|(_, k)| k.clone())
                        });
                        if let Some(key) = key {
                            ui.write_done.set(None);
                            st.pending.update(|p| p.retain(|(f, _)| *f != fid));
                            match out {
                                Ok(_) => {
                                    set_note(&st, &key, "Saved", Tone::Ok);
                                    if st.editing.get_untracked().as_deref() == Some(key.as_str()) {
                                        st.editing.set(None);
                                    }
                                }
                                Err(e) => {
                                    set_note(&st, &key, &format!("Not saved: {e}"), Tone::Error)
                                }
                            }
                        }
                    }
                });
            }
            // Esc first closes an open edit (the overlay stays).
            *guard.borrow_mut() = Some(Box::new(move || {
                if st.editing.get_untracked().is_some() {
                    st.editing.set(None);
                    return true;
                }
                false
            }));
            let body = c.clone();
            Element::new()
                .focusable()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .child(dyn_view_scoped(
                    LayoutStyle::column().gap(0).grow(1.0),
                    move |gcx| overlay_body(gcx, &body, which, st),
                ))
                .build()
        },
    );
}

fn overlay_body(cx: Scope, ctx: &Ctx, which: Which, st: St) -> View {
    let t = use_theme(cx).get().tokens;
    let width = (abstracttui::app::use_viewport(cx).get().w - 8).max(20);
    let d = match ctx.store.runtime_config.get() {
        Loadable::Ready(d) => d,
        Loadable::Failed(e) => {
            return kit::sentence(
                &t,
                &format!("Could not read the {}. {e}", which.title().to_lowercase()),
                width,
                t.error,
            )
        }
        _ => {
            return kit::sentence(
                &t,
                &format!("Reading the {}...", which.title().to_lowercase()),
                width,
                t.text_muted,
            )
        }
    };
    let list = rows(which, &d);
    let at = st.sel.get().min(list.len().saturating_sub(1));
    let editing = st.editing.get();
    let _ = st.notes.get();
    let mut col = Element::new().style(LayoutStyle::column().gap(0));
    if list.is_empty() {
        col = col.child(kit::sentence(
            &t,
            "This gateway reports no settings here.",
            width,
            t.text_muted,
        ));
    }
    if !d.writable {
        col = col.child(kit::sentence(
            &t,
            "Only an admin can change these.",
            width,
            t.warn,
        ));
    }
    for (i, r) in list.iter().enumerate() {
        let selected = i == at;
        let ink = if selected { t.accent } else { t.text };
        let mark = if selected { "▸ " } else { "  " };
        let note = note_of(&st, &r.key);
        match r.switch {
            Some(on) => {
                let mut spans = vec![
                    span(mark, ink),
                    span_bold(
                        super::switch::switch_text(&r.label, on, None, r.locked.is_some()),
                        ink,
                    ),
                    span(format!("  {}", r.source), t.text_faint),
                ];
                if let Some((text, tone)) = &note {
                    spans.push(span(format!("  {text}"), tone_ink(&t, *tone)));
                }
                col = col.child(line(spans));
            }
            None if editing.as_deref() == Some(r.key.as_str()) => {
                let c = ctx.clone();
                let row = r.clone();
                col = col.child(kit::inline_input(
                    cx,
                    &t,
                    &format!("  {}:", r.label),
                    st.draft,
                    r.placeholder.clone(),
                    move |typed| {
                        if typed.trim() == row.saved.trim() {
                            st.editing.set(None);
                            return;
                        }
                        send(&c, &st, &row.key, text_body(&row, &typed));
                    },
                    move || st.editing.set(None),
                ));
                if let Some((text, tone)) = &note {
                    col = col.child(kit::sentence(
                        &t,
                        &format!("    {text}"),
                        width,
                        tone_ink(&t, *tone),
                    ));
                }
            }
            None => {
                let mut spans = vec![
                    span(mark, ink),
                    span_bold(format!("{}: ", r.label), ink),
                    span(r.now.clone(), t.text),
                    span(format!("  {}", r.source), t.text_faint),
                ];
                if let Some((text, tone)) = &note {
                    spans.push(span(format!("  {text}"), tone_ink(&t, *tone)));
                }
                col = col.child(line(spans));
            }
        }
        if !r.help.is_empty() {
            col = col.child(kit::sentence(
                &t,
                &format!("    {}", r.help),
                width,
                t.text_faint,
            ));
        }
        if let Some(why) = &r.locked {
            col = col.child(kit::sentence(
                &t,
                &format!("    {why}"),
                width,
                t.text_muted,
            ));
        }
        if let Some(w) = &r.warn {
            col = col.child(kit::sentence(&t, &format!("    {w}"), width, t.warn));
        }
    }
    col = col.child(kit::sentence(
        &t,
        match which {
            Which::Apps => "Empty = the default (or the value this gateway's environment gives). Applies at the next app start or download.",
            Which::Continuum => "Empty folder = the gateway's own folder. Each change applies at once.",
        },
        width,
        t.text_faint,
    ));
    if editing.is_none() {
        col = col.focusable().autofocus();
    }
    let keys_ctx = ctx.clone();
    let writable = d.writable;
    let col = col.on(Phase::Bubble, move |ectx, ev| {
        if let UiEvent::Key(k) = ev {
            if k.mods.0 != 0 || st.editing.get_untracked().is_some() {
                return;
            }
            let n = list.len();
            let at = st.sel.get_untracked().min(n.saturating_sub(1));
            let handled = match k.key {
                Key::Up => {
                    st.sel.set(at.saturating_sub(1));
                    true
                }
                Key::Down => {
                    st.sel.set((at + 1).min(n.saturating_sub(1)));
                    true
                }
                Key::Enter | Key::Char(' ') => {
                    if let Some(r) = list.get(at) {
                        if !writable {
                            keys_ctx
                                .store
                                .notice
                                .set(Some("Only an admin can change these.".into()));
                        } else if let Some(why) = &r.locked {
                            set_note(&st, &r.key, why, Tone::Error);
                        } else if let Some(on) = r.switch {
                            send(&keys_ctx, &st, &r.key, json!({ r.key.clone(): !on }));
                        } else if k.key == Key::Enter {
                            st.draft.set(r.saved.clone());
                            st.editing.set(Some(r.key.clone()));
                        }
                    }
                    true
                }
                _ => false,
            };
            if handled {
                ectx.stop_propagation();
            }
        }
    });
    Scroll::new(col.build())
        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
        .scrollbar_auto_hide(true)
        .view(cx)
}

fn tone_ink(t: &TokenSet, tone: Tone) -> abstracttui::base::Rgba {
    match tone {
        Tone::Ok => t.ok,
        Tone::Error => t.error,
        Tone::Plain => t.text_muted,
    }
}
