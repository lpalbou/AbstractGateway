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

    super::util::clamp_selection(cx, ui.user_sel, move || {
        store
            .users
            .with(|d| d.ready().map(|u| u.humans.len()).unwrap_or(0))
    });
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
        // even when no table exists to take the keyboard — a non-admin
        // (no users table) on a gateway with no entities yet would
        // otherwise have dead keys, `n` (summon the first entity)
        // included. A table that mounts later still takes the focus.
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().gap(1))
        .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
            manage_selected_entity(cx, &ctx_manage);
        })
        .shortcut(KeyChord::plain(Key::Char('n')), move |_| {
            // Summon — the web's "Summon entity" (create is user-level;
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
            // Talk first — the web's first row action.
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
        // The caller's OWN workspace policy (web: "My workspace policy"
        // on the Users tab) — any principal, see ui/my_policy.rs.
        .shortcut(KeyChord::plain(Key::Char('w')), move |_| super::my_policy::open(cx, &ctx_mypolicy))
        // The caller's OWN mailbox (web: "My email" on the Users tab) — any
        // human principal, see ui/my_email.rs.
        .shortcut(KeyChord::plain(Key::Char('@')), move |_| super::my_email::open(cx, &ctx_myemail))
        // Reset an old per-user mailbox override (a one-shot action; the
        // console never creates per-user overrides — "Mailboxes for users"
        // is the one switch, state-toggles §7).
        .shortcut(KeyChord::plain(Key::Char('x')), move |_| {
            if !super::util::admin_gate(&store, "resetting a user's mailbox override") {
                return;
            }
            match selected_user(&ctx_mail) {
                Some(u) if u.mailbox_override => {
                    ctx_mail.send(crate::worker::Cmd::Operator(crate::worker::operator::OpCmd::Email {
                        action: crate::worker::operator::EmailAction::AdminResetMailbox {
                            user_id: u.user_id.clone(),
                            tenant_id: u.tenant_id.clone(),
                        },
                        form_id: None,
                    }));
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
            // Toggle the entity-inspector drawer (passive: the roster
            // keeps the keyboard; i again closes; leaving the screen
            // closes it automatically).
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
            // F2: refusals name their reason — silent keys read as dead.
            if super::util::admin_gate(&store, "adding a user") {
                open_user_form(cx, &ctx_add, None);
            }
        })
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            edit_selected_user(cx, &ctx_edit);
        })
        .shortcut(KeyChord::plain(Key::Char('t')), move |_| {
            if !super::util::admin_gate(&store, "rotating a user's token") {
                return;
            }
            if let Some(u) = selected_user(&ctx_rotate) {
                confirm_rotate(cx, &ctx_rotate, u);
            } else {
                store
                    .notice
                    .set(Some("no user selected — no token to rotate".into()));
            }
        })
        .shortcut(KeyChord::plain(Key::Char('d')), move |_| {
            if !super::util::admin_gate(&store, "deleting a user") {
                return;
            }
            if let Some(u) = selected_user(&ctx_del) {
                confirm_delete(cx, &ctx_del, u);
            } else {
                store
                    .notice
                    .set(Some("no user selected — nothing to delete".into()));
            }
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Users (admin)")
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                // The administrator's ONE email switch (+ Advanced), above
                // the table (DESIGN §5.2).
                .child(email_switches(cx, ctx, t))
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0).min_h(1), {
                    let ctx_act = ctx.clone();
                    move |gcx| {
                        // The users registry is an admin surface
                        // (`/admin/users`; the web hides the section):
                        // a non-admin gets the reason, never a 403 panel.
                        if let Some(why) = store.conn.with(|c| {
                            c.is_known_non_admin().then(|| c.admin_refusal("the users registry"))
                        }).flatten()
                        {
                            return line(vec![span(
                                format!("{why} — w edits your own workspace policy"),
                                tt.text_muted,
                            )]);
                        }
                        let data = store.users.get();
                        // Empty-state honesty: 0 humans with N hidden
                        // entity principals is NOT an empty registry.
                        let empty_text = match &data {
                            crate::store::Loadable::Ready(u)
                                if u.humans.is_empty() && u.entity_principals > 0 =>
                            {
                                "no human users — entity principals are managed in Entities below"
                            }
                            _ => "no users in the registry",
                        };
                        let ctx_act = ctx_act.clone();
                        loadable_view(
                            &tt,
                            &store.conn.get(),
                            || store.tick.get(),
                            &data,
                            |d: &crate::store::UsersData| d.humans.is_empty(),
                            empty_text,
                            |d| {
                                let me = own_key(&store);
                                let ctx_space = ctx_act.clone();
                                users_table(
                                    gcx,
                                    &tt,
                                    &d.humans,
                                    ui.user_sel,
                                    me,
                                    move |_| {
                                        // Activation (Enter / double-click) =
                                        // the `e` edit path, one body.
                                        edit_selected_user(cx, &ctx_act);
                                    },
                                    // Space switches the row's one switch
                                    // (Active), state-toggles rule.
                                    move || switch_selected_active(cx, &ctx_space),
                                )
                            },
                        )
                    }
                }))
                // The selected user's switch, highlighted (the table's cells
                // carry the marker only — the engine Table has no per-cell
                // ink), with the keys that act on the row.
                .child(dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), {
                    let ctx_sw = ctx.clone();
                    move |scx| {
                        if store.conn.with(ConnPhase::is_known_non_admin) {
                            return line(vec![span(String::new(), tt.text)]);
                        }
                        let idx = ui.user_sel.get();
                        let Some(u) = store
                            .users
                            .with(|d| d.ready().and_then(|d| d.humans.get(idx).cloned()))
                        else {
                            return line(vec![span(String::new(), tt.text)]);
                        };
                        let own = own_key(&store)
                            .map(|k| k == (u.user_id.clone(), u.tenant_id.clone()))
                            .unwrap_or(false);
                        let on = scx.signal(u.enabled);
                        let ctx_req = ctx_sw.clone();
                        Element::new()
                            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                            .child(line(vec![span_bold(u.user_id.clone(), tt.text)]))
                            .child(
                                super::switch::Switch::new("Active", on)
                                    .unavailable(own.then(|| OWN_ACCOUNT_REASON.to_string()))
                                    .notice(store.notice)
                                    .on_request(move |_| switch_selected_active(cx, &ctx_req))
                                    .element(scx, &tt)
                                    .build(),
                            )
                            .child(line(vec![span(
                                if u.mailbox_override {
                                    "  space switches Active · e edits · x resets the mailbox override"
                                } else {
                                    "  space switches Active · e edits"
                                },
                                tt.text_faint,
                            )]))
                            .build()
                    }
                }))
                .element(t)
                .build(),
        )
        // The web console's exact partition note: entity principals
        // hold a door credential but are MANAGED in the entities lane
        // — never through the users table (where rotate/delete on one
        // would mint a live entity credential / invite identity
        // capture; the rows are simply not rendered, so the actions
        // cannot target them by construction).
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let n = store
                .users
                .with(|d| d.ready().map(|u| u.entity_principals).unwrap_or(0));
            if n == 0 {
                return line(vec![span(String::new(), tt.text)]);
            }
            // Grammar-correct at both n (REG-2: the earlier singular-
            // possessive "its own access token" broke at n>=2).
            let (noun, verb, poss, obj) = if n == 1 {
                ("entity", "holds", "its own access token", "it")
            } else {
                ("entities", "hold", "their own access tokens", "them")
            };
            line(vec![span(
                format!(
                    " {n} {noun} also {verb} {poss} — manage {obj} in Entities below, never here"
                ),
                tt.text_muted,
            )])
        }))
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Entities (n = summon · c = talk · m = manage · s = spark templates)")
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                .child(dyn_view_scoped(
                    // min_h(1) (D2, cycle-1 UX): the empty-roster `∅`
                    // line must always render — it used to be crushed
                    // to zero while the users block above hoarded blank
                    // rows, so a first-run gateway with no entities
                    // showed an empty box over a crash-looking footer.
                    LayoutStyle::default().grow(1.0).min_h(1),
                    {
                        let ctx_act = ctx.clone();
                        move |gcx| {
                            let data = store.entities.get();
                            let ctx_act = ctx_act.clone();
                            loadable_view(
                                &tt,
                                &store.conn.get(),
                                || store.tick.get(),
                                &data,
                                |d: &Vec<EntityRow>| d.is_empty(),
                                "no entities yet — n summons the first one",
                                |d| {
                                    // The roster takes the keyboard when
                                    // the users table is absent (a
                                    // non-admin: the registry is not
                                    // theirs to list) — else the screen's
                                    // keys would have nothing focused.
                                    let focus = store.conn.with_untracked(ConnPhase::is_known_non_admin);
                                    entities_table(gcx, &tt, d, ui.entity_sel, focus, move |_| {
                                        // Activation = the `m` manage
                                        // path (entities have no "edit";
                                        // the manage menu is the row's
                                        // one modal).
                                        manage_selected_entity(cx, &ctx_act);
                                    })
                                },
                            )
                        }
                    },
                ))
                // The `i inspects…` teaching line appears only when
                // there are rows to inspect (P3-B: teaching keys for
                // rows that don't exist is first-run noise); it returns
                // with the first entity. Pinned so it never crushes the
                // roster it describes.
                .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    let has_rows = store
                        .entities
                        .with(|d| d.ready().map(|e| !e.is_empty()).unwrap_or(false));
                    if has_rows {
                        line(vec![span(
                            "i inspects the selected entity (side panel) · the detail loads as you move the selection",
                            tt.text_faint,
                        )])
                    } else {
                        line(vec![span(String::new(), tt.text)])
                    }
                }))
                .element(t)
                .build(),
        )
        .build()
}

fn selected_entity(ctx: &Ctx) -> Option<EntityRow> {
    let idx = ctx.ui.entity_sel.get_untracked();
    ctx.store
        .entities
        .with_untracked(|d| d.ready().and_then(|d| d.get(idx).cloned()))
}

/// ONE edit entry — shared verbatim by the `e` key and the users
/// table's activation (Enter / Space / double-click): guards and
/// refusal notices can never drift between the two gestures.
fn edit_selected_user(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "editing a user") {
        return;
    }
    if let Some(u) = selected_user(ctx) {
        open_user_form(cx, ctx, Some(u));
    } else {
        ctx.store
            .notice
            .set(Some("no user selected — nothing to edit".into()));
    }
}

/// ONE manage entry — shared by the `m` key and the entities table's
/// activation.
fn manage_selected_entity(cx: Scope, ctx: &Ctx) {
    if let Some(e) = selected_entity(ctx) {
        super::entity_manage::open_manage_menu(cx, ctx, e);
    } else {
        ctx.store
            .notice
            .set(Some("no entity selected — nothing to manage".into()));
    }
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

fn selected_user(ctx: &Ctx) -> Option<UserRow> {
    let idx = ctx.ui.user_sel.get_untracked();
    ctx.store
        .users
        .with_untracked(|d| d.ready().and_then(|d| d.humans.get(idx).cloned()))
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
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(
                Element::new()
                    .style(
                        LayoutStyle::default()
                            .w(if indent { 2 } else { 0 })
                            .h(1)
                            .shrink(0.0),
                    )
                    .build(),
            )
            .child(
                super::switch::Switch::new(label, sig)
                    .unavailable(unavailable)
                    .busy_when(move || busy.get() == Some(key))
                    .notice(store.notice)
                    .on_request(move |want| request(key, want))
                    .element(scx, &tt)
                    .build(),
            )
            .child(line(vec![span(format!("— {help}"), tt.text_faint)]))
            .build()
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

/// The Active switch of the selected user: OFF asks first (the user is
/// signed out and cannot sign in), ON applies at once; your own row is
/// unavailable, with the reason.
fn switch_selected_active(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "switching a user's Active state") {
        return;
    }
    let Some(u) = selected_user(ctx) else {
        ctx.store
            .notice
            .set(Some("no user selected — nothing to switch".into()));
        return;
    };
    if own_key(&ctx.store) == Some((u.user_id.clone(), u.tenant_id.clone())) {
        ctx.store.notice.set(Some(OWN_ACCOUNT_REASON.into()));
        return;
    }
    let send = {
        let ctx = ctx.clone();
        let u = u.clone();
        move |active: bool| {
            ctx.send(Cmd::PatchUser {
                user_id: u.user_id.clone(),
                tenant_id: u.tenant_id.clone(),
                body: json!({ "enabled": active }).into(),
                form_id: None,
            })
        }
    };
    if u.enabled {
        super::confirm_danger(
            cx,
            ctx.ui,
            format!(
                "Deactivate {}? They are signed out until you turn Active back on.",
                u.user_id
            ),
            "Deactivate",
            "Cancel",
            move || send(false),
        );
    } else {
        send(true);
    }
}

#[allow(clippy::too_many_arguments)]
fn users_table(
    cx: Scope,
    t: &TokenSet,
    data: &[UserRow],
    sel: Signal<usize>,
    me: Option<(String, String)>,
    on_activate: impl FnMut(usize) + 'static,
    mut on_space: impl FnMut() + 'static,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let wide = vw >= 104;
    let mut rows: Vec<Vec<String>> = data
        .iter()
        .map(|u| {
            let own = me
                .as_ref()
                .map(|(id, tn)| *id == u.user_id && *tn == u.tenant_id)
                .unwrap_or(false);
            let mut row = vec![
                u.user_id.clone(),
                u.roles.join(", "),
                if u.email.is_empty() {
                    "—".into()
                } else {
                    u.email.clone()
                },
                u.mailbox.clone(),
                u.runtime_id.clone(),
                // The Active switch's marker (the row below the table
                // carries the highlighted switch of the selected user).
                if own {
                    format!("{} (you)", super::switch::marker(u.enabled, true))
                } else {
                    super::switch::marker(u.enabled, false).to_string()
                },
            ];
            if wide {
                row.push(u.tenant_id.clone());
            }
            row
        })
        .collect();
    // User ids and addresses discriminate on their TAIL; the role list,
    // the mailbox words and the switch marker are bounded.
    let mut rules = vec![
        widths::ColRule::tail("user", 10),
        widths::ColRule::head("role", 10),
        widths::ColRule::tail("email address", 14),
        widths::ColRule::head("mailbox", 14),
        widths::ColRule::tail("runtime", 8),
        widths::ColRule::head("active", 9),
    ];
    if wide {
        rules.push(widths::ColRule::tail("tenant", 8));
    }
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

fn entities_table(
    cx: Scope,
    t: &TokenSet,
    data: &[EntityRow],
    sel: Signal<usize>,
    autofocus: bool,
    on_activate: impl FnMut(usize) + 'static,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let wide = vw >= 100;
    let mut rows: Vec<Vec<String>> = data
        .iter()
        .map(|e| {
            let drives = match (e.open_questions, e.open_problems, e.open_interests) {
                (None, None, None) => "—".to_string(),
                (q, p, i) => format!(
                    "q:{} p:{} i:{}",
                    q.map(|v| v.to_string()).unwrap_or_else(|| "—".into()),
                    p.map(|v| v.to_string()).unwrap_or_else(|| "—".into()),
                    i.map(|v| v.to_string()).unwrap_or_else(|| "—".into()),
                ),
            };
            let mut row = vec![
                e.name.clone(),
                e.state.clone(),
                e.mode.clone().unwrap_or_else(|| "—".into()),
            ];
            if wide {
                row.push(e.handle.clone().unwrap_or_else(|| "—".into()));
            }
            row.push(drives);
            row
        })
        .collect();
    // Entity names and handles discriminate on their TAIL; state, mode
    // and the drives triple are bounded vocabularies.
    let mut rules = vec![
        widths::ColRule::tail("entity", 12),
        widths::ColRule::head("state", 9),
        widths::ColRule::head("mode", 9),
    ];
    if wide {
        rules.push(widths::ColRule::tail("handle", 16));
    }
    rules.push(widths::ColRule::head("open drives", 14));
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    let table = Table::new(cols)
        .rows(rows)
        .selection(sel)
        .on_activate(on_activate)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t);
    let table = if autofocus { table.autofocus() } else { table };
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(table.build())
        .child(legend(t))
        .build()
}

fn legend(t: &TokenSet) -> View {
    Element::new()
        .style(LayoutStyle::row().gap(1).h(1))
        .child(badge(t, "awake", Tone::Ok))
        .child(badge(t, "asleep", Tone::Muted))
        .child(badge(t, "paused", Tone::Warn))
        .child(line(vec![span(
            "  drives: open questions / problems / interests",
            t.text_faint,
        )]))
        .build()
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
