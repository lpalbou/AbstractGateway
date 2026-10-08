//! Workflows (WORK) — the web console's page of the same name
//! (`#workflows-section`, `#workflows-skipped-section`,
//! `#agent-defaults-section` in console.py; `console_ui.py`
//! mountAgentDefaults): same data, same actions, same sentences.
//!
//! R15 (DESIGN-TUI.md §3.4): ONE page, as on the web — the head (↻ and
//! Import .flow buttons), the toolbar (search field, the Drafts / Older
//! versions / Show archived switches), the table (groups "Shared with
//! everyone" / "Mine"; per row the Available to users toggle and the web's
//! icon buttons Export · Open in AbstractFlow · Archive/Unarchive · Edit
//! description), then — below, scrolling — the "Default workflow per app"
//! card (one picker per app, saved at once; Streamed replies) and the
//! Broken workflows section. Every action is a click AND a key; one
//! `row_actions` list is the single source for the buttons, the hint bar
//! and the tests.

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Cell, Col, ColW, DataTable, FieldState, Ink, Row as WRow, Toggle};
use super::Ctx;
use crate::store::skills::Tone as MsgTone;
use crate::store::workflows_page::{
    source_label, version_label, Broken, DefaultRow, DefaultsData, WfRow, WorkflowsData,
    AVAILABLE_HELP, AVAILABLE_LABEL, BROKEN_SENTENCE, BROKEN_TITLE, DEFAULTS_ADMIN_ONLY,
    DEFAULTS_LOADING, DEFAULTS_NOTE, DEFAULTS_TITLE, EMPTY, NO_MATCH, PURPOSE, STREAMING_HELP,
    STREAMING_LABEL, SUBTITLE, TITLE,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::workflows::{ListArgs, WfCmd};
use crate::worker::Cmd;

/// Verbs only an admin may use on this page (the web hides or disables
/// them for others). Archive and Import are NOT here: the gateway lets a
/// user archive and import their own ("Mine") workflows.
pub const ADMIN_KEYS: &[&str] = &["space"];

/// The web's head and toolbar words (console.py `#workflows-section`).
pub const RELOAD_TIP: &str = "Reload the workflow list";
pub const IMPORT_LABEL: &str = "Import .flow";
pub const IMPORT_TIP: &str = "Install a .flow bundle";
pub const SEARCH_PLACEHOLDER: &str = "Search by name, description or id";
pub const BROKEN_ARCHIVE_TIP: &str = "Hide these unusable versions; the files stay on disk";
pub const OTHER_TYPES: &str = "Other workflow types";
pub const SETTINGS: &str = "Settings";

fn list_args(ctx: &Ctx) -> ListArgs {
    ListArgs {
        drafts: ctx.store.wf.drafts.get_untracked(),
        archived: ctx.store.wf.archived.get_untracked(),
        defaults: ctx.store.conn.with_untracked(ConnPhase::is_admin),
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
        defaults: store.conn.with_untracked(ConnPhase::is_admin),
    })));
}

/// The footer verbs (R15: only what applies — the selected row's actions,
/// then the page keys).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let _ = ctx.store.wf.sel.get();
    ctx.store.wf.data.with(|_| ());
    let mut out = vec![("↑↓", "rows"), ("Enter", "Export"), ("Tab", "actions")];
    if admin {
        out.push(("space", "Available to users"));
    }
    if let Some((r, v, older_row)) = selected(ctx) {
        for a in row_actions(&r, v, older_row, admin) {
            if a.is_enabled() {
                if let Some(k) = a.key {
                    out.push((key_label(k), static_label(a.id)));
                }
            }
        }
    }
    out.extend_from_slice(&[
        ("/", "search"),
        ("t", "Drafts"),
        ("o", "Older versions"),
        ("h", "Show archived"),
        ("i", IMPORT_LABEL),
        ("r", "refresh"),
    ]);
    out
}

fn key_label(k: char) -> &'static str {
    match k {
        'x' => "x",
        'f' => "f",
        'd' => "d",
        'e' => "e",
        _ => "?",
    }
}

fn static_label(id: &str) -> &'static str {
    match id {
        "export" => "Export",
        "open" => "Open in AbstractFlow",
        "archive" => "Archive",
        "unarchive" => "Unarchive",
        "edit" => "Edit description",
        _ => "",
    }
}

fn msg_ink(t: &TokenSet, tone: MsgTone) -> Rgba {
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

/// A row's stable key: the bundle id, or `bundle@version` for an older
/// version's own row.
fn item_key(it: &Item) -> Option<String> {
    match it {
        Item::Group(_) => None,
        Item::Row(r) => Some(r.bundle_id.clone()),
        Item::Older(r, v) => Some(format!("{}@{}", r.bundle_id, r.versions[*v].version)),
    }
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

/// Select the row whose key is `key` (the legacy index paths read it).
fn select_key(ctx: &Ctx, key: &str) {
    let wf = ctx.store.wf;
    let q = wf.query.get_untracked();
    let older = wf.older.get_untracked();
    let pos = wf.data.with_untracked(|d| {
        d.ready().and_then(|d| {
            items(d, &q, older)
                .iter()
                .position(|it| item_key(it).as_deref() == Some(key))
        })
    });
    if let Some(i) = pos {
        if wf.sel.get_untracked() != i {
            wf.sel.set(i);
        }
    }
}

/// The label an action names (`<name>` or `<name> <version>` for an
/// older version's own row — the web's `label`).
fn action_label(r: &WfRow, v: usize, older_row: bool) -> String {
    if older_row {
        format!("{} {}", r.name, r.versions[v].version)
    } else {
        r.name.clone()
    }
}

/// A row's actions, in the web's icon order with the web's tooltips:
/// Export · Open in AbstractFlow · Archive/Unarchive, then the description
/// pencil (bundle rows). An action the gateway refuses stays FAINT with
/// its reason (a press says it) — never silently missing.
pub fn row_actions(r: &WfRow, v: usize, older_row: bool, admin: bool) -> Vec<Action> {
    let _ = admin;
    let ver = &r.versions[v];
    let label = action_label(r, v, older_row);
    let mut out = vec![
        Action::glyph("export", "Export")
            .key('x')
            .tooltip(format!("Export {label} as a .flow file")),
        Action::glyph("open", "Open in AbstractFlow")
            .key('f')
            .tooltip(format!("Open {label} in AbstractFlow")),
    ];
    let archived = if older_row { ver.archived } else { r.archived };
    let refusal = (!ver.can_archive).then(|| {
        if ver.source == "shipped" {
            "Workflows that ship with the gateway can't be archived or deleted. An admin can turn off “Available to users” instead.".to_string()
        } else {
            "Only an admin can archive a workflow shared by the gateway.".to_string()
        }
    });
    if archived {
        out.push(
            Action::glyph("unarchive", "Unarchive")
                .key('d')
                .tooltip(format!("Unarchive {label} (shown again)"))
                .refused(refusal),
        );
    } else {
        out.push(
            Action::glyph("archive", "Archive")
                .key('d')
                .tooltip(format!("Archive {label} (kept, hidden)"))
                .refused(refusal)
                .danger(),
        );
    }
    if !older_row {
        let why = (!r.can_edit_description()).then(|| {
            if r.latest().source == "shipped" {
                "Workflows that ship with the gateway keep their own description.".to_string()
            } else if r.owner == "gateway" {
                "Only an admin can change the description of a workflow shared by the gateway."
                    .to_string()
            } else {
                "Only its owner can change this description.".to_string()
            }
        });
        out.push(
            Action::glyph("edit", "Edit description")
                .key('e')
                .tooltip(format!("Edit the description of {}", r.name))
                .refused(why),
        );
    }
    out
}

/// The broken group's one action (the web's "Archive" / "Archive N").
pub fn broken_actions(b: &Broken) -> Vec<Action> {
    let label = if b.versions.len() == 1 {
        "Archive".to_string()
    } else {
        format!("Archive {}", b.versions.len())
    };
    vec![Action::label("archive_broken", label)
        .key('d')
        .tooltip(BROKEN_ARCHIVE_TIP)
        .refused((!b.can_archive).then(|| {
            format!(
                "{}: only an admin can archive a workflow shared by the gateway",
                b.bundle_id
            )
        }))
        .danger()]
}

/// The head's buttons (↻, Import .flow).
pub fn head_actions() -> Vec<Action> {
    vec![
        Action::label("reload", "↻").key('r').tooltip(RELOAD_TIP),
        Action::label("import", IMPORT_LABEL)
            .key('i')
            .tooltip(IMPORT_TIP),
    ]
}

/// The page width the content lays out in.
fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// A page head (R15, the reference screens' shape): title + subtitle on
/// the left, the head's buttons on the right (under the title when the
/// page is narrow). Shared by the group-A screens.
pub(crate) fn page_head(
    t: &TokenSet,
    title: &str,
    subtitle: &str,
    w: i32,
    buttons: Vec<(View, i32)>,
) -> View {
    let bw: i32 = buttons.iter().map(|(_, wd)| wd + 1).sum();
    let mut btn_row = Element::new().style(
        LayoutStyle::row()
            .height(Dimension::Cells(1))
            .gap(1)
            .shrink(0.0),
    );
    for (b, _) in buttons {
        btn_row = btn_row.child(b);
    }
    let title_w = abstracttui::text::width(title).max(abstracttui::text::width(subtitle));
    let side = title_w + bw + 2 <= w;
    let titles = Element::new()
        .style(if side {
            LayoutStyle::column()
                .width(Dimension::Cells(w - bw - 1))
                .shrink(0.0)
        } else {
            LayoutStyle::column().shrink(0.0)
        })
        .child(super::w::paint::fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(title, t.text).bold()],
            None,
        ))
        .child(sentence(
            t,
            subtitle,
            (w - if side { bw + 2 } else { 0 }).max(20),
            t.text_muted,
        ))
        .build();
    Element::new()
        .style(if side {
            LayoutStyle::row().shrink(0.0)
        } else {
            LayoutStyle::column().shrink(0.0)
        })
        .child(titles)
        .child(btn_row.build())
        .build()
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let wf = ctx.store.wf;
    let tt = *t;
    if wf.data.with_untracked(|d| matches!(d, Loadable::NotAsked))
        && ctx.store.conn.with_untracked(ConnPhase::is_connected)
    {
        refresh(ctx);
    }
    install_effects(cx, ctx);
    let keys_ctx = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                if handle_key(cx, &keys_ctx, k.key) {
                    ectx.stop_propagation();
                }
            }
        })
        .child(head(cx, ctx, &tt))
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            let t = tt;
            let w = (crate::ui::page_viewport(cx).get().w - 2).max(20);
            sentence(&t, PURPOSE, w, t.text_muted)
        }))
        .child(toolbar(cx, ctx, &tt))
        // Its own reactive line: a new message must not rebuild the table.
        .child(dyn_view(
            LayoutStyle::column().gap(0).shrink(0.0),
            move || {
                let t = tt;
                let w = (crate::ui::page_viewport(cx).get().w - 2).max(20);
                match wf.msg.get() {
                    Some((text, tone)) => sentence(&t, &text, w, msg_ink(&t, tone)),
                    None => Element::new().style(LayoutStyle::default().h(0)).build(),
                }
            },
        ))
        .child(body(cx, ctx, &tt))
        .build()
}

/// The page's effects: the Open-in-AbstractFlow link modal and refusal,
/// the success toasts.
fn install_effects(cx: Scope, ctx: &Ctx) {
    let wf = ctx.store.wf;
    // "Open in AbstractFlow" mints a one-time link through the Apps lane
    // (POST /apps/flow/open); its modal opens here, and a refusal is said
    // in this page's message line.
    let ctx2 = ctx.clone();
    let ctx1 = ctx.clone();
    cx.effect(move || {
        if let Some(link) = ctx1.store.apps.open_link.get() {
            if !FLOW_PENDING.with(|p| p.get()) || ctx1.ui.prompt_open.get() > 0 {
                return;
            }
            FLOW_PENDING.with(|p| p.set(false));
            ctx1.store.apps.open_link.set(None);
            super::apps::open_link_modal(cx, &ctx1, link);
        }
    });
    cx.effect(move || {
        let notes = ctx2.store.apps.notes.get();
        if !FLOW_PENDING.with(|p| p.get()) {
            return;
        }
        if let Some((_, n)) = notes.iter().find(|(k, _)| k == "app:flow") {
            FLOW_PENDING.with(|p| p.set(false));
            let mut text = format!("Could not open AbstractFlow: {}", n.text);
            if let Some(h) = &n.hint {
                text.push(' ');
                text.push_str(h);
            }
            wf.msg.set(Some((text, MsgTone::Error)));
        }
    });
    // R15 §2.6: a verified success is a toast (the gateway's sentence);
    // refusals stay inline in the message line.
    let ctx3 = ctx.clone();
    cx.effect(move || {
        if let Some((text, MsgTone::Ok)) = wf.msg.get() {
            super::w::toast(&ctx3, cx, text);
            wf.msg.set(None);
        }
    });
}

thread_local! {
    /// An Open in AbstractFlow press is waiting for its link.
    static FLOW_PENDING: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Title + subtitle, the ↻ and Import .flow buttons.
fn head(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |hcx| {
        let w = page_w(hcx);
        let mut buttons = Vec::new();
        for a in head_actions() {
            let c = ctx.clone();
            let wd = a.width();
            let id = a.id;
            buttons.push((
                button(hcx, &tt, &a, On::Page, true, move || match id {
                    "reload" => refresh(&c),
                    _ => open_import(pcx, &c),
                }),
                wd,
            ));
        }
        page_head(&tt, TITLE, SUBTITLE, w, buttons)
    })
}

/// The search field and the three switches (applied at once): one row,
/// or the switches on their own row when the page is narrow.
fn toolbar(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    let vp = crate::ui::page_viewport(cx);
    let narrow = cx.memo(move || vp.get().w < 100);
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |tcx| {
        let wf = ctx.store.wf;
        let caret = ctx.ui.caret;
        let search = super::w::caret_tracked(
            tcx,
            caret,
            TextInput::new()
                .value(wf.query)
                .placeholder(SEARCH_PLACEHOLDER)
                .layout(LayoutStyle::default().w(36).h(1).shrink(0.0))
                .element(tcx, &tt),
        );
        let search = super::util::esc_releases_focus(search, ctx.store.notice).build();
        let (c1, c2) = (ctx.clone(), ctx.clone());
        let drafts = Toggle::bound(wf.drafts)
            .label("Drafts")
            .tip("Drafts  (t)")
            .on_change(move |v| {
                c1.store.wf.drafts.set(v);
                refresh(&c1);
            });
        let older = Toggle::bound(wf.older)
            .label("Older versions")
            .tip("Older versions  (o)")
            .on_change(move |v| wf.older.set(v));
        let archived = Toggle::bound(wf.archived)
            .label("Show archived")
            .tip("Show archived  (h)")
            .on_change(move |v| {
                c2.store.wf.archived.set(v);
                refresh(&c2);
            });
        let switches = Element::new()
            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
            .child(drafts.view(tcx, &tt))
            .child(older.view(tcx, &tt))
            .child(archived.view(tcx, &tt))
            .build();
        if narrow.get() {
            Element::new()
                .style(LayoutStyle::column().shrink(0.0))
                .child(search)
                .child(switches)
                .build()
        } else {
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(search)
                .child(switches)
                .build()
        }
    })
}

/// The table, then (scrolling) the defaults card and the broken section.
fn body(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    // Keyed selection for the table, synced two ways with the legacy index.
    let sel_key = pcx.signal(Option::<String>::None);
    {
        let ctx = ctx.clone();
        pcx.effect(move || {
            let wf = ctx.store.wf;
            let _ = (wf.sel.get(), wf.query.get(), wf.older.get());
            wf.data.with(|_| ());
            let i = wf.sel.get_untracked();
            let (q, older) = (wf.query.get_untracked(), wf.older.get_untracked());
            let key = wf.data.with_untracked(|d| {
                d.ready().and_then(|d| {
                    let its = items(d, &q, older);
                    its.get(i)
                        .and_then(item_key)
                        .or_else(|| its.iter().find_map(item_key))
                })
            });
            if key.is_some() && sel_key.with_untracked(|k| *k != key) {
                sel_key.set(key);
            }
        });
    }
    {
        let ctx = ctx.clone();
        pcx.effect(move || {
            if let Some(k) = sel_key.get() {
                select_key(&ctx, &k);
            }
        });
    }
    dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |gcx| {
        let wf = ctx.store.wf;
        let admin = ctx.store.conn.with(ConnPhase::is_admin);
        let vp = crate::ui::page_viewport(gcx).get();
        let width = (vp.w - 2).max(20);
        let query = wf.query.get().trim().to_lowercase();
        let older = wf.older.get();
        let data = wf.data.get();
        let defaults = wf.defaults.get().ready().cloned().unwrap_or_default();
        let anchor = |v: View| -> View {
            Element::new()
                .style(LayoutStyle::column().shrink(0.0))
                .focusable()
                .autofocus()
                .child(v)
                .build()
        };
        let d = match data {
            Loadable::NotAsked | Loadable::Loading => {
                return anchor(sentence(&tt, "Loading…", width, tt.text_muted));
            }
            Loadable::Failed(e) => {
                return anchor(sentence(
                    &tt,
                    &crate::worker::skills::refusal_text(&e),
                    width,
                    tt.error,
                ));
            }
            Loadable::Ready(d) => d,
        };
        let narrow = vp.w < 110;
        let label_of = |i: &str| d.interface_label(i).or_else(|| defaults.label_of(i));
        // At 80 columns (DESIGN-TUI §3.4): Name · Version · Available ·
        // Actions, with "What it does · Source · Used by" as the row's
        // second line (muted, the whole width).
        let mut cols = if narrow {
            vec![
                Col::new("Name", ColW::Flex { weight: 1, min: 12 }),
                Col::new("Version", ColW::Fit { min: 7, max: 18 }),
            ]
        } else {
            vec![
                Col::new("Name", ColW::Fit { min: 10, max: 26 }),
                Col::new("What it does", ColW::Flex { weight: 1, min: 14 }),
                Col::new("Version", ColW::Fit { min: 7, max: 18 }),
            ]
        };
        if !narrow {
            cols.push(Col::new("Source", ColW::Fit { min: 6, max: 18 }));
            cols.push(Col::new("Used by", ColW::Fit { min: 7, max: 26 }));
        }
        if admin {
            cols.push(Col::new(
                if narrow { "Available" } else { AVAILABLE_LABEL },
                ColW::Fit { min: 9, max: 18 },
            ));
        }
        cols.push(Col::new("Actions", ColW::Fit { min: 7, max: 10 }));
        let mut table_rows: Vec<WRow> = Vec::new();
        let mut group: Option<&'static str> = None;
        for it in items(&d, &query, older) {
            match it {
                Item::Group(g) => group = Some(g),
                Item::Row(r) => {
                    let mut row = wf_row(&tt, r, admin, narrow, &label_of);
                    if let Some(g) = group.take() {
                        row = row.group(g);
                    }
                    table_rows.push(row);
                }
                Item::Older(r, v) => {
                    table_rows.push(older_row(&tt, r, v, admin, narrow));
                }
            }
        }
        let empty = if d.rows.is_empty() { EMPTY } else { NO_MATCH };
        // Rows above the table: head (2+), purpose, toolbar, message line,
        // the table's own head (2).
        let purpose = super::w::paint::wrap(PURPOSE, width).len() as i32;
        let toolbar = if vp.w < 100 { 2 } else { 1 };
        let reserve = 2 + purpose + toolbar + 1 + 2;
        let room = (vp.h - reserve).max(4);
        // The cards below keep a share on a tall page, a peek on a short one
        // (the table first; the wheel / Tab reach the cards).
        let below_h = if !(admin || !d.broken.is_empty()) {
            0
        } else if vp.h < 30 {
            3
        } else {
            room * 2 / 5
        };
        let max_rows = (room - below_h).max(4);
        let (ca, ct, ce, cs) = (ctx.clone(), ctx.clone(), ctx.clone(), ctx.clone());
        let table = DataTable::new(cols, table_rows, sel_key)
            .width(width)
            .max_rows(max_rows)
            .empty(empty)
            .autofocus()
            .on_action(move |key, id| {
                select_key(&ca, key);
                row_action(pcx, &ca, id);
            })
            .on_toggle(move |key, _id, _want| {
                select_key(&ct, key);
                switch_available(&ct);
            })
            .on_activate(move |key| {
                select_key(&ce, key);
                row_action(pcx, &ce, "export");
            })
            .on_space(move |key| {
                select_key(&cs, key);
                switch_available(&cs);
            })
            .view(gcx, &tt);
        let mut below = Element::new().style(LayoutStyle::column().gap(0));
        if admin {
            below = below
                .child(super::w::fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![],
                    None,
                ))
                .child(defaults_card(gcx, pcx, &ctx, &tt, width - 1));
        }
        if !d.broken.is_empty() {
            below = below
                .child(super::w::fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![],
                    None,
                ))
                .child(broken_section(gcx, pcx, &ctx, &tt, &d, width - 1));
        }
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0).grow(1.0))
            .child(table);
        if admin || !d.broken.is_empty() {
            col = col.child(
                Scroll::new(below.build())
                    .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                    .scrollbar_auto_hide(true)
                    .view(gcx),
            );
        }
        col.build()
    })
}

fn wf_row(
    t: &TokenSet,
    r: &WfRow,
    admin: bool,
    narrow: bool,
    label_of: &dyn Fn(&str) -> Option<String>,
) -> WRow {
    let mut kind = vec![Ink::new(r.bundle_id.clone(), t.text_muted)];
    if r.deprecated {
        kind.push(Ink::new(" · Deprecated", t.warn));
    }
    if r.archived {
        kind.push(Ink::new(" · Archived", t.text_muted));
    }
    let used_by = r.used_by(label_of);
    let name = Cell::Lines(vec![vec![Ink::new(r.name.clone(), t.text).bold()], kind]);
    let mut what = r.description_text();
    if r.latest().description_edited {
        what.push_str(" (edited)");
    }
    let mut cells = vec![name];
    let mut note = None;
    if narrow {
        note = Some((
            format!("{what} · {} · {used_by}", source_label(&r.source)),
            t.text_muted,
        ));
        cells.push(Cell::text(r.version_text(), t.text));
    } else {
        cells.push(Cell::text(what, t.text));
        cells.push(Cell::text(r.version_text(), t.text));
        cells.push(Cell::text(source_label(&r.source), t.text_muted));
        cells.push(Cell::text(used_by, t.text));
    }
    if admin {
        cells.push(if r.can_set_availability() {
            Cell::Toggle {
                id: "available",
                on: r.available,
                refused: None,
                tip: Some(format!("{AVAILABLE_HELP}  (space)")),
            }
        } else {
            Cell::text("", t.text)
        });
    }
    cells.push(Cell::Actions(row_actions(r, r.latest, false, admin)));
    WRow::new(r.bundle_id.clone(), cells)
        .dim(r.archived)
        .note(note)
}

/// One older version's own row (under its bundle; "Older versions" on).
fn older_row(t: &TokenSet, r: &WfRow, v: usize, admin: bool, narrow: bool) -> WRow {
    let ver = &r.versions[v];
    let mut name = "↳ older version".to_string();
    if ver.archived {
        name.push_str(" · Archived");
    }
    let mut cells = vec![Cell::text(name, t.text_muted)];
    let mut note = None;
    if narrow {
        note = Some((
            format!("{} · {}", ver.meta(), source_label(&ver.source)),
            t.text_muted,
        ));
        cells.push(Cell::text(version_label(&ver.version), t.text_muted));
    } else {
        cells.push(Cell::text(ver.meta(), t.text_muted));
        cells.push(Cell::text(version_label(&ver.version), t.text_muted));
        cells.push(Cell::text(source_label(&ver.source), t.text_muted));
        cells.push(Cell::text("", t.text));
    }
    if admin {
        cells.push(Cell::text("", t.text));
    }
    cells.push(Cell::Actions(row_actions(r, v, true, admin)));
    WRow::new(format!("{}@{}", r.bundle_id, ver.version), cells)
        .dim(true)
        .note(note)
}

/// A row action (a click or its key) on the selected row: a refused
/// action says why and does nothing.
fn row_action(cx: Scope, ctx: &Ctx, id: &str) {
    let Some((r, v, older_row)) = selected(ctx) else {
        ctx.store
            .notice
            .set(Some("no workflow selected — choose a row first".into()));
        return;
    };
    let admin = ctx.store.conn.with_untracked(ConnPhase::is_admin);
    let Some(a) = row_actions(&r, v, older_row, admin)
        .into_iter()
        .find(|a| a.id == id)
    else {
        ctx.store
            .notice
            .set(Some(format!("{} has no {id} action", r.name)));
        return;
    };
    if let Err(why) = a.enabled {
        ctx.store.wf.msg.set(Some((why, MsgTone::Error)));
        return;
    }
    match id {
        "export" => ctx.send(Cmd::Workflows(WfCmd::Export {
            bundle_id: r.bundle_id.clone(),
            version: r.versions[v].version.clone(),
            dir: super::sandbox::artifact_dir(),
        })),
        "open" => {
            FLOW_PENDING.with(|p| p.set(true));
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
        "archive" | "unarchive" => archive_selected(cx, ctx),
        "edit" => edit_description(cx, ctx),
        _ => {}
    }
}

fn handle_key(cx: Scope, ctx: &Ctx, key: Key) -> bool {
    let wf = ctx.store.wf;
    match key {
        Key::Char('/') => {
            // The search field is on the page: say where it is.
            ctx.store.notice.set(Some(
                "the search field is in the toolbar — Tab reaches it, or click it".into(),
            ));
        }
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
        // Space reaches here when the table does not have the keyboard.
        Key::Char(' ') => switch_available(ctx),
        Key::Char('x') => row_action(cx, ctx, "export"),
        Key::Char('f') => row_action(cx, ctx, "open"),
        Key::Char('e') => row_action(cx, ctx, "edit"),
        Key::Char('d') => {
            let archived = selected(ctx)
                .map(|(r, v, o)| {
                    if o {
                        r.versions[v].archived
                    } else {
                        r.archived
                    }
                })
                .unwrap_or(false);
            row_action(cx, ctx, if archived { "unarchive" } else { "archive" })
        }
        _ => return false,
    }
    true
}

/// Archive (confirmed, the web's sentence) or Unarchive (at once).
fn archive_selected(cx: Scope, ctx: &Ctx) {
    let Some((r, v, older_row)) = selected(ctx) else {
        return;
    };
    let ver = &r.versions[v];
    // An older-version row acts on that version only; a bundle row on all.
    let label = action_label(&r, v, older_row);
    let (version, archived) = if older_row {
        (ver.version.clone(), ver.archived)
    } else {
        (String::new(), r.archived)
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
    super::w::confirm(
        ctx,
        cx,
        archive_question(&label),
        "Archive",
        "Cancel",
        move || {
            c.send(Cmd::Workflows(WfCmd::Archive {
                bundle_id: bid,
                version,
                label,
                list,
            }))
        },
    );
}

/// The web's archive confirmation (console.py archiveWorkflow).
pub fn archive_question(label: &str) -> String {
    format!(
        "Archive {label}? It disappears from lists and can't start new runs; the file and every past run stay on the gateway."
    )
}

/// Space / the toggle: the row's "Available to users" switch (admins,
/// shared rows).
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

// ------------------------------------------------------------- defaults

/// The "Default workflow per app" card (admins): one picker per app,
/// applied at once ("Saved" beside it), the other workflow types under
/// their own heading (visible — no disclosure, R15 D1), then Settings ·
/// Streamed replies.
fn defaults_card(cx: Scope, pcx: Scope, ctx: &Ctx, t: &TokenSet, width: i32) -> View {
    let _ = pcx;
    let wf = ctx.store.wf;
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(super::w::section(t, DEFAULTS_TITLE))
        .child(sentence(t, DEFAULTS_NOTE, width, t.text_muted));
    let d = match wf.defaults.get() {
        Loadable::NotAsked | Loadable::Loading => {
            return col
                .child(sentence(t, DEFAULTS_LOADING, width, t.text_muted))
                .build();
        }
        Loadable::Failed(e) => {
            return col
                .child(sentence(
                    t,
                    &format!(
                        "Could not read the default workflows. {}",
                        crate::worker::skills::refusal_text(&e)
                    ),
                    width,
                    t.error,
                ))
                .build();
        }
        Loadable::Ready(d) => d,
    };
    if let Some(err) = &d.error {
        col = col.child(sentence(
            t,
            &format!("Could not read the default workflows. {err}"),
            width,
            t.error,
        ));
    }
    let label_w = d
        .rows
        .iter()
        .map(|r| abstracttui::text::width(&r.label) + 2)
        .max()
        .unwrap_or(10)
        .min(width * 2 / 5);
    let apps: Vec<&DefaultRow> = d.rows.iter().filter(|r| r.group != "other").collect();
    let others: Vec<&DefaultRow> = d.rows.iter().filter(|r| r.group == "other").collect();
    for r in &apps {
        col = col.child(default_row(cx, ctx, t, r, &d, label_w, width));
    }
    if !others.is_empty() {
        col = col.child(super::w::section(t, OTHER_TYPES));
        for r in &others {
            col = col.child(default_row(cx, ctx, t, r, &d, label_w, width));
        }
    }
    if !d.writable {
        col = col.child(sentence(t, DEFAULTS_ADMIN_ONLY, width, t.warn));
    }
    col = col.child(super::w::section(t, SETTINGS));
    match d.streaming {
        Some(on) => {
            let c = ctx.clone();
            let why = (!d.writable).then(|| "Only an admin can change this.".to_string());
            col = col.child(
                Toggle::new(on)
                    .label(STREAMING_LABEL)
                    .tip(STREAMING_HELP)
                    .refused(why)
                    .on_change(move |v| c.send(Cmd::Workflows(WfCmd::SetStreaming { on: v })))
                    .view(cx, t),
            );
            col = col.child(sentence(t, STREAMING_HELP, width, t.text_faint));
        }
        None => {
            col = col.child(sentence(
                t,
                "Streamed replies: not available on this gateway — its settings read has no agents.streaming_default.",
                width,
                t.warn,
            ));
        }
    }
    // The last save's answer ("Saved", or the gateway's refusal).
    let st = match wf.defaults_msg.get() {
        Some((text, MsgTone::Error)) => FieldState::Refused(text),
        Some((text, _)) => FieldState::Saved(text),
        None => FieldState::Idle,
    };
    col.child(super::w::state_line(cx.signal(st), width))
        .build()
}

/// One app's picker row: its label (the help as the tooltip), the Select
/// (saved at once), and what it runs now or why it cannot.
fn default_row(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    r: &DefaultRow,
    d: &DefaultsData,
    label_w: i32,
    width: i32,
) -> View {
    let opts = r.options();
    let cur = opts
        .iter()
        .position(|(v, _)| *v == r.selected())
        .unwrap_or(0);
    let chosen = cx.signal(cur);
    let control = if d.writable {
        let c = ctx.clone();
        let iface = r.interface.clone();
        let opts2 = opts.clone();
        abstracttui::app::select::Select::new(
            opts.iter()
                .map(|(_, l)| abstracttui::app::select::SelectOption::new(l.clone()))
                .collect(),
        )
        .value(chosen)
        .layout(
            LayoutStyle::default()
                .w((width - label_w - 2).clamp(16, 52))
                .h(1)
                .shrink(0.0),
        )
        .on_change(move |i| {
            if let Some((value, _)) = opts2.get(i) {
                c.send(Cmd::Workflows(WfCmd::SaveDefault {
                    iface: iface.clone(),
                    value: value.clone(),
                }));
            }
        })
        .element(cx, t)
        .build()
    } else {
        sentence(t, &r.selected_label(), width - label_w, t.text)
    };
    let label = super::w::tip::with_tip(
        cx,
        Element::new()
            .style(
                LayoutStyle::default()
                    .width(Dimension::Cells(label_w))
                    .height(Dimension::Cells(1))
                    .shrink(0.0),
            )
            .child(super::w::fill_line(
                LayoutStyle::default()
                    .width(Dimension::Cells(label_w))
                    .height(Dimension::Cells(1)),
                vec![Ink::new(&r.label, t.text)],
                None,
            )),
        format!("{} ({})", r.help, r.interface),
    )
    .build();
    let mut col = Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(
            Element::new()
                .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
                .child(label)
                .child(control)
                .build(),
        );
    if let Some(s) = r.state_line() {
        let ink = if r.state == "broken" {
            t.warn
        } else {
            t.text_muted
        };
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().shrink(0.0))
                .child(
                    Element::new()
                        .style(LayoutStyle::default().w(label_w).h(1).shrink(0.0))
                        .build(),
                )
                .child(sentence(t, &s, (width - label_w).max(10), ink))
                .build(),
        );
    }
    col.build()
}

// --------------------------------------------------------------- broken

fn broken_section(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: &WorkflowsData,
    width: i32,
) -> View {
    let _ = pcx;
    let rows: Vec<WRow> = d
        .broken
        .iter()
        .map(|b| {
            WRow::new(
                b.bundle_id.clone(),
                vec![
                    Cell::text(b.bundle_id.clone(), t.text),
                    Cell::text(b.affected(), t.text),
                    Cell::text(b.reason.clone(), t.text),
                    Cell::Actions(broken_actions(b)),
                ],
            )
        })
        .collect();
    let sel = cx.signal(d.broken.first().map(|b| b.bundle_id.clone()));
    let c = ctx.clone();
    let table = DataTable::new(
        vec![
            Col::new("Workflow", ColW::Fit { min: 8, max: 28 }),
            Col::new("Affected", ColW::Fit { min: 8, max: 12 }),
            Col::new(
                "Why the gateway cannot run it",
                ColW::Flex { weight: 1, min: 16 },
            ),
            Col::new("Actions", ColW::Fit { min: 7, max: 14 }),
        ],
        rows,
        sel,
    )
    .width(width)
    .max_rows(12)
    .on_action(move |key, _id| archive_broken(&c, key))
    .view(cx, t);
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(super::w::section(t, &format!("⚠ {BROKEN_TITLE}")))
        .child(sentence(t, &d.broken_count_line(), width, t.warn))
        .child(sentence(t, BROKEN_SENTENCE, width, t.text_muted))
        .child(table)
        .build()
}

fn archive_broken(ctx: &Ctx, bundle_id: &str) {
    let Some(b) = ctx.store.wf.data.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.broken.iter().find(|b| b.bundle_id == bundle_id).cloned())
    }) else {
        return;
    };
    if let Some(Err(why)) = broken_actions(&b).first().map(|a| a.enabled.clone()) {
        ctx.store.notice.set(Some(why));
        return;
    }
    ctx.send(Cmd::Workflows(WfCmd::ArchiveBroken {
        bundle_id: b.bundle_id,
        versions: b.versions,
        list: list_args(ctx),
    }));
}

// ---------------------------------------------------------------- forms

/// Edit description: a small form (the web's in-place textarea) — Save
/// sends the PATCH; the gateway's answer closes it (a refusal stays
/// inline); Close with an edit asks "Discard changes?".
fn edit_description(cx: Scope, ctx: &Ctx) {
    let wf = ctx.store.wf;
    let Some(r) = selected_row(ctx) else {
        return;
    };
    wf.draft.set(r.description.clone());
    wf.msg.set(None);
    wf.editing.set(Some(r.bundle_id.clone()));
    let c = ctx.clone();
    let name = r.name.clone();
    let initial = r.description.clone();
    super::w::FormModal::new(format!("Description of {name}"))
        .lead("What it does, in your words (empty = the file's own description).")
        .size(84, 12)
        .open(ctx, cx, move |mcx, close, guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let esc_armed = mcx.signal(false);
            let form_error = mcx.signal(Option::<String>::None);
            super::install_dirty_guard(
                mcx,
                &guard,
                vec![(wf.draft, initial.clone())],
                esc_armed,
                form_error,
            );
            // The gateway saved it (the worker clears `editing`): close.
            {
                let close = close.clone();
                let sent = mcx.signal(false);
                mcx.effect(move || {
                    let editing = wf.editing.get();
                    if editing.is_none() && sent.get_untracked() {
                        close();
                    }
                });
                SAVE_SENT.with(|s| *s.borrow_mut() = Some(sent));
            }
            let save = {
                let c = c.clone();
                let bid = r.bundle_id.clone();
                let label = name.clone();
                move || {
                    if let Some(s) = SAVE_SENT.with(|s| *s.borrow()) {
                        s.set(true);
                    }
                    c.store.wf.msg.set(Some(("Saving…".into(), MsgTone::Plain)));
                    c.send(Cmd::Workflows(WfCmd::SetDescription {
                        bundle_id: bid.clone(),
                        label: label.clone(),
                        description: wf.draft.get_untracked().trim().to_string(),
                        list: list_args(&c),
                    }));
                }
            };
            let save_enter = save.clone();
            let save_btn = Action::label("save", "Save");
            let close_btn = Action::label("close", "Close");
            let close_x = {
                let (close, guard) = (close.clone(), guard.clone());
                move || {
                    let handled = guard.borrow().as_ref().map(|g| g()).unwrap_or(false);
                    if !handled {
                        close();
                    }
                }
            };
            let caret = c.ui.caret;
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(super::w::field_row(
                    &t,
                    "Description",
                    13,
                    super::w::caret_tracked(
                        mcx,
                        caret,
                        TextInput::new()
                            .value(wf.draft)
                            .placeholder("No description.")
                            .on_submit(move |_| save_enter())
                            .layout(LayoutStyle::default().w((inner_w - 14).max(20)).h(1))
                            .element(mcx, &t),
                    )
                    .autofocus()
                    .build(),
                ))
                .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    let t = abstracttui::app::current_theme().tokens;
                    match wf.msg.get() {
                        Some((text, tone)) if tone != MsgTone::Ok => {
                            sentence(&t, &text, inner_w, msg_ink(&t, tone))
                        }
                        _ => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                }))
                .child(super::w::fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![],
                    None,
                ))
                .child(super::w::form::button_row(vec![
                    button(mcx, &t, &save_btn, On::Raised, true, save),
                    button(mcx, &t, &close_btn, On::Raised, true, close_x),
                ]))
                .build()
        });
}

thread_local! {
    /// The open description form's "a save was sent" flag.
    static SAVE_SENT: std::cell::RefCell<Option<Signal<bool>>> = const { std::cell::RefCell::new(None) };
}

/// Import .flow: files on THIS machine (the web's file picker; several
/// paths separated by spaces or commas).
fn open_import(cx: Scope, ctx: &Ctx) {
    let c = ctx.clone();
    super::w::FormModal::new(IMPORT_LABEL)
        .lead("Install a .flow bundle: files on THIS machine are uploaded to the gateway; an existing version is never overwritten.")
        .size(84, 12)
        .open(ctx, cx, move |mcx, close, guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let path = mcx.signal(String::new());
            let esc_armed = mcx.signal(false);
            let form_error = mcx.signal(Option::<String>::None);
            super::install_dirty_guard(
                mcx,
                &guard,
                vec![(path, String::new())],
                esc_armed,
                form_error,
            );
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
                    if paths.is_empty() {
                        form_error.set(Some("Type the path of a .flow file first.".into()));
                        return;
                    }
                    c.send(Cmd::Workflows(WfCmd::Import {
                        paths,
                        list: list_args(&c),
                    }));
                    close();
                }
            };
            let submit_enter = submit.clone();
            let close_x = {
                let (close, guard) = (close.clone(), guard.clone());
                move || {
                    let handled = guard.borrow().as_ref().map(|g| g()).unwrap_or(false);
                    if !handled {
                        close();
                    }
                }
            };
            let caret = c.ui.caret;
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(super::w::field_row(
                    &t,
                    "Files",
                    7,
                    super::w::caret_tracked(
                        mcx,
                        caret,
                        TextInput::new()
                            .value(path)
                            .placeholder("~/Downloads/my-workflow.flow")
                            .on_submit(move |_: &str| submit_enter())
                            .layout(LayoutStyle::default().w((inner_w - 8).max(20)).h(1))
                            .element(mcx, &t),
                    )
                    .autofocus()
                    .build(),
                ))
                .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    let t = abstracttui::app::current_theme().tokens;
                    match form_error.get() {
                        Some(e) => sentence(&t, &e, inner_w, t.error),
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                }))
                .child(super::w::fill_line(LayoutStyle::line(1).shrink(0.0), vec![], None))
                .child(super::w::form::button_row(vec![
                    button(
                        mcx,
                        &t,
                        &Action::label("import", "Import").tooltip(IMPORT_TIP),
                        On::Raised,
                        true,
                        submit,
                    ),
                    button(mcx, &t, &Action::label("close", "Close"), On::Raised, true, close_x),
                ]))
                .build()
        });
}
