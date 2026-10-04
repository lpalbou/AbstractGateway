//! Users & entities: gateway user CRUD (admin) + the entity roster
//! (summon `n`, talk `c`, spark templates `s`, manage `m`).
//!
//! The token rule: create/rotate responses carry the token EXACTLY
//! ONCE — it goes to a dedicated modal with a copy affordance and an
//! explicit "will not be shown again", never to logs or the journal.

use abstracttui::prelude::*;
use abstracttui::widgets::{Table, Tone};
use serde_json::{json, Value};

use super::kit::{self, InlineConfirm, Row, WrapTable};
use super::util::{badge, field, line, span, span_bold};
use super::widths;
use super::{open_form, Ctx};
use crate::store::accounts::{activity_time, AccountRow, ACTIVITY_EMPTY, ACTIVITY_FILTERS};
use crate::store::{ConnPhase, EntityRow, Loadable, UserRow};
use crate::worker::Cmd;

/// The footer verbs of this screen that only an admin may use: the users
/// registry (add / edit / rotate — `/admin/users*`) and the retained
/// runtimes (`/admin/runtime-reservations`). The entity
/// roster verbs stay open (their own admin-only acts are gated inside the
/// manage menu).
/// `d` (archive) is not here: a non-admin archives an entity they created
/// (the gateway's row says what applies).
pub const ADMIN_KEYS: &[&str] = &["a", "e", "t", "v", "x"];

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let acc = store.acc;
    let confirm = InlineConfirm::new(cx);
    let keeper = super::util::FocusKeeper::new();

    super::util::clamp_selection(cx, ui.entity_sel, move || {
        store
            .entities
            .with(|d| d.ready().map(Vec::len).unwrap_or(0))
    });
    super::util::clamp_selection(cx, ui.account_sel, move || visible_len(&store));
    // The account selection drives the entity selection: an entity row
    // selected in the one table IS the entity the inspector, Manage and
    // Talk act on (they read `entity_sel`).
    {
        let ctx_sel = ctx.clone();
        cx.effect(move || {
            let _ = ui.account_sel.get();
            let _ = acc.show_archived.get();
            store.accounts.with(|_| ());
            let Some(row) = selected_account(&ctx_sel).filter(AccountRow::is_entity) else {
                return;
            };
            let pos = store.entities.with(|d| {
                d.ready()
                    .and_then(|es| es.iter().position(|e| e.is_account(&row.id)))
            });
            if let Some(pos) = pos {
                if ui.entity_sel.get_untracked() != pos {
                    ui.entity_sel.set(pos);
                }
            }
        });
    }

    // Keep the manage snapshot warm for the SELECTED entity: arrowing
    // to a row loads its detail (worker serializes; entity rosters are
    // small), so the manage menu and the panel below open warm.
    {
        let ctx_detail = ctx.clone();
        // The name of the last detail request this effect sent — the
        // Failed arm below holds ONLY for that name, so a persistent
        // failure never loops while a selection move still loads the
        // new row (round-4 transport audit).
        let last_requested = cx.signal(Option::<String>::None);
        cx.effect(move || {
            let idx = ui.entity_sel.get();
            let name = store
                .entities
                .with(|d| d.ready().and_then(|d| d.get(idx).map(|e| e.name.clone())));
            let Some(name) = name else { return };
            if !store.conn.with_untracked(ConnPhase::is_connected) {
                return;
            }
            let held = store.entity_detail.with(|d| match d {
                Loadable::Ready(d) => d.name == name,
                Loadable::Loading => true,
                Loadable::Failed(_) => {
                    last_requested.with_untracked(|l| l.as_deref() == Some(name.as_str()))
                }
                Loadable::NotAsked => false,
            });
            if !held {
                last_requested.set(Some(name.clone()));
                store.entity_detail.set(Loadable::Loading);
                ctx_detail.send(Cmd::LoadEntityDetail { name });
            }
        });
    }
    // The administrator's email switches are read once an admin is here
    // (their tab shows them; the gateway's defaults load with the page).
    {
        use crate::worker::operator::{EmailAction, OpCmd};
        let ctx_caps = ctx.clone();
        cx.effect(move || {
            let admin = store.conn.with(ConnPhase::is_admin);
            let not_asked = store
                .op
                .email_caps
                .with(|c| matches!(c, Loadable::NotAsked));
            if admin && not_asked {
                store.op.email_caps.set(Loadable::Loading);
                ctx_caps.send(Cmd::Operator(OpCmd::Email {
                    action: EmailAction::LoadCaps,
                    form_id: None,
                }));
            }
        });
    }
    // "Open in Observer" from Logs mints a link through the Apps lane;
    // its modal opens here.
    {
        let ctx_link = ctx.clone();
        cx.effect(move || {
            if let Some(link) = ctx_link.store.apps.open_link.get() {
                if !OBSERVER_PENDING.with(|p| p.get()) || ctx_link.ui.prompt_open.get() > 0 {
                    return;
                }
                OBSERVER_PENDING.with(|p| p.set(false));
                ctx_link.store.apps.open_link.set(None);
                super::apps::open_link_modal(cx, &ctx_link, link);
            }
        });
    }

    let keys = ctx.clone();
    let root = Element::new()
        // Focusable + autofocus content root: the screen's keys must live
        // even when no table exists to take the keyboard.
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .on(abstracttui::ui::Phase::Bubble, move |ectx, ev| {
            if let abstracttui::ui::UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                if handle_key(cx, &keys, confirm, k.key) {
                    ectx.stop_propagation();
                }
            }
        });
    let root = confirm.keys(root);
    let body = ctx.clone();
    root.child(
        Block::new()
            .border(BorderKind::Rounded)
            .title(dyn_title(&store))
            .fill(t.surface)
            .layout(
                LayoutStyle::column()
                    .gap(0)
                    .grow(1.0)
                    .padding(Edges::all(1)),
            )
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let admin = store.conn.with(ConnPhase::is_admin);
                let tab = acc.tab.get();
                let mark = |on: bool, label: &str| {
                    if on {
                        span_bold(format!("[{label}]"), tt.accent)
                    } else {
                        span(format!(" {label} "), tt.text_muted)
                    }
                };
                let mut spans = vec![mark(tab == 0, "Accounts")];
                if admin {
                    spans.push(span(" ", tt.text));
                    spans.push(mark(tab == 1, "Email for everyone"));
                }
                line(spans)
            }))
            .child(dyn_view_scoped(
                LayoutStyle::column().gap(0).grow(1.0).min_h(3),
                move |gcx| {
                    if acc.tab.get() == 1 && store.conn.with(ConnPhase::is_admin) {
                        email_switches(gcx, &body, &tt)
                    } else {
                        accounts_tab(gcx, &body, &tt, &keeper)
                    }
                },
            ))
            .child(confirm.view(t, 0))
            .element(t)
            .build(),
    )
    .build()
}

thread_local! {
    /// A Logs "Open in Observer" is waiting for its one-time link.
    static OBSERVER_PENDING: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// The page title: the web's header for an admin, "Your account" otherwise.
fn dyn_title(store: &crate::store::Store) -> String {
    if store.conn.with_untracked(ConnPhase::is_known_non_admin) {
        format!("{NON_ADMIN_TITLE} — {NON_ADMIN_SUBTITLE}")
    } else {
        ACCOUNTS_TITLE.to_string()
    }
}

pub const NON_ADMIN_TITLE: &str = "Your account";
pub const NON_ADMIN_SUBTITLE: &str = "Your account and the entities you created.";

/// The rows shown (archived ones only with Show archived).
pub fn visible_accounts(store: &crate::store::Store) -> Vec<AccountRow> {
    let show = store.acc.show_archived.get_untracked();
    store.accounts.with_untracked(|d| {
        d.ready()
            .map(|rows| {
                rows.iter()
                    .filter(|r| show || !r.archived)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    })
}

fn visible_len(store: &crate::store::Store) -> usize {
    let show = store.acc.show_archived.get();
    store.accounts.with(|d| {
        d.ready()
            .map(|rows| rows.iter().filter(|r| show || !r.archived).count())
            .unwrap_or(0)
    })
}

/// The footer verbs of this page (the vec already follows the role).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let store = ctx.store;
    if store.acc.tab.get() == 1 {
        return vec![
            ("Tab", "tab"),
            ("↑/↓", "move"),
            ("space", "switch"),
            ("r", "refresh"),
        ];
    }
    let mut out = vec![
        ("Tab", "tab"),
        ("Enter", "expand row"),
        ("space", "Active"),
        ("@", "Email"),
        ("o", "OpenAI API"),
        ("l", "Logs"),
        ("w", "Workspace"),
        ("m", "Manage"),
        ("t", "Rotate token"),
        ("d", "Archive/Unarchive"),
        ("g", "Runtime"),
        ("h", "Show archived"),
        ("a", "Create user"),
        ("n", "Create entity"),
        ("e", "edit user"),
        ("c", "talk"),
        ("i", "inspect"),
        ("s", "spark templates"),
        ("v", "kept data of deleted users"),
        ("x", "reset mailbox override"),
    ];
    out.push(("r", "refresh"));
    out
}

/// One key of the page. True when handled.
fn handle_key(cx: Scope, ctx: &Ctx, confirm: InlineConfirm, key: Key) -> bool {
    let store = ctx.store;
    let acc = store.acc;
    let admin = store.conn.with_untracked(ConnPhase::is_admin);
    if matches!(key, Key::Tab | Key::Char('[') | Key::Char(']')) {
        if admin {
            acc.tab
                .set(if acc.tab.get_untracked() == 0 { 1 } else { 0 });
        }
        return true;
    }
    if acc.tab.get_untracked() == 1 {
        return false;
    }
    match key {
        Key::Char(' ') => switch_selected_active(cx, ctx, confirm),
        Key::Char('h') => {
            if super::util::admin_gate(&store, "showing archived accounts") {
                acc.show_archived.update(|v| *v = !*v);
            }
        }
        Key::Char('m') => manage_selected_entity(cx, ctx),
        Key::Char('n') => {
            if store.conn.with_untracked(ConnPhase::is_connected) {
                super::entity_create::open_summon_form(cx, ctx);
            } else {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            }
        }
        Key::Char('c') => {
            if !store.conn.with_untracked(ConnPhase::is_connected) {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            } else if let Some(e) = selected_entity(ctx) {
                super::entity_chat::open_talk_modal(cx, ctx, e.name);
            } else {
                store
                    .notice
                    .set(Some("no entity selected — nobody to talk to".into()));
            }
        }
        Key::Char('s') => {
            if store.conn.with_untracked(ConnPhase::is_connected) {
                super::entity_create::open_templates_modal(cx, ctx);
            } else {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            }
        }
        Key::Char('v') => {
            if super::util::admin_gate(&store, "the kept data of deleted users") {
                open_reservations_modal(cx, ctx);
            }
        }
        Key::Char('w') => workspace_selected(cx, ctx),
        Key::Char('g') => runtime_selected(ctx),
        Key::Char('@') => email_selected(cx, ctx),
        Key::Char('l') => open_activity(cx, ctx),
        Key::Char('o') => openai_selected(cx, ctx),
        Key::Char('x') => {
            if !super::util::admin_gate(&store, "resetting a user's mailbox override") {
                return true;
            }
            match selected_user(ctx) {
                Some(u) if u.mailbox_view().1 => {
                    ctx.send(crate::worker::Cmd::Operator(
                        crate::worker::operator::OpCmd::Email {
                            action: crate::worker::operator::EmailAction::AdminResetMailbox {
                                user_id: u.user_id.clone(),
                                tenant_id: u.tenant_id.clone(),
                            },
                            form_id: None,
                        },
                    ));
                }
                Some(u) => store.notice.set(Some(format!(
                    "{} has no mailbox override — nothing to reset",
                    u.user_id
                ))),
                None => store
                    .notice
                    .set(Some("no user selected — nothing to reset".into())),
            }
        }
        Key::Char('i') => {
            if selected_entity(ctx).is_some() {
                if let Some(h) = ctx.entity_drawer.borrow().as_ref() {
                    h.toggle();
                }
            } else {
                store
                    .notice
                    .set(Some("no entity selected — nothing to inspect".into()));
            }
        }
        Key::Char('a') => {
            if super::util::admin_gate(&store, "creating a user") {
                open_user_form(cx, ctx, None);
            }
        }
        Key::Char('e') => edit_selected_user(cx, ctx),
        Key::Char('t') => rotate_selected(cx, ctx),
        Key::Char('d') => archive_selected(ctx, confirm),
        _ => return false,
    }
    true
}

/// The Accounts tab: the toolbar, the one table, the legend.
fn accounts_tab(cx: Scope, ctx: &Ctx, tt: &TokenSet, keeper: &super::util::FocusKeeper) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let acc = store.acc;
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let width = (vw - 4).max(20);
    let admin = store.conn.with(ConnPhase::is_admin);
    let show = acc.show_archived.get();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    // RBAC (operator ruling 2026-10-01): a non-admin's table is themself
    // + the entities they created (`/me/accounts`).
    let scope_note = store.conn.with(|c| match c {
        ConnPhase::Connected(id) | ConnPhase::Verifying(id) if !id.admin => {
            Some(non_admin_scope_line(&id.user_id))
        }
        _ => None,
    });
    if let Some(note) = scope_note {
        col = col.child(kit::sentence(tt, &note, width, tt.text_muted));
    }
    let toolbar = if admin {
        format!(
            "{}   a Create user · n Create entity",
            super::switch::switch_text("Show archived", show, None, false)
        )
    } else {
        "n Create entity".to_string()
    };
    col = col.child(kit::sentence(tt, &toolbar, width, tt.text));
    let data = store.accounts.get();
    match data {
        Loadable::NotAsked | Loadable::Loading => {
            col = col.child(keeper.anchor(kit::sentence(tt, "Loading…", width, tt.text_muted)));
        }
        Loadable::Failed(e) => {
            col = col.child(keeper.anchor(super::util::error_panel(tt, &e)));
        }
        Loadable::Ready(all) => {
            let rows: Vec<AccountRow> = all.into_iter().filter(|r| show || !r.archived).collect();
            // R8.2: Name · Email (ONE column: address + connection state)
            // · Runtime (g opens the Runtimes page filtered to it) · Active
            // — at every width; cells wrap, nothing scrolls sideways.
            let rules = vec![
                widths::ColRule::tail("Name", 10),
                widths::ColRule::head("Email", 16),
                widths::ColRule::tail("Runtime", 8),
                widths::ColRule::head("Active", 6),
            ];
            let own = own_key(&store);
            let table_rows: Vec<Row> = rows
                .iter()
                .map(|r| account_row(r, false, own.as_ref()))
                .collect();

            let ctx_act = ctx.clone();
            col = col.child(
                keeper.wire(
                    WrapTable::new(rules, table_rows, ui.account_sel)
                        .expanded(acc.expanded)
                        .empty("No accounts yet.")
                        .on_activate(move |_| activate_selected(cx, &ctx_act))
                        .element(cx, tt),
                ),
            );
            // The highlighted row's actions in ONE line — the web's icon
            // buttons (no "⋯" menu): each key, in the web's order.
            // Its own reactive line: a selection move must not rebuild
            // the table (a double-click's second press needs the same
            // table instance as its first).
            let t2 = *tt;
            col = col.child(dyn_view(
                LayoutStyle::column().gap(0).shrink(0.0),
                move || {
                    let sel = ui.account_sel.get();
                    match rows.get(sel) {
                        Some(r) => kit::sentence(
                            &t2,
                            &format!("{}: {}", r.id, row_actions(r).join(" · ")),
                            width,
                            t2.accent,
                        ),
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                },
            ));
        }
    }
    col = col.child(kind_legend(tt));
    col.build()
}

/// The name cell: `tenant/id` off the default tenant, then the kind badge
/// (and Archived) in words.
fn name_cell(r: &AccountRow) -> String {
    let id = if r.tenant_id != "default" {
        format!("{}/{}", r.tenant_id, r.id)
    } else {
        r.id.clone()
    };
    let mut out = format!("{id}\n{}", r.kind_label());
    if r.archived {
        out.push_str(" · Archived");
    }
    out
}

/// The Active cell: `[x]` / `[ ]`, `[-]` when it can't be switched here
/// (the reason is in the row's detail and said on Space), "Archived".
fn active_cell(r: &AccountRow) -> String {
    if r.archived {
        return "Archived".into();
    }
    super::switch::marker(r.active, r.refusal("suspend").is_some()).to_string()
}

/// One table row with its detail lines: what the actions are, which keys
/// they are on, and why the others can't apply (visible, never
/// tooltip-only).
fn account_row(r: &AccountRow, narrow: bool, _own: Option<&(String, String)>) -> Row {
    let _ = narrow;
    let runtime = r.runtime_id.clone().unwrap_or_else(|| "No runtime".into());
    let cells = vec![name_cell(r), email_cell(r), runtime, active_cell(r)];
    let mut detail = vec![crate::store::accounts::kind_help(r.kind_label()).to_string()];
    if r.mailbox.state == "receive_only" {
        if let Some(why) = &r.mailbox.reason {
            detail.push(why.clone());
        }
    }
    if r.archived {
        detail.push("Archived: can't sign in or act; runs and history are kept.".into());
    }
    if let Some(why) = r.refusal("suspend").filter(|_| !r.archived) {
        detail.push(format!("Active: {why}"));
    }
    if let Some(m) = mailbox_detail(r) {
        detail.push(m);
    }
    let names: &[(&str, &str)] = if r.archived {
        &[]
    } else if r.is_entity() {
        &[
            ("email", "Email"),
            ("manage", "Manage"),
            ("rotate", "Rotate token"),
            ("archive", "Archive"),
        ]
    } else {
        &[
            ("email", "Email"),
            ("workspace", "Workspace"),
            ("rotate", "Rotate token"),
            ("archive", "Archive"),
        ]
    };
    for (key, label) in names {
        if let Some(why) = r.refusal(key) {
            detail.push(format!("{label}: {why}"));
        }
    }
    Row::new(cells).detail(detail).dim(r.archived || !r.active)
}

/// The Email cell (R8.2: ONE column): the address and the mailbox's
/// connection state — `test@x · connected`, `No address`.
pub fn email_cell(r: &AccountRow) -> String {
    let state = match r.mailbox.state.as_str() {
        "connected" => "connected",
        "receive_only" => "receive only",
        "not_connected" => "not connected",
        "paused" => "paused",
        "unavailable" => "not available",
        _ => "",
    };
    let address = r.email_address.as_ref().or(r.mailbox.address.as_ref());
    match (address, state) {
        (None, _) => "No address".into(),
        (Some(a), "") => a.clone(),
        (Some(a), s) => format!("{a} · {s}"),
    }
}

/// The detail line naming the mailbox in full when the cell abbreviates
/// it (a different connected address, or a receive-only reason).
fn mailbox_detail(r: &AccountRow) -> Option<String> {
    match (
        r.mailbox.state.as_str(),
        &r.mailbox.address,
        &r.email_address,
    ) {
        ("connected", Some(m), Some(a)) if m != a => Some(format!("Mailbox: connected as {m}")),
        ("connected", Some(m), None) => Some(format!("Mailbox: connected as {m}")),
        _ => None,
    }
}

/// The row's actions as keys, in the web's icon order (users: Email ·
/// OpenAI API · Logs · Workspace · Rotate · Archive; entities: Email ·
/// Logs · Manage · Archive), then Runtime and Active — only the ones that
/// apply (why the others don't is in the row's detail, Enter).
pub fn row_actions(r: &AccountRow) -> Vec<String> {
    let mut keys: Vec<String> = Vec::new();
    let mut add = |avail: bool, k: &str| {
        if avail {
            keys.push(k.to_string());
        }
    };
    if r.archived {
        add(r.refusal("logs").is_none(), "l Logs");
        add(r.refusal("unarchive").is_none(), "d Unarchive");
        return keys;
    }
    add(r.refusal("email").is_none(), "@ Email");
    if let Some(a) = &r.openai_action {
        add(
            a.available,
            if r.openai_api {
                "o OpenAI API (on)"
            } else {
                "o OpenAI API (off)"
            },
        );
    }
    add(r.refusal("logs").is_none(), "l Logs");
    if r.is_entity() {
        add(r.refusal("manage").is_none(), "m Manage");
    } else {
        add(r.refusal("workspace").is_none(), "w Workspace");
        add(r.refusal("rotate").is_none(), "t Rotate");
    }
    add(r.refusal("archive").is_none(), "d Archive");
    add(r.runtime_id.is_some(), "g Runtime");
    add(r.refusal("suspend").is_none(), "space Active");
    keys
}

/// `g`: the Runtimes page filtered to the selected account (the web's
/// Runtime link, `#runtimes?account=<id>`).
fn runtime_selected(ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "the Runtimes page") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — no runtime to show".into()));
        return;
    };
    super::runtimes::show_account(ctx, &r.id, &r.tenant_id);
    ctx.ui.screen.set(super::SCREEN_RUNTIMES);
}

/// The screen's block title (DESIGN-v2 §2.1: the page line, in words).
pub const ACCOUNTS_TITLE: &str =
    "Accounts — people who use this gateway and the entities that act on it";

/// The selected row of the one table (admin view).
pub fn selected_account(ctx: &Ctx) -> Option<AccountRow> {
    let idx = ctx.ui.account_sel.get_untracked();
    visible_accounts(&ctx.store).get(idx).cloned()
}

/// Is `row` the signed-in principal's own account?
fn is_own(store: &crate::store::Store, row: &AccountRow) -> bool {
    own_key(store) == Some((row.id.clone(), row.tenant_id.clone()))
}

/// The accounts table is the selection surface for everyone: an admin's
/// rows are every account, a non-admin's are themself + the entities they
/// created (`/me/accounts`).
fn uses_accounts(_ctx: &Ctx) -> bool {
    true
}

/// The line above a non-admin's table: whose view it is and why it is
/// short (the gateway enforces the same rule on every entity route).
pub fn non_admin_scope_line(user_id: &str) -> String {
    format!(
        "Signed in as {user_id}, not an admin: you see your own account and the entities you created."
    )
}

fn selected_entity(ctx: &Ctx) -> Option<EntityRow> {
    if uses_accounts(ctx) {
        let row = selected_account(ctx).filter(AccountRow::is_entity)?;
        return ctx.store.entities.with_untracked(|d| {
            d.ready()
                .and_then(|es| es.iter().find(|e| e.is_account(&row.id)).cloned())
        });
    }
    let idx = ctx.ui.entity_sel.get_untracked();
    ctx.store
        .entities
        .with_untracked(|d| d.ready().and_then(|d| d.get(idx).cloned()))
}

/// The registry record of the selected USER row (edit / rotate / delete
/// / reset act on it).
fn selected_user(ctx: &Ctx) -> Option<UserRow> {
    let row = selected_account(ctx).filter(|r| !r.is_entity())?;
    ctx.store.users.with_untracked(|d| {
        d.ready().and_then(|u| {
            u.humans
                .iter()
                .find(|h| h.user_id == row.id && h.tenant_id == row.tenant_id)
                .cloned()
        })
    })
}

/// ONE edit entry — shared by the `e` key and a user row's activation.
fn edit_selected_user(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "editing a user") {
        return;
    }
    match selected_account(ctx) {
        Some(r) if r.is_entity() => ctx.store.notice.set(Some(format!(
            "{} is an entity — m manages it (entities have no user record to edit)",
            r.id
        ))),
        Some(r) => match selected_user(ctx) {
            Some(u) => open_user_form(cx, ctx, Some(u)),
            None => ctx.store.notice.set(Some(format!(
                "the users registry has not loaded {} yet — r refreshes",
                r.id
            ))),
        },
        None => ctx
            .store
            .notice
            .set(Some("no account selected — nothing to edit".into())),
    }
}

/// Enter / double-click on a row: a user → edit, an entity → Manage.
fn activate_selected(cx: Scope, ctx: &Ctx) {
    match selected_account(ctx) {
        Some(r) if r.is_entity() => manage_selected_entity(cx, ctx),
        Some(_) => edit_selected_user(cx, ctx),
        None => {}
    }
}

/// ONE manage entry — the `m` key and an entity row's activation.
fn manage_selected_entity(cx: Scope, ctx: &Ctx) {
    if uses_accounts(ctx) {
        if let Some(r) = selected_account(ctx) {
            if let Some(why) = r.refusal("manage") {
                ctx.store.notice.set(Some(why));
                return;
            }
            if !r.is_entity() {
                ctx.store
                    .notice
                    .set(Some(format!("Manage is for entities — e edits {}", r.id)));
                return;
            }
        }
    }
    if let Some(e) = selected_entity(ctx) {
        super::entity_manage::open_manage_menu(cx, ctx, e);
    } else {
        ctx.store
            .notice
            .set(Some("no entity selected — nothing to manage".into()));
    }
}

/// `w`: the selected account's workspace policy — your own on your row
/// (and always for a non-admin), a user's own policy as the admin, the
/// reason for an entity.
fn workspace_selected(_cx: Scope, ctx: &Ctx) {
    // R8.2: the policy lives on its own page now — the Workspace action
    // opens Workspaces focused on that account (the web's
    // `#workspaces?account=<id>`); a non-admin's page is their own policy.
    let row = selected_account(ctx);
    let admin = ctx.store.conn.with_untracked(ConnPhase::is_admin);
    match row {
        Some(r) if r.is_entity() => match r.refusal("workspace") {
            Some(why) => ctx.store.notice.set(Some(why)),
            None => ctx.store.notice.set(Some(format!(
                "{}'s file access is set on the entity itself (workspace mounts) — m manages it",
                r.id
            ))),
        },
        Some(r) if r.refusal("workspace").is_some() && !is_own(&ctx.store, &r) => {
            ctx.store.notice.set(r.refusal("workspace"))
        }
        Some(r) => {
            if admin {
                super::workspaces::focus_account(ctx, &r.tenant_id, &r.id);
            }
            ctx.ui.screen.set(super::SCREEN_WORKSPACES);
        }
        None => ctx.ui.screen.set(super::SCREEN_WORKSPACES),
    }
}

/// `@`: Email. Your row → the full account email view; another user's
/// row → the address-only view (you never touch anyone's mailbox); an
/// entity → why it has none.
fn email_selected(cx: Scope, ctx: &Ctx) {
    let row = if uses_accounts(ctx) {
        selected_account(ctx)
    } else {
        None
    };
    match row {
        None => super::my_email::open(cx, ctx),
        Some(r) if is_own(&ctx.store, &r) => super::my_email::open(cx, ctx),
        // An entity's mailbox is its own: the full Email form on the
        // gateway's `/accounts/{id}/email` mirror (admin or its creator).
        Some(r) if r.is_entity() => match r.refusal("email") {
            Some(why) => ctx.store.notice.set(Some(why)),
            None => super::my_email::open_entity(cx, ctx, r.id.clone()),
        },
        // Entities are AI users with their own mailbox (round 3): the same
        // read-only view as another user's; it is set up in the web console.
        Some(r) => match r.refusal("email") {
            Some(why) => ctx.store.notice.set(Some(why)),
            None => {
                let line = other_mailbox_line(&r);
                super::my_email::open_other(
                    cx,
                    ctx,
                    r.id.clone(),
                    r.tenant_id.clone(),
                    r.email_address.clone(),
                    line,
                );
            }
        },
    }
}

/// Another user's mailbox, read-only, in the web's words.
pub fn other_mailbox_line(r: &AccountRow) -> String {
    match r.mailbox.state.as_str() {
        "connected" => match &r.mailbox.address {
            Some(a) => format!("Connected as {a}."),
            None => "Connected.".to_string(),
        },
        "paused" => format!("Paused — {} turned their mailbox off.", r.id),
        "unavailable" => r
            .mailbox
            .reason
            .clone()
            .unwrap_or_else(|| "Mailboxes are not available for this account.".into()),
        _ => format!(
            "Not connected — only {} can connect a mailbox. You never see anyone's mail.",
            r.id
        ),
    }
}

/// `t`: rotate the selected account's token (a user: the registry's
/// rotate; an entity: only where the gateway offers it).
fn rotate_selected(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "rotating a token") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — no token to rotate".into()));
        return;
    };
    // Entities have no token (the gateway's `rotate` reason says so).
    if let Some(why) = r.refusal("rotate") {
        ctx.store.notice.set(Some(why));
        return;
    }
    if let Some(u) = selected_user(ctx) {
        confirm_rotate(cx, ctx, u);
    } else {
        ctx.store.notice.set(Some(format!(
            "the users registry has not loaded {} yet — r refreshes",
            r.id
        )));
    }
}

/// `d`: archive the selected account, or unarchive an archived one
/// (round 3: accounts are archived, never deleted). The gateway's row says
/// which applies (`actions.archive` / `actions.unarchive`) and why not.
fn archive_selected(ctx: &Ctx, confirm: InlineConfirm) {
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — nothing to archive".into()));
        return;
    };
    let verb = if r.archived { "unarchive" } else { "archive" };
    if let Some(why) = r.refusal(verb) {
        ctx.store.notice.set(Some(why));
        return;
    }
    let admin = ctx.store.conn.with_untracked(|c| c.is_admin());
    if r.archived {
        // Unarchive applies at once: the account comes back INACTIVE.
        ctx.send(Cmd::ArchiveAccount {
            id: r.id,
            tenant_id: r.tenant_id,
            unarchive: true,
            admin,
        });
    } else {
        confirm_archive(ctx, confirm, r, admin);
    }
}

/// `k`: the account's OpenAI API switch (the web's "OpenAI API — <id>"
/// dialog: one switch, applied at once).
fn openai_selected(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "changing who may use the OpenAI API") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
        ctx.store.notice.set(Some("no account selected".into()));
        return;
    };
    match &r.openai_action {
        None => {
            ctx.store.notice.set(Some(
                "this gateway does not offer the OpenAI API switch".into(),
            ));
            return;
        }
        Some(a) if !a.available => {
            ctx.store.notice.set(Some(
                a.reason
                    .clone()
                    .unwrap_or_else(|| "The OpenAI API switch is not available here.".into()),
            ));
            return;
        }
        _ => {}
    }
    let id = r.id.clone();
    let c = ctx.clone();
    kit::open_overlay(
        ctx,
        cx,
        format!("OpenAI API — {id}"),
        &[("space", "switch")],
        move |mcx, _close, _guard| {
            let t = use_theme(mcx).get().tokens;
            let width = abstracttui::app::use_viewport(mcx).get_untracked().w - 6;
            let store = c.store;
            let on = mcx.signal(r.openai_api);
            // The shown state is the gateway's: republished from the
            // accounts list after each verified write.
            {
                let id = id.clone();
                mcx.effect(move || {
                    if let Some(row) = store.accounts.with(|d| {
                        d.ready()
                            .and_then(|rows| rows.iter().find(|a| a.id == id).cloned())
                    }) {
                        if on.get_untracked() != row.openai_api {
                            on.set(row.openai_api);
                        }
                    }
                });
            }
            let request = {
                let c = c.clone();
                let r = r.clone();
                move |want: bool| {
                    c.send(Cmd::SetAccountOpenAi {
                        id: r.id.clone(),
                        tenant_id: r.tenant_id.clone(),
                        enabled: want,
                    })
                }
            };
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(kit::sentence(
                    &t,
                    &format!(
                        "Lets {id} use the OpenAI-compatible API (/v1) with their own gateway token as the key. Off: that key is refused there; signing in to the console is unchanged."
                    ),
                    width,
                    t.text_muted,
                ))
                .child(
                    super::switch::Switch::new("OpenAI API", on)
                        .notice(store.notice)
                        .on_request(request)
                        .fill()
                        .element(mcx, &t)
                        .autofocus()
                        .build(),
                )
                .build()
        },
    );
}

/// The archive confirm, in the web's words (DESIGN-v3 §1.3).
pub fn archive_question(r: &AccountRow) -> String {
    if r.is_entity() {
        format!(
            "Archive {}? It stops acting and never wakes. Its memory, runs and history are kept; you can unarchive later.",
            r.id
        )
    } else {
        format!(
            "Archive {}? They can't sign in any more. Their runtime, runs and history are kept; you can unarchive later.",
            r.id
        )
    }
}

/// The web's inline confirm under the row: the sentence, `[y] Archive`,
/// `[n] Keep`.
fn confirm_archive(ctx: &Ctx, confirm: InlineConfirm, r: AccountRow, admin: bool) {
    let ctx2 = ctx.clone();
    confirm.ask(archive_question(&r), "Archive", move || {
        ctx2.send(Cmd::ArchiveAccount {
            id: r.id.clone(),
            tenant_id: r.tenant_id.clone(),
            unarchive: false,
            admin,
        })
    });
}

/// The Active switch of the selected account: OFF asks first (a user is
/// signed out; an entity stops acting), ON applies at once; an
/// unavailable switch (your own account) says why.
fn switch_selected_active(_cx: Scope, ctx: &Ctx, confirm: InlineConfirm) {
    if !super::util::admin_gate(&ctx.store, "switching an account's Active state") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — nothing to switch".into()));
        return;
    };
    if is_own(&ctx.store, &r) {
        ctx.store.notice.set(Some(
            r.refusal("suspend")
                .unwrap_or_else(|| OWN_ACCOUNT_REASON.to_string()),
        ));
        return;
    }
    if let Some(why) = r.refusal("suspend") {
        ctx.store.notice.set(Some(why));
        return;
    }
    let send = {
        let ctx = ctx.clone();
        let r = r.clone();
        move |active: bool| {
            ctx.send(Cmd::SetAccountActive {
                id: r.id.clone(),
                tenant_id: r.tenant_id.clone(),
                entity: r.is_entity(),
                active,
            })
        }
    };
    if r.active {
        let (question, verb) = if r.is_entity() {
            (
                format!(
                    "Suspend {}? It stops acting until you turn Active back on.",
                    r.id
                ),
                "Suspend",
            )
        } else {
            (
                format!(
                    "Deactivate {}? They are signed out until you turn Active back on.",
                    r.id
                ),
                "Deactivate",
            )
        };
        confirm.ask(question, verb, move || send(false));
    } else {
        send(true);
    }
}

/// The kind chip's tone: admin accent, user neutral, entity info — the
/// web's row tints, as colour on the chip (the engine Table has no
/// per-row ink).
pub fn kind_tone(label: &str) -> Tone {
    match label {
        "Admin" => Tone::Accent,
        "Entity" => Tone::Info,
        _ => Tone::Muted,
    }
}

fn kind_legend(t: &TokenSet) -> View {
    Element::new()
        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
        .child(super::util::line_styled(
            LayoutStyle::default().w(5).h(1).shrink(0.0),
            vec![span("kind:", t.text_faint)],
        ))
        .child(badge(t, "Admin", kind_tone("Admin")))
        .child(badge(t, "User", kind_tone("User")))
        .child(badge(t, "Entity", kind_tone("Entity")))
        .child(line(vec![span(String::new(), t.text_faint)]))
        .build()
}

/// `l`: the activity of the selected account (admin) or your own.
fn open_activity(cx: Scope, ctx: &Ctx) {
    let target = if uses_accounts(ctx) {
        match selected_account(ctx) {
            Some(r) => {
                if let Some(why) = r.refusal("logs") {
                    ctx.store.notice.set(Some(why));
                    return;
                }
                if is_own(&ctx.store, &r) {
                    None
                } else {
                    Some((r.id.clone(), r.tenant_id.clone()))
                }
            }
            None => None,
        }
    } else {
        None
    };
    let who = match (&target, own_key(&ctx.store)) {
        (Some((id, _)), _) => id.clone(),
        (None, Some((id, _))) => id,
        (None, None) => "you".to_string(),
    };
    let key = target
        .as_ref()
        .map(|(i, t)| format!("{t}/{i}"))
        .unwrap_or_else(|| "me".to_string());
    let filter = ctx.ui.activity_filter;
    filter.set(0);
    // A non-admin reads its own entities' activity through /me/accounts.
    let mine = ctx.store.conn.with_untracked(ConnPhase::is_known_non_admin);
    ctx.send(Cmd::LoadActivity {
        target: target.clone(),
        mine,
        key: key.clone(),
        kind: String::new(),
    });
    let ctx2 = ctx.clone();
    super::open_form(
        ctx,
        cx,
        abstracttui::app::use_viewport(cx).get_untracked(),
        move |mcx, _close| {
            let theme = use_theme(mcx);
            let store = ctx2.store;
            let cycle = {
                let ctx3 = ctx2.clone();
                let target = target.clone();
                let key = key.clone();
                move |dir: isize| {
                    let n = ACTIVITY_FILTERS.len() as isize;
                    let next = (filter.get_untracked() as isize + dir).rem_euclid(n) as usize;
                    filter.set(next);
                    ctx3.send(Cmd::LoadActivity {
                        target: target.clone(),
                        mine,
                        key: key.clone(),
                        kind: ACTIVITY_FILTERS[next].1.to_string(),
                    });
                }
            };
            let cycle_b = cycle.clone();
            let sel = mcx.signal(0usize);
            // The list regenerates on each filter: the keeper hands the
            // keyboard (and with it Esc) to each new instance.
            let keeper = super::util::FocusKeeper::new();
            let open_obs = {
                let ctx_o = ctx2.clone();
                move || {
                    let ev = store.activity.with_untracked(|a| match a {
                        Some((_, _, Loadable::Ready(d))) => {
                            d.events.get(sel.get_untracked()).cloned()
                        }
                        _ => None,
                    });
                    let Some(ev) = ev else { return };
                    let path = match (&ev.observer_path, &ev.run_id) {
                        (Some(p), _) => p.clone(),
                        (None, Some(id)) => format!("/apps/observer/#run/{id}"),
                        _ => {
                            ctx_o
                                .store
                                .notice
                                .set(Some("this event has no run to open".into()));
                            return;
                        }
                    };
                    OBSERVER_PENDING.with(|p| p.set(true));
                    ctx_o.send(Cmd::AppAct {
                        app_id: "observer".into(),
                        name: "AbstractObserver".into(),
                        verb: crate::store::apps::AppVerb::Open,
                        path: Some(path),
                        start_first: false,
                    });
                }
            };
            Element::new()
                .focusable()
                .autofocus()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .shortcut(KeyChord::plain(Key::Char('f')), move |_| cycle(1))
                .shortcut(KeyChord::plain(Key::Char('F')), move |_| cycle_b(-1))
                .shortcut(KeyChord::plain(Key::Char('o')), move |_| open_obs())
                .child(dyn_view(LayoutStyle::line(1), {
                    let who = who.clone();
                    move || {
                        let t = theme.get().tokens;
                        line(vec![span_bold(format!("Activity — {who}"), t.accent)])
                    }
                }))
                // The filter chips: the current one lit (f / F cycle).
                .child(dyn_view(LayoutStyle::line(1), move || {
                    let t = theme.get().tokens;
                    let cur = filter.get();
                    let mut spans = vec![];
                    for (i, (label, _)) in ACTIVITY_FILTERS.iter().enumerate() {
                        if i > 0 {
                            spans.push(span(" · ", t.text_faint));
                        }
                        if i == cur {
                            spans.push(span_bold(format!("[{label}]"), t.accent));
                        } else {
                            spans.push(span(label.to_string(), t.text_muted));
                        }
                    }
                    line(spans)
                }))
                .child(dyn_view_scoped(
                    LayoutStyle::column().grow(1.0).min_h(3),
                    move |gcx| {
                        let t = theme.get().tokens;
                        let width = abstracttui::app::use_viewport(gcx).get().w - 6;
                        match store.activity.get().map(|(_, _, d)| d) {
                            None | Some(Loadable::NotAsked) | Some(Loadable::Loading) => {
                                keeper.anchor(line(vec![span("Loading…", t.text_muted)]))
                            }
                            Some(Loadable::Failed(e)) => keeper.anchor(kit::sentence(
                                &t,
                                &crate::worker::skills::refusal_text(&e),
                                width,
                                t.error,
                            )),
                            Some(Loadable::Ready(d)) if d.events.is_empty() => keeper
                                .anchor(kit::sentence(&t, ACTIVITY_EMPTY, width, t.text_muted)),
                            Some(Loadable::Ready(d)) => {
                                keeper.wire(activity_table(gcx, &t, &d, sel))
                            }
                        }
                    },
                ))
                .child(dyn_view_scoped(
                    LayoutStyle::column().gap(0).shrink(0.0),
                    move |gcx| {
                        let t = theme.get().tokens;
                        let width = abstracttui::app::use_viewport(gcx).get().w - 6;
                        let mut col =
                            Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                        if let Some((_, _, Loadable::Ready(d))) = store.activity.get() {
                            if let Some(note) = d.note.clone().filter(|n| !n.trim().is_empty()) {
                                col =
                                    col.child(kit::sentence(&t, note.trim(), width, t.text_faint));
                            }
                        }
                        col.child(kit::key_hint_bar(
                            &t,
                            &[
                                ("f / F", "filter"),
                                ("Enter", "expand"),
                                ("o", "Open in Observer"),
                                ("esc", "close"),
                            ],
                            width,
                        ))
                        .build()
                    },
                ))
                .build()
        },
    );
}

/// One row per event, newest first (time · event · detail), wrapping;
/// the run and its Observer link in the row's detail.
fn activity_table(
    cx: Scope,
    t: &TokenSet,
    d: &crate::store::accounts::ActivityData,
    sel: Signal<usize>,
) -> Element {
    let today = crate::localtime::local_today();
    let rows: Vec<Row> = d
        .events
        .iter()
        .map(|e| {
            let title = if e.ok {
                e.title.clone()
            } else {
                format!("✗ {}", e.title)
            };
            let mut detail = Vec::new();
            if let Some(id) = &e.run_id {
                detail.push(format!("Run {id} — o Open in Observer"));
            } else if e.observer_path.is_some() {
                detail.push("o Open in Observer".into());
            }
            Row::new(vec![
                activity_time(&e.ts, &today),
                title,
                e.detail.clone().unwrap_or_default(),
            ])
            .detail(detail)
            .dim(!e.ok)
        })
        .collect();
    let expanded = cx.signal(None);
    WrapTable::new(
        vec![
            widths::ColRule::head("Time", 11),
            widths::ColRule::head("Event", 16),
            widths::ColRule::head("Detail", 20),
        ],
        rows,
        sel,
    )
    .expanded(expanded)
    .element(cx, t)
}

/// Retained runtime planes (a user moved to another runtime, or a deleted
/// user before 0.11): transfer to a living user. Never purged — accounts
/// and their data are archived, never deleted (round 3).
/// The retained-runtimes dialog's declared width, and what its chrome
/// spends: the Modal's own 1-cell margin plus the dress Block's border
/// and padding, left and right (`ui::open_form_guarded`). Budgeting the
/// grid inside it starts here, not at the viewport.
const RESV_MODAL_W: i32 = 88;
const RESV_MODAL_CHROME: i32 = 4;

fn open_reservations_modal(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    store.reservations.set(crate::store::Loadable::Loading);
    ctx.send(Cmd::LoadReservations);
    let ctx2 = ctx.clone();
    let screen_cx = cx;
    super::open_form(
        ctx,
        cx,
        abstracttui::app::use_viewport(cx).get_untracked(),
        move |mcx, close| {
            let theme = use_theme(mcx);
            let ui = ctx2.ui;
            let ctx3 = ctx2.clone();
            let close_b = close.clone();
            let target = mcx.signal(String::new());
            // F8: rows shrink by this modal's own action (transfer) —
            // an unclamped stranded index would dead-end the reopened modal.
            super::util::clamp_selection(mcx, ui.resv_sel, move || {
                store
                    .reservations
                    .with(|d| d.ready().map(Vec::len).unwrap_or(0))
            });
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(dyn_view(LayoutStyle::line(1), move || {
                    let t = theme.get().tokens;
                    line(vec![span_bold(
                        "Retained runtimes — transfer to a user (data is never deleted)"
                            .to_string(),
                        t.accent,
                    )])
                }))
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                    move |gcx| {
                        let t = theme.get().tokens;
                        match store.reservations.get() {
                            Loadable::NotAsked | Loadable::Loading => {
                                line(vec![span("⟳ loading reservations…", t.info)])
                            }
                            Loadable::Failed(e) => super::util::error_panel_hint(
                                &t,
                                &e,
                                Some("close and reopen this dialog to retry (opening re-reads)"),
                            ),
                            Loadable::Ready(rows) if rows.is_empty() => line(vec![span(
                                "no retained runtimes — deleting a user creates one",
                                t.text_muted,
                            )]),
                            Loadable::Ready(rows) => {
                                // THIS GRID IS IN A MODAL, not on the page:
                                // its budget comes from the modal's own
                                // width (clipped by a smaller terminal),
                                // never from the viewport — over-budgeting
                                // here would clamp the last columns to
                                // nothing.
                                let vw = RESV_MODAL_W
                                    .min(abstracttui::app::use_viewport(gcx).get().w)
                                    - RESV_MODAL_CHROME;
                                let mut table_rows: Vec<Vec<String>> = rows
                                    .iter()
                                    .map(|r| {
                                        vec![
                                            r.runtime_id.clone(),
                                            r.tenant_id.clone(),
                                            r.owner_user_id.clone(),
                                            r.reason.clone(),
                                            if r.data_exists {
                                                "on disk".into()
                                            } else {
                                                "no data".into()
                                            },
                                        ]
                                    })
                                    .collect();
                                // Ids discriminate on their TAIL; the reason
                                // and the on-disk answer are bounded words.
                                let rules = [
                                    widths::ColRule::tail("runtime", 16),
                                    widths::ColRule::tail("tenant", 8),
                                    widths::ColRule::tail("was owned by", 12),
                                    widths::ColRule::head("reason", 12),
                                    widths::ColRule::head("data", 7),
                                ];
                                let cols = widths::columns(&rules, &mut table_rows, vw);
                                Table::new(cols)
                                    .rows(table_rows)
                                    .selection(ui.resv_sel)
                                    .layout(LayoutStyle::default().grow(1.0))
                                    .element(gcx, &t)
                                    .autofocus()
                                    .build()
                            }
                        }
                    }
                }))
                .child(dyn_view_scoped(LayoutStyle::default().h(1).shrink(0.0), {
                    let theme2 = theme;
                    move |fcx| {
                        let t = theme2.get().tokens;
                        field(
                            &t,
                            "transfer to",
                            TextInput::new()
                                .value(target)
                                .placeholder("existing user id (for Transfer)")
                                .placeholder_while_focused(true)
                                .layout(LayoutStyle::default().w(30).h(1))
                                .element(fcx, &t)
                                .build(),
                        )
                    }
                }))
                .child(dyn_view_scoped(
                    LayoutStyle::default().h(1).shrink(0.0),
                    move |bcx| {
                        let t = theme.get().tokens;
                        let ctx_t = ctx3.clone();
                        let close_t = close_b.clone();
                        let close_esc = close_b.clone();
                        Element::new()
                            .style(LayoutStyle::row().gap(2))
                            .child(
                                Button::new("Transfer to user")
                                    .on_click(move || {
                                        let idx = ctx_t.ui.resv_sel.get_untracked();
                                        let row = ctx_t.store.reservations.with_untracked(|d| {
                                            d.ready().and_then(|r| r.get(idx).cloned())
                                        });
                                        let Some(row) = row else {
                                            ctx_t
                                                .store
                                                .notice
                                                .set(Some("no reservation selected".into()));
                                            return;
                                        };
                                        let tgt = target.get_untracked().trim().to_string();
                                        if tgt.is_empty() {
                                            ctx_t
                                                .store
                                                .notice
                                                .set(Some("type the target user id first".into()));
                                            return;
                                        }
                                        let c = ctx_t.clone();
                                        close_t();
                                        confirm_transfer(screen_cx, &c, row, tgt);
                                    })
                                    .element(bcx, &t)
                                    .build(),
                            )
                            .child(
                                Button::new("Close (Esc)")
                                    .on_click(move || close_esc())
                                    .element(bcx, &t)
                                    .build(),
                            )
                            .build()
                    },
                ))
                .build()
        },
    );
}

fn confirm_transfer(cx: Scope, ctx: &Ctx, row: crate::store::ReservationRow, target: String) {
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Transfer retained runtime '{}' (tenant {}) to user '{}'? The target user takes over the whole data plane.",
            row.runtime_id, row.tenant_id, target
        ),
        "Transfer it",
        "Keep it retained",
        move || {
            ctx2.send(Cmd::ReservationTransfer {
                runtime_id: row.runtime_id,
                tenant_id: row.tenant_id,
                target_user_id: target,
            })
        },
    );
}

/// "Mailboxes for users" — the administrator's one email switch.
pub const MAILBOXES_LABEL: &str = "Mailboxes for users";
pub const MAILBOXES_HELP: &str = "Users may connect their own mailbox for their agents, automations and notifications. You never see anyone's mail.";
pub const AGENT_TOOLS_LABEL: &str = "Agent email tools for users";
pub const AGENT_TOOLS_HELP: &str = "Users may let their agents and workflows use their mailbox. Each user still switches the tools on for themselves.";
pub const RECOVERY_LABEL: &str = "Sign-in by email";
pub const RECOVERY_HELP: &str = "Shows 'Forgot your token?' on the sign-in page. Whoever controls a user's mailbox can then sign in as that user.";

/// The gateway-wide email switches (`/admin/email/capabilities`): each
/// applies at once, the status line names the new state; no Save.
fn email_switches(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    use crate::worker::operator::{EmailAction, OpCmd};
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let form_id = crate::worker::next_form_id();
    let busy = cx.signal(Option::<&'static str>::None);
    let sw_mail = cx.signal(true);
    let sw_tools = cx.signal(true);
    let sw_rec = cx.signal(true);
    // Read the defaults once an admin is here (and again after a reset).
    {
        let ctx_load = ctx.clone();
        cx.effect(move || {
            let admin = store.conn.with(ConnPhase::is_admin);
            let not_asked = store
                .op
                .email_caps
                .with(|c| matches!(c, Loadable::NotAsked));
            if admin && not_asked {
                store.op.email_caps.set(Loadable::Loading);
                ctx_load.send(Cmd::Operator(OpCmd::Email {
                    action: EmailAction::LoadCaps,
                    form_id: None,
                }));
            }
        });
    }
    cx.effect(move || {
        if let Loadable::Ready(c) = store.op.email_caps.get() {
            sw_mail.set(c.email);
            sw_tools.set(c.agent_tools);
            sw_rec.set(c.recovery);
        }
    });
    cx.effect(move || {
        if let Some((fid, _)) = ui.write_done.get() {
            if fid == form_id {
                ui.write_done.set(None);
                busy.set(None);
            }
        }
    });
    let request = {
        let ctx = ctx.clone();
        move |key: &'static str, want: bool| {
            if !super::util::admin_gate(&ctx.store, "changing the gateway's email switches") {
                return;
            }
            if busy.get_untracked().is_some() {
                return;
            }
            busy.set(Some(key));
            ctx.send(Cmd::Operator(OpCmd::Email {
                action: EmailAction::CapsDefaults(json!({ key: want }).into()),
                form_id: Some(form_id),
            }));
        }
    };
    let row = move |scx: Scope,
                    key: &'static str,
                    label: &'static str,
                    help: &'static str,
                    sig: Signal<bool>,
                    indent: bool| {
        let request = request.clone();
        let unavailable = match store.op.email_caps.get_untracked() {
            Loadable::Failed(e) => Some(format!("couldn't read the email switches: {e}")),
            Loadable::Ready(_) => None,
            _ => Some("reading…".to_string()),
        };
        // The switch on its row, its description under it (wrapped, never
        // cut: "Sign-in by email" carries a security warning).
        let pad = if indent { 2 } else { 0 };
        let w = (abstracttui::app::use_viewport(scx).get_untracked().w - 10 - pad).max(20) as usize;
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(
                // Indented by padding (a spacer beside a full-width switch
                // pushed it over the block border).
                Element::new()
                    .style(LayoutStyle::column().h(1).shrink(0.0).padding(Edges {
                        left: pad,
                        right: 0,
                        top: 0,
                        bottom: 0,
                    }))
                    .child(
                        super::switch::Switch::new(label, sig)
                            .fill()
                            .unavailable(unavailable)
                            .busy_when(move || busy.get() == Some(key))
                            .notice(store.notice)
                            .on_request(move |want| request(key, want))
                            .element(scx, &tt)
                            .build(),
                    )
                    .build(),
            );
        let lead = " ".repeat(pad as usize + 4);
        for l in super::util::wrap_text(help, w) {
            col = col.child(line(vec![span(format!("{lead}{l}"), tt.text_faint)]));
        }
        col.build()
    };
    dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |scx| {
        if !store.conn.with(ConnPhase::is_admin) {
            return Element::new().style(LayoutStyle::default().h(0)).build();
        }
        // Rebuilt when the read lands or fails (the unavailable reason) —
        // never by a switch press.
        let _ = store.op.email_caps.with(|c| match c {
            Loadable::Ready(_) => 1,
            Loadable::Failed(_) => 2,
            _ => 0,
        });
        // R8.1: the three switches sit directly in the card (no Advanced).
        Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(row(
                scx,
                "email",
                MAILBOXES_LABEL,
                MAILBOXES_HELP,
                sw_mail,
                false,
            ))
            .child(row(
                scx,
                "email_agent_tools",
                AGENT_TOOLS_LABEL,
                AGENT_TOOLS_HELP,
                sw_tools,
                false,
            ))
            .child(row(
                scx,
                "email_recovery",
                RECOVERY_LABEL,
                RECOVERY_HELP,
                sw_rec,
                false,
            ))
            .child(line(vec![span(String::new(), tt.text)]))
            .build()
    })
}

/// Why your own row's Active switch is unavailable (state-toggles §4).
pub const OWN_ACCOUNT_REASON: &str = "You can't deactivate your own account.";

/// The signed-in principal's `(user_id, tenant_id)`.
fn own_key(store: &crate::store::Store) -> Option<(String, String)> {
    store.conn.with_untracked(|c| match c {
        ConnPhase::Connected(id) | ConnPhase::Verifying(id) => {
            Some((id.user_id.clone(), id.tenant_id.clone()))
        }
        _ => None,
    })
}

fn confirm_rotate(cx: Scope, ctx: &Ctx, u: UserRow) {
    let ctx = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Rotate the token for '{}'? The current token stops working immediately; the new one is shown once.",
            u.user_id
        ),
        "Rotate the token",
        "Keep the current token",
        move || {
            ctx.send(Cmd::PatchUser {
                user_id: u.user_id,
                tenant_id: u.tenant_id,
                body: json!({ "rotate_token": true }).into(),
                form_id: None,
            })
        },
    );
}

/// Create (existing=None) or edit a user.
fn open_user_form(cx: Scope, ctx: &Ctx, existing: Option<UserRow>) {
    let create = existing.is_none();
    let ctx2 = ctx.clone();
    super::open_form_guarded(
        ctx,
        cx,
        abstracttui::app::use_viewport(cx).get_untracked(),
        move |mcx, close, guard| {
            let theme = use_theme(mcx);
            let t0 = theme.get().tokens;
            let ex = existing.clone();

            let user_id = mcx.signal(ex.as_ref().map(|u| u.user_id.clone()).unwrap_or_default());
            let email = mcx.signal(ex.as_ref().map(|u| u.email.clone()).unwrap_or_default());
            let roles = mcx.signal(
                ex.as_ref()
                    .map(|u| u.roles.clone())
                    .unwrap_or_else(|| vec!["user".to_string()]),
            );
            // Advanced create-time bindings (web parity). Blank = the
            // gateway's own defaults (tenant "default"; one runtime named
            // after the user id) — the placeholders SAY so instead of
            // fabricating a value into the field.
            let tenant = mcx.signal(String::new());
            let runtime = mcx.signal(String::new());
            // Advanced (create only): the tenant and runtime binding.
            let advanced = mcx.signal(false);
            let form_error = mcx.signal(Option::<String>::None);
            let in_flight = mcx.signal(false);
            let esc_armed = mcx.signal(false);
            let form_id = crate::worker::next_form_id();

            // Dirty-Esc guard + disarm + write_done: the shared contract (F4).
            {
                let initial = (
                    user_id.get_untracked(),
                    email.get_untracked(),
                    roles.get_untracked(),
                );
                super::install_dirty_guard_with(
                    mcx,
                    &guard,
                    move || {
                        user_id.get_untracked() != initial.0
                            || email.get_untracked() != initial.1
                            || roles.get_untracked() != initial.2
                            || !tenant.get_untracked().is_empty()
                            || !runtime.get_untracked().is_empty()
                    },
                    move || {
                        let _ = (
                            user_id.get(),
                            email.get(),
                            roles.get(),
                            tenant.get(),
                            runtime.get(),
                        );
                    },
                    esc_armed,
                    form_error,
                );
            }
            super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

            let title = if create {
                "Create user".to_string()
            } else {
                format!(
                    "Edit user '{}'",
                    ex.as_ref().map(|u| u.user_id.as_str()).unwrap_or("")
                )
            };
            let ctx_save = ctx2.clone();
            let ex_save = ex.clone();
            let close_cancel = close.clone();

            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(line(vec![span_bold(title, t0.accent)]))
                .child(field(
                    &t0,
                    "User ID",
                    if create {
                        TextInput::new()
                            .value(user_id)
                            .placeholder("e.g. alice")
                            .placeholder_while_focused(true)
                            .layout(LayoutStyle::default().w(30).h(1))
                            .element(mcx, &t0)
                            .autofocus()
                            .build()
                    } else {
                        line(vec![span(user_id.get_untracked(), t0.text_muted)])
                    },
                ))
                .child(if create {
                    helper_line(&t0, USER_ID_HELP)
                } else {
                    Element::new().style(LayoutStyle::default().h(0)).build()
                })
                .child(field(
                    &t0,
                    "Role",
                    MultiSelect::new(vec![
                        SelectOption::keyed("user", "User — runs workflows on their own runtime"),
                        SelectOption::keyed("admin", "Admin — manages this gateway"),
                        SelectOption::keyed("readonly", "Read-only — can look, not change"),
                    ])
                    .values(roles)
                    .placeholder("pick a role…")
                    .layout(LayoutStyle::default().w(46).h(1).shrink(0.0))
                    .element(mcx, &t0)
                    .build(),
                ))
                .child(field(&t0, "Email address", {
                    let e = TextInput::new()
                        .value(email)
                        .placeholder("")
                        .layout(LayoutStyle::default().w(36).h(1))
                        .element(mcx, &t0);
                    if create {
                        e.build()
                    } else {
                        e.autofocus().build()
                    }
                }))
                .child(helper_line(&t0, EMAIL_ADDRESS_HELP))
                .child(if create {
                    // A disclosure (not a setting): opens the two rarely-set
                    // bindings below.
                    dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), move |dcx| {
                        let t = theme.get().tokens;
                        Button::new(if advanced.get() {
                            "Advanced ▾"
                        } else {
                            "Advanced ▸  runtime, tenant"
                        })
                        .on_click(move || advanced.update(|v| *v = !*v))
                        .element(dcx, &t)
                        .build()
                    })
                } else {
                    Element::new().style(LayoutStyle::default().h(0)).build()
                })
                .child(dyn_view_scoped(
                    LayoutStyle::column().gap(0).shrink(0.0),
                    move |acx| {
                        if !(create && advanced.get()) {
                            return Element::new().style(LayoutStyle::default().h(0)).build();
                        }
                        let t = theme.get().tokens;
                        Element::new()
                            .style(LayoutStyle::column().gap(0).shrink(0.0))
                            .child(field(
                                &t,
                                "Runtime",
                                TextInput::new()
                                    .value(runtime)
                                    .placeholder("")
                                    .layout(LayoutStyle::default().w(30).h(1))
                                    .element(acx, &t)
                                    .build(),
                            ))
                            .child(helper_line(&t, RUNTIME_HELP))
                            .child(field(
                                &t,
                                "Tenant",
                                TextInput::new()
                                    .value(tenant)
                                    .placeholder("default")
                                    .placeholder_while_focused(true)
                                    .layout(LayoutStyle::default().w(30).h(1))
                                    .element(acx, &t)
                                    .build(),
                            ))
                            .child(helper_line(&t, TENANT_HELP))
                            .build()
                    },
                ))
                .child(if create {
                    line(vec![span(
                        "The gateway makes their token when you create the user; it is shown once.",
                        t0.text_faint,
                    )])
                } else {
                    line(vec![span(
                        "token rotation lives on the table (t) — this form edits the record only",
                        t0.text_faint,
                    )])
                })
                .child(super::message_slot(theme, form_error, in_flight))
                .child(dyn_view_scoped(
                    LayoutStyle::default().h(1).shrink(0.0),
                    move |bcx| {
                        let t = theme.get().tokens;
                        let busy_form = in_flight.get();
                        let ctx_save = ctx_save.clone();
                        let ex_save = ex_save.clone();
                        let close_cancel = close_cancel.clone();
                        Element::new()
                            .style(LayoutStyle::row().gap(2))
                            .child(
                                Button::new(if create { "Create user" } else { "Save" })
                                    .disabled(busy_form)
                                    .on_click(move || {
                                        if in_flight.get_untracked() {
                                            return; // a write is already running
                                        }
                                        let uid = user_id.get_untracked().trim().to_string();
                                        if create && uid.is_empty() {
                                            form_error.set(Some("Type a User ID.".into()));
                                            return;
                                        }
                                        let roles_v = roles.get_untracked();
                                        if roles_v.is_empty() {
                                            form_error.set(Some("Pick a role.".into()));
                                            return;
                                        }
                                        // Active is the table's switch (space), not a
                                        // form field: a new user starts active.
                                        let mut body = json!({
                                            "roles": roles_v,
                                            "email": email.get_untracked().trim(),
                                        });
                                        if create {
                                            body["enabled"] = Value::Bool(true);
                                        }
                                        form_error.set(None);
                                        in_flight.set(true);
                                        if create {
                                            body["user_id"] = Value::String(uid.clone());
                                            // Optional bindings: omitted when blank so
                                            // the gateway's own defaults apply (tenant
                                            // "default"; runtime = user id).
                                            let tv = tenant.get_untracked().trim().to_string();
                                            if !tv.is_empty() {
                                                body["tenant_id"] = Value::String(tv);
                                            }
                                            let rv = runtime.get_untracked().trim().to_string();
                                            if !rv.is_empty() {
                                                body["runtime_id"] = Value::String(rv);
                                            }
                                            ctx_save.send(Cmd::CreateUser {
                                                body: body.into(),
                                                form_id: Some(form_id),
                                            });
                                        } else if let Some(u) = &ex_save {
                                            ctx_save.send(Cmd::PatchUser {
                                                user_id: u.user_id.clone(),
                                                tenant_id: u.tenant_id.clone(),
                                                body: body.into(),
                                                form_id: Some(form_id),
                                            });
                                        }
                                    })
                                    .element(bcx, &t)
                                    .build(),
                            )
                            .child(
                                Button::new("Cancel (Esc)")
                                    .on_click(move || close_cancel())
                                    .element(bcx, &t)
                                    .build(),
                            )
                            .build()
                    },
                ))
                .build()
        },
    );
}

pub const USER_ID_HELP: &str = "Letters, digits, dots or dashes. This is how they sign in.";
pub const EMAIL_ADDRESS_HELP: &str =
    "Where sign-in codes and notifications go. Leave empty if they have none; they can add it later.";
pub const RUNTIME_HELP: &str =
    "The data plane their runs, flows and sessions live in. Empty = their own, named after them.";
pub const TENANT_HELP: &str = "Leave 'default' unless you run several tenants.";

/// A faint helper under a form field, indented to the field column and
/// wrapped (never cut) to the form's width.
fn helper_line(t: &TokenSet, text: &str) -> View {
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for l in super::util::wrap_text(text, 58) {
        col = col.child(line(vec![span(l, t.text_faint)]));
    }
    field(t, "", col.build())
}

/// The once-shown token modal (create-user / rotate-token).
pub fn open_token_modal(cx: Scope, ctx: &Ctx, user: String, token: String) {
    let ctx2 = ctx.clone();
    open_form(
        ctx,
        cx,
        abstracttui::app::use_viewport(cx).get_untracked(),
        move |mcx, close| {
            let theme = use_theme(mcx);
            let t0 = theme.get().tokens;
            let tok_copy = token.clone();
            let tok_show = token.clone();
            let store = ctx2.store;
            let close_b = close.clone();
            Element::new()
                .style(LayoutStyle::column().gap(1))
                .child(line(vec![span_bold(
                    format!("Access token for '{user}'"),
                    t0.accent,
                )]))
                .child(line(vec![span(
                    format!("Give this token to {user}. It is shown once."),
                    t0.warn,
                )]))
                .child(line(vec![span_bold(tok_show, t0.text)]))
                .child(
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                        .child(
                            Button::new("Copy to clipboard")
                                .on_click(move || {
                                    copy_to_clipboard(tok_copy.clone());
                                    // Do NOT name the route: since abstracttui
                                    // 0.3.0 the engine picks exactly one (OSC 52
                                    // when the terminal advertises it, else the
                                    // host clipboard) and labels its own notice
                                    // if neither worked. Claiming "OSC 52" here
                                    // was wrong on every Terminal.app-class host.
                                    store
                                        .notice
                                        .set(Some("token copied to the clipboard".into()));
                                })
                                .element(mcx, &t0)
                                .build(),
                        )
                        .child(
                            Button::new("Done — I copied it")
                                .on_click(move || close_b())
                                .element(mcx, &t0)
                                .build(),
                        )
                        .build(),
                )
                .build()
        },
    );
}
