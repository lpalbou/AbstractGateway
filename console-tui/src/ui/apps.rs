//! Apps: the browser apps on this gateway (Flow Editor, Code, Observer,
//! Continuum, Entity), the desktop Assistant, and Node.js — the web
//! console's Apps tab (console_ui.py, `appCardMarkup` and friends) in the
//! terminal.
//!
//! Same routes, same states, same guards: every verb the web card offers
//! is a key here, and a verb that is not available says WHY (admin-only,
//! installs off, started outside the gateway, another computer…). The
//! web opens a signed-in tab; a terminal cannot, so Open shows the
//! one-time link — copy it, or open it in a browser on this machine —
//! with the SSH-forward line when the console reaches the gateway on a
//! loopback address (a headless server).
//!
//! The rules themselves live in `store::apps` (unit-tested there); this
//! file only renders them and sends commands.

use abstracttui::app::{ChoiceOutcome, ChoicePrompt};
use abstracttui::prelude::*;
use abstracttui::widgets::Table;

use super::util::{line, span, span_bold, wrap_text};
use super::widths;
use super::{confirm_danger, open_form, open_prompt, Ctx};
use crate::store::apps::{
    app_key, copyables, part_word, primary_verb, secondary_verbs, status_label, tui_key, AppJob,
    AppNote, AppOpenLink, AppRow, AppVerb, AppsOverview, Tone, VerbState, NODE_KEY,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The footer's verbs for this screen (ui/mod.rs footer arm).
pub const HINTS: &[(&str, &str)] = &[
    ("Enter/o", "open/install"),
    ("r", "check again"),
    ("i/u", "install/update"),
    ("s/x", "start/stop"),
    ("l", "log"),
    ("c", "cancel"),
    ("t/T", "terminal"),
    ("n", "Node.js"),
    ("y", "copy"),
];

const LOG_TAIL: u32 = 200;

fn is_admin(conn: &ConnPhase) -> bool {
    matches!(conn, ConnPhase::Connected(id) | ConnPhase::Verifying(id) if id.admin)
}

fn ink(t: &TokenSet, tone: Tone) -> abstracttui::base::Rgba {
    match tone {
        Tone::Ok => t.ok,
        Tone::Info => t.info,
        Tone::Warn => t.warn,
        Tone::Err => t.error,
        Tone::Muted => t.text_muted,
    }
}

fn selected(ctx: &Ctx) -> Option<AppRow> {
    let sel = ctx.store.apps.sel.get_untracked();
    ctx.store
        .apps
        .overview
        .with_untracked(|o| o.ready().and_then(|d| d.apps.get(sel).cloned()))
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let apps = store.apps;
    let tt = *t;

    super::util::clamp_selection(cx, apps.sel, move || {
        apps.overview.with(|o| o.ready().map(|d| d.apps.len()).unwrap_or(0))
    });

    // A minted sign-in link opens its modal (here, where the operator is:
    // a link minted while away waits for the screen).
    {
        let ctx = ctx.clone();
        cx.effect(move || {
            if let Some(link) = apps.open_link.get() {
                if ctx.ui.prompt_open.get() > 0 {
                    return; // never stack over a live prompt; its close re-wakes us
                }
                apps.open_link.set(None);
                open_link_modal(cx, &ctx, link);
            }
        });
    }

    let mut root = Element::new().style(LayoutStyle::column().gap(0));
    let key = |c: char| KeyChord::plain(Key::Char(c));
    for (ch, verb) in [
        ('o', None),
        ('i', Some(AppVerb::Install)),
        ('u', Some(AppVerb::Update)),
        ('s', Some(AppVerb::Start)),
        ('x', Some(AppVerb::Stop)),
        ('l', Some(AppVerb::Log)),
        ('c', Some(AppVerb::Cancel)),
        ('t', Some(AppVerb::OpenTerminal)),
        ('T', Some(AppVerb::InstallTerminal)),
        ('C', Some(AppVerb::CancelTerminal)),
    ] {
        let c = ctx.clone();
        root = root.shortcut(key(ch), move |_| run_key(cx, &c, verb));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('n'), move |_| node_key(cx, &c));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('y'), move |_| copy_menu(cx, &c));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('r'), move |_| {
            c.store.apps.notes.set(Vec::new());
            c.send(Cmd::LoadApps { latest: true });
        });
    }
    let ctx_body = ctx.clone();
    root.child(
        Block::new()
            .border(BorderKind::Rounded)
            .title("Apps — open in your browser, already signed in to this gateway")
            .fill(t.surface)
            .layout(LayoutStyle::column().gap(0).grow(1.0).min_h(6).padding(Edges::all(1)))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), move |gcx| {
                let conn = store.conn.get();
                let data = apps.overview.get();
                let admin = is_admin(&conn);
                match &data {
                    // The web's "This gateway cannot manage apps right now":
                    // the honest failure kind, never a guessed list.
                    Loadable::Failed(e) => Element::new()
                        .style(LayoutStyle::column())
                        .child(line(vec![span_bold("This gateway cannot manage apps right now.", tt.warn)]))
                        .child(super::util::error_panel_conn(&tt, e, &conn, Some("r checks again")))
                        .build(),
                    Loadable::NotAsked => line(vec![span(
                        "— not loaded yet (connect first, or press r to check)",
                        tt.text_muted,
                    )]),
                    Loadable::Loading => abstracttui::widgets::Spinner::new()
                        .frame(store.tick.get())
                        .label("looking for the apps…")
                        .element(&tt)
                        .build(),
                    Loadable::Ready(d) => ready_view(gcx, &ctx_body, &tt, d, admin),
                }
            }))
            .element(t)
            .build(),
    )
    .build()
}

fn ready_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: &AppsOverview, admin: bool) -> View {
    let apps = ctx.store.apps;
    // Tracked: job progress and notes re-render the panel.
    let jobs = apps.jobs.get();
    let _ = apps.notes.get();
    let _ = apps.pending.get();
    let job_of = |key: &str, fallback: Option<&AppJob>| -> Option<AppJob> {
        jobs.iter()
            .find(|(k, _)| k == key)
            .map(|(_, j)| j.clone())
            .or_else(|| fallback.cloned())
    };
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    let gw = d.gateway_url.clone().unwrap_or_else(|| "this gateway".into());
    let mut intro = vec![
        span("Apps talk to ", t.text_muted),
        span(gw, t.text),
        span(".", t.text_muted),
    ];
    if let Some(h) = &d.apps_host {
        intro.push(span(
            if h.starts_with("127.") || h == "localhost" {
                format!("  They listen on {h}: this machine only.")
            } else {
                format!("  They listen on {h}.")
            },
            t.text_faint,
        ));
    }
    col = col.child(line(intro));
    col = col.child(node_view(t, d, job_of(NODE_KEY, d.node.active_job.as_ref()), apps.note_for(NODE_KEY), apps.is_pending(NODE_KEY), admin));
    if d.registry_reachable == Some(false) {
        col = col.child(line(vec![
            span_bold("The app store (npm) is not reachable. ", t.warn),
            span("Installed apps keep working; installing needs the internet.", t.text_muted),
        ]));
    }
    if d.apps.is_empty() {
        return col
            .child(line(vec![span("∅ this gateway lists no apps", t.text_muted)]))
            .build();
    }
    col = col.child(apps_table(cx, ctx, t, d, admin, &job_of));
    if let Some(row) = d.apps.get(apps.sel.get()) {
        let job = job_of(&app_key(&row.id), row.active_job.as_ref());
        let tjob = job_of(&tui_key(&row.id), row.tui.as_ref().and_then(|x| x.active_job.as_ref()));
        col = col.child(detail_view(cx, t, row, job.as_ref(), tjob.as_ref(), apps.note_for(&app_key(&row.id)), apps.note_for(&tui_key(&row.id)), apps.is_pending(&app_key(&row.id)) || apps.is_pending(&tui_key(&row.id)), admin));
    }
    col.build()
}

fn node_view(t: &TokenSet, d: &AppsOverview, job: Option<AppJob>, note: Option<AppNote>, pending: bool, admin: bool) -> View {
    let n = &d.node;
    let mut spans = vec![span_bold("Node.js  ", t.text)];
    let active = job.as_ref().map(AppJob::is_active).unwrap_or(false);
    if active {
        spans.push(span("Installing", t.info));
    } else if n.available {
        spans.push(span("Ready", t.ok));
    } else {
        spans.push(span("Not installed", t.text_muted));
    }
    if let Some(v) = &n.version {
        spans.push(span(format!(" · {v}"), t.text_muted));
    }
    match n.source.as_str() {
        "system" => spans.push(span(" · found on this computer", t.text_muted)),
        "managed" => spans.push(span(" · installed by the gateway", t.text_muted)),
        _ => {}
    }
    spans.push(span("  — the engine the browser apps run on", t.text_faint));
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0)).child(line(spans));
    if let Some(j) = job.as_ref().filter(|j| j.is_active()) {
        col = col.child(line(vec![span(format!("  ⟳ {}", j.progress_line("Installing Node.js")), t.info)]));
        col = col.child(line(vec![span(
            if admin { "  n cancels" } else { "  only an admin can cancel" },
            t.text_faint,
        )]));
    } else if !n.available {
        col = col.child(line(vec![span(
            "  Node.js will be installed for you: the gateway puts it in its own folder (about 56 MB, no password, no terminal) the first time you install an app, or now.",
            t.text_muted,
        )]));
        let action = if pending {
            "  starting…".to_string()
        } else if !n.install_available {
            format!("  Install is not available here{}", n.message.as_ref().map(|m| format!(": {m}")).unwrap_or_default())
        } else if admin {
            "  n installs Node.js now".to_string()
        } else {
            "  Install Node.js: only an admin can install Node.js".to_string()
        };
        col = col.child(line(vec![span(action, t.text_faint)]));
    }
    if let Some(note) = note {
        col = note_lines(col, t, &note, 2);
    }
    col.build()
}

fn apps_table(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: &AppsOverview,
    admin: bool,
    job_of: &dyn Fn(&str, Option<&AppJob>) -> Option<AppJob>,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let mut rows: Vec<Vec<String>> = d
        .apps
        .iter()
        .map(|a| {
            let job = job_of(&app_key(&a.id), a.active_job.as_ref());
            let active = job.as_ref().map(AppJob::is_active).unwrap_or(false);
            let (label, _) = status_label(a, active);
            let status = if a.is_external() { format!("{label} (outside)") } else { label };
            let action = match primary_verb(a, job.as_ref(), admin) {
                Some(v) if v.available.is_ok() => format!("{} {}", if v.verb == AppVerb::Cancel { "c" } else { "o" }, v.label),
                Some(v) => format!("({} — unavailable)", v.label),
                None => "—".into(),
            };
            let kind = if a.is_desktop() {
                "desktop".to_string()
            } else if a.tui.is_some() {
                "browser + terminal".to_string()
            } else {
                "browser".to_string()
            };
            vec![
                a.name.clone(),
                kind,
                status,
                a.version.clone().unwrap_or_else(|| "—".into()),
                action,
            ]
        })
        .collect();
    let rules = vec![
        widths::ColRule::head("app", 10),
        widths::ColRule::head("kind", 8),
        widths::ColRule::head("status", 12),
        widths::ColRule::head("version", 8),
        widths::ColRule::head("action", 12),
    ];
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME - 2);
    let visible = (d.apps.len() as i32 + 1).clamp(2, 8);
    let ctx_act = ctx.clone();
    Table::new(cols)
        .rows(rows)
        .selection(ctx.store.apps.sel)
        .on_activate(move |_| run_key(cx, &ctx_act, None))
        .layout(LayoutStyle::default().h(visible).shrink(0.0))
        .element(cx, t)
        .autofocus()
        .build()
}

fn note_lines(mut col: Element, t: &TokenSet, note: &AppNote, indent: usize) -> Element {
    let pad = " ".repeat(indent);
    let tone = note.tone.unwrap_or(Tone::Info);
    col = col.child(line(vec![span_bold(format!("{pad}{}", note.text), ink(t, tone))]));
    if let Some(h) = &note.hint {
        col = col.child(line(vec![span(format!("{pad}{h}"), t.text_muted)]));
    }
    for (label, cmd) in &note.commands {
        col = col.child(line(vec![
            span(format!("{pad}{label}: "), t.text_muted),
            span(cmd.clone(), t.text),
        ]));
    }
    if note.details.is_some() {
        col = col.child(line(vec![span(format!("{pad}(y copies the details)"), t.text_faint)]));
    }
    col
}

#[allow(clippy::too_many_arguments)]
fn detail_view(
    cx: Scope,
    t: &TokenSet,
    row: &AppRow,
    job: Option<&AppJob>,
    tjob: Option<&AppJob>,
    note: Option<AppNote>,
    tnote: Option<AppNote>,
    pending: bool,
    admin: bool,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w.max(40) as usize;
    let active = job.map(AppJob::is_active).unwrap_or(false);
    let (label, tone) = status_label(row, active);
    let mut col = Element::new().style(LayoutStyle::column().gap(0));
    col = col.child(line(vec![span(String::new(), t.text)]));
    col = col.child(line(vec![
        span_bold(row.name.clone(), t.accent),
        span("  ", t.text),
        span_bold(label, ink(t, tone)),
        span(format!("  {}", row.description), t.text_muted),
    ]));
    // The body: only what the operator must see now (web card body).
    if let Some(j) = job.filter(|j| j.is_active()) {
        col = col.child(line(vec![span(format!("⟳ {}", j.progress_line(&format!("Installing {}", row.name))), t.info)]));
        for p in &j.parts {
            col = col.child(line(vec![span(format!("  {} · {}", p.label, part_word(&p.state)), t.text_muted)]));
        }
    } else if let Some(j) = job.filter(|j| j.state == "failed") {
        let err = j.error.clone().unwrap_or_default();
        col = col.child(line(vec![span_bold(
            if err.message.is_empty() { "The install did not finish.".to_string() } else { err.message },
            t.error,
        )]));
        if let Some(h) = err.hint {
            col = col.child(line(vec![span(h, t.text_muted)]));
        }
        for p in &j.parts {
            col = col.child(line(vec![span(format!("  {} · {}", p.label, part_word(&p.state)), t.text_muted)]));
        }
        col = col.child(line(vec![span("(y copies the install log)", t.text_faint)]));
    } else if matches!(row.status.as_str(), "crashed" | "crash_loop") {
        col = col.child(line(vec![
            span_bold(format!("{} stopped unexpectedly. ", row.name), t.error),
            span("Open starts it again.", t.text_muted),
        ]));
        if let Some(e) = &row.last_error {
            for l in wrap_text(e, vw.saturating_sub(6)).into_iter().take(4) {
                col = col.child(line(vec![span(format!("  {l}"), t.text_muted)]));
            }
        }
    }
    if !row.installed && !active {
        if let Some(r) = &row.install_blocked_reason {
            col = col.child(line(vec![span(r.clone(), t.warn)]));
        }
    }
    if let Some(desk) = &row.desktop {
        if row.installed && desk.launch_blocked.as_deref() == Some("other_computer") {
            col = col.child(line(vec![span(desk.launch_blocked_reason.clone().unwrap_or_default(), t.text_muted)]));
        }
    }
    if let Some(j) = tjob.filter(|j| j.is_active()) {
        col = col.child(line(vec![span(format!("⟳ {}", j.progress_line(&format!("Installing {} for the terminal", row.name))), t.info)]));
    } else if let Some(j) = tjob.filter(|j| j.state == "failed") {
        let err = j.error.clone().unwrap_or_default();
        col = col.child(line(vec![span_bold(
            if err.message.is_empty() { format!("{}'s terminal app did not install.", row.name) } else { err.message },
            t.error,
        )]));
    }
    if let Some(n) = &note {
        col = note_lines(col, t, n, 0);
    }
    if let Some(n) = &tnote {
        col = note_lines(col, t, n, 0);
    }

    // The verbs: the primary one, then everything under the web's
    // "Technical details", each with its key or the reason it is off.
    let mut verbs: Vec<VerbState> = Vec::new();
    if let Some(p) = primary_verb(row, job, admin) {
        verbs.push(p);
    }
    verbs.extend(secondary_verbs(row, job, tjob, admin));
    let mut on: Vec<(String, abstracttui::base::Rgba, bool)> = Vec::new();
    let mut off: Vec<(String, String)> = Vec::new();
    for v in &verbs {
        match &v.available {
            Ok(()) => {
                if !on.is_empty() {
                    on.push(span("  ·  ", t.text_faint));
                }
                let k = if v.verb == AppVerb::Cancel && primary_verb(row, job, admin).map(|p| p.verb) == Some(AppVerb::Cancel) {
                    "c"
                } else {
                    v.verb.key()
                };
                on.push(span_bold(k.to_string(), t.accent));
                on.push(span(format!(" {}", v.label), t.text));
            }
            Err(why) => off.push((v.label.clone(), why.clone())),
        }
    }
    if pending {
        col = col.child(line(vec![span("⟳ working…", t.info)]));
    } else if !on.is_empty() {
        col = col.child(line(on));
    }
    for (label, why) in off.iter().take(6) {
        col = col.child(line(vec![
            span(format!("{label}: "), t.text_faint),
            span(why.clone(), t.text_faint),
        ]));
    }

    // Facts (the web's technical line and rows).
    let mut facts: Vec<String> = Vec::new();
    if let Some(v) = &row.version {
        facts.push(format!("version {v}"));
    } else if let Some(l) = row.latest_version.as_ref().filter(|_| row.update_available) {
        facts.push(format!("latest {l}"));
    }
    if let Some(port) = row.external_port {
        facts.push(format!("Started outside the gateway on port {port}"));
    }
    if row.running {
        if let Some(pid) = row.pid {
            facts.push(format!("process {pid}"));
        }
    }
    if let Some(u) = &row.url {
        facts.push(format!("address {u}"));
    }
    if !row.is_desktop() && !row.package.is_empty() {
        facts.push(format!("npx {}", row.package));
    }
    if let Some(t2) = &row.tui {
        if t2.installed {
            facts.push(format!("terminal {}", t2.version.clone().unwrap_or_else(|| "installed".into())));
        }
    }
    if let Some(dk) = &row.desktop {
        if let Some(l) = &dk.location {
            facts.push(format!("location {l}"));
        }
    }
    if !facts.is_empty() {
        col = col.child(line(vec![span(facts.join("  ·  "), t.text_faint)]));
    }
    if let Some(t2) = &row.tui {
        if t2.installed && t2.launch_available {
            if let Some(c) = &t2.command {
                col = col.child(line(vec![span("terminal: ", t.text_faint), span(c.clone(), t.text_muted)]));
            }
        } else if t2.installed {
            if let Some(c) = &t2.command {
                col = col.child(line(vec![span("terminal, on the other computer: ", t.text_faint), span(c.clone(), t.text_muted)]));
            }
            if let Some(c) = &t2.signin_command {
                col = col.child(line(vec![span("first time there, sign in once: ", t.text_faint), span(c.clone(), t.text_muted)]));
            }
        } else if !t2.install_available && t2.install_method == "cargo" {
            if let Some(c) = &t2.install_command {
                col = col.child(line(vec![span("terminal version needs the Rust toolchain: ", t.text_faint), span(c.clone(), t.text_muted)]));
            }
        }
    }
    if let Some(dk) = &row.desktop {
        if let Some(c) = &dk.launch_command {
            col = col.child(line(vec![span("launch command: ", t.text_faint), span(c.clone(), t.text_muted)]));
        }
        if !row.installed {
            if let Some(c) = &dk.install_command {
                col = col.child(line(vec![span("install command: ", t.text_faint), span(c.clone(), t.text_muted)]));
            }
        }
    }
    col.build()
}

// ---------------------------------------------------------------------
// Verbs
// ---------------------------------------------------------------------

fn run_key(cx: Scope, ctx: &Ctx, verb: Option<AppVerb>) {
    let store = ctx.store;
    let Some(row) = selected(ctx) else {
        store.notice.set(Some(match store.apps.overview.get_untracked() {
            Loadable::Ready(_) => "no app selected".into(),
            _ => "the apps are not loaded — r checks".into(),
        }));
        return;
    };
    let admin = store.conn.with_untracked(is_admin);
    let job = store.apps.job_for(&app_key(&row.id), row.active_job.as_ref());
    let tjob = store.apps.job_for(&tui_key(&row.id), row.tui.as_ref().and_then(|t| t.active_job.as_ref()));
    let state = match verb {
        None => primary_verb(&row, job.as_ref(), admin),
        Some(v) => primary_verb(&row, job.as_ref(), admin)
            .into_iter()
            .chain(secondary_verbs(&row, job.as_ref(), tjob.as_ref(), admin))
            .find(|s| s.verb == v || (v == AppVerb::Open && s.verb == AppVerb::DesktopOpen)),
    };
    let Some(state) = state else {
        store.notice.set(Some(match verb {
            Some(AppVerb::Cancel) => format!("{}: no install is running", row.name),
            Some(AppVerb::CancelTerminal) => format!("{}: no terminal install is running", row.name),
            Some(AppVerb::OpenTerminal | AppVerb::InstallTerminal) => format!("{} has no terminal version", row.name),
            Some(AppVerb::Install) => format!("{} is already installed", row.name),
            Some(v) => format!("{}: {v:?} is not offered for this app", row.name),
            None => format!("{}: nothing to do", row.name),
        }));
        return;
    };
    if let Err(why) = &state.available {
        store.notice.set(Some(format!("{} — {}: {why}", row.name, state.label)));
        return;
    }
    let pending_key = match state.verb {
        AppVerb::OpenTerminal | AppVerb::InstallTerminal | AppVerb::CancelTerminal => tui_key(&row.id),
        _ => app_key(&row.id),
    };
    if store.apps.is_pending(&pending_key) {
        store.notice.set(Some(format!("{}: already working on it", row.name)));
        return;
    }
    let act = |c: &Ctx, verb: AppVerb, path: Option<String>, start_first: bool| {
        c.send(Cmd::AppAct {
            app_id: row.id.clone(),
            name: row.name.clone(),
            verb,
            path,
            start_first,
        })
    };
    match state.verb {
        AppVerb::Open => act(ctx, AppVerb::Open, state.path.clone(), row.installed && !row.running),
        AppVerb::DesktopOpen | AppVerb::Start | AppVerb::OpenTerminal => act(ctx, state.verb, None, false),
        AppVerb::Log => open_log_modal(cx, ctx, &row),
        AppVerb::Cancel | AppVerb::CancelTerminal => {
            let (key, j) = if state.verb == AppVerb::Cancel { (app_key(&row.id), job) } else { (tui_key(&row.id), tjob) };
            match j.filter(AppJob::is_active) {
                Some(j) => ctx.send(Cmd::CancelAppJob { key, name: row.name.clone(), job_id: j.id }),
                None => store.notice.set(Some(format!("{}: no install is running", row.name))),
            }
        }
        AppVerb::Stop => {
            let c = ctx.clone();
            let r = row.clone();
            confirm_danger(
                cx,
                ctx.ui,
                format!("Stop {}? Browser tabs open on it lose their connection until it starts again.", row.name),
                &format!("Stop {}", row.name),
                "Keep it running",
                move || {
                    c.send(Cmd::AppAct { app_id: r.id.clone(), name: r.name.clone(), verb: AppVerb::Stop, path: None, start_first: false })
                },
            );
        }
        AppVerb::Install | AppVerb::Update | AppVerb::InstallTerminal => {
            let what = match state.verb {
                AppVerb::Install if row.is_desktop() => format!(
                    "Install the {} on the gateway's computer (into the gateway's own Python)?",
                    row.name
                ),
                AppVerb::Install => format!(
                    "Install {}{}{}? It downloads from npm onto the gateway host and starts nothing.",
                    row.name,
                    if row.install_parts.iter().any(|p| p == "tui") { " for the browser and the terminal" } else { "" },
                    if row.needs_node_install { " (Node.js is installed for you first: about 56 MB, no password needed)" } else { "" }
                ),
                AppVerb::Update => format!(
                    "Install the newest {} ({})?{}",
                    row.name,
                    row.latest_version.clone().unwrap_or_else(|| "latest".into()),
                    if row.running { " It is running: it restarts on the new version." } else { "" }
                ),
                _ => format!(
                    "{} {}'s terminal app? A ready-made download from its release, checked against its published checksums, into the gateway's own folder.",
                    if row.tui.as_ref().map(|x| x.installed).unwrap_or(false) { "Update" } else { "Install" },
                    row.name
                ),
            };
            let c = ctx.clone();
            let r = row.clone();
            let verb = state.verb;
            open_prompt(
                cx,
                ctx.ui,
                ChoicePrompt::new(what)
                    .option("go", state.label.clone())
                    .option("keep", "Not now")
                    .initial("go"),
                move |outcome| {
                    if let ChoiceOutcome::Answered(a) = outcome {
                        if a.selected.iter().any(|s| s == "go") {
                            c.send(Cmd::AppAct { app_id: r.id.clone(), name: r.name.clone(), verb, path: None, start_first: false });
                        }
                    }
                },
            );
        }
    }
}

/// `n`: install Node.js (confirmed), or cancel its running install.
fn node_key(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let Some(d) = store.apps.overview.with_untracked(|o| o.ready().cloned()) else {
        store.notice.set(Some("the apps are not loaded — r checks".into()));
        return;
    };
    let admin = store.conn.with_untracked(is_admin);
    let job = store.apps.job_for(NODE_KEY, d.node.active_job.as_ref());
    if let Some(j) = job.filter(AppJob::is_active) {
        if !admin {
            store.notice.set(Some("Node.js install: only an admin can cancel".into()));
            return;
        }
        ctx.send(Cmd::CancelAppJob { key: NODE_KEY.into(), name: "Node.js".into(), job_id: j.id });
        return;
    }
    if d.node.available {
        store.notice.set(Some(format!(
            "Node.js is ready{} — nothing to install",
            d.node.version.map(|v| format!(" ({v})")).unwrap_or_default()
        )));
        return;
    }
    if !d.node.install_available {
        store.notice.set(Some(format!(
            "Install Node.js is not available here{}",
            d.node.message.map(|m| format!(": {m}")).unwrap_or_default()
        )));
        return;
    }
    if !admin {
        store.notice.set(Some("Install Node.js: only an admin can install Node.js".into()));
        return;
    }
    if store.apps.is_pending(NODE_KEY) {
        store.notice.set(Some("Node.js: already working on it".into()));
        return;
    }
    let c = ctx.clone();
    open_prompt(
        cx,
        ctx.ui,
        ChoicePrompt::new("Install Node.js into the gateway's own folder? About 56 MB, no password, no terminal.")
            .option("go", "Install Node.js")
            .option("keep", "Not now")
            .initial("go"),
        move |outcome| {
            if let ChoiceOutcome::Answered(a) = outcome {
                if a.selected.iter().any(|s| s == "go") {
                    c.send(Cmd::InstallAppsNode);
                }
            }
        },
    );
}

/// `y`: pick one of the app's copyable lines (address, commands, the
/// failure's details) and put it on the clipboard.
fn copy_menu(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let Some(row) = selected(ctx) else {
        store.notice.set(Some("no app selected — nothing to copy".into()));
        return;
    };
    let note = store.apps.note_for(&app_key(&row.id));
    let tnote = store.apps.note_for(&tui_key(&row.id));
    let mut items = copyables(&row, note.as_ref());
    if let Some(n) = &tnote {
        for (l, c) in &n.commands {
            if !items.iter().any(|(_, x)| x == c) {
                items.push((l.clone(), c.clone()));
            }
        }
    }
    let job = store.apps.job_for(&app_key(&row.id), row.active_job.as_ref());
    if let Some(j) = job.filter(|j| !j.is_active()) {
        let log = j.log_text();
        if !log.is_empty() {
            items.push(("Install log".into(), log));
        }
    }
    for n in [note, tnote].into_iter().flatten() {
        if let Some(d) = n.details {
            items.push(("Failure details".into(), d));
        }
    }
    if items.is_empty() {
        store.notice.set(Some(format!("{}: nothing to copy", row.name)));
        return;
    }
    let mut prompt = ChoicePrompt::new(format!("Copy from {}", row.name));
    for (i, (label, value)) in items.iter().enumerate() {
        let first = value.lines().next().unwrap_or("");
        prompt = prompt.option_detail(i.to_string(), label.clone(), super::util::ellipsize(first, 90));
    }
    prompt = prompt.option("keep", "Nothing");
    open_prompt(cx, ctx.ui, prompt, move |outcome| {
        if let ChoiceOutcome::Answered(a) = outcome {
            if let Some(i) = a.selected.first().and_then(|s| s.parse::<usize>().ok()) {
                if let Some((label, value)) = items.get(i) {
                    copy_to_clipboard(value.clone());
                    store.notice.set(Some(format!("copied: {label}")));
                }
            }
        }
    });
}

// ---------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------

fn open_link_modal(cx: Scope, ctx: &Ctx, link: AppOpenLink) {
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(96, 16), move |mcx, close| {
        let theme = use_theme(mcx);
        let t = theme.get().tokens;
        let wrap_w = 88usize;
        let mut col = Element::new().style(LayoutStyle::column().gap(0));
        col = col.child(line(vec![span_bold(
            format!("{}{} — signed-in link", link.name, if link.started { " started" } else { "" }),
            t.accent,
        )]));
        col = col.child(line(vec![span(link.link.clone(), t.text)]));
        col = col.child(line(vec![span(
            format!(
                "Works once{}, only on this address; opening it signs this browser in to {} as you.",
                link.expires_in_s.map(|n| format!(", within {n} seconds")).unwrap_or_default(),
                link.name
            ),
            t.text_muted,
        )]));
        if let Some(u) = &link.app_url {
            col = col.child(line(vec![span("The app itself: ", t.text_faint), span(u.clone(), t.text_muted)]));
        }
        if let Some(h) = &link.tunnel_hint {
            for l in wrap_text(h, wrap_w) {
                col = col.child(line(vec![span(l, t.warn)]));
            }
        }
        let l_copy = link.link.clone();
        let l_open = link.link.clone();
        let store = ctx2.store;
        let ctx_open = ctx2.clone();
        let close_btn = close.clone();
        let close_open = close.clone();
        col = col.child(line(vec![span(String::new(), t.text)]));
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Copy link (y)")
                        .on_click(move || {
                            copy_to_clipboard(l_copy.clone());
                            store.notice.set(Some("copied the one-time link".into()));
                        })
                        .element(mcx, &t)
                        .autofocus()
                        .build(),
                )
                .child(
                    Button::new("Open in a browser here (o)")
                        .on_click(move || {
                            ctx_open.screens.open_url(&l_open);
                            close_open();
                        })
                        .element(mcx, &t)
                        .build(),
                )
                .child(Button::new("Close (Esc)").on_click(move || close_btn()).element(mcx, &t).build())
                .build(),
        );
        let l_y = link.link.clone();
        let l_o = link.link.clone();
        let ctx_o = ctx2.clone();
        let close_o = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .shortcut(KeyChord::plain(Key::Char('y')), move |_| {
                copy_to_clipboard(l_y.clone());
                store.notice.set(Some("copied the one-time link".into()));
            })
            .shortcut(KeyChord::plain(Key::Char('o')), move |_| {
                ctx_o.screens.open_url(&l_o);
                close_o();
            })
            .child(col.build())
            .build()
    });
}

fn open_log_modal(cx: Scope, ctx: &Ctx, row: &AppRow) {
    let apps = ctx.store.apps;
    apps.log.set(Loadable::Loading);
    ctx.send(Cmd::LoadAppLog { app_id: row.id.clone(), tail: LOG_TAIL });
    let size = super::preview_size(cx);
    // The pane's rows: the modal minus its title, head, footer, keys and
    // button lines and the border — so the newest lines fill the pane.
    let page = (size.h - 8).max(3);
    let ctx2 = ctx.clone();
    let id = row.id.clone();
    let name = row.name.clone();
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let top = mcx.signal(i32::MAX);
        let (c_ref, c_more, c_copy) = (ctx2.clone(), ctx2.clone(), ctx2.clone());
        let (id_ref, id_more) = (id.clone(), id.clone());
        let reload = move |c: &Ctx, app_id: &str, tail: u32| {
            c.store.apps.log.set(Loadable::Loading);
            c.send(Cmd::LoadAppLog { app_id: app_id.to_string(), tail });
        };
        let close_btn = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .shortcut(KeyChord::plain(Key::Char('r')), move |_| {
                let tail = c_ref.store.apps.log.with_untracked(|l| l.ready().map(|x| x.tail)).unwrap_or(LOG_TAIL);
                reload(&c_ref, &id_ref, tail);
            })
            .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
                let cur = c_more.store.apps.log.with_untracked(|l| l.ready().cloned());
                match cur {
                    Some(l) if l.can_show_more() => {
                        reload(&c_more, &id_more, (l.tail * 4).min(crate::api::api_apps::APP_LOG_MAX))
                    }
                    Some(_) => c_more.store.notice.set(Some("the whole log is shown (or the 5000-line ceiling is reached)".into())),
                    None => {}
                }
            })
            .shortcut(KeyChord::plain(Key::Char('y')), move |_| {
                let text = c_copy.store.apps.log.with_untracked(|l| l.ready().map(|x| x.lines.join("\n")));
                match text {
                    Some(t) if !t.is_empty() => {
                        copy_to_clipboard(t);
                        c_copy.store.notice.set(Some("copied the log lines".into()));
                    }
                    _ => c_copy.store.notice.set(Some("the log is empty — nothing to copy".into())),
                }
            })
            .child(line(vec![span_bold(format!("{name} — log"), t0.accent)]))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0).min_h(3), move |pcx| {
                let log = apps.log.get();
                match &log {
                    Loadable::Ready(l) => {
                        let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
                        col = col.child(line(vec![span_bold(l.head(), t0.text)]));
                        if !l.lines.is_empty() {
                            col = col.child(scroll_lines(pcx, &t0, l.lines.join("\n"), top, page));
                        }
                        let mut foot = Vec::new();
                        if l.capped() {
                            foot.push(span("Older lines are in the log file.  ", t0.text_muted));
                        }
                        if let Some(p) = &l.path {
                            foot.push(span("Log file ", t0.text_faint));
                            foot.push(span(p.clone(), t0.text_muted));
                        }
                        if !foot.is_empty() {
                            col = col.child(line(foot));
                        }
                        let mut keys = vec![span("r refresh", t0.text_faint)];
                        if l.can_show_more() {
                            keys.push(span("  ·  m show more", t0.text_faint));
                        }
                        if !l.lines.is_empty() {
                            keys.push(span("  ·  y copy", t0.text_faint));
                        }
                        col.child(line(keys)).build()
                    }
                    Loadable::Failed(e) => Element::new()
                        .style(LayoutStyle::column())
                        .child(line(vec![span_bold("Could not read the log", t0.error)]))
                        .child(super::util::error_panel(&t0, e))
                        .build(),
                    _ => line(vec![span("Reading the log...", t0.text_muted)]),
                }
            }))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(Button::new("Close (Esc)").on_click(move || close_btn()).element(mcx, &t0).build())
                    .build(),
            )
            .build()
    });
}

/// A read-only scrolling text pane that opens at the END (newest lines
/// in view, like the web panel). ↑/↓ line, PgUp/PgDn page, Home/End.
fn scroll_lines(_cx: Scope, t: &TokenSet, text: String, top: Signal<i32>, page: i32) -> View {
    let t0 = *t;
    let total = abstracttui::widgets::CodeView::line_count(&text) as i32;
    let max_top = (total - page).max(0);
    let cur = top.get().clamp(0, max_top);
    if cur != top.get_untracked() {
        top.set(cur);
    }
    Element::new()
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().grow(1.0))
        .shortcut(KeyChord::plain(Key::Down), move |_| top.set((top.get_untracked() + 1).min(max_top)))
        .shortcut(KeyChord::plain(Key::Up), move |_| top.set((top.get_untracked() - 1).max(0)))
        .shortcut(KeyChord::plain(Key::PageDown), move |_| top.set((top.get_untracked() + page).min(max_top)))
        .shortcut(KeyChord::plain(Key::PageUp), move |_| top.set((top.get_untracked() - page).max(0)))
        .shortcut(KeyChord::plain(Key::Home), move |_| top.set(0))
        .shortcut(KeyChord::plain(Key::End), move |_| top.set(max_top))
        .child(
            abstracttui::widgets::CodeView::new(text)
                .scroll_offset(cur)
                .layout(LayoutStyle::default().grow(1.0))
                .element(&t0)
                .build(),
        )
        .build()
}
