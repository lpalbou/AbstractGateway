//! Skills & MCP (WORK) — the web console's page of the same name
//! (`console_skills_mcp.py`), same data, same actions, same sentences.
//!
//! R15 (DESIGN-TUI.md §3.5): the web's two tabs are a `Segmented` [Skills]
//! [MCP servers] in the head (one Tab stop per segment). Each panel is the
//! web's toolbar (search field, Show archived toggle, Import .zip /
//! Import folder / Add server buttons), a `DataTable` whose Actions cell
//! holds the web's labelled buttons (View · Export · Archive/Unarchive;
//! Edit · Test · Archive/Unarchive), and — MCP — the "Enabled for agents"
//! toggle cell (turning it on asks the web's question, [Turn on] [Cancel]).
//! The skills shelf row (folder field saved on Enter + Refresh curated
//! shelf) sits under the skills table. The skill and server editors are
//! `FormModal`s with the web's footer buttons. One action list per table
//! (`skill_actions`, `mcp_actions`) is the single source for the buttons,
//! the hint bar and the tests.
//!
//! Every write goes through the web page's own routes (see
//! `api_skills.rs`); nothing here is a second data path.

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use abstracttui::widgets::{SubmitPolicy, TextArea, TextAreaState};

use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Cell, Col, ColW, DataTable, Ink, Row as WRow, Segmented, Toggle};
use super::Ctx;
use crate::store::skills::{
    filter_skills, McpForm, McpRow, SkillDetail, SkillRow, Tone as MsgTone, AGENTS_LABEL,
    MCP_EMPTY, MCP_EMPTY_ADMIN_TAIL, MCP_LOADING, MCP_PURPOSE, SKILLS_EMPTY, SKILLS_LOADING,
    SKILLS_NO_MATCH, SKILLS_PURPOSE, SUBTITLE, TITLE,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::skills::SkCmd;
use crate::worker::{Body, Cmd};

/// The verbs only an admin may use (the web hides them for others).
pub const ADMIN_KEYS: &[&str] = &["i", "a", "e", "t", "d", "space", "u"];

/// The web's words (console.py `#tab-skills`, console_skills_mcp.py).
pub const TABS: [&str; 2] = ["Skills", "MCP servers"];
pub const SEARCH_PLACEHOLDER: &str = "Search by name or description";
pub const IMPORT_ZIP: &str = "Import .zip";
pub const IMPORT_ZIP_TIP: &str = "Import a skill from a .zip of its folder";
pub const IMPORT_FOLDER: &str = "Import folder";
pub const IMPORT_FOLDER_TIP: &str = "Import a skill folder (it holds SKILL.md)";
pub const ADD_SERVER: &str = "Add server";
pub const REFRESH_SHELF: &str = "Refresh curated shelf";
pub const SHELF_LABEL: &str = "Shelf folder";

fn is_admin(ctx: &Ctx) -> bool {
    ctx.store.conn.with_untracked(ConnPhase::is_admin)
}

/// Read both lists (the web reloads both each time the page opens).
pub fn refresh(ctx: &Ctx) {
    let sk = ctx.store.skills;
    ctx.send(Cmd::Skills(SkCmd::LoadSkills {
        include_archived: sk.skills_archived.get_untracked(),
    }));
    ctx.send(Cmd::Skills(SkCmd::LoadMcp));
}

/// [`refresh`] for a harness that holds the store and the command
/// channel but no `Ctx` (the drive tests).
pub fn refresh_for_tests(store: &crate::store::Store, tx: &std::sync::mpsc::Sender<Cmd>) {
    let _ = tx.send(Cmd::Skills(SkCmd::LoadSkills {
        include_archived: store.skills.skills_archived.get_untracked(),
    }));
    let _ = tx.send(Cmd::Skills(SkCmd::LoadMcp));
}

/// The footer verbs (R15: only what applies — the selected row's
/// actions, then the panel keys).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let sk = ctx.store.skills;
    let mut out = vec![("↑↓", "rows"), ("Tab", "actions")];
    if sk.tab.get() == 0 {
        let _ = sk.skill_sel.get();
        out.push(("Enter", "View"));
        if let Some(r) = selected_skill(ctx) {
            for a in skill_actions(&r, admin) {
                if let Some(k) = a.key {
                    out.push((key_label(k), static_label(a.id)));
                }
            }
        }
        out.extend_from_slice(&[
            ("h", "Show archived"),
            ("i", IMPORT_ZIP),
            ("u", REFRESH_SHELF),
        ]);
    } else {
        let _ = sk.mcp_sel.get();
        out.push(("Enter", "Edit"));
        if admin {
            out.push(("space", AGENTS_LABEL));
        }
        if let Some(r) = selected_mcp(ctx) {
            for a in mcp_actions(&r, admin) {
                if let Some(k) = a.key {
                    out.push((key_label(k), static_label(a.id)));
                }
            }
        }
        out.extend_from_slice(&[("h", "Show archived"), ("a", ADD_SERVER)]);
    }
    out.push(("[ ]", "Skills ⇄ MCP servers"));
    out.push(("r", "refresh"));
    out
}

fn key_label(k: char) -> &'static str {
    match k {
        'v' => "v",
        'x' => "x",
        'd' => "d",
        'e' => "e",
        't' => "t",
        _ => "?",
    }
}

fn static_label(id: &str) -> &'static str {
    match id {
        "view" => "View",
        "export" => "Export",
        "archive" => "Archive",
        "unarchive" => "Unarchive",
        "edit" => "Edit",
        "test" => "Test",
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

fn selected_skill(ctx: &Ctx) -> Option<SkillRow> {
    let sk = ctx.store.skills;
    let q = sk.query.get_untracked();
    let i = sk.skill_sel.get_untracked();
    sk.skills.with_untracked(|d| {
        d.ready()
            .and_then(|d| filter_skills(&d.rows, &q).get(i).map(|r| (*r).clone()))
    })
}

fn visible_mcp(ctx: &Ctx) -> Vec<McpRow> {
    let sk = ctx.store.skills;
    let show = sk.mcp_archived.get_untracked();
    sk.mcp.with_untracked(|d| {
        d.ready()
            .map(|d| {
                d.rows
                    .iter()
                    .filter(|r| show || !r.archived)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    })
}

fn selected_mcp(ctx: &Ctx) -> Option<McpRow> {
    visible_mcp(ctx)
        .get(ctx.store.skills.mcp_sel.get_untracked())
        .cloned()
}

/// A skill row's actions, the web's labelled buttons in its order (the
/// web shows only what applies: Export for a live skill, Archive for an
/// imported one, Unarchive for an archived one — admins).
pub fn skill_actions(r: &SkillRow, admin: bool) -> Vec<Action> {
    let mut out = vec![Action::label("view", "View").key('v')];
    if !r.archived {
        out.push(Action::label("export", "Export").key('x'));
    }
    if admin && r.origin == "imported" && !r.archived {
        out.push(Action::label("archive", "Archive").key('d').danger());
    }
    if admin && r.archived {
        out.push(Action::label("unarchive", "Unarchive").key('d'));
    }
    out
}

/// An MCP server row's actions (admins): Edit · Test · Archive, or
/// Unarchive for an archived server.
pub fn mcp_actions(r: &McpRow, admin: bool) -> Vec<Action> {
    if !admin {
        return Vec::new();
    }
    if r.archived {
        return vec![Action::label("unarchive", "Unarchive").key('d')];
    }
    vec![
        Action::label("edit", "Edit").key('e'),
        Action::label("test", "Test").key('t'),
        Action::label("archive", "Archive").key('d').danger(),
    ]
}

/// The head buttons of each panel (admins).
pub fn panel_actions(tab: usize, admin: bool) -> Vec<Action> {
    if !admin {
        return Vec::new();
    }
    if tab == 0 {
        vec![
            Action::label("import_zip", IMPORT_ZIP)
                .key('i')
                .tooltip(IMPORT_ZIP_TIP),
            Action::label("import_folder", IMPORT_FOLDER).tooltip(IMPORT_FOLDER_TIP),
            Action::label("reseed", REFRESH_SHELF).key('u'),
        ]
    } else {
        vec![Action::label("add", ADD_SERVER).key('a')]
    }
}

/// The page.
pub fn screen(ctx: &Ctx, cx: Scope) -> View {
    let sk = ctx.store.skills;
    // First visit: read both lists (the web's openSkillsMcpPage).
    if sk
        .skills
        .with_untracked(|d| matches!(d, Loadable::NotAsked))
        && ctx.store.conn.with_untracked(ConnPhase::is_connected)
    {
        refresh(ctx);
    }
    install_effects(cx, ctx);
    let keys_ctx = ctx.clone();
    let body_ctx = ctx.clone();
    let head_ctx = ctx.clone();
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
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |hcx| {
                let t = use_theme(hcx).get().tokens;
                let w = (crate::ui::page_viewport(hcx).get().w - 2).max(20);
                let seg = Segmented::new(TABS, None)
                    .bind(head_ctx.store.skills.tab)
                    .tip(0, "Skills agents can load")
                    .tip(1, "The MCP tool servers this gateway knows");
                let wd = seg.width();
                super::workflows::page_head(&t, TITLE, SUBTITLE, w, vec![(seg.view(hcx, &t), wd)])
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).grow(1.0).min_h(3),
            move |gcx| {
                if sk.tab.get() == 0 {
                    skills_panel(gcx, cx, &body_ctx)
                } else {
                    mcp_panel(gcx, cx, &body_ctx)
                }
            },
        ))
        .build()
}

/// Success messages become toasts (R15 §2.6); refusals stay inline.
fn install_effects(cx: Scope, ctx: &Ctx) {
    let sk = ctx.store.skills;
    let (c1, c2) = (ctx.clone(), ctx.clone());
    cx.effect(move || {
        if let Some((text, MsgTone::Ok)) = sk.skills_msg.get() {
            super::w::toast(&c1, cx, text);
            sk.skills_msg.set(None);
        }
    });
    cx.effect(move || {
        if let Some((text, MsgTone::Ok)) = sk.mcp_msg.get() {
            super::w::toast(&c2, cx, text);
            sk.mcp_msg.set(None);
        }
    });
}

/// One page key. Returns true when handled. ←/→ never arrive here.
fn handle_key(cx: Scope, ctx: &Ctx, key: Key) -> bool {
    let sk = ctx.store.skills;
    let tab = sk.tab.get_untracked();
    match key {
        Key::Char('[') | Key::Char(']') => {
            sk.tab.set(if tab == 0 { 1 } else { 0 });
        }
        Key::Char('h') => toggle_archived(ctx, tab, None),
        Key::Char('v') if tab == 0 => skill_action(cx, ctx, "view"),
        Key::Char('x') if tab == 0 => skill_action(cx, ctx, "export"),
        Key::Char('u') if tab == 0 => panel_action(cx, ctx, "reseed"),
        Key::Char('i') if tab == 0 => panel_action(cx, ctx, "import_zip"),
        Key::Char('d') if tab == 0 => {
            let archived = selected_skill(ctx).map(|r| r.archived).unwrap_or(false);
            skill_action(cx, ctx, if archived { "unarchive" } else { "archive" })
        }
        Key::Char('a') if tab == 1 => panel_action(cx, ctx, "add"),
        Key::Char('e') if tab == 1 => mcp_action(cx, ctx, "edit"),
        Key::Char('t') if tab == 1 => mcp_action(cx, ctx, "test"),
        Key::Char('d') if tab == 1 => {
            let archived = selected_mcp(ctx).map(|r| r.archived).unwrap_or(false);
            mcp_action(cx, ctx, if archived { "unarchive" } else { "archive" })
        }
        Key::Char(' ') if tab == 1 => {
            if let Some(r) = selected_mcp(ctx) {
                toggle_agents(cx, ctx, &r);
            }
        }
        _ => return false,
    }
    true
}

fn toggle_archived(ctx: &Ctx, tab: usize, want: Option<bool>) {
    let sk = ctx.store.skills;
    if tab == 0 {
        let on = want.unwrap_or(!sk.skills_archived.get_untracked());
        sk.skills_archived.set(on);
        ctx.send(Cmd::Skills(SkCmd::LoadSkills {
            include_archived: on,
        }));
    } else {
        let on = want.unwrap_or(!sk.mcp_archived.get_untracked());
        sk.mcp_archived.set(on);
    }
}

/// A head button of a panel (or its key).
fn panel_action(cx: Scope, ctx: &Ctx, id: &str) {
    let what = match id {
        "import_zip" | "import_folder" => "importing a skill",
        "reseed" => "refreshing the curated skills shelf",
        _ => "adding an MCP server",
    };
    if !super::util::admin_gate(&ctx.store, what) {
        return;
    }
    match id {
        "import_zip" => open_import(cx, ctx, false),
        "import_folder" => open_import(cx, ctx, true),
        "reseed" => {
            ctx.store
                .skills
                .shelf_msg
                .set(Some(("Working...".into(), MsgTone::Plain)));
            ctx.send(Cmd::Operator(crate::worker::operator::OpCmd::ReseedSkills));
        }
        _ => open_mcp_form(cx, ctx, None),
    }
}

/// A skill row action (click or key) on the selected row.
fn skill_action(cx: Scope, ctx: &Ctx, id: &str) {
    let sk = ctx.store.skills;
    let Some(r) = selected_skill(ctx) else {
        ctx.store
            .notice
            .set(Some("no skill selected — choose a row first".into()));
        return;
    };
    let admin = is_admin(ctx);
    if !skill_actions(&r, admin).iter().any(|a| a.id == id) {
        // What the web would not offer: say why.
        let why = match id {
            "export" => format!("{} is archived — unarchive it to export it", r.name),
            "archive" if !admin => {
                return {
                    super::util::admin_gate(&ctx.store, "archiving");
                }
            }
            "archive" => format!(
                "{}: Curated skills are read-only; duplicate to edit.",
                r.name
            ),
            "unarchive" if !admin => {
                return {
                    super::util::admin_gate(&ctx.store, "archiving");
                }
            }
            _ => format!("{} has no {id} action", r.name),
        };
        sk.skills_msg.set(Some((why, MsgTone::Error)));
        return;
    }
    let include_archived = sk.skills_archived.get_untracked();
    match id {
        "view" => open_skill(cx, ctx, r.name),
        "export" => ctx.send(Cmd::Skills(SkCmd::ExportSkill {
            name: r.name,
            dir: super::sandbox::artifact_dir(),
        })),
        // The web archives a skill at once (it comes back with Show
        // archived + Unarchive): no confirmation, as on the web.
        "archive" | "unarchive" => ctx.send(Cmd::Skills(SkCmd::SetSkillArchived {
            name: r.name,
            archive: id == "archive",
            include_archived,
            reopen: false,
        })),
        _ => {}
    }
}

/// An MCP row action (click or key) on the selected row.
fn mcp_action(cx: Scope, ctx: &Ctx, id: &str) {
    if !super::util::admin_gate(&ctx.store, "managing MCP servers") {
        return;
    }
    let Some(r) = selected_mcp(ctx) else {
        ctx.store
            .notice
            .set(Some("no server selected — choose a row first".into()));
        return;
    };
    if !mcp_actions(&r, true).iter().any(|a| a.id == id) {
        ctx.store.skills.mcp_msg.set(Some((
            format!("{}: Archived: unarchive it first.", r.name),
            MsgTone::Error,
        )));
        return;
    }
    match id {
        "edit" => open_mcp_form(cx, ctx, Some(r)),
        "test" => {
            ctx.store.skills.mcp_msg.set(Some((
                format!("Testing {} (up to 10 seconds)...", r.name),
                MsgTone::Plain,
            )));
            ctx.send(Cmd::Skills(SkCmd::TestMcp { name: r.name }))
        }
        "archive" | "unarchive" => ctx.send(Cmd::Skills(SkCmd::SetMcpArchived {
            name: r.name,
            archive: id == "archive",
        })),
        _ => {}
    }
}

/// The "Enabled for agents" switch. Off applies at once; on asks the
/// web's question first ([Turn on] [Cancel]); a blocked switch says why.
fn toggle_agents(cx: Scope, ctx: &Ctx, r: &McpRow) {
    if !super::util::admin_gate(&ctx.store, "Enabled for agents") {
        return;
    }
    let sk = ctx.store.skills;
    if let Some(why) = r.agents_block_reason() {
        sk.mcp_msg
            .set(Some((format!("{}: {why}", r.name), MsgTone::Error)));
        return;
    }
    let name = r.name.clone();
    if r.enabled_for_agents {
        ctx.send(Cmd::Skills(SkCmd::SetMcpAgents {
            name,
            enabled: false,
        }));
        return;
    }
    let c = ctx.clone();
    super::w::Confirm::plain(r.agents_confirm_sentence(), "Turn on", "Cancel").open(
        cx,
        ctx.ui,
        move || {
            c.send(Cmd::Skills(SkCmd::SetMcpAgents {
                name,
                enabled: true,
            }));
        },
    );
}

fn message_line(t: &TokenSet, msg: Option<(String, MsgTone)>, width: i32) -> View {
    match msg {
        Some((text, tone)) => sentence(t, &text, width, msg_ink(t, tone)),
        None => Element::new().style(LayoutStyle::default().h(0)).build(),
    }
}

/// A row of panel buttons (+ the leading controls).
/// The panel's toolbar: the leading controls (`lead_w` cells), then the
/// panel's buttons on the right — on a row of their own when both do not
/// fit the page width.
#[allow(clippy::too_many_arguments)]
fn button_bar(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    tab: usize,
    lead: Vec<View>,
    lead_w: i32,
    width: i32,
) -> View {
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let acts: Vec<Action> = panel_actions(tab, admin)
        .into_iter()
        .filter(|a| a.id != "reseed") // it sits on the shelf row
        .collect();
    let buttons_w: i32 = acts.iter().map(|a| a.width() + 2).sum();
    let mut lead_row = Element::new().style(LayoutStyle::row().gap(2).h(1).shrink(0.0));
    for v in lead {
        lead_row = lead_row.child(v);
    }
    let mut btn_row = Element::new().style(LayoutStyle::row().gap(2).h(1).shrink(0.0));
    btn_row = btn_row.child(
        Element::new()
            .style(LayoutStyle::default().grow(1.0))
            .build(),
    );
    for a in acts {
        let c = ctx.clone();
        let id = a.id;
        btn_row = btn_row.child(button(cx, t, &a, On::Page, true, move || {
            panel_action(pcx, &c, id)
        }));
    }
    if lead_w + buttons_w + 2 <= width {
        Element::new()
            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
            .child(lead_row.build())
            .child(
                Element::new()
                    .style(LayoutStyle::row().grow(1.0).h(1))
                    .child(btn_row.build())
                    .build(),
            )
            .build()
    } else {
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .child(lead_row.build())
            .child(btn_row.build())
            .build()
    }
}

fn skills_panel(cx: Scope, pcx: Scope, ctx: &Ctx) -> View {
    let t = use_theme(cx).get().tokens;
    let sk = ctx.store.skills;
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let vp = crate::ui::page_viewport(cx).get();
    let width = (vp.w - 2).max(20);
    let archived_on = sk.skills_archived.get_untracked();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(sentence(&t, SKILLS_PURPOSE, width, t.text_muted));
    // The toolbar: search (filters as you type), Show archived, imports.
    let search = super::w::caret_tracked(
        cx,
        ctx.ui.caret,
        TextInput::new()
            .value(sk.query)
            .placeholder(SEARCH_PLACEHOLDER)
            .layout(LayoutStyle::default().w(32).h(1).shrink(0.0))
            .element(cx, &t),
    );
    let search = super::util::esc_releases_focus(search, ctx.store.notice).build();
    let c = ctx.clone();
    let show = Toggle::new(archived_on)
        .label("Show archived")
        .tip("Show archived  (h)")
        .on_change(move |v| toggle_archived(&c, 0, Some(v)))
        .view(cx, &t);
    col = col.child(button_bar(
        cx,
        pcx,
        ctx,
        &t,
        0,
        vec![search, show],
        32 + 2 + 16,
        width,
    ));
    col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
        let t = abstracttui::app::current_theme().tokens;
        message_line(&t, sk.skills_msg.get(), width)
    }));
    let c = ctx.clone();
    // The table takes its rows' height (the shelf row follows it).
    col = col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |gcx| {
            let t = use_theme(gcx).get().tokens;
            skills_table(gcx, pcx, &c, &t, width, vp.h, admin)
        },
    ));
    if admin {
        col = col.child(shelf_row(cx, pcx, ctx, width));
    }
    col.build()
}

fn skills_table(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    width: i32,
    page_h: i32,
    admin: bool,
) -> View {
    let sk = ctx.store.skills;
    let query = sk.query.get();
    let anchor = |v: View| -> View {
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .focusable()
            .autofocus()
            .child(v)
            .build()
    };
    let d = match sk.skills.get() {
        Loadable::NotAsked | Loadable::Loading => {
            return anchor(sentence(t, SKILLS_LOADING, width, t.text_muted));
        }
        Loadable::Failed(e) => {
            return anchor(sentence(
                t,
                &format!(
                    "Could not list the skills: {}",
                    crate::worker::skills::refusal_text(&e)
                ),
                width,
                t.error,
            ));
        }
        Loadable::Ready(d) => d,
    };
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    if !d.warnings.is_empty() {
        col = col.child(sentence(t, &d.warnings.join(" "), width, t.warn));
    }
    let rows = filter_skills(&d.rows, &query);
    let empty = if query.trim().is_empty() {
        SKILLS_EMPTY
    } else {
        SKILLS_NO_MATCH
    };
    let narrow = width < 100;
    let cols = if narrow {
        vec![
            Col::new("Name", ColW::Flex { weight: 1, min: 12 }),
            Col::new("Trust", ColW::Fit { min: 5, max: 12 }),
            Col::new("Actions", ColW::Fit { min: 6, max: 30 }),
        ]
    } else {
        vec![
            Col::new("Name", ColW::Fit { min: 8, max: 24 }),
            Col::new("What it does", ColW::Flex { weight: 1, min: 16 }),
            Col::new("Version", ColW::Fit { min: 7, max: 12 }),
            Col::new("Trust", ColW::Fit { min: 5, max: 12 }),
            Col::new("Source", ColW::Fit { min: 6, max: 20 }),
            Col::new("Actions", ColW::Fit { min: 6, max: 30 }),
        ]
    };
    let table_rows: Vec<WRow> = rows
        .iter()
        .map(|r| {
            let trust = Cell::Badge {
                label: r.trust_text().to_string(),
                ink: trust_ink(t, r),
                action: None,
                tip: (!r.reasons.is_empty()).then(|| r.reasons.join(" ")),
            };
            let (cells, note) = if narrow {
                (
                    vec![
                        Cell::Lines(vec![
                            vec![Ink::new(r.name.clone(), t.text).bold()],
                            vec![Ink::new(
                                format!("{} · {}", r.version_text(), r.source_text()),
                                t.text_muted,
                            )],
                        ]),
                        trust,
                        Cell::Actions(skill_actions(r, admin)),
                    ],
                    Some((r.description.clone(), t.text_muted)),
                )
            } else {
                (
                    vec![
                        Cell::Lines(vec![vec![Ink::new(r.name.clone(), t.text).bold()]]),
                        Cell::text(r.description.clone(), t.text),
                        Cell::text(r.version_text(), t.text),
                        trust,
                        Cell::text(r.source_text(), t.text_muted),
                        Cell::Actions(skill_actions(r, admin)),
                    ],
                    None,
                )
            };
            WRow::new(r.name.clone(), cells).dim(r.archived).note(note)
        })
        .collect();
    let keys: Vec<String> = rows.iter().map(|r| r.name.clone()).collect();
    let sel = keyed_selection(cx, sk.skill_sel, keys.clone());
    let (ca, ce) = (ctx.clone(), ctx.clone());
    let (ka, ke) = (keys.clone(), keys);
    let max_rows = (page_h - 14).max(4);
    col.child(
        DataTable::new(cols, table_rows, sel)
            .width(width)
            .max_rows(max_rows)
            .empty(empty)
            .autofocus()
            .on_action(move |key, id| {
                select_index(ca.store.skills.skill_sel, &ka, key);
                skill_action(pcx, &ca, id);
            })
            .on_activate(move |key| {
                select_index(ce.store.skills.skill_sel, &ke, key);
                skill_action(pcx, &ce, "view");
            })
            .view(cx, t),
    )
    .build()
}

/// The trust chip's ink (the web's SKILL_TRUST_TONE).
fn trust_ink(t: &TokenSet, r: &SkillRow) -> Rgba {
    match r.trust_text() {
        "First party" | "Audited" => t.ok,
        "Adopted" | "Community" => t.info,
        "Blocked" => t.error,
        "Unverified" => t.warn,
        _ => t.text_muted,
    }
}

/// A keyed selection for a table over `keys`, synced two ways with the
/// store's legacy index `idx`.
fn keyed_selection(cx: Scope, idx: Signal<usize>, keys: Vec<String>) -> Signal<Option<String>> {
    let i = idx.get_untracked().min(keys.len().saturating_sub(1));
    let sel = cx.signal(keys.get(i).cloned());
    cx.effect(move || {
        if let Some(k) = sel.get() {
            if let Some(p) = keys.iter().position(|x| *x == k) {
                if idx.get_untracked() != p {
                    idx.set(p);
                }
            }
        }
    });
    sel
}

fn select_index(idx: Signal<usize>, keys: &[String], key: &str) {
    if let Some(p) = keys.iter().position(|x| x == key) {
        if idx.get_untracked() != p {
            idx.set(p);
        }
    }
}

// ------------------------------------------------------------ shelf row

/// The shelf's source in the web's words (`skillsShelfSourcePill`); None
/// for the gateway's own copy (the web shows no pill then).
pub fn shelf_source_words(source: &str) -> Option<&'static str> {
    match source {
        "seeded" => None,
        "stored" => Some("Saved setting"),
        "env" => Some("Environment (legacy)"),
        "checkout" => Some("Framework checkout"),
        _ => Some("None"),
    }
}

/// The helper under the shelf field (the web's field title).
pub fn shelf_help(sh: &crate::store::SkillsShelf) -> String {
    let v = if sh.bundled_version.is_empty() {
        String::new()
    } else {
        format!(" (version {})", sh.bundled_version)
    };
    format!("Empty: the gateway's own copy of the curated shelf{v}, refreshed at each start.")
}

/// The R8.1 shelf row under the skills list: the folder field (saved on
/// Enter, "Saved" under it), its source, "Refresh curated shelf", the
/// helper and the outcome. Data = `GET /admin/runtime-config`
/// `skills.shelf`; writes = POST /admin/runtime-config `{"skills.shelf":
/// …}` and POST /admin/skills/reseed (the web's own routes).
fn shelf_row(cx: Scope, pcx: Scope, ctx: &Ctx, width: i32) -> View {
    let store = ctx.store;
    let sk = store.skills;
    // Read the setting once an admin is here (the web reads it with the tab).
    {
        let c = ctx.clone();
        cx.effect(move || {
            if c.store.conn.with(ConnPhase::is_admin)
                && c.store
                    .runtime_config
                    .with_untracked(|r| matches!(r, Loadable::NotAsked))
            {
                c.store.runtime_config.set(Loadable::Loading);
                c.send(Cmd::LoadRuntimeConfig);
            }
        });
    }
    // The field shows the SAVED value only (a default written back would
    // silently become a saved setting).
    {
        let c = ctx.clone();
        cx.effect(move || {
            if let Loadable::Ready(d) = c.store.runtime_config.get() {
                if let Some(sh) = d.skills_shelf {
                    let v = if sh.source == "stored" {
                        sh.value.clone()
                    } else {
                        String::new()
                    };
                    if sk.shelf_draft.get_untracked() != v
                        && sk.shelf_form.get_untracked().is_none()
                    {
                        sk.shelf_draft.set(v);
                    }
                }
            }
        });
    }
    // The folder save's outcome, in place.
    {
        let ui = ctx.ui;
        cx.effect(move || {
            if let Some((fid, out)) = ui.write_done.get() {
                if sk.shelf_form.get_untracked() == Some(fid) {
                    ui.write_done.set(None);
                    sk.shelf_form.set(None);
                    match out {
                        Ok(_) => sk.shelf_msg.set(Some(("Saved".into(), MsgTone::Ok))),
                        Err(e) => sk
                            .shelf_msg
                            .set(Some((format!("Not saved: {e}"), MsgTone::Error))),
                    }
                }
            }
        });
    }
    let c = ctx.clone();
    dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |gcx| {
        let t = use_theme(gcx).get().tokens;
        let cfg = c.store.runtime_config.get();
        let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
        let shelf = match &cfg {
            Loadable::Ready(d) => match &d.skills_shelf {
                Some(sh) => sh.clone(),
                None => {
                    return sentence(
                        &t,
                        "This gateway did not report its skills shelf.",
                        width,
                        t.error,
                    )
                }
            },
            Loadable::Failed(e) => {
                return sentence(
                    &t,
                    &format!("Could not read the skills shelf setting. {e}"),
                    width,
                    t.error,
                )
            }
            _ => {
                return sentence(
                    &t,
                    "Reading the skills shelf setting...",
                    width,
                    t.text_muted,
                )
            }
        };
        let c_save = c.clone();
        let sh = shelf.clone();
        let save = move |typed: &str| {
            let body = super::runtimes::skills_shelf_body(&sh, typed);
            if body.as_object().is_none_or(|m| m.is_empty()) {
                return;
            }
            let fid = crate::worker::next_form_id();
            sk.shelf_form.set(Some(fid));
            sk.shelf_msg.set(Some(("Saving...".into(), MsgTone::Plain)));
            c_save.send(Cmd::SaveRuntimeConfig {
                body: body.into(),
                form_id: Some(fid),
            });
        };
        let field_w = (width - 14 - 26).clamp(16, 52);
        let input = super::w::caret_tracked(
            gcx,
            c.ui.caret,
            TextInput::new()
                .value(sk.shelf_draft)
                .placeholder(shelf.default_path.clone())
                .on_submit(move |s: &str| save(s))
                .layout(LayoutStyle::default().w(field_w).h(1).shrink(0.0))
                .element(gcx, &t),
        );
        // Esc keeps the saved folder (the typed text goes) and hands the
        // keyboard back to the page.
        let saved = if shelf.source == "stored" {
            shelf.value.clone()
        } else {
            String::new()
        };
        let notice = c.store.notice;
        let input = input.shortcut(KeyChord::plain(Key::Escape), move |ecx| {
            sk.shelf_draft.set(saved.clone());
            if let Some(root) = ecx.current() {
                ecx.request_focus(root);
                notice.set(Some(super::util::FOCUS_RELEASED.into()));
            }
        });
        let input = super::w::tip::with_tip(gcx, input, shelf_help(&shelf)).build();
        let mut row = Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(super::w::fill_line(
                LayoutStyle::default().w(13).h(1).shrink(0.0),
                vec![Ink::new(SHELF_LABEL, t.text)],
                None,
            ))
            .child(input);
        if let Some(w) = shelf_source_words(&shelf.source) {
            row = row.child(super::w::fill_line(
                LayoutStyle::default()
                    .w(abstracttui::text::width(w))
                    .h(1)
                    .shrink(0.0),
                vec![Ink::new(w, t.info)],
                None,
            ));
        }
        let reseed = panel_actions(0, true)
            .into_iter()
            .find(|a| a.id == "reseed")
            .expect("reseed action");
        let c_r = c.clone();
        row = row.child(button(gcx, &t, &reseed, On::Page, true, move || {
            panel_action(pcx, &c_r, "reseed")
        }));
        col = col.child(row.build());
        col = col.child(sentence(&t, &shelf_help(&shelf), width, t.text_faint));
        if !shelf.available {
            col = col.child(sentence(
                &t,
                &format!("Not available: {}", shelf.reason),
                width,
                t.warn,
            ));
        }
        for w in &shelf.warnings {
            col = col.child(sentence(&t, w, width, t.warn));
        }
        col = col.child(dyn_view(
            LayoutStyle::column().gap(0).shrink(0.0),
            move || {
                let t = abstracttui::app::current_theme().tokens;
                match sk.shelf_msg.get() {
                    Some((text, tone)) => sentence(&t, &text, width, msg_ink(&t, tone)),
                    None => Element::new().style(LayoutStyle::default().h(0)).build(),
                }
            },
        ));
        col.build()
    })
}

// ------------------------------------------------------------- MCP panel

fn mcp_panel(cx: Scope, pcx: Scope, ctx: &Ctx) -> View {
    let t = use_theme(cx).get().tokens;
    let sk = ctx.store.skills;
    let vp = crate::ui::page_viewport(cx).get();
    let width = (vp.w - 2).max(20);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
        let t = abstracttui::app::current_theme().tokens;
        match sk.mcp.get() {
            Loadable::Ready(d) if !d.agents_note.is_empty() => {
                sentence(&t, &d.agents_note, width, t.warn)
            }
            _ => Element::new().style(LayoutStyle::default().h(0)).build(),
        }
    }));
    col = col.child(sentence(&t, MCP_PURPOSE, width, t.text_muted));
    let c = ctx.clone();
    let show = Toggle::new(sk.mcp_archived.get_untracked())
        .label("Show archived")
        .tip("Show archived  (h)")
        .on_change(move |v| toggle_archived(&c, 1, Some(v)))
        .view(cx, &t);
    col = col.child(button_bar(cx, pcx, ctx, &t, 1, vec![show], 16, width));
    col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
        let t = abstracttui::app::current_theme().tokens;
        message_line(&t, sk.mcp_msg.get(), width)
    }));
    let c = ctx.clone();
    col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).grow(1.0),
        move |gcx| {
            let t = use_theme(gcx).get().tokens;
            mcp_table(gcx, pcx, &c, &t, width, vp.h)
        },
    ))
    .build()
}

fn mcp_table(cx: Scope, pcx: Scope, ctx: &Ctx, t: &TokenSet, width: i32, page_h: i32) -> View {
    let sk = ctx.store.skills;
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let show = sk.mcp_archived.get();
    let anchor = |v: View| -> View {
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .focusable()
            .autofocus()
            .child(v)
            .build()
    };
    let d = match sk.mcp.get() {
        Loadable::NotAsked | Loadable::Loading => {
            return anchor(sentence(t, MCP_LOADING, width, t.text_muted));
        }
        Loadable::Failed(e) => {
            return anchor(sentence(
                t,
                &format!(
                    "Could not list the MCP servers: {}",
                    crate::worker::skills::refusal_text(&e)
                ),
                width,
                t.error,
            ));
        }
        Loadable::Ready(d) => d,
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let rows: Vec<&McpRow> = d.rows.iter().filter(|r| show || !r.archived).collect();
    let narrow = width < 100;
    let mut cols = vec![Col::new("Name", ColW::Fit { min: 8, max: 24 })];
    if !narrow {
        cols.push(Col::new("Transport", ColW::Flex { weight: 1, min: 14 }));
    }
    cols.push(Col::new("Status", ColW::Flex { weight: 1, min: 14 }));
    if admin {
        // The web form's own short label where the full one does not fit.
        cols.push(Col::new(
            if narrow { "Agents" } else { AGENTS_LABEL },
            ColW::Fit { min: 6, max: 18 },
        ));
    }
    cols.push(Col::new("Tools", ColW::Fit { min: 5, max: 9 }));
    if admin {
        cols.push(Col::new("Actions", ColW::Fit { min: 6, max: 26 }));
    }
    let table_rows: Vec<WRow> = rows
        .iter()
        .map(|r| {
            let mut name = vec![vec![Ink::new(r.name.clone(), t.text).bold()]];
            if r.archived {
                name.push(vec![Ink::new("Archived", t.text_muted)]);
            }
            if !r.description.is_empty() {
                name.push(vec![Ink::new(r.description.clone(), t.text_muted)]);
            }
            let transport = format!("{}: {}", r.transport_label(), r.target());
            let failed = r.last_test.as_ref().is_some_and(|x| !x.ok);
            let mut cells = vec![Cell::Lines(name)];
            if !narrow {
                cells.push(Cell::text(transport.clone(), t.text));
            }
            cells.push(Cell::text(
                r.status_text(now),
                if failed { t.error } else { t.text },
            ));
            if admin {
                cells.push(Cell::Toggle {
                    id: "agents",
                    on: r.enabled_for_agents,
                    refused: r.agents_block_reason().map(str::to_string),
                    tip: Some(format!(
                        "{AGENTS_LABEL}: {}  (space)",
                        r.agents_block_reason()
                            .map(str::to_string)
                            .unwrap_or_else(|| r.agents_status.clone())
                    )),
                });
            }
            cells.push(Cell::text(r.tools_text(), t.text));
            if admin {
                cells.push(Cell::Actions(mcp_actions(r, admin)));
            }
            let mut note: Vec<String> = Vec::new();
            if narrow {
                note.push(transport);
            }
            if let Some(tst) = r.last_test.as_ref().filter(|t| t.ok) {
                let names: Vec<String> = tst.tools.iter().map(|(n, _)| n.clone()).collect();
                if !names.is_empty() {
                    note.push(names.join(", "));
                }
            }
            let note = (!note.is_empty()).then(|| (note.join(" · "), t.text_muted));
            WRow::new(r.name.clone(), cells).dim(r.archived).note(note)
        })
        .collect();
    let empty = if admin {
        format!("{MCP_EMPTY}{MCP_EMPTY_ADMIN_TAIL}")
    } else {
        MCP_EMPTY.to_string()
    };
    let keys: Vec<String> = rows.iter().map(|r| r.name.clone()).collect();
    let sel = keyed_selection(cx, sk.mcp_sel, keys.clone());
    let (ca, ct, ce, cs) = (ctx.clone(), ctx.clone(), ctx.clone(), ctx.clone());
    let (ka, kt, ke, ks) = (keys.clone(), keys.clone(), keys.clone(), keys);
    DataTable::new(cols, table_rows, sel)
        .width(width)
        .max_rows((page_h - 10).max(4))
        .empty(empty)
        .autofocus()
        .on_action(move |key, id| {
            select_index(ca.store.skills.mcp_sel, &ka, key);
            mcp_action(pcx, &ca, id);
        })
        .on_toggle(move |key, _id, _want| {
            select_index(ct.store.skills.mcp_sel, &kt, key);
            if let Some(r) = selected_mcp(&ct) {
                toggle_agents(pcx, &ct, &r);
            }
        })
        .on_activate(move |key| {
            select_index(ce.store.skills.mcp_sel, &ke, key);
            mcp_action(pcx, &ce, "edit");
        })
        .on_space(move |key| {
            select_index(cs.store.skills.mcp_sel, &ks, key);
            if let Some(r) = selected_mcp(&cs) {
                toggle_agents(pcx, &cs, &r);
            }
        })
        .view(cx, t)
}

// ---------------------------------------------------------------- forms

/// A modal's closer that asks the form's guard first (Close button).
fn guarded_close(close: super::CloserFn, guard: super::GuardSlot) -> impl FnMut() + 'static {
    move || {
        let handled = guard.borrow().as_ref().map(|g| g()).unwrap_or(false);
        if !handled {
            close();
        }
    }
}

/// Import .zip / Import folder: a path on THIS machine (the web's file
/// pickers); its files are uploaded.
fn open_import(cx: Scope, ctx: &Ctx, folder: bool) {
    let c = ctx.clone();
    let (title, lead, placeholder) = if folder {
        (IMPORT_FOLDER, IMPORT_FOLDER_TIP, "~/Downloads/field-notes/")
    } else {
        (IMPORT_ZIP, IMPORT_ZIP_TIP, "~/Downloads/field-notes.zip")
    };
    super::w::FormModal::new(title)
        .lead(format!("{lead} — on THIS machine; its files are uploaded."))
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
                    let p = path.get_untracked();
                    if p.trim().is_empty() {
                        form_error.set(Some("Type the path first.".into()));
                        return;
                    }
                    c.send(Cmd::Skills(SkCmd::ImportSkill {
                        path: p,
                        include_archived: c.store.skills.skills_archived.get_untracked(),
                    }));
                    close();
                }
            };
            let submit_enter = submit.clone();
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(super::w::field_row(
                    &t,
                    if folder { "Folder" } else { "File" },
                    8,
                    super::w::caret_tracked(
                        mcx,
                        c.ui.caret,
                        TextInput::new()
                            .value(path)
                            .placeholder(placeholder)
                            .on_submit(move |_: &str| submit_enter())
                            .layout(LayoutStyle::default().w((inner_w - 9).max(20)).h(1))
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
                .child(super::w::fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![],
                    None,
                ))
                .child(super::w::form::button_row(vec![
                    button(
                        mcx,
                        &t,
                        &Action::label("import", "Import"),
                        On::Raised,
                        true,
                        submit,
                    ),
                    button(
                        mcx,
                        &t,
                        &Action::label("close", "Close"),
                        On::Raised,
                        true,
                        guarded_close(close, guard),
                    ),
                ]))
                .build()
        });
}

/// View: the skill editor (the web's "Skill — <name>" modal): Save /
/// Duplicate to edit / Unarchive as the web, then Close.
pub fn open_skill(cx: Scope, ctx: &Ctx, name: String) {
    let sk = ctx.store.skills;
    sk.detail.set(Loadable::Loading);
    sk.detail_msg.set(None);
    ctx.send(Cmd::Skills(SkCmd::OpenSkill { name: name.clone() }));
    let c = ctx.clone();
    super::w::FormModal::new(format!("Skill — {name}"))
        .size(96, 34)
        .open(ctx, cx, move |_mcx, close, guard, inner_w| {
            dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |gcx| {
                let t = use_theme(gcx).get().tokens;
                match sk.detail.get() {
                    Loadable::NotAsked | Loadable::Loading => {
                        sentence(&t, &format!("Reading {name}..."), inner_w, t.text_muted)
                    }
                    Loadable::Failed(e) => sentence(
                        &t,
                        &format!(
                            "Could not open {name}: {}",
                            crate::worker::skills::refusal_text(&e)
                        ),
                        inner_w,
                        t.error,
                    ),
                    Loadable::Ready(d) => {
                        skill_body(gcx, &c, d, inner_w, close.clone(), guard.clone())
                    }
                }
            })
        });
}

/// The skill editor's footer button (the web's renderSkillModalFooter).
pub fn skill_footer_action(d: &SkillDetail, admin: bool) -> Option<Action> {
    if !admin {
        return None;
    }
    if d.editable {
        Some(Action::label("save", "Save"))
    } else if d.archived {
        Some(Action::label("unarchive", "Unarchive"))
    } else {
        Some(Action::label("duplicate", "Duplicate to edit"))
    }
}

fn skill_body(
    cx: Scope,
    ctx: &Ctx,
    d: SkillDetail,
    width: i32,
    close: super::CloserFn,
    guard: super::GuardSlot,
) -> View {
    let t = use_theme(cx).get().tokens;
    let admin = is_admin(ctx);
    let sk = ctx.store.skills;
    let can_edit = d.editable && admin;
    let version = cx.signal(d.version.clone());
    let desc = cx.signal(d.description.clone());
    let license = cx.signal(d.license.clone());
    let md_state = TextAreaState::new(cx);
    md_state.set_text(d.skill_md.clone());
    let md = cx.signal(d.skill_md.clone());
    // An unsaved edit is never dropped silently (R15 F2): the guard asks.
    if can_edit {
        let (ov, od, ol, omd) = (
            d.version.clone(),
            d.description.clone(),
            d.license.clone(),
            d.skill_md.clone(),
        );
        let dirty = move || {
            version.get_untracked() != ov
                || desc.get_untracked() != od
                || license.get_untracked() != ol
                || md.get_untracked() != omd
        };
        let ui = ctx.ui;
        let close_g = close.clone();
        *guard.borrow_mut() = Some(Box::new(move || {
            if !dirty() {
                return false;
            }
            let close = close_g.clone();
            super::w::Confirm::danger(super::DISCARD_QUESTION, "Discard", "Keep editing").open(
                cx,
                ui,
                move || close(),
            );
            true
        }));
    }
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    if let Some(lead) = d.lead(admin) {
        col = col.child(sentence(&t, &lead, width, t.warn));
    }
    let field_w = (width - 16).clamp(20, 80);
    let caret = ctx.ui.caret;
    let text_field = |label: &str, sig: Signal<String>| -> View {
        let control = if can_edit {
            super::w::caret_tracked(
                cx,
                caret,
                TextInput::new()
                    .value(sig)
                    .layout(LayoutStyle::default().w(field_w).h(1))
                    .element(cx, &t),
            )
            .build()
        } else {
            sentence(&t, &sig.get_untracked(), field_w, t.text)
        };
        super::w::field_row(&t, label, 15, control)
    };
    let help = |text: &str| -> View {
        Element::new()
            .style(LayoutStyle::row().shrink(0.0))
            .child(
                Element::new()
                    .style(LayoutStyle::default().w(15).h(1).shrink(0.0))
                    .build(),
            )
            .child(sentence(&t, text, (width - 15).max(10), t.text_faint))
            .build()
    };
    col = col.child(super::w::field_row(
        &t,
        "Name",
        15,
        super::w::fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(d.name.clone(), t.text).bold()],
            None,
        ),
    ));
    col = col.child(help(
        "How agents refer to this skill; it never changes (duplicate to rename).",
    ));
    col = col.child(text_field("Version", version));
    col = col.child(text_field("What it does", desc));
    col = col.child(help(
        "One sentence agents read to decide when to load this skill.",
    ));
    col = col.child(text_field("License", license));
    col = col.child(super::w::field_row(
        &t,
        "Source",
        15,
        sentence(&t, d.source_text(), field_w, t.text),
    ));
    col = col.child(super::w::section(&t, "SKILL.md"));
    if can_edit {
        col = col.child(
            TextArea::new()
                .state(&md_state)
                .on_change(move |s: &str| {
                    if md.with_untracked(|cur| cur != s) {
                        md.set(s.to_string());
                    }
                })
                .submit_policy(SubmitPolicy::EnterInserts)
                .rows(4, 10)
                .layout(LayoutStyle::default().h(10).shrink(0.0))
                .element(cx, &t)
                .build(),
        );
    } else {
        let mut md_col = Element::new().style(LayoutStyle::column().gap(0));
        for l in d.skill_md.lines().take(12) {
            md_col = md_col.child(sentence(&t, l, width.max(10), t.text));
        }
        let n = d.skill_md.lines().count();
        if n > 12 {
            md_col = md_col.child(sentence(
                &t,
                &format!("… {} more lines (Export saves the whole skill)", n - 12),
                width,
                t.text_faint,
            ));
        }
        col = col.child(md_col.build());
    }
    col = col.child(sentence(
        &t,
        "What agents read when they load the skill; the fields above are written into its frontmatter on Save.",
        width,
        t.text_faint,
    ));
    let files: Vec<String> = d.files.iter().map(|(p, s)| format!("{p} {s} B")).collect();
    col = col.child(sentence(
        &t,
        &format!("Files: {}", files.join(" · ")),
        width,
        t.text_muted,
    ));
    if let Some(p) = &d.problem {
        col = col.child(sentence(&t, p, width, t.warn));
    }
    col = col.child(dyn_view(
        LayoutStyle::column().gap(0).shrink(0.0),
        move || {
            let t = abstracttui::app::current_theme().tokens;
            match sk.detail_msg.get() {
                Some((m, tone)) => sentence(&t, &m, width, msg_ink(&t, tone)),
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        },
    ));
    let mut buttons: Vec<View> = Vec::new();
    if let Some(a) = skill_footer_action(&d, admin) {
        let name = d.name.clone();
        let c = ctx.clone();
        let (orig_v, orig_d, orig_l, orig_md) = (
            d.version.clone(),
            d.description.clone(),
            d.license.clone(),
            d.skill_md.clone(),
        );
        let id = a.id;
        buttons.push(button(cx, &t, &a, On::Raised, true, move || {
            let include_archived = c.store.skills.skills_archived.get_untracked();
            match id {
                "save" => {
                    let mut body = serde_json::Map::new();
                    if md.get_untracked() != orig_md {
                        body.insert("skill_md".into(), md.get_untracked().into());
                    }
                    if desc.get_untracked() != orig_d {
                        body.insert("description".into(), desc.get_untracked().into());
                    }
                    if version.get_untracked() != orig_v {
                        body.insert("version".into(), version.get_untracked().into());
                    }
                    if license.get_untracked() != orig_l {
                        body.insert("license".into(), license.get_untracked().into());
                    }
                    if body.is_empty() {
                        c.store
                            .skills
                            .detail_msg
                            .set(Some(("Nothing changed.".into(), MsgTone::Plain)));
                        return;
                    }
                    c.send(Cmd::Skills(SkCmd::SaveSkill {
                        name: name.clone(),
                        body: Body(serde_json::Value::Object(body)),
                        include_archived,
                    }));
                }
                "unarchive" => c.send(Cmd::Skills(SkCmd::SetSkillArchived {
                    name: name.clone(),
                    archive: false,
                    include_archived,
                    reopen: true,
                })),
                _ => c.send(Cmd::Skills(SkCmd::DuplicateSkill {
                    name: name.clone(),
                    include_archived,
                })),
            }
        }));
    }
    buttons.push(button(
        cx,
        &t,
        &Action::label("close", "Close"),
        On::Raised,
        true,
        guarded_close(close, guard),
    ));
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .child(
            Scroll::new(col.build())
                .layout(LayoutStyle::default().grow(1.0).min_h(4))
                .element(cx, &t)
                .build(),
        )
        .child(super::w::form::button_row(buttons))
        .build()
}

/// One header row of the URL form (`fp`: the stored fingerprint).
#[derive(Clone)]
struct HeaderSig {
    name: Signal<String>,
    value: Signal<String>,
    fp: Option<String>,
}

/// Add server / Edit: the MCP server form (the web's modal) — Name,
/// Description, Agents, How to reach it [Command] [URL], the fields of
/// each, the test block; [Test connection] [Save] [Close].
fn open_mcp_form(cx: Scope, ctx: &Ctx, row: Option<McpRow>) {
    let sk = ctx.store.skills;
    sk.form_test.set(None);
    sk.form_note.set(None);
    let title = match &row {
        None => "Add MCP server".to_string(),
        Some(r) => format!("MCP server — {}", r.name),
    };
    let c = ctx.clone();
    super::w::FormModal::new(title)
        .size(96, 34)
        .open(ctx, cx, move |mcx, close, guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let width = inner_w;
            let field_w = (width - 18).clamp(20, 76);
            let init = row.as_ref().map(McpForm::from_row).unwrap_or(McpForm {
                stdio: true,
                ..McpForm::default()
            });
            let editing = row.as_ref().map(|r| r.name.clone());
            let name = mcx.signal(init.name.clone());
            let description = mcx.signal(init.description.clone());
            let how = mcx.signal(if init.stdio { 0usize } else { 1 });
            let command = mcx.signal(init.command.clone());
            let cwd = mcx.signal(init.cwd.clone());
            let args = mcx.signal(init.args.clone());
            let url = mcx.signal(init.url.clone());
            // Headers: the stored ones (fingerprint kept) + Add header rows.
            let headers = mcx.signal(
                init.headers
                    .iter()
                    .map(|(k, v, fp)| HeaderSig {
                        name: mcx.signal(k.clone()),
                        value: mcx.signal(v.clone()),
                        fp: fp.clone(),
                    })
                    .collect::<Vec<_>>(),
            );
            let form = {
                move || McpForm {
                    name: name.get_untracked(),
                    description: description.get_untracked(),
                    stdio: how.get_untracked() == 0,
                    command: command.get_untracked(),
                    cwd: cwd.get_untracked(),
                    args: args.get_untracked(),
                    url: url.get_untracked(),
                    headers: headers
                        .get_untracked()
                        .iter()
                        .map(|h| (h.name.get_untracked(), h.value.get_untracked(), h.fp.clone()))
                        .collect(),
                }
            };
            // An unsaved edit is never dropped silently (R15 F2).
            {
                let init_body = form().body();
                let form_d = form;
                let ui = c.ui;
                let close_g = close.clone();
                *guard.borrow_mut() = Some(Box::new(move || {
                    if form_d().body() == init_body {
                        return false;
                    }
                    let close = close_g.clone();
                    super::w::Confirm::danger(super::DISCARD_QUESTION, "Discard", "Keep editing")
                        .open(mcx, ui, move || close());
                    true
                }));
            }
            // A save that succeeded closes the form.
            {
                let close = close.clone();
                let start = sk.form_saved.get_untracked();
                mcx.effect(move || {
                    if sk.form_saved.get() != start {
                        close();
                    }
                });
            }
            let caret = c.ui.caret;
            let input = move |cx: Scope, sig: Signal<String>, ph: &str, w: i32| -> View {
                super::w::caret_tracked(
                    cx,
                    caret,
                    TextInput::new()
                        .value(sig)
                        .placeholder(ph)
                        .layout(LayoutStyle::default().w(w).h(1))
                        .element(cx, &t),
                )
                .build()
            };
            let help = move |text: &str| -> View {
                Element::new()
                    .style(LayoutStyle::row().shrink(0.0))
                    .child(
                        Element::new()
                            .style(LayoutStyle::default().w(17).h(1).shrink(0.0))
                            .build(),
                    )
                    .child(sentence(&t, text, (width - 17).max(10), t.text_faint))
                    .build()
            };
            let mut col = Element::new().style(LayoutStyle::column().gap(0));
            col = col.child(match &editing {
                None => super::w::field_row(
                    &t,
                    "Name",
                    17,
                    super::w::caret_tracked(
                        mcx,
                        caret,
                        TextInput::new()
                            .value(name)
                            .layout(LayoutStyle::default().w(field_w.min(40)).h(1))
                            .element(mcx, &t),
                    )
                    .autofocus()
                    .build(),
                ),
                Some(n) => super::w::field_row(
                    &t,
                    "Name",
                    17,
                    super::w::fill_line(
                        LayoutStyle::line(1).shrink(0.0),
                        vec![Ink::new(n.clone(), t.text).bold()],
                        None,
                    ),
                ),
            });
            col = col.child(help(
                "A short name for this server (letters, digits, - _ .); its tools will be named after it.",
            ));
            col = col.child(super::w::field_row(
                &t,
                "Description",
                17,
                input(mcx, description, "", field_w),
            ));
            col = col.child(help(
                "What this server is for, for the admins who read this list.",
            ));
            // Agents: the same switch as the table's (asks before on).
            let agents = match &row {
                Some(r) => {
                    let (c2, r2) = (c.clone(), r.clone());
                    Toggle::new(r.enabled_for_agents)
                        .label(AGENTS_LABEL)
                        .refused(r.agents_block_reason().map(str::to_string))
                        .on_change(move |_| toggle_agents(mcx, &c2, &r2))
                        .view(mcx, &t)
                }
                None => Toggle::new(false)
                    .label(AGENTS_LABEL)
                    .refused(Some(
                        "Save and test the server first: agents get the tools a successful test lists."
                            .into(),
                    ))
                    .view(mcx, &t),
            };
            col = col.child(super::w::field_row(&t, "Agents", 17, agents));
            if let Some(r) = &row {
                if r.agents_block_reason().is_none() && !r.agents_status.is_empty() {
                    col = col.child(help(&r.agents_status));
                }
            }
            col = col.child(super::w::field_row(
                &t,
                "How to reach it",
                17,
                Segmented::new(["Command", "URL"], None)
                    .bind(how)
                    .view(mcx, &t),
            ));
            col = col.child(dyn_view_scoped(LayoutStyle::column().gap(0), move |gcx| {
                if how.get() == 0 {
                    reach_stdio(gcx, caret, command, cwd, args, field_w, width)
                } else {
                    reach_url(gcx, caret, url, headers, field_w, width)
                }
            }));
            // The test block.
            col = col.child(dyn_view(
                LayoutStyle::column().gap(0).shrink(0.0),
                move || {
                    let t = abstracttui::app::current_theme().tokens;
                    match sk.form_test.get() {
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                        Some(None) => sentence(
                            &t,
                            "Connecting (up to 10 seconds)...",
                            width,
                            t.text_muted,
                        ),
                        Some(Some(Err(m))) => {
                            sentence(&t, &format!("Connection failed {m}"), width, t.error)
                        }
                        Some(Some(Ok(r))) => {
                            let mut c = Element::new().style(LayoutStyle::column().gap(0));
                            let m = if r.message.is_empty() {
                                "Connected.".into()
                            } else {
                                r.message.clone()
                            };
                            c = c.child(sentence(&t, &m, width, t.ok));
                            for (n, d) in &r.tools {
                                let l = if d.is_empty() {
                                    n.clone()
                                } else {
                                    format!("{n} — {d}")
                                };
                                c = c.child(sentence(&t, &l, width, t.text));
                            }
                            c.build()
                        }
                    }
                },
            ));
            col = col.child(dyn_view(
                LayoutStyle::column().gap(0).shrink(0.0),
                move || {
                    let t = abstracttui::app::current_theme().tokens;
                    match sk.form_note.get() {
                        Some(n) => sentence(
                            &t,
                            &n,
                            width,
                            if n.starts_with("Not saved") {
                                t.error
                            } else {
                                t.text_muted
                            },
                        ),
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                },
            ));
            let test = {
                let c = c.clone();
                move || {
                    c.send(Cmd::Skills(SkCmd::TestMcpForm {
                        body: Body(form().body()),
                    }))
                }
            };
            let save = {
                let c = c.clone();
                let editing = editing.clone();
                move || {
                    c.send(Cmd::Skills(SkCmd::SaveMcp {
                        editing: editing.clone(),
                        body: Body(form().body()),
                    }))
                }
            };
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    Scroll::new(col.build())
                        .layout(LayoutStyle::default().grow(1.0).min_h(4))
                        .element(mcx, &t)
                        .build(),
                )
                .child(super::w::form::button_row(vec![
                    button(
                        mcx,
                        &t,
                        &Action::label("test_form", "Test connection"),
                        On::Raised,
                        true,
                        test,
                    ),
                    button(mcx, &t, &Action::label("save", "Save"), On::Raised, true, save),
                    button(
                        mcx,
                        &t,
                        &Action::label("close", "Close"),
                        On::Raised,
                        true,
                        guarded_close(close, guard),
                    ),
                ]))
                .build()
        });
}

fn reach_stdio(
    cx: Scope,
    caret: super::w::Caret,
    command: Signal<String>,
    cwd: Signal<String>,
    args: Signal<String>,
    field_w: i32,
    width: i32,
) -> View {
    let t = use_theme(cx).get().tokens;
    let help = |text: &str| -> View {
        Element::new()
            .style(LayoutStyle::row().shrink(0.0))
            .child(
                Element::new()
                    .style(LayoutStyle::default().w(17).h(1).shrink(0.0))
                    .build(),
            )
            .child(sentence(&t, text, (width - 17).max(10), t.text_faint))
            .build()
    };
    let input = |sig: Signal<String>, ph: &str| -> View {
        super::w::caret_tracked(
            cx,
            caret,
            TextInput::new()
                .value(sig)
                .placeholder(ph)
                .layout(LayoutStyle::default().w(field_w).h(1))
                .element(cx, &t),
        )
        .build()
    };
    let args_state = TextAreaState::new(cx);
    args_state.set_text(args.get_untracked());
    let mut p = Element::new().style(LayoutStyle::column().gap(0));
    p = p.child(sentence(
        &t,
        "The gateway starts the server on this computer and talks to it over stdin/stdout.",
        width,
        t.text_faint,
    ));
    p = p.child(super::w::field_row(
        &t,
        "Command",
        17,
        input(command, "npx"),
    ));
    p = p.child(help(
        "The program that starts the server, for example npx or uvx.",
    ));
    p = p.child(super::w::field_row(
        &t,
        "Working folder",
        17,
        input(cwd, "A scratch folder"),
    ));
    p = p.child(help(
        "Where the command runs. Empty: a scratch folder removed after each test.",
    ));
    p = p.child(super::w::field_row(
        &t,
        "Arguments",
        17,
        TextArea::new()
            .state(&args_state)
            .placeholder("-y\n@modelcontextprotocol/server-everything")
            .on_change(move |s: &str| {
                if args.with_untracked(|cur| cur != s) {
                    args.set(s.to_string());
                }
            })
            .submit_policy(SubmitPolicy::EnterInserts)
            .rows(2, 4)
            .layout(LayoutStyle::default().w(field_w).h(4).shrink(0.0))
            .element(cx, &t)
            .build(),
    ));
    p = p.child(help(
        "One argument per line, passed to the command as they are.",
    ));
    p.build()
}

fn reach_url(
    cx: Scope,
    caret: super::w::Caret,
    url: Signal<String>,
    headers: Signal<Vec<HeaderSig>>,
    field_w: i32,
    width: i32,
) -> View {
    let t = use_theme(cx).get().tokens;
    let help = |text: &str| -> View {
        Element::new()
            .style(LayoutStyle::row().shrink(0.0))
            .child(
                Element::new()
                    .style(LayoutStyle::default().w(17).h(1).shrink(0.0))
                    .build(),
            )
            .child(sentence(&t, text, (width - 17).max(10), t.text_faint))
            .build()
    };
    let mut p = Element::new().style(LayoutStyle::column().gap(0));
    p = p.child(sentence(
        &t,
        "The server already runs somewhere and answers MCP over HTTP.",
        width,
        t.text_faint,
    ));
    p = p.child(super::w::field_row(
        &t,
        "URL",
        17,
        super::w::caret_tracked(
            cx,
            caret,
            TextInput::new()
                .value(url)
                .placeholder("https://example.com/mcp")
                .layout(LayoutStyle::default().w(field_w).h(1))
                .element(cx, &t),
        )
        .build(),
    ));
    p = p.child(help("The server's MCP endpoint."));
    p = p.child(super::w::field_row(
        &t,
        "Headers",
        17,
        dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |hcx| {
            let t = use_theme(hcx).get().tokens;
            let rows = headers.get();
            let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
            for (i, h) in rows.iter().enumerate() {
                let ph = match h.fp.clone() {
                    Some(f) => format!("Saved · fingerprint {f}; type to replace"),
                    None => "Value".to_string(),
                };
                let remove = Action::label("remove_header", "Remove").tooltip("Remove this header");
                col = col.child(
                    Element::new()
                        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                        .child(
                            super::w::caret_tracked(
                                hcx,
                                caret,
                                TextInput::new()
                                    .value(h.name)
                                    .placeholder("Name, e.g. Authorization")
                                    .layout(LayoutStyle::default().w(28).h(1))
                                    .element(hcx, &t),
                            )
                            .build(),
                        )
                        .child(
                            super::w::caret_tracked(
                                hcx,
                                caret,
                                TextInput::new()
                                    .value(h.value)
                                    .placeholder(ph)
                                    .masked(true)
                                    .layout(LayoutStyle::default().w((field_w - 39).max(12)).h(1))
                                    .element(hcx, &t),
                            )
                            .build(),
                        )
                        .child(button(hcx, &t, &remove, On::Raised, true, move || {
                            headers.update(|v| {
                                if i < v.len() {
                                    v.remove(i);
                                }
                            })
                        }))
                        .build(),
                );
            }
            let add = Action::label("add_header", "Add header");
            col = col.child(button(hcx, &t, &add, On::Raised, true, move || {
                headers.update(|v| {
                    v.push(HeaderSig {
                        name: cx.signal(String::new()),
                        value: cx.signal(String::new()),
                        fp: None,
                    })
                })
            }));
            col.build()
        }),
    ));
    p = p.child(help(
        "Sent with every request, for example Authorization: Bearer <token>. Values are stored encrypted and never shown again.",
    ));
    p.build()
}
