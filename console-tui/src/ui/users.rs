//! Users & entities: gateway user CRUD (admin) + the entity roster
//! (summon `n`, talk `c`, spark templates `s`, manage `m`).
//!
//! The token rule: create/rotate responses carry the token EXACTLY
//! ONCE — it goes to a dedicated modal with a copy affordance and an
//! explicit "will not be shown again", never to logs or the journal.

use abstracttui::prelude::*;
use abstracttui::widgets::{Table, Tone};
use serde_json::{json, Value};

use super::util::{badge, field, line, loadable_view, span, span_bold};
use super::widths;
use super::{open_form, Ctx};
use crate::store::accounts::{
    activity_time, AccountRow, ACTIVITY_EMPTY, ACTIVITY_FILTERS, ACTIVITY_SCOPE,
};
use crate::store::{ConnPhase, EntityRow, Loadable, UserRow};
use crate::worker::Cmd;

/// The footer verbs of this screen that only an admin may use: the users
/// registry (add / edit / rotate / delete — `/admin/users*`) and the kept
/// data of deleted users (`/admin/runtime-reservations`). The entity
/// roster verbs stay open (their own admin-only acts are gated inside the
/// manage menu).
pub const ADMIN_KEYS: &[&str] = &["a", "e", "t", "d", "v", "x"];

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    super::util::clamp_selection(cx, ui.entity_sel, move || {
        store
            .entities
            .with(|d| d.ready().map(Vec::len).unwrap_or(0))
    });

    let ctx_add = ctx.clone();
    let ctx_edit = ctx.clone();
    let ctx_rotate = ctx.clone();
    let ctx_del = ctx.clone();
    let ctx_manage = ctx.clone();
    let ctx_resv = ctx.clone();
    let ctx_inspect = ctx.clone();
    let ctx_mypolicy = ctx.clone();
    let ctx_myemail = ctx.clone();
    let ctx_mail = ctx.clone();
    let ctx_summon = ctx.clone();
    let ctx_talk = ctx.clone();
    let ctx_tpl = ctx.clone();
    let ctx_logs = ctx.clone();

    super::util::clamp_selection(cx, ui.account_sel, move || {
        store
            .accounts
            .with(|d| d.ready().map(Vec::len).unwrap_or(0))
    });
    // The account selection drives the entity selection: an entity row
    // selected in the one table IS the entity the inspector, Manage and
    // Talk act on (they read `entity_sel`).
    cx.effect(move || {
        let idx = ui.account_sel.get();
        let Some(name) = store.accounts.with(|d| {
            d.ready()
                .and_then(|rows| rows.get(idx))
                .filter(|r| r.is_entity())
                .map(|r| r.id.clone())
        }) else {
            return;
        };
        let pos = store.entities.with(|d| {
            d.ready()
                .and_then(|es| es.iter().position(|e| e.is_account(&name)))
        });
        if let Some(pos) = pos {
            if ui.entity_sel.get_untracked() != pos {
                ui.entity_sel.set(pos);
            }
        }
    });

    // Keep the manage snapshot warm for the SELECTED entity: arrowing
    // to a row loads its detail (worker serializes; entity rosters are
    // small), so the manage menu and the panel below open warm.
    {
        let ctx_detail = ctx.clone();
        // The name of the last detail request this effect sent — the
        // Failed arm below holds ONLY for that name, so a persistent
        // failure never loops while a selection move still loads the
        // new row (round-4 transport audit: `Failed => reload` was an
        // unbounded auto-retry against a persistently failing read).
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
            // TRACKED read (F2, proven stall): moving the selection while
            // a load is in flight used to skip the send AND never re-run
            // (untracked slot) — the drawer then said "reading Bestor…"
            // forever with nothing loading. Tracking the slot re-runs
            // this effect when the stale result lands; a name mismatch
            // then sends for the CURRENT row (the Loading arm terminates
            // the one extra self-triggered run).
            let held = store.entity_detail.with(|d| match d {
                Loadable::Ready(d) => d.name == name,
                Loadable::Loading => true,
                // Hold on Failed only for the name we last asked for:
                // same row → no retry loop (recovery stays r / manage
                // menu); different row → load it.
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

    Element::new()
        // Focusable + autofocus content root: the screen's keys must live
        // even when no table exists to take the keyboard (a non-admin on a
        // gateway with no entities yet). A table that mounts later still
        // takes the focus.
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
            manage_selected_entity(cx, &ctx_manage);
        })
        .shortcut(KeyChord::plain(Key::Char('n')), move |_| {
            // Create entity (the web's secondary header button; summon —
            // Advanced configuration inside is admin-only).
            if store.conn.with_untracked(ConnPhase::is_connected) {
                super::entity_create::open_summon_form(cx, &ctx_summon);
            } else {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('c')), move |_| {
            if !store.conn.with_untracked(ConnPhase::is_connected) {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            } else if let Some(e) = selected_entity(&ctx_talk) {
                super::entity_chat::open_talk_modal(cx, &ctx_talk, e.name);
            } else {
                store
                    .notice
                    .set(Some("no entity selected — nobody to talk to".into()));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('s')), move |_| {
            if store.conn.with_untracked(ConnPhase::is_connected) {
                super::entity_create::open_templates_modal(cx, &ctx_tpl);
            } else {
                store.notice.set(Some(
                    "not connected — probe on the Connection screen first".into(),
                ));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('v')), move |_| {
            if super::util::admin_gate(&store, "the kept data of deleted users") {
                open_reservations_modal(cx, &ctx_resv);
            }
        })
        // Workspace: the selected account's workspace policy (your own on
        // your row or for a non-admin).
        .shortcut(KeyChord::plain(Key::Char('w')), move |_| {
            workspace_selected(cx, &ctx_mypolicy)
        })
        // Email: your row = the full account email view; another user's
        // row = the address-only view; an entity = the reason.
        .shortcut(KeyChord::plain(Key::Char('@')), move |_| {
            email_selected(cx, &ctx_myemail)
        })
        // Logs: the selected account's activity (yours for a non-admin).
        .shortcut(KeyChord::plain(Key::Char('l')), move |_| {
            open_activity(cx, &ctx_logs)
        })
        // Reset an old per-user mailbox override (a one-shot action; the
        // console never creates per-user overrides).
        .shortcut(KeyChord::plain(Key::Char('x')), move |_| {
            if !super::util::admin_gate(&store, "resetting a user's mailbox override") {
                return;
            }
            match selected_user(&ctx_mail) {
                Some(u) if u.mailbox_view().1 => {
                    ctx_mail.send(crate::worker::Cmd::Operator(
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
        })
        .shortcut(KeyChord::plain(Key::Char('i')), move |_| {
            // Toggle the entity-inspector drawer (passive: the table keeps
            // the keyboard; i again closes; leaving the screen closes it).
            if selected_entity(&ctx_inspect).is_some() {
                if let Some(h) = ctx_inspect.entity_drawer.borrow().as_ref() {
                    h.toggle();
                }
            } else {
                store
                    .notice
                    .set(Some("no entity selected — nothing to inspect".into()));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('a')), move |_| {
            if super::util::admin_gate(&store, "creating a user") {
                open_user_form(cx, &ctx_add, None);
            }
        })
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            edit_selected_user(cx, &ctx_edit);
        })
        .shortcut(KeyChord::plain(Key::Char('t')), move |_| {
            rotate_selected(cx, &ctx_rotate);
        })
        .shortcut(KeyChord::plain(Key::Char('d')), move |_| {
            delete_selected(cx, &ctx_del);
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title(ACCOUNTS_TITLE)
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                .child(dyn_view_scoped(
                    LayoutStyle::default().grow(1.0).min_h(1),
                    {
                        let ctx_act = ctx.clone();
                        move |gcx| {
                            // RBAC (operator ruling 2026-10-01): an admin's
                            // table is every account (`/admin/accounts`); a
                            // non-admin's is themself + the entities they
                            // created (`/me/accounts`) — the same table,
                            // with one line saying whose view it is.
                            let scope_note = store.conn.with(|c| match c {
                                ConnPhase::Connected(id) | ConnPhase::Verifying(id)
                                    if !id.admin =>
                                {
                                    Some(non_admin_scope_line(&id.user_id))
                                }
                                _ => None,
                            });
                            let data = store.accounts.get();
                            let ctx_act = ctx_act.clone();
                            let table = loadable_view(
                                &tt,
                                &store.conn.get(),
                                || store.tick.get(),
                                &data,
                                |d: &Vec<AccountRow>| d.is_empty(),
                                "no accounts yet — a creates a user, n creates an entity",
                                |d| {
                                    let ctx_space = ctx_act.clone();
                                    accounts_table(
                                        gcx,
                                        &tt,
                                        d,
                                        ui.account_sel,
                                        move |_| {
                                            // Activation (Enter / double-click):
                                            // a user → edit; an entity → Manage.
                                            activate_selected(cx, &ctx_act);
                                        },
                                        // Space switches the row's Active.
                                        move || switch_selected_active(cx, &ctx_space),
                                    )
                                },
                            );
                            match scope_note {
                                None => table,
                                Some(note) => {
                                    let vw = abstracttui::app::use_viewport(gcx).get_untracked().w;
                                    let mut col = Element::new()
                                        .style(LayoutStyle::column().gap(0).grow(1.0));
                                    for l in
                                        super::util::wrap_text(&note, (vw - 6).max(20) as usize)
                                    {
                                        col = col.child(line(vec![span(l, tt.text_muted)]));
                                    }
                                    col.child(table).build()
                                }
                            }
                        }
                    },
                ))
                // The selected account: its kind chip, its Active switch and
                // the actions that cannot apply, each with its reason.
                .child(dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), {
                    let ctx_sw = ctx.clone();
                    move |scx| selected_row_lines(scx, &ctx_sw, &tt)
                }))
                // The administrator's email switches: "Email for everyone"
                // below the table (DESIGN-v2 §2.1).
                .child(email_switches(cx, ctx, t))
                .element(t)
                .build(),
        )
        .build()
}

/// The screen's block title (DESIGN-v2 §2.1: the page line, in words).
pub const ACCOUNTS_TITLE: &str =
    "Accounts — people who use this gateway and the entities that act on it";

/// An entity's Email view (DESIGN-v2 §2.3): entities cannot hold a
/// mailbox (`plane_for_principal` refuses entity principals; mail belongs
/// to a user's runtime plane). Used when the gateway's row carries no
/// reason of its own.
pub const ENTITY_EMAIL_REASON: &str =
    "Entities can't have their own mailbox yet: mailboxes belong to a user's runtime.";

/// The selected row of the one table (admin view).
pub fn selected_account(ctx: &Ctx) -> Option<AccountRow> {
    let idx = ctx.ui.account_sel.get_untracked();
    ctx.store
        .accounts
        .with_untracked(|d| d.ready().and_then(|rows| rows.get(idx).cloned()))
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
fn workspace_selected(cx: Scope, ctx: &Ctx) {
    let row = if uses_accounts(ctx) {
        selected_account(ctx)
    } else {
        None
    };
    match row {
        None => super::my_policy::open(cx, ctx),
        Some(r) if is_own(&ctx.store, &r) => super::my_policy::open(cx, ctx),
        Some(r) => match r.refusal("workspace") {
            Some(why) => ctx.store.notice.set(Some(why)),
            None if r.is_entity() => ctx.store.notice.set(Some(format!(
                "{}'s file access is set on the entity itself (workspace mounts) — m manages it",
                r.id
            ))),
            None => super::runtimes::open_user_policy(cx, ctx, r.tenant_id.clone(), r.id.clone()),
        },
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
        Some(r) if r.is_entity() => {
            let why = r
                .refusal("email")
                .or_else(|| r.mailbox.reason.clone())
                .unwrap_or_else(|| ENTITY_EMAIL_REASON.to_string());
            ctx.store.notice.set(Some(why));
        }
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
    if let Some(why) = r.refusal("rotate") {
        ctx.store.notice.set(Some(why));
        return;
    }
    if r.is_entity() {
        let ctx2 = ctx.clone();
        super::confirm_danger(
            cx,
            ctx.ui,
            format!(
                "Rotate the token of entity '{}'? Its current token stops working immediately; the new one is shown once.",
                r.id
            ),
            "Rotate the token",
            "Keep the current token",
            move || {
                ctx2.send(Cmd::RotateAccount {
                    id: r.id,
                    tenant_id: r.tenant_id,
                })
            },
        );
    } else if let Some(u) = selected_user(ctx) {
        confirm_rotate(cx, ctx, u);
    } else {
        ctx.store.notice.set(Some(format!(
            "the users registry has not loaded {} yet — r refreshes",
            r.id
        )));
    }
}

/// `d`: delete the selected user (an entity's name is kept for life —
/// the gateway's reason says so).
fn delete_selected(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "deleting an account") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — nothing to delete".into()));
        return;
    };
    if let Some(why) = r.refusal("delete") {
        ctx.store.notice.set(Some(why));
        return;
    }
    match selected_user(ctx) {
        Some(u) if !r.is_entity() => confirm_delete(cx, ctx, u),
        _ => ctx.store.notice.set(Some(format!(
            "the users registry has not loaded {} yet — r refreshes",
            r.id
        ))),
    }
}

/// The Active switch of the selected account: OFF asks first (a user is
/// signed out; an entity stops acting), ON applies at once; an
/// unavailable switch (your own account) says why.
fn switch_selected_active(cx: Scope, ctx: &Ctx) {
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
        super::confirm_danger(cx, ctx.ui, question, verb, "Cancel", move || send(false));
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

/// The accounts table: Name · Kind · Email address · Mailbox · Runtime ·
/// Active (`[x]` / `[ ]` / `[-] reason`).
fn accounts_table(
    cx: Scope,
    t: &TokenSet,
    data: &[AccountRow],
    sel: Signal<usize>,
    on_activate: impl FnMut(usize) + 'static,
    mut on_space: impl FnMut() + 'static,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let mut rows: Vec<Vec<String>> = data
        .iter()
        .map(|r| {
            vec![
                r.id.clone(),
                r.kind_label().to_string(),
                r.email_address.clone().unwrap_or_else(|| "—".into()),
                r.mailbox_cell(),
                r.runtime_id.clone().unwrap_or_else(|| "—".into()),
                r.active_cell(),
            ]
        })
        .collect();
    // Ids and addresses discriminate on their TAIL; the kind word, the
    // mailbox words and the switch marker lead with what matters.
    let rules = vec![
        widths::ColRule::tail("name", 10),
        widths::ColRule::head("kind", 6),
        widths::ColRule::tail("email address", 14),
        widths::ColRule::head("mailbox", 14),
        widths::ColRule::tail("runtime", 8),
        widths::ColRule::head("active", 6),
    ];
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    let table = Table::new(cols)
        .rows(rows)
        .selection(sel)
        .on_activate(on_activate)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t)
        .autofocus()
        .build();
    // Space is the switch key (the Table would alias it to activation):
    // caught on the way down, before the Table sees it.
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .on(abstracttui::ui::Phase::Capture, move |ectx, ev| {
            if let abstracttui::ui::UiEvent::Key(k) = ev {
                if k.key == Key::Char(' ') && k.mods.0 == 0 {
                    on_space();
                    ectx.stop_propagation();
                }
            }
        })
        .child(table)
        .build()
}

/// Under the table: the selected account (chip + id + Active switch), the
/// actions that cannot apply with their reasons, and the kind legend.
fn selected_row_lines(scx: Scope, ctx: &Ctx, tt: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let empty = || Element::new().style(LayoutStyle::default().h(0)).build();
    let idx = ui.account_sel.get();
    let Some(r) = store
        .accounts
        .with(|d| d.ready().and_then(|rows| rows.get(idx).cloned()))
    else {
        return empty();
    };
    let on = scx.signal(r.active);
    let ctx_req = ctx.clone();
    let vw = abstracttui::app::use_viewport(scx).get_untracked().w;
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    col = col.child(
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(badge(tt, r.kind_label(), kind_tone(r.kind_label())))
            .child(super::util::line_styled(
                LayoutStyle::default()
                    .w(r.id.chars().count() as i32 + 1)
                    .h(1)
                    .shrink(0.0),
                vec![span_bold(r.id.clone(), tt.text)],
            ))
            .child(
                super::switch::Switch::new("Active", on)
                    .unavailable(r.refusal("suspend"))
                    .notice(store.notice)
                    .on_request(move |_| switch_selected_active(scx, &ctx_req))
                    .layout(LayoutStyle::default().grow(1.0).h(1))
                    .element(scx, tt)
                    .build(),
            )
            .build(),
    );
    // Every action that cannot apply, with its reason (visible, never
    // tooltip-only — DESIGN-v2 §2.1).
    let labels = [
        ("email", "Email"),
        ("logs", "Logs"),
        ("workspace", "Workspace"),
        ("rotate", "Rotate"),
        ("manage", "Manage"),
        ("delete", "Delete"),
    ];
    let mut refusals: Vec<String> = Vec::new();
    for (key, label) in labels {
        if key == "manage" && !r.is_entity() {
            continue; // Manage is an entity action: users never show it
        }
        if let Some(why) = r.refusal(key) {
            refusals.push(format!("{label}: {why}"));
        }
    }
    let keys = if r.is_entity() {
        "@ email · l logs · w workspace · t rotate · m manage · d delete · space Active"
    } else {
        "@ email · l logs · w workspace · t rotate · e edit · d delete · space Active"
    };
    for l in super::util::wrap_text(keys, (vw - 6).max(20) as usize) {
        col = col.child(line(vec![span(l, tt.text_faint)]));
    }
    if !refusals.is_empty() {
        let text = format!("Unavailable — {}", refusals.join(" · "));
        for l in super::util::wrap_text(&text, (vw - 6).max(20) as usize) {
            col = col.child(line(vec![span(l, tt.text_muted)]));
        }
    }
    col.child(kind_legend(tt))
        .child(line(vec![span(String::new(), tt.text)]))
        .build()
}

/// The chip legend (the web's "Tint: admin · user · entity").
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
    super::open_form(ctx, cx, Size::new(110, 30), move |mcx, close| {
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
        let close_b = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .shortcut(KeyChord::plain(Key::Char('f')), move |_| cycle(1))
            .shortcut(KeyChord::plain(Key::Char('F')), move |_| cycle_b(-1))
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
                let mut spans = vec![span("filter: ", t.text_faint)];
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
                spans.push(span("   f / F change the filter", t.text_faint));
                line(spans)
            }))
            .child(dyn_view_scoped(
                LayoutStyle::default().grow(1.0),
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.activity.get().map(|(_, _, d)| d) {
                        None | Some(Loadable::NotAsked) | Some(Loadable::Loading) => {
                            line(vec![span("⟳ reading the activity…", t.info)])
                        }
                        Some(Loadable::Failed(e)) => super::util::error_panel_hint(
                            &t,
                            &e,
                            Some("change the filter (f) or reopen to read again"),
                        ),
                        Some(Loadable::Ready(d)) if d.events.is_empty() => {
                            line(vec![span(ACTIVITY_EMPTY, t.text_muted)])
                        }
                        Some(Loadable::Ready(d)) => activity_table(gcx, &t, &d),
                    }
                },
            ))
            .child(dyn_view_scoped(
                LayoutStyle::column().gap(0).shrink(0.0),
                move |_| {
                    let t = theme.get().tokens;
                    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                    if let Some((_, _, Loadable::Ready(d))) = store.activity.get() {
                        let mut note = d.note.clone().unwrap_or_default();
                        if d.truncated {
                            note.push_str(" Older events are not shown.");
                        }
                        if !note.trim().is_empty() {
                            col =
                                col.child(line(vec![span(note.trim().to_string(), t.text_faint)]));
                        }
                    }
                    col.child(line(vec![span(ACTIVITY_SCOPE, t.text_faint)]))
                        .build()
                },
            ))
            .child(dyn_view_scoped(
                LayoutStyle::line(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let close_c = close_b.clone();
                    Button::new("Close (Esc)")
                        .on_click(move || close_c())
                        .element(bcx, &t)
                        .build()
                },
            ))
            .build()
    });
}

/// One line per event, newest first: time · event · detail · run (with
/// the Observer path to open it).
fn activity_table(cx: Scope, t: &TokenSet, d: &crate::store::accounts::ActivityData) -> View {
    let vw = 110.min(abstracttui::app::use_viewport(cx).get().w) - 4;
    let today = crate::localtime::local_today();
    let mut rows: Vec<Vec<String>> = d
        .events
        .iter()
        .map(|e| {
            let title = if e.ok {
                e.title.clone()
            } else {
                format!("✗ {}", e.title)
            };
            let run = match (&e.run_id, &e.observer_path) {
                (Some(id), Some(p)) => format!("{id} · Observer {p}"),
                (Some(id), None) => id.clone(),
                _ => String::new(),
            };
            vec![
                activity_time(&e.ts, &today),
                title,
                e.detail.clone().unwrap_or_default(),
                run,
            ]
        })
        .collect();
    let rules = [
        widths::ColRule::head("time", 12),
        widths::ColRule::head("event", 18),
        widths::ColRule::head("detail", 20),
        widths::ColRule::tail("run", 16),
    ];
    let cols = widths::columns(&rules, &mut rows, vw);
    Table::new(cols)
        .rows(rows)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t)
        .build()
}

/// Retained runtime planes of deleted users: transfer to a living user
/// or purge (delete_data) — the web's reservations panel.
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
    super::open_form(ctx, cx, Size::new(RESV_MODAL_W, 20), move |mcx, close| {
        let theme = use_theme(mcx);
        let ui = ctx2.ui;
        let ctx3 = ctx2.clone();
        let close_b = close.clone();
        let target = mcx.signal(String::new());
        // F8: rows shrink by this modal's own actions (transfer/purge) —
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
                    "Kept data of deleted users — transfer or purge".to_string(),
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
                            let vw = RESV_MODAL_W.min(abstracttui::app::use_viewport(gcx).get().w)
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
                    let ctx_p = ctx3.clone();
                    let close_t = close_b.clone();
                    let close_p = close_b.clone();
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
                            Button::new("Purge (delete data)")
                                .on_click(move || {
                                    let idx = ctx_p.ui.resv_sel.get_untracked();
                                    let row = ctx_p.store.reservations.with_untracked(|d| {
                                        d.ready().and_then(|r| r.get(idx).cloned())
                                    });
                                    let Some(row) = row else {
                                        ctx_p
                                            .store
                                            .notice
                                            .set(Some("no reservation selected".into()));
                                        return;
                                    };
                                    let c = ctx_p.clone();
                                    close_p();
                                    confirm_resv_purge(screen_cx, &c, row);
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
    });
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

fn confirm_resv_purge(cx: Scope, ctx: &Ctx, row: crate::store::ReservationRow) {
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "PURGE retained runtime '{}' (tenant {})? Its data on disk is DELETED — this cannot be undone.",
            row.runtime_id, row.tenant_id
        ),
        "Delete the data",
        "Keep it retained",
        move || {
            ctx2.send(Cmd::ReservationPurge {
                runtime_id: row.runtime_id,
                tenant_id: row.tenant_id,
            })
        },
    );
}

/// "Mailboxes for users" — the administrator's one email switch.
pub const MAILBOXES_LABEL: &str = "Mailboxes for users";
pub const MAILBOXES_HELP: &str = "Users may connect their own mailbox for their agents, automations and notifications. You never see anyone's mail.";
pub const AGENT_TOOLS_LABEL: &str = "Agent email tools for users";
pub const AGENT_TOOLS_HELP: &str = "Each user still opts in on their own page.";
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
    let advanced = cx.signal(false);
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
        // Rebuilt when the read lands or fails (the unavailable reason),
        // or Advanced opens — never by a switch press.
        let _ = store.op.email_caps.with(|c| match c {
            Loadable::Ready(_) => 1,
            Loadable::Failed(_) => 2,
            _ => 0,
        });
        let open = advanced.get();
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(row(
                scx,
                "email",
                MAILBOXES_LABEL,
                MAILBOXES_HELP,
                sw_mail,
                false,
            ))
            .child(
                Button::new(if open {
                    "Advanced ▾"
                } else {
                    "Advanced ▸  agent email tools, sign-in by email"
                })
                .on_click(move || advanced.update(|v| *v = !*v))
                .element(scx, &tt)
                .build(),
            );
        if open {
            col = col
                .child(row(
                    scx,
                    "email_agent_tools",
                    AGENT_TOOLS_LABEL,
                    AGENT_TOOLS_HELP,
                    sw_tools,
                    true,
                ))
                .child(row(
                    scx,
                    "email_recovery",
                    RECOVERY_LABEL,
                    RECOVERY_HELP,
                    sw_rec,
                    true,
                ));
        }
        col.child(line(vec![span(String::new(), tt.text)])).build()
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

fn confirm_delete(cx: Scope, ctx: &Ctx, u: UserRow) {
    let ctx = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Delete user '{}' (tenant {})? Their token stops working; their runtime data stays on disk as a retained plane.",
            u.user_id, u.tenant_id
        ),
        "Delete the user",
        "Keep the user",
        move || {
            ctx.send(Cmd::DeleteUser {
                user_id: u.user_id,
                tenant_id: u.tenant_id,
            })
        },
    );
}

/// Create (existing=None) or edit a user.
fn open_user_form(cx: Scope, ctx: &Ctx, existing: Option<UserRow>) {
    let create = existing.is_none();
    let ctx2 = ctx.clone();
    super::open_form_guarded(ctx, cx, Size::new(84, 26), move |mcx, close, guard| {
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
            "New gateway user".to_string()
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
    });
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
    open_form(ctx, cx, Size::new(74, 12), move |mcx, close| {
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
                format!(
                    "Give this token to {user}. It is shown once — the gateway stores only a hash."
                ),
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
    });
}
