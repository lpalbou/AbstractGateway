//! Skills & MCP (WORK) — the web console's page of the same name
//! (`console_skills_mcp.py`), same data, same actions, same sentences:
//!
//! - **Skills**: the shelf (Name · What it does · Version · Trust ·
//!   Source), search, Show archived, Import (a `.zip` or a skill folder on
//!   THIS machine — the web's file pickers), Export (`<name>.zip`, saved
//!   on this machine), Archive / Unarchive (imported skills, admin), and
//!   the skill overlay (View: fields, SKILL.md, files; Save / Duplicate to
//!   edit / Unarchive).
//! - **MCP servers**: the agents note, Name · Transport · Status · Tools,
//!   Show archived, Add server / Edit (overlay: Command or URL, headers,
//!   Test connection, Save), Test, Archive / Unarchive, and the
//!   `[x] Enabled for agents` switch (turning it on asks inline first).
//!
//! Every write goes through the web page's own routes (see
//! `api_skills.rs`); nothing here is a second data path.

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

use super::kit::{self, InlineConfirm, Row, WrapTable};
use super::util::{line, span, span_bold, wrap_text};
use super::widths::ColRule;
use super::Ctx;
use crate::store::skills::{
    filter_skills, McpForm, McpRow, SkillDetail, SkillRow, Tone as MsgTone, AGENTS_LABEL,
    MCP_EMPTY, MCP_EMPTY_ADMIN_TAIL, MCP_LOADING, MCP_PURPOSE, SKILLS_EMPTY, SKILLS_LOADING,
    SKILLS_NO_MATCH, SKILLS_PURPOSE, SUBTITLE, TITLE,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::skills::SkCmd;
use crate::worker::{Body, Cmd};
use abstracttui::widgets::{SubmitPolicy, TextArea, TextAreaState};

/// The verbs only an admin may use (the web hides them for others).
pub const ADMIN_KEYS: &[&str] = &["i", "a", "e", "t", "d", "space", "f", "u"];

/// A labelled form row that keeps its line under height pressure (a
/// form inside a Scroll must never lose a field to a crushed row).
fn field(t: &TokenSet, label: &str, child: View) -> View {
    Element::new()
        .style(LayoutStyle::column().h(1).shrink(0.0))
        .child(super::util::field(t, label, child))
        .build()
}

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

/// The footer verbs for the current tab.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let mut out = vec![("Tab", "Skills ⇄ MCP servers"), ("Enter", "expand row")];
    if ctx.store.skills.tab.get() == 0 {
        out.extend_from_slice(&[
            ("v", "view"),
            ("/", "search"),
            ("h", "show archived"),
            ("x", "export"),
            ("i", "import"),
            ("d", "archive/unarchive"),
            ("f", "shelf folder"),
            ("u", "Refresh curated shelf"),
        ]);
    } else {
        out.extend_from_slice(&[
            ("space", "Enabled for agents"),
            ("h", "show archived"),
            ("a", "add server"),
            ("e", "edit"),
            ("t", "test"),
            ("d", "archive/unarchive"),
        ]);
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

/// The page.
pub fn screen(ctx: &Ctx, cx: Scope) -> View {
    let t = use_theme(cx).get().tokens;
    let sk = ctx.store.skills;
    let confirm = InlineConfirm::new(cx);
    // The table regenerates when data lands: the keeper hands the keyboard
    // back to each new instance (util::FocusKeeper).
    let keeper = super::util::FocusKeeper::new();
    // First visit: read both lists (the web's openSkillsMcpPage).
    if sk
        .skills
        .with_untracked(|d| matches!(d, Loadable::NotAsked))
        && ctx.store.conn.with_untracked(ConnPhase::is_connected)
    {
        refresh(ctx);
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
                if handle_key(cx, &keys_ctx, confirm, k.key) {
                    ectx.stop_propagation();
                }
            }
        });
    let root = confirm.keys(root);
    let body_ctx = ctx.clone();
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
                let t = use_theme(cx).get().tokens;
                let tab = sk.tab.get();
                let mark = |on: bool, label: &str| {
                    if on {
                        span_bold(format!("[{label}]"), t.accent)
                    } else {
                        span(format!(" {label} "), t.text_muted)
                    }
                };
                line(vec![
                    mark(tab == 0, "Skills"),
                    span("  ", t.text),
                    mark(tab == 1, "MCP servers"),
                ])
            }))
            .child(dyn_view_scoped(
                LayoutStyle::column().gap(0).grow(1.0).min_h(3),
                move |gcx| {
                    if sk.tab.get() == 0 {
                        skills_tab(gcx, &body_ctx, &keeper)
                    } else {
                        mcp_tab(gcx, &body_ctx, &keeper)
                    }
                },
            ))
            .child(confirm.view(&t, 0))
            .element(&t)
            .build(),
    )
    .build()
}

/// One page key. Returns true when handled.
fn handle_key(cx: Scope, ctx: &Ctx, confirm: InlineConfirm, key: Key) -> bool {
    let sk = ctx.store.skills;
    // The in-place shelf folder input owns every key while it is open.
    if sk.shelf_editing.get_untracked() {
        return false;
    }
    let tab = sk.tab.get_untracked();
    match key {
        Key::Tab | Key::Char('[') | Key::Char(']') => {
            sk.tab.set(if tab == 0 { 1 } else { 0 });
            true
        }
        Key::Char('h') => {
            if tab == 0 {
                let on = !sk.skills_archived.get_untracked();
                sk.skills_archived.set(on);
                ctx.send(Cmd::Skills(SkCmd::LoadSkills {
                    include_archived: on,
                }));
            } else {
                sk.mcp_archived.update(|v| *v = !*v);
            }
            true
        }
        Key::Char('/') if tab == 0 => {
            open_search(cx, ctx);
            true
        }
        Key::Char('v') if tab == 0 => {
            match selected_skill(ctx) {
                Some(r) => open_skill(cx, ctx, r.name),
                None => ctx
                    .store
                    .notice
                    .set(Some("no skill selected — nothing to view".into())),
            }
            true
        }
        Key::Char('x') if tab == 0 => {
            match selected_skill(ctx) {
                Some(r) if r.archived => ctx.store.notice.set(Some(format!(
                    "{} is archived — unarchive it to export it",
                    r.name
                ))),
                Some(r) => ctx.send(Cmd::Skills(SkCmd::ExportSkill {
                    name: r.name,
                    dir: super::sandbox::artifact_dir(),
                })),
                None => ctx
                    .store
                    .notice
                    .set(Some("no skill selected — nothing to export".into())),
            }
            true
        }
        Key::Char('f') if tab == 0 => {
            if super::util::admin_gate(&ctx.store, "changing the skills shelf folder") {
                edit_shelf(ctx);
            }
            true
        }
        Key::Char('u') if tab == 0 => {
            if super::util::admin_gate(&ctx.store, "refreshing the curated skills shelf") {
                sk.shelf_msg
                    .set(Some(("Working...".into(), MsgTone::Plain)));
                ctx.send(Cmd::Operator(crate::worker::operator::OpCmd::ReseedSkills));
            }
            true
        }
        Key::Char('i') if tab == 0 => {
            if super::util::admin_gate(&ctx.store, "importing a skill") {
                open_import(cx, ctx);
            }
            true
        }
        Key::Char('d') => {
            if !super::util::admin_gate(&ctx.store, "archiving") {
                return true;
            }
            if tab == 0 {
                archive_skill(ctx, confirm);
            } else {
                archive_mcp(ctx, confirm);
            }
            true
        }
        Key::Char('a') if tab == 1 => {
            if super::util::admin_gate(&ctx.store, "adding an MCP server") {
                open_mcp_form(cx, ctx, None);
            }
            true
        }
        Key::Char('e') if tab == 1 => {
            if super::util::admin_gate(&ctx.store, "editing an MCP server") {
                match selected_mcp(ctx) {
                    Some(r) if r.archived => ctx
                        .store
                        .notice
                        .set(Some(format!("{}: Archived: unarchive it first.", r.name))),
                    Some(r) => open_mcp_form(cx, ctx, Some(r)),
                    None => ctx
                        .store
                        .notice
                        .set(Some("no server selected — nothing to edit".into())),
                }
            }
            true
        }
        Key::Char('t') if tab == 1 => {
            if super::util::admin_gate(&ctx.store, "testing an MCP server") {
                match selected_mcp(ctx) {
                    Some(r) if r.archived => ctx
                        .store
                        .notice
                        .set(Some(format!("{}: Archived: unarchive it first.", r.name))),
                    Some(r) => ctx.send(Cmd::Skills(SkCmd::TestMcp { name: r.name })),
                    None => ctx
                        .store
                        .notice
                        .set(Some("no server selected — nothing to test".into())),
                }
            }
            true
        }
        Key::Char(' ') if tab == 1 => {
            if super::util::admin_gate(&ctx.store, "Enabled for agents") {
                if let Some(r) = selected_mcp(ctx) {
                    toggle_agents(ctx, confirm, &r);
                }
            }
            true
        }
        _ => false,
    }
}

/// Space on a server: the "Enabled for agents" switch. Off applies at
/// once; on asks inline first; a blocked switch says why.
fn toggle_agents(ctx: &Ctx, confirm: InlineConfirm, r: &McpRow) {
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
    confirm.ask(r.agents_confirm_sentence(), "Turn on", move || {
        c.send(Cmd::Skills(SkCmd::SetMcpAgents {
            name: name.clone(),
            enabled: true,
        }));
    });
}

/// `d` on a skill: Archive an imported skill / Unarchive an archived one
/// (the web asks no confirmation for skills; Archive here asks inline
/// because a keypress is easier to hit by mistake than a button).
fn archive_skill(ctx: &Ctx, confirm: InlineConfirm) {
    let sk = ctx.store.skills;
    let Some(r) = selected_skill(ctx) else {
        ctx.store.notice.set(Some("no skill selected".into()));
        return;
    };
    let include_archived = sk.skills_archived.get_untracked();
    if r.archived {
        ctx.send(Cmd::Skills(SkCmd::SetSkillArchived {
            name: r.name,
            archive: false,
            include_archived,
            reopen: false,
        }));
        return;
    }
    if r.origin != "imported" {
        sk.skills_msg.set(Some((
            format!(
                "{}: Curated skills are read-only; duplicate to edit.",
                r.name
            ),
            MsgTone::Error,
        )));
        return;
    }
    let c = ctx.clone();
    let name = r.name.clone();
    confirm.ask(
        format!(
            "Archive {}? Runs no longer see it; Show archived finds it again.",
            r.name
        ),
        "Archive",
        move || {
            c.send(Cmd::Skills(SkCmd::SetSkillArchived {
                name: name.clone(),
                archive: true,
                include_archived,
                reopen: false,
            }))
        },
    );
}

fn archive_mcp(ctx: &Ctx, confirm: InlineConfirm) {
    let Some(r) = selected_mcp(ctx) else {
        ctx.store.notice.set(Some("no server selected".into()));
        return;
    };
    let name = r.name.clone();
    if r.archived {
        ctx.send(Cmd::Skills(SkCmd::SetMcpArchived {
            name,
            archive: false,
        }));
        return;
    }
    let c = ctx.clone();
    confirm.ask(
        format!(
            "Archive {}? Its tools are no longer offered; Show archived finds it again.",
            r.name
        ),
        "Archive",
        move || {
            c.send(Cmd::Skills(SkCmd::SetMcpArchived {
                name: name.clone(),
                archive: true,
            }))
        },
    );
}

fn message_line(t: &TokenSet, msg: Option<(String, MsgTone)>, width: i32) -> View {
    match msg {
        Some((text, tone)) => kit::sentence(t, &text, width, msg_ink(t, tone)),
        None => Element::new().style(LayoutStyle::default().h(0)).build(),
    }
}

fn skills_tab(cx: Scope, ctx: &Ctx, keeper: &super::util::FocusKeeper) -> View {
    let t = use_theme(cx).get().tokens;
    let sk = ctx.store.skills;
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let width = (vw - 4).max(20);
    let query = sk.query.get();
    let archived_on = sk.skills_archived.get();
    let data = sk.skills.get();
    let msg = sk.skills_msg.get();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(kit::sentence(&t, SKILLS_PURPOSE, width, t.text_muted));
    let search = if query.is_empty() {
        "Search: (/ to search by name or description)".to_string()
    } else {
        format!("Search: {query}")
    };
    col = col.child(line(vec![
        span(search, t.text),
        span("   ", t.text),
        span_bold(
            super::switch::switch_text("Show archived", archived_on, None, false),
            if archived_on { t.accent } else { t.text },
        ),
    ]));
    col = col.child(message_line(&t, msg, width));
    match data {
        Loadable::NotAsked | Loadable::Loading => {
            col = col.child(keeper.anchor(kit::sentence(&t, SKILLS_LOADING, width, t.text_muted)));
        }
        Loadable::Failed(e) => {
            col = col.child(keeper.anchor(kit::sentence(
                &t,
                &format!(
                    "Could not list the skills: {}",
                    crate::worker::skills::refusal_text(&e)
                ),
                width,
                t.error,
            )));
        }
        Loadable::Ready(d) => {
            if !d.warnings.is_empty() {
                col = col.child(kit::sentence(&t, &d.warnings.join(" "), width, t.warn));
            }
            let rows = filter_skills(&d.rows, &query);
            let empty = if query.trim().is_empty() {
                SKILLS_EMPTY
            } else {
                SKILLS_NO_MATCH
            };
            let narrow = vw < 100;
            let rules = if narrow {
                vec![
                    ColRule::tail("Name", 10),
                    ColRule::head("What it does", 16),
                    ColRule::head("Trust", 8),
                ]
            } else {
                vec![
                    ColRule::tail("Name", 12),
                    ColRule::head("What it does", 24),
                    ColRule::head("Version", 7),
                    ColRule::head("Trust", 9),
                    ColRule::head("Source", 10),
                ]
            };
            let table_rows: Vec<Row> = rows
                .iter()
                .map(|r| {
                    let cells = if narrow {
                        vec![
                            r.name.clone(),
                            r.description.clone(),
                            r.trust_text().to_string(),
                        ]
                    } else {
                        vec![
                            r.name.clone(),
                            r.description.clone(),
                            r.version_text(),
                            r.trust_text().to_string(),
                            r.source_text(),
                        ]
                    };
                    let mut detail = vec![format!(
                        "Version: {} · Source: {}",
                        r.version_text(),
                        r.source_text()
                    )];
                    if !r.reasons.is_empty() {
                        detail.push(format!("Trust: {}", r.reasons.join(" ")));
                    }
                    let acts = r.actions(admin);
                    detail.push(format!(
                        "Actions: {}",
                        acts.iter()
                            .map(|a| match *a {
                                "View" => "v View",
                                "Export" => "x Export",
                                "Archive" => "d Archive",
                                "Unarchive" => "d Unarchive",
                                other => other,
                            })
                            .collect::<Vec<_>>()
                            .join(" · ")
                    ));
                    Row::new(cells).detail(detail).dim(r.archived)
                })
                .collect();
            super::util::clamp_selection(cx, sk.skill_sel, {
                let n = table_rows.len();
                move || n
            });
            col = col.child(
                keeper.wire(
                    WrapTable::new(rules, table_rows, sk.skill_sel)
                        .expanded(sk.skill_expanded)
                        .empty(empty)
                        .element(cx, &t),
                ),
            );
        }
    }
    if admin {
        // Tracked: closing the in-place input re-renders the table, which
        // takes the keyboard back (the keeper was told to reclaim it).
        let _ = sk.shelf_editing.get();
        col = col.child(shelf_row(cx, ctx, width, keeper));
    }
    col.build()
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

/// The shelf row's lines when not editing (tests read these).
pub fn shelf_lines(sh: &crate::store::SkillsShelf, width: i32) -> Vec<(String, &'static str)> {
    let mut out = Vec::new();
    let folder = if sh.source == "stored" && !sh.value.is_empty() {
        sh.value.clone()
    } else if !sh.default_path.is_empty() {
        format!("(the gateway's own copy) {}", sh.default_path)
    } else {
        "(the gateway's own copy)".to_string()
    };
    let mut head = format!("Shelf folder: {folder}");
    if let Some(w) = shelf_source_words(&sh.source) {
        head.push_str(&format!(" · {w}"));
    }
    // The two verbs stay together: on the folder's line when it fits,
    // else on their own line (never split mid-label).
    const KEYS: &str = "f change · u Refresh curated shelf";
    if abstracttui::text::width(&head) as i32 + 3 + KEYS.len() as i32 <= width {
        out.push((format!("{head}   {KEYS}"), "text"));
    } else {
        out.push((head, "text"));
        out.push((KEYS.to_string(), "keys"));
    }
    let v = if sh.bundled_version.is_empty() {
        String::new()
    } else {
        format!(" (version {})", sh.bundled_version)
    };
    out.push((
        format!("Empty: the gateway's own copy of the curated shelf{v}, refreshed at each start."),
        "faint",
    ));
    if !sh.available {
        out.push((format!("Not available: {}", sh.reason), "warn"));
    }
    for w in &sh.warnings {
        out.push((w.clone(), "warn"));
    }
    out
}

/// `f`: edit the folder in place (prefilled with the SAVED value only — a
/// default written back would silently become a saved setting).
fn edit_shelf(ctx: &Ctx) {
    let sk = ctx.store.skills;
    let shelf = match ctx.store.runtime_config.get_untracked() {
        Loadable::Ready(d) => d.skills_shelf,
        _ => {
            ctx.store.notice.set(Some(
                "Reading the skills shelf setting... — one moment".into(),
            ));
            return;
        }
    };
    let Some(sh) = shelf else {
        sk.shelf_msg.set(Some((
            "This gateway did not report its skills shelf.".into(),
            MsgTone::Error,
        )));
        return;
    };
    sk.shelf_draft.set(if sh.source == "stored" {
        sh.value.clone()
    } else {
        String::new()
    });
    sk.shelf_msg.set(None);
    sk.shelf_editing.set(true);
}

/// The R8.1 shelf row under the skills list: ONE inline row — the folder
/// (edited in place: Enter saves, Esc keeps) and "Refresh curated shelf"
/// — plus the helper line and the outcome. Data = `GET
/// /admin/runtime-config` `skills.shelf`; writes = POST
/// /admin/runtime-config `{"skills.shelf": …}` and POST
/// /admin/skills/reseed (the web's own routes).
fn shelf_row(cx: Scope, ctx: &Ctx, width: i32, keeper: &super::util::FocusKeeper) -> View {
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
    // The folder save's outcome, in place.
    {
        let ui = ctx.ui;
        let keeper_saved = keeper.clone();
        cx.effect(move || {
            if let Some((fid, out)) = ui.write_done.get() {
                if sk.shelf_form.get_untracked() == Some(fid) {
                    ui.write_done.set(None);
                    sk.shelf_form.set(None);
                    match out {
                        Ok(_) => {
                            keeper_saved.reclaim();
                            sk.shelf_editing.set(false);
                            sk.shelf_msg.set(Some(("Saved".into(), MsgTone::Ok)));
                        }
                        Err(e) => sk
                            .shelf_msg
                            .set(Some((format!("Not saved: {e}"), MsgTone::Error))),
                    }
                }
            }
        });
    }
    let c = ctx.clone();
    let keeper = keeper.clone();
    dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |gcx| {
        let t = use_theme(gcx).get().tokens;
        let cfg = c.store.runtime_config.get();
        let editing = sk.shelf_editing.get();
        let msg = sk.shelf_msg.get();
        let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
        let shelf = match &cfg {
            Loadable::Ready(d) => match &d.skills_shelf {
                Some(sh) => sh.clone(),
                None => {
                    return kit::sentence(
                        &t,
                        "This gateway did not report its skills shelf.",
                        width,
                        t.error,
                    )
                }
            },
            Loadable::Failed(e) => {
                return kit::sentence(
                    &t,
                    &format!("Could not read the skills shelf setting. {e}"),
                    width,
                    t.error,
                )
            }
            _ => {
                return kit::sentence(
                    &t,
                    "Reading the skills shelf setting...",
                    width,
                    t.text_muted,
                )
            }
        };
        if editing {
            let keeper_submit = keeper.clone();
            let keeper_cancel = keeper.clone();
            let c_save = c.clone();
            let sh = shelf.clone();
            col = col.child(kit::inline_input(
                gcx,
                &t,
                "Shelf folder:",
                sk.shelf_draft,
                shelf.default_path.clone(),
                move |typed| {
                    let body = super::runtimes::skills_shelf_body(&sh, &typed);
                    if body.as_object().is_none_or(|m| m.is_empty()) {
                        keeper_submit.reclaim();
                        sk.shelf_editing.set(false);
                        return;
                    }
                    let fid = crate::worker::next_form_id();
                    sk.shelf_form.set(Some(fid));
                    sk.shelf_msg.set(Some(("Saving...".into(), MsgTone::Plain)));
                    c_save.send(Cmd::SaveRuntimeConfig {
                        body: body.into(),
                        form_id: Some(fid),
                    });
                },
                move || {
                    keeper_cancel.reclaim();
                    sk.shelf_editing.set(false);
                    sk.shelf_msg.set(None);
                },
            ));
            col = col.child(kit::sentence(
                &t,
                "Enter saves · Esc keeps the current folder · empty = the gateway's own copy",
                width,
                t.text_faint,
            ));
        } else {
            for (text, tone) in shelf_lines(&shelf, width) {
                let ink = match tone {
                    "keys" => t.accent,
                    "warn" => t.warn,
                    "faint" => t.text_faint,
                    _ => t.text,
                };
                col = col.child(kit::sentence(&t, &text, width, ink));
            }
        }
        if let Some((text, tone)) = msg {
            col = col.child(kit::sentence(&t, &text, width, msg_ink(&t, tone)));
        }
        col.build()
    })
}

fn mcp_tab(cx: Scope, ctx: &Ctx, keeper: &super::util::FocusKeeper) -> View {
    let t = use_theme(cx).get().tokens;
    let sk = ctx.store.skills;
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let width = (vw - 4).max(20);
    let show = sk.mcp_archived.get();
    let data = sk.mcp.get();
    let msg = sk.mcp_msg.get();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    if let Loadable::Ready(d) = &data {
        if !d.agents_note.is_empty() {
            col = col.child(kit::sentence(&t, &d.agents_note, width, t.warn));
        }
    }
    col = col.child(kit::sentence(&t, MCP_PURPOSE, width, t.text_muted));
    col = col.child(line(vec![span_bold(
        super::switch::switch_text("Show archived", show, None, false),
        if show { t.accent } else { t.text },
    )]));
    col = col.child(message_line(&t, msg, width));
    match data {
        Loadable::NotAsked | Loadable::Loading => {
            col = col.child(keeper.anchor(kit::sentence(&t, MCP_LOADING, width, t.text_muted)));
        }
        Loadable::Failed(e) => {
            col = col.child(keeper.anchor(kit::sentence(
                &t,
                &format!(
                    "Could not list the MCP servers: {}",
                    crate::worker::skills::refusal_text(&e)
                ),
                width,
                t.error,
            )));
        }
        Loadable::Ready(d) => {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            let rows: Vec<&McpRow> = d.rows.iter().filter(|r| show || !r.archived).collect();
            let narrow = vw < 100;
            let rules = if narrow {
                vec![
                    ColRule::tail("Name", 10),
                    ColRule::head("Status", 18),
                    ColRule::head("Tools", 6),
                ]
            } else {
                vec![
                    ColRule::tail("Name", 12),
                    ColRule::tail("Transport", 16),
                    ColRule::head("Status", 20),
                    ColRule::head("Tools", 6),
                ]
            };
            let table_rows: Vec<Row> = rows
                .iter()
                .map(|r| {
                    let mut name = r.name.clone();
                    if r.archived {
                        name.push_str(" · Archived");
                    }
                    let status = if admin {
                        let sw = super::switch::switch_text(
                            AGENTS_LABEL,
                            r.enabled_for_agents,
                            None,
                            false,
                        );
                        format!("{} · {sw}", r.status_text(now))
                    } else {
                        r.status_text(now)
                    };
                    let transport = format!("{}: {}", r.transport_label(), r.target());
                    let cells = if narrow {
                        vec![name, status, r.tools_text()]
                    } else {
                        vec![name, transport.clone(), status, r.tools_text()]
                    };
                    let mut detail = Vec::new();
                    if !r.description.is_empty() {
                        detail.push(r.description.clone());
                    }
                    if narrow {
                        detail.push(transport);
                    }
                    if admin {
                        let line = match r.agents_block_reason() {
                            Some(why) => format!("{AGENTS_LABEL}: {why}"),
                            None => format!("{AGENTS_LABEL}: {}", r.agents_status),
                        };
                        detail.push(line);
                    }
                    if let Some(tst) = r.last_test.as_ref().filter(|t| t.ok) {
                        for (n, desc) in &tst.tools {
                            if desc.is_empty() {
                                detail.push(format!("  {n}"));
                            } else {
                                detail.push(format!("  {n} — {desc}"));
                            }
                        }
                    }
                    let acts = r.actions(admin);
                    if !acts.is_empty() {
                        detail.push(format!(
                            "Actions: {}",
                            acts.iter()
                                .map(|a| match *a {
                                    "Edit" => "e Edit",
                                    "Test" => "t Test",
                                    "Archive" => "d Archive",
                                    "Unarchive" => "d Unarchive",
                                    other => other,
                                })
                                .chain(if admin && !r.archived {
                                    Some("space Enabled for agents")
                                } else {
                                    None
                                })
                                .collect::<Vec<_>>()
                                .join(" · ")
                        ));
                    }
                    Row::new(cells).detail(detail).dim(r.archived)
                })
                .collect();
            let empty = if admin {
                format!("{MCP_EMPTY}{MCP_EMPTY_ADMIN_TAIL}")
            } else {
                MCP_EMPTY.to_string()
            };
            super::util::clamp_selection(cx, sk.mcp_sel, {
                let n = table_rows.len();
                move || n
            });
            col = col.child(
                keeper.wire(
                    WrapTable::new(rules, table_rows, sk.mcp_sel)
                        .expanded(sk.mcp_expanded)
                        .empty(empty)
                        .element(cx, &t),
                ),
            );
        }
    }
    col.build()
}

/// `/`: the search box (filters as you type, like the web's).
fn open_search(cx: Scope, ctx: &Ctx) {
    let sk = ctx.store.skills;
    kit::open_overlay(
        ctx,
        cx,
        "Search skills",
        &[("Enter", "done")],
        move |mcx, close, _guard| {
            let t = use_theme(mcx).get().tokens;
            let close2 = close.clone();
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(field(
                    &t,
                    "Search",
                    TextInput::new()
                        .value(sk.query)
                        .placeholder("Search by name or description")
                        .on_submit(move |_: &str| close2())
                        .layout(LayoutStyle::default().w(48).h(1))
                        .element(mcx, &t)
                        .autofocus()
                        .build(),
                ))
                .build()
        },
    );
}

/// `i`: import a `.zip` or a skill folder from THIS machine.
fn open_import(cx: Scope, ctx: &Ctx) {
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        "Import a skill",
        &[("Enter", "import")],
        move |mcx, close, _guard| {
            let t = use_theme(mcx).get().tokens;
            let path = mcx.signal(String::new());
            let submit = {
                let c = c.clone();
                let close = close.clone();
                move || {
                    let p = path.get_untracked();
                    c.send(Cmd::Skills(SkCmd::ImportSkill {
                        path: p,
                        include_archived: c.store.skills.skills_archived.get_untracked(),
                    }));
                    close();
                }
            };
            let submit2 = submit.clone();
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(line(vec![span(
                    "A .zip of a skill's folder, or the folder itself (it holds SKILL.md) — on THIS machine; its files are uploaded.",
                    t.text_muted,
                )]))
                .child(field(
                    &t,
                    "File or folder",
                    TextInput::new()
                        .value(path)
                        .placeholder("~/Downloads/field-notes.zip")
                        .on_submit(move |_: &str| submit())
                        .layout(LayoutStyle::default().w(56).h(1))
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

/// `v`: the skill overlay (the web's "Skill — <name>" modal).
pub fn open_skill(cx: Scope, ctx: &Ctx, name: String) {
    let sk = ctx.store.skills;
    sk.detail.set(Loadable::Loading);
    ctx.send(Cmd::Skills(SkCmd::OpenSkill { name: name.clone() }));
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        format!("Skill — {name}"),
        &[("Tab", "next field"), ("Ctrl+S", "save")],
        move |_mcx, _close, _guard| {
            dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |gcx| {
                let t = use_theme(gcx).get().tokens;
                let width = abstracttui::app::use_viewport(gcx).get_untracked().w - 6;
                match sk.detail.get() {
                    Loadable::NotAsked | Loadable::Loading => {
                        kit::sentence(&t, &format!("Reading {name}..."), width, t.text_muted)
                    }
                    Loadable::Failed(e) => kit::sentence(
                        &t,
                        &format!(
                            "Could not open {name}: {}",
                            crate::worker::skills::refusal_text(&e)
                        ),
                        width,
                        t.error,
                    ),
                    Loadable::Ready(d) => skill_body(gcx, &c, d, width),
                }
            })
        },
    );
}

fn skill_body(cx: Scope, ctx: &Ctx, d: SkillDetail, width: i32) -> View {
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
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    if let Some(lead) = d.lead(admin) {
        col = col.child(kit::sentence(&t, &lead, width, t.warn));
    }
    let field_w = (width - 22).clamp(20, 80);
    let text_field = |label: &str, sig: Signal<String>| -> View {
        if can_edit {
            field(
                &t,
                label,
                TextInput::new()
                    .value(sig)
                    .layout(LayoutStyle::default().w(field_w).h(1))
                    .element(cx, &t)
                    .build(),
            )
        } else {
            field(&t, label, line(vec![span(sig.get_untracked(), t.text)]))
        }
    };
    col = col.child(field(
        &t,
        "Name",
        line(vec![span_bold(d.name.clone(), t.text)]),
    ));
    col = col.child(kit::sentence(
        &t,
        "How agents refer to this skill; it never changes (duplicate to rename).",
        width,
        t.text_faint,
    ));
    col = col.child(text_field("Version", version));
    col = col.child(text_field("What it does", desc));
    col = col.child(kit::sentence(
        &t,
        "One sentence agents read to decide when to load this skill.",
        width,
        t.text_faint,
    ));
    col = col.child(text_field("License", license));
    col = col.child(field(
        &t,
        "Source",
        line(vec![span(d.source_text(), t.text)]),
    ));
    col = col.child(line(vec![span_bold("SKILL.md", t.text_muted)]));
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
                .layout(LayoutStyle::default().grow(1.0))
                .element(cx, &t)
                .build(),
        );
    } else {
        let mut md_col = Element::new().style(LayoutStyle::column().gap(0));
        for l in d.skill_md.lines().take(12) {
            for w in wrap_text(l, width.max(10) as usize) {
                md_col = md_col.child(line(vec![span(w, t.text)]));
            }
        }
        let n = d.skill_md.lines().count();
        if n > 12 {
            md_col = md_col.child(line(vec![span(
                format!("… {} more lines (Export saves the whole skill)", n - 12),
                t.text_faint,
            )]));
        }
        col = col.child(md_col.build());
    }
    col = col.child(kit::sentence(
        &t,
        "What agents read when they load the skill; the fields above are written into its frontmatter on Save.",
        width,
        t.text_faint,
    ));
    let files: Vec<String> = d.files.iter().map(|(p, s)| format!("{p} {s} B")).collect();
    col = col.child(kit::sentence(
        &t,
        &format!("Files: {}", files.join(" · ")),
        width,
        t.text_muted,
    ));
    if let Some(p) = &d.problem {
        col = col.child(kit::sentence(&t, p, width, t.warn));
    }
    col = col.child(dyn_view(
        LayoutStyle::column().gap(0).shrink(0.0),
        move || {
            let t = use_theme(cx).get().tokens;
            match sk.detail_msg.get() {
                Some((m, tone)) => kit::sentence(&t, &m, width, msg_ink(&t, tone)),
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        },
    ));
    if admin {
        let name = d.name.clone();
        let c = ctx.clone();
        let (orig_v, orig_d, orig_l, orig_md) = (
            d.version.clone(),
            d.description.clone(),
            d.license.clone(),
            d.skill_md.clone(),
        );
        let button = if d.editable {
            let save = move || {
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
                    include_archived: c.store.skills.skills_archived.get_untracked(),
                }));
            };
            Button::new("Save").on_click(save).element(cx, &t).build()
        } else if d.archived {
            let unarchive = move || {
                c.send(Cmd::Skills(SkCmd::SetSkillArchived {
                    name: name.clone(),
                    archive: false,
                    include_archived: c.store.skills.skills_archived.get_untracked(),
                    reopen: true,
                }))
            };
            Button::new("Unarchive")
                .on_click(unarchive)
                .element(cx, &t)
                .build()
        } else {
            let dup = move || {
                c.send(Cmd::Skills(SkCmd::DuplicateSkill {
                    name: name.clone(),
                    include_archived: c.store.skills.skills_archived.get_untracked(),
                }))
            };
            Button::new("Duplicate to edit")
                .on_click(dup)
                .element(cx, &t)
                .build()
        };
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(button)
                .build(),
        );
    }
    Scroll::new(col.build())
        .layout(LayoutStyle::default().grow(1.0).min_h(4))
        .element(cx, &t)
        .build()
}

/// `a` / `e`: the Add / Edit MCP server overlay.
fn open_mcp_form(cx: Scope, ctx: &Ctx, row: Option<McpRow>) {
    let sk = ctx.store.skills;
    sk.form_test.set(None);
    sk.form_note.set(None);
    let title = match &row {
        None => "Add MCP server".to_string(),
        Some(r) => format!("MCP server — {}", r.name),
    };
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        title,
        &[("Tab", "next field"), ("Ctrl+T", "Command ⇄ URL")],
        move |mcx, close, _guard| {
            let t = use_theme(mcx).get().tokens;
            let width = abstracttui::app::use_viewport(mcx).get_untracked().w - 6;
            let field_w = (width - 22).clamp(20, 80);
            let init = row.as_ref().map(McpForm::from_row).unwrap_or(McpForm {
                stdio: true,
                ..McpForm::default()
            });
            let editing = row.as_ref().map(|r| r.name.clone());
            let name = mcx.signal(init.name.clone());
            let description = mcx.signal(init.description.clone());
            let stdio = mcx.signal(init.stdio);
            let command = mcx.signal(init.command.clone());
            let cwd = mcx.signal(init.cwd.clone());
            let args = mcx.signal(init.args.clone());
            let url = mcx.signal(init.url.clone());
            // Headers: up to the stored ones plus two free rows.
            let mut header_sigs: Vec<(Signal<String>, Signal<String>, Option<String>)> = init
                .headers
                .iter()
                .map(|(k, v, fp)| (mcx.signal(k.clone()), mcx.signal(v.clone()), fp.clone()))
                .collect();
            for _ in 0..2 {
                header_sigs.push((mcx.signal(String::new()), mcx.signal(String::new()), None));
            }
            let form = {
                let header_sigs = header_sigs.clone();
                move || McpForm {
                    name: name.get_untracked(),
                    description: description.get_untracked(),
                    stdio: stdio.get_untracked(),
                    command: command.get_untracked(),
                    cwd: cwd.get_untracked(),
                    args: args.get_untracked(),
                    url: url.get_untracked(),
                    headers: header_sigs
                        .iter()
                        .map(|(k, v, fp)| (k.get_untracked(), v.get_untracked(), fp.clone()))
                        .collect(),
                }
            };
            // A save that succeeded closes the overlay.
            {
                let close = close.clone();
                let start = sk.form_saved.get_untracked();
                mcx.effect(move || {
                    if sk.form_saved.get() != start {
                        close();
                    }
                });
            }
            let mut col = Element::new().style(LayoutStyle::column().gap(0));
            let help = |text: &str| kit::sentence(&t, text, width, t.text_faint);
            col = col.child(match &editing {
                None => field(
                    &t,
                    "Name",
                    TextInput::new()
                        .value(name)
                        .layout(LayoutStyle::default().w(field_w.min(40)).h(1))
                        .element(mcx, &t)
                        .autofocus()
                        .build(),
                ),
                Some(n) => field(&t, "Name", line(vec![span_bold(n.clone(), t.text)])),
            });
            col = col.child(help(
                "A short name for this server (letters, digits, - _ .); its tools will be named after it.",
            ));
            col = col.child(field(
                &t,
                "Description",
                TextInput::new()
                    .value(description)
                    .layout(LayoutStyle::default().w(field_w).h(1))
                    .element(mcx, &t)
                    .build(),
            ));
            col = col.child(help(
                "What this server is for, for the admins who read this list.",
            ));
            if let Some(r) = &row {
                let reason = r.agents_block_reason().map(str::to_string);
                let status = r.agents_status.clone();
                col = col.child(kit::sentence(
                    &t,
                    &format!(
                        "Agents: {}",
                        super::switch::switch_text(
                            AGENTS_LABEL,
                            r.enabled_for_agents,
                            reason.as_deref(),
                            false,
                        )
                    ),
                    width,
                    t.text,
                ));
                if reason.is_none() && !status.is_empty() {
                    col = col.child(help(&status));
                }
            } else {
                col = col.child(kit::sentence(
                    &t,
                    &format!(
                        "Agents: {}",
                        super::switch::switch_text(
                            AGENTS_LABEL,
                            false,
                            Some("Save and test the server first: agents get the tools a successful test lists."),
                            false,
                        )
                    ),
                    width,
                    t.text_faint,
                ));
            }
            // How to reach it: Command | URL (Ctrl+T switches).
            col = col.child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = use_theme(mcx).get().tokens;
                let on = stdio.get();
                let mark = |sel: bool, label: &str| {
                    if sel {
                        span_bold(format!("[{label}]"), t.accent)
                    } else {
                        span(format!(" {label} "), t.text_muted)
                    }
                };
                line(vec![
                    span("How to reach it: ", t.text_muted),
                    mark(on, "Command"),
                    span(" ", t.text),
                    mark(!on, "URL"),
                    span("  (Ctrl+T switches)", t.text_faint),
                ])
            }));
            col = col.child(dyn_view_scoped(LayoutStyle::column().gap(0), {
                let header_sigs = header_sigs.clone();
                move |gcx| {
                    if stdio.get() {
                        reach_stdio(gcx, command, cwd, args, field_w, width)
                    } else {
                        reach_url(gcx, url, &header_sigs, field_w, width)
                    }
                }
            }));
            // The test block.
            col = col.child(dyn_view(
                LayoutStyle::column().gap(0).shrink(0.0),
                move || {
                    let t = use_theme(mcx).get().tokens;
                    match sk.form_test.get() {
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                        Some(None) => kit::sentence(
                            &t,
                            "Connecting (up to 10 seconds)...",
                            width,
                            t.text_muted,
                        ),
                        Some(Some(Err(m))) => {
                            kit::sentence(&t, &format!("Connection failed {m}"), width, t.error)
                        }
                        Some(Some(Ok(r))) => {
                            let mut c = Element::new().style(LayoutStyle::column().gap(0));
                            let m = if r.message.is_empty() {
                                "Connected.".into()
                            } else {
                                r.message.clone()
                            };
                            c = c.child(kit::sentence(&t, &m, width, t.ok));
                            for (n, d) in &r.tools {
                                let l = if d.is_empty() {
                                    n.clone()
                                } else {
                                    format!("{n} — {d}")
                                };
                                c = c.child(kit::sentence(&t, &l, width, t.text));
                            }
                            c.build()
                        }
                    }
                },
            ));
            col = col.child(dyn_view(
                LayoutStyle::column().gap(0).shrink(0.0),
                move || {
                    let t = use_theme(mcx).get().tokens;
                    match sk.form_note.get() {
                        Some(n) => kit::sentence(
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
                let form = form.clone();
                move || {
                    c.send(Cmd::Skills(SkCmd::TestMcpForm {
                        body: Body(form().body()),
                    }))
                }
            };
            let save = {
                let c = c.clone();
                let form = form.clone();
                let editing = editing.clone();
                move || {
                    c.send(Cmd::Skills(SkCmd::SaveMcp {
                        editing: editing.clone(),
                        body: Body(form().body()),
                    }))
                }
            };
            col = col.child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Test connection")
                            .on_click(test)
                            .element(mcx, &t)
                            .build(),
                    )
                    .child(Button::new("Save").on_click(save).element(mcx, &t).build())
                    .build(),
            );
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .shortcut(KeyChord::ctrl(Key::Char('t')), move |_| {
                    stdio.update(|v| *v = !*v)
                })
                .child(
                    Scroll::new(col.build())
                        .layout(LayoutStyle::default().grow(1.0).min_h(4))
                        .element(mcx, &t)
                        .build(),
                )
                .build()
        },
    );
}

fn reach_stdio(
    cx: Scope,
    command: Signal<String>,
    cwd: Signal<String>,
    args: Signal<String>,
    field_w: i32,
    width: i32,
) -> View {
    let t = use_theme(cx).get().tokens;
    let help = |text: &str| kit::sentence(&t, text, width, t.text_faint);
    let args_state = TextAreaState::new(cx);
    args_state.set_text(args.get_untracked());
    let mut p = Element::new().style(LayoutStyle::column().gap(0));
    p = p.child(help(
        "The gateway starts the server on this computer and talks to it over stdin/stdout.",
    ));
    p = p.child(field(
        &t,
        "Command",
        TextInput::new()
            .value(command)
            .placeholder("npx")
            .layout(LayoutStyle::default().w(field_w).h(1))
            .element(cx, &t)
            .build(),
    ));
    p = p.child(help(
        "The program that starts the server, for example npx or uvx.",
    ));
    p = p.child(field(
        &t,
        "Working folder",
        TextInput::new()
            .value(cwd)
            .placeholder("A scratch folder")
            .layout(LayoutStyle::default().w(field_w).h(1))
            .element(cx, &t)
            .build(),
    ));
    p = p.child(help(
        "Where the command runs. Empty: a scratch folder removed after each test.",
    ));
    p = p.child(line(vec![span("Arguments", t.text_muted)]));
    p = p.child(
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
            .layout(LayoutStyle::default().w(field_w + 19).h(4).shrink(0.0))
            .element(cx, &t)
            .build(),
    );
    p = p.child(help(
        "One argument per line, passed to the command as they are.",
    ));
    p.build()
}

fn reach_url(
    cx: Scope,
    url: Signal<String>,
    headers: &[(Signal<String>, Signal<String>, Option<String>)],
    field_w: i32,
    width: i32,
) -> View {
    let t = use_theme(cx).get().tokens;
    let help = |text: &str| kit::sentence(&t, text, width, t.text_faint);
    let mut p = Element::new().style(LayoutStyle::column().gap(0));
    p = p.child(help(
        "The server already runs somewhere and answers MCP over HTTP.",
    ));
    p = p.child(field(
        &t,
        "URL",
        TextInput::new()
            .value(url)
            .placeholder("https://example.com/mcp")
            .layout(LayoutStyle::default().w(field_w).h(1))
            .element(cx, &t)
            .build(),
    ));
    p = p.child(help("The server's MCP endpoint."));
    p = p.child(line(vec![span("Headers", t.text_muted)]));
    for (k, v, fp) in headers {
        let ph = match fp {
            Some(f) => format!(
                "Saved · fingerprint {}; type to replace",
                f.chars().take(12).collect::<String>()
            ),
            None => "Value".to_string(),
        };
        p = p.child(
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(
                    TextInput::new()
                        .value(*k)
                        .placeholder("Name, e.g. Authorization")
                        .layout(LayoutStyle::default().w(26).h(1))
                        .element(cx, &t)
                        .build(),
                )
                .child(
                    TextInput::new()
                        .value(*v)
                        .placeholder(ph)
                        .masked(true)
                        .layout(LayoutStyle::default().w((field_w - 8).max(16)).h(1))
                        .element(cx, &t)
                        .build(),
                )
                .build(),
        );
    }
    p = p.child(help(
        "Sent with every request, for example Authorization: Bearer <token>. Values are stored encrypted and never shown again. An emptied name removes that header.",
    ));
    p.build()
}
