//! Workflows (WORK) — the web console's page of the same name
//! (`#workflows-section`, `#workflows-skipped-section`,
//! `#agent-defaults-section` in console.py; `console_ui.py`
//! mountAgentDefaults): same data, same actions, same sentences.
//!
//! Three tabs (←/→): **Workflows** (the "Shared with everyone" and "Mine"
//! groups; search, Drafts, Older versions, Show archived; per row the
//! `[x] Available to users` switch, Export, Open in AbstractFlow,
//! Archive / Unarchive; Import .flow), **Default workflow per app** (one
//! picker per app, saved at once; Streamed replies) and **Broken
//! workflows** (shown when the gateway refused to load some versions).

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

use super::kit::{self, InlineConfirm, Row, WrapTable};
use super::switch::switch_text;
use super::util::{line, span, span_bold};
use super::widths::ColRule;
use super::Ctx;
use crate::store::skills::Tone as MsgTone;
use crate::store::workflows_page::{
    source_label, version_label, DefaultRow, DefaultsData, WfRow, WorkflowsData, AVAILABLE_HELP,
    AVAILABLE_LABEL, BROKEN_SENTENCE, BROKEN_TITLE, DEFAULTS_ADMIN_ONLY, DEFAULTS_LOADING,
    DEFAULTS_NOTE, DEFAULTS_TITLE, EMPTY, NO_MATCH, PURPOSE, STREAMING_HELP, STREAMING_LABEL,
    SUBTITLE, TITLE,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::workflows::{ListArgs, WfCmd};
use crate::worker::Cmd;

/// Verbs only an admin may use on this page (the web hides or disables
/// them for others). Archive and Import are NOT here: the gateway lets a
/// user archive and import their own ("Mine") workflows.
/// `s` (Streamed replies) is not listed: the defaults tab shows that
/// switch unavailable with its reason, the web's way.
pub const ADMIN_KEYS: &[&str] = &["space"];

fn list_args(ctx: &Ctx) -> ListArgs {
    ListArgs {
        drafts: ctx.store.wf.drafts.get_untracked(),
        archived: ctx.store.wf.archived.get_untracked(),
    }
}

/// Re-read the list and the defaults (the web's ↻ = a plain re-GET).
pub fn refresh(ctx: &Ctx) {
    ctx.send(Cmd::Workflows(WfCmd::Load(list_args(ctx))));
}

/// [`refresh`] for a harness without a `Ctx`.
pub fn refresh_for_tests(store: &crate::store::Store, tx: &std::sync::mpsc::Sender<Cmd>) {
    let _ = tx.send(Cmd::Workflows(WfCmd::Load(ListArgs {
        drafts: store.wf.drafts.get_untracked(),
        archived: store.wf.archived.get_untracked(),
    })));
}

/// The footer verbs for the current tab.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let mut out = vec![("Tab", "tab")];
    match ctx.store.wf.tab.get() {
        0 => out.extend_from_slice(&[
            ("space", "Available to users"),
            ("/", "search"),
            ("t", "drafts"),
            ("o", "older versions"),
            ("h", "show archived"),
            ("x", "export"),
            ("f", "open in AbstractFlow"),
            ("d", "archive/unarchive"),
            ("e", "edit description"),
            ("i", "import .flow"),
        ]),
        1 => out.extend_from_slice(&[
            ("Enter", "pick a workflow"),
            ("o", "other workflow types"),
            ("s", "Streamed replies"),
        ]),
        _ => out.extend_from_slice(&[("d", "archive")]),
    }
    out.push(("r", "refresh"));
    out
}

fn msg_ink(t: &TokenSet, tone: MsgTone) -> abstracttui::base::Rgba {
    match tone {
        MsgTone::Ok => t.ok,
        MsgTone::Error => t.error,
        MsgTone::Plain => t.text_muted,
    }
}

/// The table's rows in display order: (caption or row) — the same list
/// the selection indexes.
enum Item<'a> {
    Group(&'static str),
    Row(&'a WfRow),
    /// R8.1: with "Older versions" on, each older version is its own row
    /// under its bundle (its own Export / Open / Archive) — rows never
    /// expand.
    Older(&'a WfRow, usize),
}

fn items<'a>(d: &'a WorkflowsData, query: &str, older: bool) -> Vec<Item<'a>> {
    let mut out = Vec::new();
    for (title, rows) in d.groups(query) {
        out.push(Item::Group(title));
        for r in rows {
            out.push(Item::Row(r));
            if older {
                for i in 0..r.versions.len() {
                    if i != r.latest {
                        out.push(Item::Older(r, i));
                    }
                }
            }
        }
    }
    out
}

/// The highlighted row: the bundle and the version its actions act on
/// (the latest for a bundle row, that version for an older row).
fn selected(ctx: &Ctx) -> Option<(WfRow, usize, bool)> {
    let wf = ctx.store.wf;
    let q = wf.query.get_untracked();
    let older = wf.older.get_untracked();
    let i = wf.sel.get_untracked();
    wf.data.with_untracked(|d| {
        d.ready().and_then(|d| match items(d, &q, older).get(i) {
            Some(Item::Row(r)) => Some(((*r).clone(), r.latest, false)),
            Some(Item::Older(r, v)) => Some(((*r).clone(), *v, true)),
            _ => None,
        })
    })
}

fn selected_row(ctx: &Ctx) -> Option<WfRow> {
    selected(ctx).map(|(r, _, _)| r)
}

/// The broken tab is listed only when something is broken.
fn has_broken(ctx: &Ctx) -> bool {
    ctx.store
        .wf
        .data
        .with(|d| d.ready().is_some_and(|d| !d.broken.is_empty()))
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let wf = ctx.store.wf;
    let tt = *t;
    let confirm = InlineConfirm::new(cx);
    let keeper = super::util::FocusKeeper::new();
    if wf.data.with_untracked(|d| matches!(d, Loadable::NotAsked))
        && ctx.store.conn.with_untracked(ConnPhase::is_connected)
    {
        refresh(ctx);
    }
    // "Open in AbstractFlow" mints a one-time link through the Apps lane
    // (POST /apps/flow/open); its modal opens here, and a refusal is said
    // in this page's message line.
    let flow_pending = cx.signal(false);
    {
        let ctx2 = ctx.clone();
        let ctx = ctx.clone();
        cx.effect(move || {
            if let Some(link) = ctx.store.apps.open_link.get() {
                if !flow_pending.get_untracked() || ctx.ui.prompt_open.get() > 0 {
                    return;
                }
                flow_pending.set(false);
                ctx.store.apps.open_link.set(None);
                super::apps::open_link_modal(cx, &ctx, link);
            }
        });
        cx.effect(move || {
            let notes = ctx2.store.apps.notes.get();
            if !flow_pending.get_untracked() {
                return;
            }
            if let Some((_, n)) = notes.iter().find(|(k, _)| k == "app:flow") {
                flow_pending.set(false);
                let mut text = format!("Could not open AbstractFlow: {}", n.text);
                if let Some(h) = &n.hint {
                    text.push(' ');
                    text.push_str(h);
                }
                wf.msg.set(Some((text, MsgTone::Error)));
            }
        });
    }
    let keys_ctx = ctx.clone();
    let root = Element::new()
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                if handle_key(cx, &keys_ctx, confirm, flow_pending, k.key) {
                    ectx.stop_propagation();
                }
            }
        });
    let root = confirm.keys(root);
    let body_ctx = ctx.clone();
    let tabs_ctx = ctx.clone();
    root.child(
        Block::new()
            .border(BorderKind::Rounded)
            .title(format!("{TITLE} — {SUBTITLE}"))
            .fill(t.surface)
            .layout(
                LayoutStyle::column()
                    .gap(0)
                    .grow(1.0)
                    .padding(Edges::all(1)),
            )
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let tab = wf.tab.get();
                let broken_n = wf
                    .data
                    .with(|d| d.ready().map(|d| d.broken.len()).unwrap_or(0));
                let _ = &tabs_ctx;
                let mark = |on: bool, label: String| {
                    if on {
                        span_bold(format!("[{label}]"), tt.accent)
                    } else {
                        span(format!(" {label} "), tt.text_muted)
                    }
                };
                let mut spans = vec![
                    mark(tab == 0, "Workflows".into()),
                    span(" ", tt.text),
                    mark(tab == 1, DEFAULTS_TITLE.into()),
                ];
                if broken_n > 0 {
                    spans.push(span(" ", tt.text));
                    spans.push(mark(tab == 2, format!("⚠ {BROKEN_TITLE}")));
                }
                line(spans)
            }))
            .child(dyn_view_scoped(
                LayoutStyle::column().gap(0).grow(1.0).min_h(3),
                move |gcx| match wf.tab.get() {
                    0 => workflows_tab(gcx, &body_ctx, &keeper),
                    1 => defaults_tab(gcx, &body_ctx, &keeper),
                    _ => broken_tab(gcx, &body_ctx, &keeper),
                },
            ))
            .child(confirm.view(t, 0))
            .element(t)
            .build(),
    )
    .build()
}

fn handle_key(
    cx: Scope,
    ctx: &Ctx,
    confirm: InlineConfirm,
    flow_pending: Signal<bool>,
    key: Key,
) -> bool {
    let wf = ctx.store.wf;
    // The in-place description input owns every key while it is open.
    if wf.editing.with_untracked(Option::is_some) {
        return false;
    }
    let tab = wf.tab.get_untracked();
    let ntabs = if has_broken(ctx) { 3 } else { 2 };
    match key {
        Key::Char('[') => {
            wf.tab.set((tab + ntabs - 1) % ntabs);
            true
        }
        Key::Tab | Key::Char(']') => {
            wf.tab.set((tab + 1) % ntabs);
            true
        }
        _ if tab == 0 => workflows_key(cx, ctx, confirm, flow_pending, key),
        _ if tab == 1 => defaults_key(cx, ctx, key),
        _ => broken_key(ctx, key),
    }
}

fn workflows_key(
    cx: Scope,
    ctx: &Ctx,
    confirm: InlineConfirm,
    flow_pending: Signal<bool>,
    key: Key,
) -> bool {
    let wf = ctx.store.wf;
    match key {
        Key::Char('/') => open_search(cx, ctx),
        Key::Char('t') => {
            wf.drafts.update(|v| *v = !*v);
            refresh(ctx);
        }
        Key::Char('o') => wf.older.update(|v| *v = !*v),
        Key::Char('h') => {
            wf.archived.update(|v| *v = !*v);
            refresh(ctx);
        }
        Key::Char('i') => open_import(cx, ctx),
        Key::Char('e') => edit_description(ctx),
        Key::Char('x') => match selected(ctx) {
            Some((r, v, _)) => ctx.send(Cmd::Workflows(WfCmd::Export {
                bundle_id: r.bundle_id.clone(),
                version: r.versions[v].version.clone(),
                dir: super::sandbox::artifact_dir(),
            })),
            None => ctx
                .store
                .notice
                .set(Some("no workflow selected — nothing to export".into())),
        },
        Key::Char('f') => match selected(ctx) {
            Some((r, v, _)) => {
                flow_pending.set(true);
                ctx.store.apps.set_note("app:flow", None);
                ctx.send(Cmd::AppAct {
                    app_id: "flow".into(),
                    name: "AbstractFlow".into(),
                    verb: crate::store::apps::AppVerb::Open,
                    path: Some(format!(
                        "/?bundle={}&version={}",
                        crate::api::urlencode(&r.bundle_id),
                        crate::api::urlencode(&r.versions[v].version)
                    )),
                    start_first: false,
                });
            }
            None => ctx
                .store
                .notice
                .set(Some("no workflow selected — nothing to open".into())),
        },
        Key::Char('d') => archive_selected(ctx, confirm),
        Key::Char(' ') => switch_available(ctx),
        _ => return false,
    }
    true
}

/// `d`: Archive (inline confirm, the web's sentence) or Unarchive (no
/// confirm). Shipped workflows have neither — the reason is said.
fn archive_selected(ctx: &Ctx, confirm: InlineConfirm) {
    let wf = ctx.store.wf;
    let Some((r, v, older_row)) = selected(ctx) else {
        ctx.store
            .notice
            .set(Some("no workflow selected — nothing to archive".into()));
        return;
    };
    let ver = &r.versions[v];
    if !ver.can_archive {
        let why = if ver.source == "shipped" {
            "Workflows that ship with the gateway can't be archived or deleted. An admin can turn off “Available to users” instead."
        } else {
            "Only an admin can archive a workflow shared by the gateway."
        };
        wf.msg.set(Some((why.into(), MsgTone::Error)));
        return;
    }
    // An older-version row acts on that version only; a bundle row on all.
    let (label, version, archived) = if older_row {
        (
            format!("{} {}", r.name, version_label(&ver.version)),
            ver.version.clone(),
            ver.archived,
        )
    } else {
        (r.name.clone(), String::new(), r.archived)
    };
    let list = list_args(ctx);
    if archived {
        ctx.send(Cmd::Workflows(WfCmd::Unarchive {
            bundle_id: r.bundle_id,
            version,
            label,
            list,
        }));
        return;
    }
    let c = ctx.clone();
    let bid = r.bundle_id.clone();
    confirm.ask(
        format!(
            "Archive {label}? It disappears from lists and can't start new runs; the file and every past run stay on the gateway."
        ),
        "Archive",
        move || {
            c.send(Cmd::Workflows(WfCmd::Archive {
                bundle_id: bid.clone(),
                version: version.clone(),
                label: label.clone(),
                list,
            }))
        },
    );
}

/// `e`: edit the highlighted workflow's description in place (only when
/// the gateway says this caller may — `actions.can_edit_description`).
fn edit_description(ctx: &Ctx) {
    let wf = ctx.store.wf;
    let Some(r) = selected_row(ctx) else {
        ctx.store
            .notice
            .set(Some("no workflow selected — nothing to edit".into()));
        return;
    };
    if !r.can_edit_description() {
        let why = if r.latest().source == "shipped" {
            "Workflows that ship with the gateway keep their own description."
        } else if r.owner == "gateway" {
            "Only an admin can change the description of a workflow shared by the gateway."
        } else {
            "Only its owner can change this description."
        };
        wf.msg.set(Some((why.into(), MsgTone::Error)));
        return;
    }
    wf.draft.set(r.description.clone());
    wf.msg.set(None);
    wf.editing.set(Some(r.bundle_id.clone()));
}

/// The highlighted row's actions as keys, in the web's icon order
/// (Export · Open · Archive), then the description pencil and the switch.
pub fn row_actions(r: &WfRow, v: usize, older_row: bool, admin: bool) -> Vec<String> {
    let ver = &r.versions[v];
    let mut acts = vec!["x Export".to_string(), "f Open in AbstractFlow".to_string()];
    if ver.can_archive {
        let archived = if older_row { ver.archived } else { r.archived };
        acts.push(if archived { "d Unarchive" } else { "d Archive" }.into());
    }
    if !older_row && r.can_edit_description() {
        acts.push("e Edit description".into());
    }
    if !older_row && admin && r.can_set_availability() {
        acts.push("space Available to users".into());
    }
    acts
}

/// Space: the row's "Available to users" switch (admins, shared rows).
fn switch_available(ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "changing which workflows users see") {
        return;
    }
    let Some(r) = selected_row(ctx) else { return };
    if !r.can_set_availability() {
        ctx.store.wf.msg.set(Some((
            "Only workflows shared by the gateway have an availability switch.".into(),
            MsgTone::Error,
        )));
        return;
    }
    ctx.send(Cmd::Workflows(WfCmd::SetAvailability {
        bundle_id: r.bundle_id.clone(),
        name: r.name.clone(),
        available: !r.available,
        list: list_args(ctx),
    }));
}

fn message(t: &TokenSet, msg: Option<(String, MsgTone)>, width: i32) -> View {
    match msg {
        Some((text, tone)) => kit::sentence(t, &text, width, msg_ink(t, tone)),
        None => Element::new().style(LayoutStyle::default().h(0)).build(),
    }
}

fn workflows_tab(cx: Scope, ctx: &Ctx, keeper: &super::util::FocusKeeper) -> View {
    let t = use_theme(cx).get().tokens;
    let wf = ctx.store.wf;
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let width = (vw - 4).max(20);
    let query = wf.query.get();
    let (drafts, older, archived) = (wf.drafts.get(), wf.older.get(), wf.archived.get());
    let data = wf.data.get();
    let defaults = wf.defaults.get().ready().cloned().unwrap_or_default();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(kit::sentence(&t, PURPOSE, width, t.text_muted));
    let search = if query.is_empty() {
        "Search: (/)".to_string()
    } else {
        format!("Search: {query}")
    };
    col = col.child(kit::sentence(
        &t,
        &format!(
            "{search}   {}   {}   {}",
            switch_text("Drafts", drafts, None, false),
            switch_text("Older versions", older, None, false),
            switch_text("Show archived", archived, None, false)
        ),
        width,
        t.text,
    ));
    // Its own reactive line: a new message must not rebuild the table or
    // an open description input (it would lose its caret).
    col = col.child(dyn_view(
        LayoutStyle::column().gap(0).shrink(0.0),
        move || {
            let t = use_theme(cx).get().tokens;
            message(&t, wf.msg.get(), width)
        },
    ));
    match data {
        Loadable::NotAsked | Loadable::Loading => {
            col = col.child(keeper.anchor(kit::sentence(&t, "Loading…", width, t.text_muted)));
        }
        Loadable::Failed(e) => {
            col = col.child(keeper.anchor(kit::sentence(
                &t,
                &crate::worker::skills::refusal_text(&e),
                width,
                t.error,
            )));
        }
        Loadable::Ready(d) => {
            let narrow = vw < 110;
            let mut rules = vec![
                ColRule::tail("Name", 12),
                ColRule::head("What it does", 18),
                ColRule::head("Version", 7),
            ];
            if !narrow {
                rules.push(ColRule::head("Source", 8));
                rules.push(ColRule::head("Used by", 10));
            }
            if admin {
                // The full words where they fit; the detail line and the
                // footer always name the switch in full.
                rules.push(ColRule::head(
                    if narrow { "Available" } else { AVAILABLE_LABEL },
                    9,
                ));
            }
            let label_of = |i: &str| defaults.label_of(i);
            let rows: Vec<Row> = items(&d, &query, older)
                .into_iter()
                .map(|it| match it {
                    Item::Group(g) => Row::group(g),
                    Item::Row(r) => wf_row(r, admin, narrow, &label_of),
                    Item::Older(r, v) => older_row(r, v, admin, narrow),
                })
                .collect();
            let empty = if d.rows.is_empty() { EMPTY } else { NO_MATCH };
            let editing = wf.editing.get();
            if editing.is_none() {
                // An in-place edit that held the keyboard has closed: the
                // table takes it back.
                keeper.reclaim();
            }
            // R8.1: rows never expand (no ▸ unfold duplicating the row).
            col = col.child(
                keeper.wire(
                    WrapTable::new(rules, rows, wf.sel)
                        .empty(empty)
                        .element(cx, &t),
                ),
            );
            match editing {
                Some(bid) => {
                    let label = d
                        .rows
                        .iter()
                        .find(|r| r.bundle_id == bid)
                        .map(|r| r.name.clone())
                        .unwrap_or_else(|| bid.clone());
                    let c = ctx.clone();
                    let (bid2, label2) = (bid.clone(), label.clone());
                    col = col.child(kit::inline_input(
                        cx,
                        &t,
                        &format!("Description of {label}:"),
                        wf.draft,
                        "What it does, in your words (empty = the file's own description)",
                        move |text| {
                            c.store
                                .wf
                                .msg
                                .set(Some(("Saving...".into(), MsgTone::Plain)));
                            c.send(Cmd::Workflows(WfCmd::SetDescription {
                                bundle_id: bid2.clone(),
                                label: label2.clone(),
                                description: text.trim().to_string(),
                                list: list_args(&c),
                            }));
                        },
                        move || {
                            wf.editing.set(None);
                            wf.msg.set(None);
                        },
                    ));
                    col = col.child(kit::sentence(
                        &t,
                        "Enter saves · Esc keeps the current description · End jumps to the end of the text",
                        width,
                        t.text_faint,
                    ));
                }
                None => {
                    // The highlighted row's actions in ONE line (the web's
                    // icon buttons): its own reactive line, so a selection
                    // move never rebuilds the table.
                    let c = ctx.clone();
                    let tall = abstracttui::app::use_viewport(cx).get_untracked().h >= 30;
                    let labels = defaults.clone();
                    col = col.child(dyn_view(
                        LayoutStyle::column().gap(0).shrink(0.0),
                        move || {
                            let _ = wf.sel.get();
                            match selected(&c) {
                                Some((r, v, older_row)) => {
                                    let mut head = if older_row {
                                        format!(
                                            "{} {}",
                                            r.name,
                                            version_label(&r.versions[v].version)
                                        )
                                    } else {
                                        r.name.clone()
                                    };
                                    if narrow && !older_row {
                                        // No room for the Source / Used by
                                        // columns: the highlighted row says them.
                                        head = format!(
                                            "{head} ({} · used by {})",
                                            source_label(&r.source),
                                            r.used_by(&|i: &str| labels.label_of(i))
                                        );
                                    }
                                    let mut col = Element::new()
                                        .style(LayoutStyle::column().gap(0).shrink(0.0));
                                    for l in super::users::action_lines(
                                        &head,
                                        &row_actions(&r, v, older_row, admin),
                                        width,
                                    ) {
                                        col = col.child(line(vec![span(l, t.accent)]));
                                    }
                                    if !older_row && admin && r.can_set_availability() && tall {
                                        col = col.child(kit::sentence(
                                            &t,
                                            &format!("{AVAILABLE_LABEL}: {AVAILABLE_HELP}"),
                                            width,
                                            t.text_faint,
                                        ));
                                    }
                                    col.build()
                                }
                                None => Element::new().style(LayoutStyle::default().h(0)).build(),
                            }
                        },
                    ));
                }
            }
        }
    }
    col.build()
}

fn wf_row(r: &WfRow, admin: bool, narrow: bool, label_of: &dyn Fn(&str) -> Option<String>) -> Row {
    let mut name = format!("{}\n{}", r.name, r.bundle_id);
    if r.deprecated {
        name.push_str(" · Deprecated");
    }
    if r.archived {
        name.push_str(" · Archived");
    }
    let used_by = r.used_by(label_of);
    let mut what = r.description_text();
    if r.latest().description_edited {
        what.push_str(" (edited)");
    }
    let mut cells = vec![name, what, r.version_text()];
    if !narrow {
        cells.push(source_label(&r.source).into());
        cells.push(used_by);
    }
    if admin {
        cells.push(if r.can_set_availability() {
            super::switch::marker(r.available, false).to_string()
        } else {
            String::new()
        });
    }
    Row::new(cells).dim(r.archived)
}

/// One older version's own row (under its bundle; "Older versions" on).
fn older_row(r: &WfRow, v: usize, admin: bool, narrow: bool) -> Row {
    let ver = &r.versions[v];
    let _ = &r.name;
    let mut name = "↳ older version".to_string();
    if ver.archived {
        name.push_str(" · Archived");
    }
    let mut cells = vec![name, ver.meta(), version_label(&ver.version)];
    if !narrow {
        cells.push(source_label(&ver.source).into());
        cells.push(String::new());
    }
    if admin {
        cells.push(String::new());
    }
    Row::new(cells).dim(true)
}

// ------------------------------------------------------------- defaults

/// The rows shown: the app rows, then "other" rows when unfolded.
fn shown_defaults(d: &DefaultsData, other_open: bool) -> (Vec<DefaultRow>, usize) {
    let apps: Vec<DefaultRow> = d
        .rows
        .iter()
        .filter(|r| r.group != "other")
        .cloned()
        .collect();
    let others: Vec<DefaultRow> = d
        .rows
        .iter()
        .filter(|r| r.group == "other")
        .cloned()
        .collect();
    let n_other = others.len();
    let mut out = apps;
    if other_open {
        out.extend(others);
    }
    (out, n_other)
}

fn defaults_tab(cx: Scope, ctx: &Ctx, keeper: &super::util::FocusKeeper) -> View {
    let t = use_theme(cx).get().tokens;
    let wf = ctx.store.wf;
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let width = (vw - 4).max(20);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(kit::sentence(&t, DEFAULTS_NOTE, width, t.text_muted));
    col = col.child(message(&t, wf.defaults_msg.get(), width));
    match wf.defaults.get() {
        Loadable::NotAsked | Loadable::Loading => {
            col =
                col.child(keeper.anchor(kit::sentence(&t, DEFAULTS_LOADING, width, t.text_muted)));
        }
        Loadable::Failed(e) => {
            col = col.child(keeper.anchor(kit::sentence(
                &t,
                &format!(
                    "Could not read the default workflows. {}",
                    crate::worker::skills::refusal_text(&e)
                ),
                width,
                t.error,
            )));
        }
        Loadable::Ready(d) => {
            if let Some(err) = &d.error {
                col = col.child(kit::sentence(
                    &t,
                    &format!("Could not read the default workflows. {err}"),
                    width,
                    t.error,
                ));
            }
            if !d.writable {
                col = col.child(kit::sentence(&t, DEFAULTS_ADMIN_ONLY, width, t.warn));
            }
            let other_open = wf.other_open.get();
            let (rows, n_other) = shown_defaults(&d, other_open);
            let table_rows: Vec<Row> = rows
                .iter()
                .map(|r| {
                    let mut runs = r.selected_label();
                    if r.state == "broken" {
                        runs.push_str(" · Broken");
                    }
                    Row::new(vec![r.label.clone(), runs])
                })
                .collect();
            let pick_ctx = ctx.clone();
            let pick_d = d.clone();
            col = col.child(
                keeper.wire(
                    WrapTable::new(
                        vec![ColRule::head("App", 12), ColRule::head("Runs", 16)],
                        table_rows,
                        wf.def_sel,
                    )
                    .on_activate(move |_| pick_default(cx, &pick_ctx, &pick_d))
                    .layout(LayoutStyle::default().grow(1.0).min_h(3))
                    .element(cx, &t),
                ),
            );
            if let Some(r) = rows.get(wf.def_sel.get()) {
                col = col.child(kit::sentence(
                    &t,
                    &format!("{} ({})", r.help, r.interface),
                    width,
                    t.text_faint,
                ));
            }
            if n_other > 0 {
                col = col.child(kit::sentence(
                    &t,
                    &format!(
                        "Other workflow types ({n_other}) — o {}",
                        if other_open {
                            "hides them"
                        } else {
                            "shows them"
                        }
                    ),
                    width,
                    t.text_muted,
                ));
            }
            // The selected row's state line, always visible (broken says why).
            if let Some(r) = rows.get(wf.def_sel.get()) {
                if let Some(s) = r.state_line() {
                    let ink = if r.state == "broken" {
                        t.warn
                    } else {
                        t.text_muted
                    };
                    col = col.child(kit::sentence(&t, &s, width, ink));
                }
            }
            col = col.child(line(vec![span_bold("Settings", t.text)]));
            match d.streaming {
                Some(on) => {
                    let why = (!d.writable).then_some("Only an admin can change this.");
                    col = col.child(kit::sentence(
                        &t,
                        &switch_text(STREAMING_LABEL, on, why, false),
                        width,
                        if on { t.accent } else { t.text },
                    ));
                    col = col.child(kit::sentence(&t, STREAMING_HELP, width, t.text_faint));
                }
                None => {
                    col = col.child(kit::sentence(
                        &t,
                        "Streamed replies: not available on this gateway — its settings read has no agents.streaming_default.",
                        width,
                        t.text_faint,
                    ));
                }
            }
        }
    }
    col.build()
}

fn defaults_key(cx: Scope, ctx: &Ctx, key: Key) -> bool {
    let wf = ctx.store.wf;
    let Some(d) = wf.defaults.with_untracked(|d| d.ready().cloned()) else {
        return false;
    };
    match key {
        Key::Char('o') => wf.other_open.update(|v| *v = !*v),
        Key::Char('s') => {
            if !d.writable {
                ctx.store
                    .notice
                    .set(Some("Only an admin can change this.".into()));
            } else if let Some(on) = d.streaming {
                ctx.send(Cmd::Workflows(WfCmd::SetStreaming { on: !on }));
            }
        }
        Key::Char('p') => pick_default(cx, ctx, &d),
        _ => return false,
    }
    true
}

/// Enter / `p` on a defaults row: the picker overlay (saved at once).
fn pick_default(cx: Scope, ctx: &Ctx, d: &DefaultsData) {
    let wf = ctx.store.wf;
    let (rows, _) = shown_defaults(d, wf.other_open.get_untracked());
    let Some(row) = rows.get(wf.def_sel.get_untracked()).cloned() else {
        return;
    };
    if !d.writable {
        ctx.store.notice.set(Some(DEFAULTS_ADMIN_ONLY.into()));
        return;
    }
    let options = row.options();
    let current = row.selected();
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        format!("{} — {}", DEFAULTS_TITLE, row.label),
        &[("↑↓", "choose"), ("Enter", "save")],
        move |mcx, close, _guard| {
            let t = use_theme(mcx).get().tokens;
            let sel = mcx.signal(options.iter().position(|(v, _)| *v == current).unwrap_or(0));
            let rows: Vec<Row> = options
                .iter()
                .map(|(v, l)| {
                    let mark = if *v == current { "●" } else { " " };
                    Row::new(vec![format!("{mark} {l}")])
                })
                .collect();
            let opts = options.clone();
            let iface = row.interface.clone();
            let c = c.clone();
            let close = close.clone();
            Element::new()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .child(kit::sentence(
                    &t,
                    &format!("{} ({})", row.help, row.interface),
                    80,
                    t.text_muted,
                ))
                .child(
                    WrapTable::new(vec![ColRule::head("Workflow", 20)], rows, sel)
                        .on_activate(move |i| {
                            if let Some((v, _)) = opts.get(i) {
                                c.send(Cmd::Workflows(WfCmd::SaveDefault {
                                    iface: iface.clone(),
                                    value: v.clone(),
                                }));
                            }
                            close();
                        })
                        .element(mcx, &t)
                        .autofocus()
                        .build(),
                )
                .build()
        },
    );
}

// --------------------------------------------------------------- broken

fn broken_tab(cx: Scope, ctx: &Ctx, keeper: &super::util::FocusKeeper) -> View {
    let t = use_theme(cx).get().tokens;
    let wf = ctx.store.wf;
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let width = (vw - 4).max(20);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(message(&t, wf.msg.get(), width));
    let Some(d) = wf.data.get().ready().cloned() else {
        return col
            .child(keeper.anchor(kit::sentence(&t, "Loading…", width, t.text_muted)))
            .build();
    };
    col = col.child(kit::sentence(&t, &d.broken_count_line(), width, t.warn));
    col = col.child(kit::sentence(&t, BROKEN_SENTENCE, width, t.text_muted));
    let rows: Vec<Row> = d
        .broken
        .iter()
        .map(|b| {
            let mut detail = vec![format!("Files: {}", b.paths.join(", "))];
            if b.can_archive {
                detail.push(format!(
                    "Actions: d {}",
                    if b.versions.len() == 1 {
                        "Archive".to_string()
                    } else {
                        format!("Archive {}", b.versions.len())
                    }
                ));
            }
            Row::new(vec![b.bundle_id.clone(), b.affected(), b.reason.clone()]).detail(detail)
        })
        .collect();
    col = col.child(
        keeper.wire(
            WrapTable::new(
                vec![
                    ColRule::tail("Workflow", 10),
                    ColRule::head("Affected", 8),
                    ColRule::head("Why the gateway cannot run it", 20),
                ],
                rows,
                wf.broken_sel,
            )
            .element(cx, &t),
        ),
    );
    col.build()
}

fn broken_key(ctx: &Ctx, key: Key) -> bool {
    let wf = ctx.store.wf;
    if key != Key::Char('d') {
        return false;
    }
    let i = wf.broken_sel.get_untracked();
    let Some(b) = wf
        .data
        .with_untracked(|d| d.ready().and_then(|d| d.broken.get(i).cloned()))
    else {
        return true;
    };
    if !b.can_archive {
        ctx.store.notice.set(Some(format!(
            "{}: only an admin can archive a workflow shared by the gateway",
            b.bundle_id
        )));
        return true;
    }
    ctx.send(Cmd::Workflows(WfCmd::ArchiveBroken {
        bundle_id: b.bundle_id,
        versions: b.versions,
        list: list_args(ctx),
    }));
    true
}

// ---------------------------------------------------------------- forms

/// `/`: the search box (filters as you type).
fn open_search(cx: Scope, ctx: &Ctx) {
    let wf = ctx.store.wf;
    kit::open_overlay(
        ctx,
        cx,
        "Search workflows",
        &[("Enter", "done")],
        move |mcx, close, _| {
            let t = use_theme(mcx).get().tokens;
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(super::util::field(
                    &t,
                    "Search",
                    TextInput::new()
                        .value(wf.query)
                        .placeholder("Search by name, description or id")
                        .on_submit(move |_: &str| close())
                        .layout(LayoutStyle::default().w(48).h(1))
                        .element(mcx, &t)
                        .autofocus()
                        .build(),
                ))
                .build()
        },
    );
}

/// `i`: install `.flow` files from THIS machine (the web's file picker;
/// several paths separated by spaces or commas).
fn open_import(cx: Scope, ctx: &Ctx) {
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        "Import .flow",
        &[("Enter", "import")],
        move |mcx, close, _| {
            let t = use_theme(mcx).get().tokens;
            let path = mcx.signal(String::new());
            let submit = {
                let c = c.clone();
                let close = close.clone();
                move || {
                    let paths: Vec<String> = path
                        .get_untracked()
                        .split([',', ' '])
                        .filter(|p| !p.trim().is_empty())
                        .map(str::to_string)
                        .collect();
                    c.send(Cmd::Workflows(WfCmd::Import {
                        paths,
                        list: list_args(&c),
                    }));
                    close();
                }
            };
            let submit2 = submit.clone();
            Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(kit::sentence(
                &t,
                "Install a .flow bundle: files on THIS machine are uploaded to the gateway; an existing version is never overwritten.",
                72,
                t.text_muted,
            ))
            .child(super::util::field(
                &t,
                "Files",
                TextInput::new()
                    .value(path)
                    .placeholder("~/Downloads/my-workflow.flow")
                    .on_submit(move |_: &str| submit())
                    .layout(LayoutStyle::default().w(52).h(1))
                    .element(mcx, &t)
                    .autofocus()
                    .build(),
            ))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(Button::new("Import").on_click(submit2).element(mcx, &t).build())
                    .build(),
            )
            .build()
        },
    );
}
