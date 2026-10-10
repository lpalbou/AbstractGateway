//! Users & entities: gateway user CRUD (admin) + the entity roster
//! (summon `n`, talk `c`, spark templates `s`, manage `m`).
//!
//! The token rule: create/rotate responses carry the token EXACTLY
//! ONCE — it goes to a dedicated modal with a copy affordance and an
//! explicit "will not be shown again", never to logs or the journal.

use abstracttui::prelude::*;
use abstracttui::widgets::{Table, Tone};
use serde_json::{json, Value};

use super::util::{field, line, span, span_bold};
use super::w::action::{button, On};
use super::w::{Action, Cell, Col, ColW, DataTable, Row as WRow};
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
pub const ADMIN_KEYS: &[&str] = &["E", "a", "e", "t", "v", "x"];

/// The page's data effects (selection sync, warm entity detail, the email
/// switches' read, the sandbox line's read, the Observer link modal).
fn install_page_effects(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let ui = ctx.ui;
    let acc = store.acc;

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
    // The command sandbox state line (R12.1) reads `GET /workspace/policy`
    // once a principal is here (every signed-in principal sees it).
    {
        let ctx_ws = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected
                && store
                    .workspace_policy
                    .with_untracked(|a| matches!(a, Loadable::NotAsked))
            {
                store.workspace_policy.set(Loadable::Loading);
                ctx_ws.send(Cmd::LoadWorkspacePolicy);
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
}

// ---------------------------------------------------------------------------
// R15 Accounts (DESIGN-TUI.md §3.1): the head with its buttons and the Show
// archived toggle, ONE table (Name · Email · Runtime · Active · Actions)
// whose row actions are glyph buttons in the web's order with the web's
// tooltips, the Active state a Toggle, the Runtime a link; "Email for
// everyone" as a card under the table. Every action is a click AND a key.
// ---------------------------------------------------------------------------

/// The Accounts page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    install_page_effects(cx, ctx);
    let store = ctx.store;
    let ui = ctx.ui;
    // The keyboard selection (by key) drives the legacy index the other
    // paths read (`selected_account`, the entity inspector).
    cx.effect(move || {
        let key = ui.acc_key.get();
        let _ = store.acc.show_archived.get();
        store.accounts.with(|_| ());
        let rows = visible_accounts(&store);
        let i = key
            .and_then(|k| rows.iter().position(|r| row_key(r) == k))
            .unwrap_or(0);
        if ui.account_sel.get_untracked() != i {
            ui.account_sel.set(i);
        }
    });
    // …and the legacy index (other screens, keys) moves the keyed one.
    cx.effect(move || {
        let i = ui.account_sel.get();
        let rows = visible_accounts(&store);
        if let Some(r) = rows.get(i) {
            let k = row_key(r);
            if ui
                .acc_key
                .with_untracked(|cur| cur.as_deref() != Some(k.as_str()))
            {
                ui.acc_key.set(Some(k));
            }
        }
    });
    let keys = ctx.clone();
    let tt = *t;
    Element::new()
        .style(LayoutStyle::column().grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .on(abstracttui::ui::Phase::Bubble, move |ectx, ev| {
            if let abstracttui::ui::UiEvent::Key(k) = ev {
                if k.mods.0 != 0
                    && !matches!(k.key, Key::Char(c) if c.is_ascii_uppercase() || c == '@')
                {
                    return;
                }
                if handle_key(cx, &keys, k.key) {
                    ectx.stop_propagation();
                }
            }
        })
        .child(head(cx, ctx, &tt))
        .child(sandbox_state(ctx, &tt))
        .child(table_region(cx, ctx, &tt))
        .child(email_card(cx, ctx, &tt))
        .build()
}

/// A row's stable key (tenant/id).
pub fn row_key(r: &AccountRow) -> String {
    format!("{}/{}", r.tenant_id, r.id)
}

/// The page width the content lays out in.
fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// Title + subtitle on the left, the web's head buttons on the right
/// (admins: Eligible workspaces · Show archived · Create user · Create
/// entity; everyone: Create entity). Wraps the buttons under the title
/// on a narrow page.
fn head(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    // Actions open on the PAGE scope (pcx): a region re-render must never
    // dispose a modal or prompt one of its buttons opened.
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |hcx| {
        let store = ctx.store;
        let admin = store.conn.with(ConnPhase::is_admin);
        let non_admin = store.conn.with(ConnPhase::is_known_non_admin);
        let show = store.acc.show_archived.get();
        let w = page_w(hcx);
        let (title, subtitle) = if non_admin {
            (NON_ADMIN_TITLE, NON_ADMIN_SUBTITLE)
        } else {
            ("Accounts", ACCOUNTS_SUBTITLE)
        };
        let mut buttons: Vec<View> = Vec::new();
        let mut bw = 0;
        let mut push = |v: View, width: i32, buttons: &mut Vec<View>| {
            bw += width + 1;
            buttons.push(v);
        };
        // Show archived (everyone, R16.5): an admin's archived accounts, a member's
        // archived entities (their creator unarchives them). The web's tooltip sentences.
        let show_archived = |push: &mut dyn FnMut(View, i32, &mut Vec<View>),
                             buttons: &mut Vec<View>| {
            let c = ctx.clone();
            let tip = if admin {
                format!("{SHOW_ARCHIVED_TIP_ADMIN}  (h)")
            } else {
                format!("{SHOW_ARCHIVED_TIP_MEMBER}  (h)")
            };
            let tg = super::w::Toggle::new(show)
                .label("Show archived")
                .tip(tip)
                .on_change(move |v| c.store.acc.show_archived.set(v));
            let wd = tg.width();
            push(tg.view(hcx, &tt), wd, buttons);
        };
        if admin {
            let a = Action::label("eligible", "Eligible workspaces")
                .key('E')
                .tooltip("The workspaces accounts may choose from, and the most each one allows.");
            let c = ctx.clone();
            let wd = a.width();
            push(
                button(hcx, &tt, &a, On::Page, true, move || {
                    eligible_workspaces(pcx, &c)
                }),
                wd,
                &mut buttons,
            );
            show_archived(&mut push, &mut buttons);
            let a = Action::label("create_user", "Create user")
                .key('a')
                .tooltip("Create a gateway user and issue their token (shown once)");
            let c = ctx.clone();
            let wd = a.width();
            push(
                button(hcx, &tt, &a, On::Page, true, move || {
                    open_user_form(pcx, &c, None)
                }),
                wd,
                &mut buttons,
            );
        }
        if !admin {
            show_archived(&mut push, &mut buttons);
        }
        let a = Action::label("create_entity", "Create entity")
            .key('n')
            .tooltip("Summon a new entity from a spark template (the name is permanent)");
        let c = ctx.clone();
        let wd = a.width();
        push(
            button(hcx, &tt, &a, On::Page, true, move || {
                if c.store.conn.with_untracked(ConnPhase::is_connected) {
                    super::entity_create::open_summon_form(pcx, &c);
                }
            }),
            wd,
            &mut buttons,
        );
        let title_w = abstracttui::text::width(title).max(abstracttui::text::width(subtitle));
        let mut btn_row = Element::new().style(
            LayoutStyle::row()
                .height(Dimension::Cells(1))
                .gap(1)
                .shrink(0.0),
        );
        for b in buttons {
            btn_row = btn_row.child(b);
        }
        let btn_row = btn_row.build();
        let side = title_w + bw + 2 <= w;
        let title_views = Element::new()
            .style(if side {
                LayoutStyle::column()
                    .width(Dimension::Cells(w - bw - 1))
                    .shrink(0.0)
            } else {
                LayoutStyle::column().shrink(0.0)
            })
            .child(super::w::paint::fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![super::w::Ink::new(title, tt.text).bold()],
                None,
            ))
            .child(super::w::form::sentence(
                &tt,
                subtitle,
                (w - if side { bw + 2 } else { 0 }).max(20),
                tt.text_muted,
            ))
            .build();
        if side {
            Element::new()
                .style(LayoutStyle::row().shrink(0.0))
                .child(title_views)
                .child(btn_row)
                .build()
        } else {
            Element::new()
                .style(LayoutStyle::column().shrink(0.0))
                .child(title_views)
                .child(btn_row)
                .build()
        }
    })
}

/// The web subtitle of the admin page.
pub const ACCOUNTS_SUBTITLE: &str = "People who use this gateway and the entities that act on it";

/// R12.1: the host's command sandbox, a STATE line under the head, the
/// gateway's words verbatim; its sentence (the web tooltip) as the tooltip
/// AND as a muted line (keyboard users read it too).
fn sandbox_state(ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |scx| {
        let (l, sentence, warn) =
            super::workspace_chooser::sandbox_line(&store.workspace_policy.get());
        let mut c = Element::new().style(LayoutStyle::column().shrink(0.0));
        let scope_note = store.conn.with(|c| match c {
            ConnPhase::Connected(id) | ConnPhase::Verifying(id) if !id.admin => {
                Some(non_admin_scope_line(&id.user_id))
            }
            _ => None,
        });
        let w = page_w(scx);
        if let Some(note) = scope_note {
            c = c.child(super::w::form::sentence(&tt, &note, w, tt.text_muted));
        }
        if !l.is_empty() {
            let line_el = Element::new()
                .style(LayoutStyle::line(1).shrink(0.0))
                .child(super::w::paint::fill_line(
                    LayoutStyle::fill(),
                    vec![super::w::Ink::new(
                        l,
                        if warn { tt.warn } else { tt.text_muted },
                    )],
                    None,
                ));
            c = c.child(super::w::tip::with_tip(scx, line_el, sentence.clone()).build());
            // The web's tooltip, also as a muted line: keyboard users read it.
            if !sentence.is_empty() {
                c = c.child(super::w::form::sentence(&tt, &sentence, w, tt.text_faint));
            }
        }
        c.build()
    })
}

/// The table region (rebuilt when the rows, the role or the width change).
fn table_region(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    // Row actions open on the PAGE scope (cx), never this region's: an
    // accounts reload re-renders the table while a modal it opened is up.
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
        let store = ctx.store;
        let ui = ctx.ui;
        let admin = store.conn.with(ConnPhase::is_admin);
        let show = store.acc.show_archived.get();
        let vp = crate::ui::page_viewport(gcx).get();
        let w = (vp.w - 2).max(20);
        let data = store.accounts.get();
        // With no table to take the keyboard, a focus anchor keeps the
        // page's keys alive (summon, create, refresh).
        let anchor = |v: View| -> View {
            Element::new()
                .style(LayoutStyle::column().shrink(0.0))
                .focusable()
                .autofocus()
                .child(v)
                .build()
        };
        let rows: Vec<AccountRow> = match data {
            Loadable::NotAsked | Loadable::Loading => {
                return anchor(super::w::form::sentence(&tt, "Loading…", w, tt.text_muted));
            }
            Loadable::Failed(e) => return anchor(super::util::error_panel(&tt, &e)),
            Loadable::Ready(all) => all.into_iter().filter(|r| show || !r.archived).collect(),
        };
        let own = own_key(&store);
        let cols = vec![
            Col::new("Name", ColW::Fit { min: 8, max: 24 }),
            Col::new("Email", ColW::Flex { weight: 1, min: 14 }),
            Col::new("Runtime", ColW::Fit { min: 7, max: 16 }),
            Col::new("Active", ColW::Fit { min: 6, max: 8 }),
            Col::new("Actions", ColW::Fit { min: 6, max: 30 }),
        ];
        let table_rows: Vec<WRow> = rows
            .iter()
            .map(|r| account_table_row(&tt, r, admin, own.as_ref()))
            .collect();
        // Rows below the head, the sandbox line and the card (admins).
        // The card's height (compact on a short terminal: its sentences
        // move into the toggles' tooltips).
        let card = match (admin, email_card_compact(gcx)) {
            (false, _) => 0,
            (true, true) => 5,
            (true, false) => 13,
        };
        let max_rows = (vp.h - 6 - card).max(4);
        let ctx_a = ctx.clone();
        let ctx_t = ctx.clone();
        let ctx_e = ctx.clone();
        let ctx_s = ctx.clone();
        DataTable::new(cols, table_rows, ui.acc_key)
            .width(w)
            .max_rows(max_rows)
            .top(ui.acc_top)
            .empty("No accounts yet.")
            .autofocus()
            .on_focus(move || {})
            .on_action(move |key, id| row_action(cx, &ctx_a, key, id))
            .on_toggle(move |key, _id, want| {
                select_key(&ctx_t, key);
                switch_active(cx, &ctx_t, want);
            })
            .on_activate(move |key| row_action(cx, &ctx_e, key, "email"))
            .on_space(move |key| {
                select_key(&ctx_s, key);
                if let Some(r) = selected_account(&ctx_s) {
                    switch_active(cx, &ctx_s, !r.active);
                }
            })
            .view(gcx, &tt)
    })
}

/// One account as a table row.
fn account_table_row(
    t: &TokenSet,
    r: &AccountRow,
    admin: bool,
    own: Option<&(String, String)>,
) -> WRow {
    use super::w::Ink;
    let id = if r.tenant_id != "default" {
        format!("{}/{}", r.tenant_id, r.id)
    } else {
        r.id.clone()
    };
    let kind = r.kind_label();
    let mut kind_line = vec![Ink::new(kind, kind_ink(t, kind))];
    if r.archived {
        kind_line.push(Ink::new(" · Archived", t.text_muted));
    }
    let name = Cell::Lines(vec![vec![Ink::new(id, t.text).bold()], kind_line]);
    let state = match r.mailbox.state.as_str() {
        "connected" => "connected",
        "receive_only" => "receive only",
        "needs_reconnect" => "needs reconnecting",
        "not_connected" => "not connected",
        "paused" => "paused",
        "unavailable" => "mailbox not available",
        _ => "",
    };
    let address = r.email_address.as_ref().or(r.mailbox.address.as_ref());
    let email = match (address, state) {
        (None, _) => Cell::text("No address", t.text_muted),
        (Some(a), "") => Cell::text(a.clone(), t.text),
        (Some(a), s) => Cell::Text(vec![
            Ink::new(format!("{a} · "), t.text),
            Ink::new(s, if s == "connected" { t.ok } else { t.warn }),
        ]),
    };
    let runtime = match &r.runtime_id {
        Some(rt) if admin => Cell::Link {
            label: rt.clone(),
            action: "runtime",
            tip: Some(format!("Runtimes of {}: {rt}  (g)", r.id)),
        },
        Some(rt) => Cell::text(rt.clone(), t.text),
        None => Cell::text("No runtime", t.text_muted),
    };
    let is_own = own == Some(&(r.id.clone(), r.tenant_id.clone()));
    let active = if r.archived {
        Cell::text("Archived", t.text_muted)
    } else {
        let refused = if is_own {
            Some(
                r.refusal("suspend")
                    .unwrap_or_else(|| OWN_ACCOUNT_REASON.to_string()),
            )
        } else {
            // The row's served `suspend` action decides (an admin: any account;
            // R16.5: the creator of an entity turns it on or off).
            r.refusal("suspend")
        };
        Cell::Toggle {
            id: "active",
            on: r.active,
            refused,
            tip: Some("Active  (Space)".into()),
        }
    };
    let note = if r.mailbox.state == "receive_only" || r.mailbox.state == "needs_reconnect" {
        r.mailbox.reason.clone().map(|why| (why, t.warn))
    } else {
        mailbox_detail(r).map(|m| (m, t.text_muted))
    };
    WRow::new(
        row_key(r),
        vec![
            name,
            email,
            runtime,
            active,
            Cell::Actions(row_actions(r, admin)),
        ],
    )
    .dim(r.archived || !r.active)
    .note(note)
}

/// The kind's ink (the web's row tints, as the kind label's colour).
fn kind_ink(t: &TokenSet, kind: &str) -> Rgba {
    match kind {
        "Admin" => t.accent,
        "Entity" => t.info,
        _ => t.text_muted,
    }
}

/// A row's actions, in the web's icon order with the web's tooltips
/// (`ACCOUNT_TIPS`): users Email · OpenAI API · Logs · Workspaces ·
/// Preferences · Rotate · Archive; entities Email · Logs · Workspaces ·
/// Preferences · Manage · Archive; archived rows Logs · Unarchive. An
/// action the gateway refuses is shown FAINT with its reason in the
/// tooltip (and on a press) — never silently missing.
pub fn row_actions(r: &AccountRow, admin: bool) -> Vec<Action> {
    let n = &r.id;
    let mut out = Vec::new();
    if r.archived {
        out.push(
            Action::glyph("logs", "Logs")
                .key('l')
                .tooltip(format!("Activity log of {n}"))
                .refused(r.refusal("logs")),
        );
        out.push(
            Action::glyph("unarchive", "Unarchive")
                .key('d')
                .tooltip(format!("Unarchive {n} (comes back inactive)"))
                .refused(r.refusal("unarchive")),
        );
        return out;
    }
    out.push(
        Action::glyph("email", "Email")
            .key('@')
            .tooltip(format!("Email address and mailbox of {n}"))
            .refused(r.refusal("email")),
    );
    if !r.is_entity() {
        let why = match &r.openai_action {
            None => Some("This gateway does not offer the OpenAI API switch.".to_string()),
            Some(a) if !a.available => Some(
                a.reason
                    .clone()
                    .unwrap_or_else(|| "The OpenAI API switch is not available here.".into()),
            ),
            _ if !admin => Some("Only an admin can change who may use the OpenAI API.".into()),
            _ => None,
        };
        out.push(
            Action::glyph("openai", "OpenAI API")
                .key('o')
                .tooltip(format!("OpenAI API access for {n}"))
                .refused(why),
        );
    }
    out.push(
        Action::glyph("logs", "Logs")
            .key('l')
            .tooltip(format!("Activity log of {n}"))
            .refused(r.refusal("logs")),
    );
    out.push(
        Action::glyph("workspaces", "Workspaces")
            .key('w')
            .tooltip(format!("Workspaces {n}'s agents may use"))
            .refused(r.refusal("workspace")),
    );
    let prefs_why = match &r.preferences_action {
        None => Some(
            "this gateway does not offer per-account preferences (GET /accounts/{id}/preferences needs a newer gateway)"
                .to_string(),
        ),
        Some(a) if !a.available => Some(
            a.reason
                .clone()
                .unwrap_or_else(|| format!("Preferences are not available for {n}.")),
        ),
        _ => None,
    };
    out.push(
        Action::glyph("preferences", "Preferences")
            .key('p')
            .tooltip(format!("Default workflows of {n}"))
            .refused(prefs_why),
    );
    if r.is_entity() {
        out.push(
            Action::glyph("manage", "Manage")
                .key('m')
                .tooltip(format!("Manage {n} (mind, voice, prompt…)"))
                .refused(r.refusal("manage")),
        );
    } else {
        out.push(
            Action::glyph("rotate", "Rotate")
                .key('t')
                .tooltip(format!("Rotate {n}'s sign-in token"))
                .refused(if admin {
                    r.refusal("rotate")
                } else {
                    Some("Only an admin can rotate a token.".into())
                }),
        );
    }
    out.push(
        Action::glyph("archive", "Archive")
            .key('d')
            .tooltip(format!("Archive {n} (kept, hidden)"))
            .refused(r.refusal("archive"))
            .danger(),
    );
    out
}

/// Select the row `key` (the legacy index paths read it at once).
fn select_key(ctx: &Ctx, key: &str) {
    ctx.ui.acc_key.set(Some(key.to_string()));
    let rows = visible_accounts(&ctx.store);
    if let Some(i) = rows.iter().position(|r| row_key(r) == key) {
        if ctx.ui.account_sel.get_untracked() != i {
            ctx.ui.account_sel.set(i);
        }
    }
}

/// A row action (a click or its key): the row becomes the selection, the
/// action runs; a refused action says why (status bar) and does nothing.
fn row_action(cx: Scope, ctx: &Ctx, key: &str, id: &str) {
    select_key(ctx, key);
    let Some(r) = selected_account(ctx) else {
        return;
    };
    let admin = ctx.store.conn.with_untracked(ConnPhase::is_admin);
    // The admin-only verbs answer with the gateway-wide admin sentence.
    let gate = match id {
        "openai" => Some("changing who may use the OpenAI API"),
        "rotate" => Some("rotating a token"),
        "runtime" => Some("the Runtimes page"),
        _ => None,
    };
    if let Some(what) = gate {
        if !super::util::admin_gate(&ctx.store, what) {
            return;
        }
    }
    if id != "runtime" {
        if let Some(a) = row_actions(&r, admin).into_iter().find(|a| a.id == id) {
            if let Err(why) = a.enabled {
                ctx.store.notice.set(Some(why));
                return;
            }
        } else {
            ctx.store
                .notice
                .set(Some(format!("{} has no {id} action", r.id)));
            return;
        }
    }
    match id {
        "email" => email_selected(cx, ctx),
        "openai" => openai_selected(cx, ctx),
        "logs" => open_activity(cx, ctx),
        "workspaces" => workspace_selected(cx, ctx),
        "preferences" => preferences_selected(cx, ctx),
        "manage" => manage_selected_entity(cx, ctx),
        "rotate" => rotate_selected(cx, ctx),
        "archive" | "unarchive" => archive_selected(cx, ctx),
        "runtime" => runtime_selected(ctx),
        _ => {}
    }
}

/// The footer verbs of this page (R15: only what applies — the selected
/// row's actions, then the page keys).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let store = ctx.store;
    let admin = store.conn.with(ConnPhase::is_admin);
    store.accounts.with(|_| ());
    let _ = ctx.ui.acc_key.get();
    let mut out = vec![("↑↓", "rows"), ("Enter", "Email"), ("Tab", "actions")];
    out.push(("Space", "Active"));
    let row = selected_account(ctx);
    if let Some(r) = row {
        for a in row_actions(&r, admin) {
            if a.is_enabled() {
                if let Some(k) = a.key {
                    out.push((key_label(k), static_label(&a.label)));
                }
            }
        }
    }
    out.push(("h", "Show archived"));
    if admin {
        out.push(("E", "Eligible workspaces"));
        out.push(("a", "Create user"));
    }
    out.push(("n", "Create entity"));
    out.push(("e", "edit user"));
    out.push(("c", "talk"));
    out.push(("i", "inspect"));
    out.push(("s", "spark templates"));
    out.push(("v", "kept data of deleted users"));
    out.push(("x", "reset mailbox override"));
    out.push(("r", "refresh"));
    out
}

fn key_label(k: char) -> &'static str {
    match k {
        '@' => "@",
        'o' => "o",
        'l' => "l",
        'w' => "w",
        'p' => "p",
        'm' => "m",
        't' => "t",
        'd' => "d",
        _ => "?",
    }
}

fn static_label(l: &str) -> &'static str {
    match l {
        "Email" => "Email",
        "OpenAI API" => "OpenAI API",
        "Logs" => "Logs",
        "Workspaces" => "Workspaces",
        "Preferences" => "Preferences",
        "Manage" => "Manage",
        "Rotate" => "Rotate",
        "Archive" => "Archive",
        "Unarchive" => "Unarchive",
        _ => "",
    }
}

/// One key of the page (the selected row's accelerators + page keys).
/// True when handled. ←/→ never arrive here (the shell owns them).
fn handle_key(cx: Scope, ctx: &Ctx, key: Key) -> bool {
    let store = ctx.store;
    let acc = store.acc;
    let row_key_of = |ctx: &Ctx| selected_account(ctx).map(|r| row_key(&r));
    let act = |id: &str| {
        if let Some(k) = row_key_of(ctx) {
            row_action(cx, ctx, &k, id);
        } else {
            store
                .notice
                .set(Some("no account selected — choose a row first".into()));
        }
    };
    match key {
        Key::Char('@') => act("email"),
        Key::Char('o') => act("openai"),
        Key::Char('l') => act("logs"),
        Key::Char('w') => act("workspaces"),
        Key::Char('p') => act("preferences"),
        Key::Char('m') => act("manage"),
        Key::Char('t') => act("rotate"),
        Key::Char('d') => {
            let archived = selected_account(ctx).map(|r| r.archived).unwrap_or(false);
            act(if archived { "unarchive" } else { "archive" })
        }
        Key::Char('g') => act("runtime"),
        // Everyone: an admin's archived accounts, a member's archived entities (R16.5:
        // their creator unarchives them).
        Key::Char('h') => acc.show_archived.update(|v| *v = !*v),
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
        Key::Char('E') => eligible_workspaces(cx, ctx),
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
        _ => return false,
    }
    true
}

/// Rotate (a confirmation with the web's sentence, then the token box).
fn rotate_selected(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "rotating a token") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
        return;
    };
    if let Some(why) = r.refusal("rotate") {
        ctx.store.notice.set(Some(why));
        return;
    }
    let Some(u) = selected_user(ctx) else {
        ctx.store.notice.set(Some(format!(
            "the users registry has not loaded {} yet — r refreshes",
            r.id
        )));
        return;
    };
    let c = ctx.clone();
    super::w::confirm(
        ctx,
        cx,
        format!(
            "Rotate the token of {}? The current token stops working now; the new one is shown once.",
            u.user_id
        ),
        "Rotate",
        "Cancel",
        move || {
            c.send(Cmd::PatchUser {
                user_id: u.user_id.clone(),
                tenant_id: u.tenant_id.clone(),
                body: json!({ "rotate_token": true }).into(),
                form_id: None,
            })
        },
    );
}

/// Archive (confirmed) / Unarchive (at once — it comes back inactive).
fn archive_selected(cx: Scope, ctx: &Ctx) {
    let Some(r) = selected_account(ctx) else {
        return;
    };
    let verb = if r.archived { "unarchive" } else { "archive" };
    if let Some(why) = r.refusal(verb) {
        ctx.store.notice.set(Some(why));
        return;
    }
    let admin = ctx.store.conn.with_untracked(|c| c.is_admin());
    if r.archived {
        ctx.send(Cmd::ArchiveAccount {
            id: r.id,
            tenant_id: r.tenant_id,
            unarchive: true,
            admin,
        });
        return;
    }
    let c = ctx.clone();
    let q = archive_question(&r);
    super::w::confirm(ctx, cx, q, "Archive", "Cancel", move || {
        c.send(Cmd::ArchiveAccount {
            id: r.id.clone(),
            tenant_id: r.tenant_id.clone(),
            unarchive: false,
            admin,
        })
    });
}

/// The Active toggle: turning OFF asks the web's question (Deactivate /
/// Suspend), turning ON applies at once. Who may switch it is the row's
/// served `suspend` action (an admin: any account; R16.5: the creator of an
/// entity, that entity).
fn switch_active(cx: Scope, ctx: &Ctx, want: bool) {
    let Some(r) = selected_account(ctx) else {
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
    if want == r.active {
        return;
    }
    let admin = ctx.store.conn.with_untracked(ConnPhase::is_admin);
    let send = {
        let ctx = ctx.clone();
        let r = r.clone();
        move |active: bool| {
            ctx.send(Cmd::SetAccountActive {
                id: r.id.clone(),
                tenant_id: r.tenant_id.clone(),
                entity: r.is_entity(),
                active,
                admin,
            })
        }
    };
    if want {
        send(true);
        return;
    }
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
    super::w::confirm(ctx, cx, question, verb, "Cancel", move || send(false));
}

/// OpenAI API — <id>: the web modal (one switch, applies on change).
fn openai_selected(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "changing who may use the OpenAI API") {
        return;
    }
    let Some(r) = selected_account(ctx) else {
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
    super::w::FormModal::new(format!("OpenAI API — {id}"))
        .lead(super::openai_api::account_lead(&id))
        .size(84, 22)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let store = c.store;
            let state = mcx.signal(super::w::FieldState::Idle);
            // The state line follows the write's verified outcome (the
            // worker's notice carries the web sentence).
            {
                let id = id.clone();
                mcx.effect(move || {
                    if let Some(n) = store.notice.get() {
                        if n.starts_with(&format!("Saved: {id}")) {
                            let head = n.split(" — ").next().unwrap_or(&n).to_string();
                            if n.contains("FAILED") || n.contains("VERIFY FAILED") {
                                state.set(super::w::FieldState::Refused(n.clone()));
                            } else {
                                state.set(super::w::FieldState::Saved(head));
                            }
                        }
                    }
                });
            }
            let toggle_region = {
                let c = c.clone();
                let r = r.clone();
                let id = id.clone();
                dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), move |tcx| {
                    let t = use_theme(tcx).get().tokens;
                    let on = store.accounts.with(|d| {
                        d.ready()
                            .and_then(|rows| rows.iter().find(|a| a.id == id).map(|a| a.openai_api))
                    });
                    let on = on.unwrap_or(r.openai_api);
                    let c = c.clone();
                    let r = r.clone();
                    Element::new()
                        .style(LayoutStyle::line(1).shrink(0.0))
                        .child(
                            super::w::Toggle::new(on)
                                .label("OpenAI API")
                                .autofocus(true)
                                .on_change(move |want| {
                                    state.set(super::w::FieldState::Saving);
                                    c.send(Cmd::SetAccountOpenAi {
                                        id: r.id.clone(),
                                        tenant_id: r.tenant_id.clone(),
                                        enabled: want,
                                    })
                                })
                                .view(tcx, &t),
                        )
                        .build()
                })
            };
            let t = use_theme(mcx).get().tokens;
            let close2 = close.clone();
            let keys = account_openai_keys(mcx, cx, &c, &r, w);
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(toggle_region)
                .child(super::w::state_line(state, w))
                .child(keys)
                .child(
                    Element::new()
                        .style(LayoutStyle::default().grow(1.0))
                        .build(),
                )
                .child(super::w::form::button_row(vec![button(
                    mcx,
                    &t,
                    &Action::label("close", "Close"),
                    On::Raised,
                    true,
                    move || close2(),
                )]))
                .build()
        });
}

/// The OpenAI API dialog's "API keys" (round 16): the account's named keys
/// (name · created · last used · fingerprint; never a key), each with
/// Revoke → `w::Confirm` [Revoke] [Cancel] → applies at once.
fn account_openai_keys(mcx: Scope, pcx: Scope, ctx: &Ctx, r: &AccountRow, w: i32) -> View {
    use super::openai_api as oa;
    use crate::store::json::WriteState;
    use crate::worker::json::JsonCmd;
    let store = ctx.store;
    let id = r.id.clone();
    let tenant = r.tenant_id.clone();
    let slot = oa::account_keys_slot(&id);
    let path = oa::account_keys_path(&id, &tenant);
    store.json.set(&slot, Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get(&slot, path.clone())));
    let status = mcx.signal(Option::<(bool, String)>::None);
    let revoking = mcx.signal(String::new());
    let sel = mcx.signal(Option::<String>::None);
    {
        mcx.effect(move || {
            if let Some(wr) = store.json.write(oa::KEY_ACCOUNT_REVOKE) {
                if !wr.is_pending() {
                    let label = revoking.get_untracked();
                    match wr {
                        WriteState::Done(_) => {
                            status.set(Some((true, oa::revoked_sentence(&label))))
                        }
                        WriteState::Failed(e) => {
                            status.set(Some((false, format!("Not revoked: {}", e.message))))
                        }
                        WriteState::Pending => {}
                    }
                    store.json.set_write(oa::KEY_ACCOUNT_REVOKE, None);
                }
            }
        });
    }
    let c = ctx.clone();
    dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |kcx| {
        let t = use_theme(kcx).get().tokens;
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(super::w::fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![],
                None,
            ))
            .child(super::w::fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![super::w::Ink::new(oa::ACCOUNT_KEYS_TITLE, t.text).bold()],
                None,
            ));
        let keys = store.json.get(&slot);
        match &keys {
            Loadable::Failed(e) => {
                col = col.child(super::w::form::sentence(
                    &t,
                    &format!("Could not read the keys: {}", e.message),
                    w,
                    t.error,
                ));
            }
            Loadable::Ready(v) => {
                let list: Vec<Value> = v
                    .get("keys")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                if list.is_empty() {
                    col = col.child(super::w::form::sentence(
                        &t,
                        &oa::account_keys_empty(&id),
                        w,
                        t.text_muted,
                    ));
                } else {
                    let rows: Vec<WRow> = list
                        .iter()
                        .map(|k| {
                            let cells = oa::key_cells(k);
                            WRow::new(
                                k.get("fingerprint")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_string(),
                                vec![
                                    Cell::text(cells[0].clone(), t.text),
                                    Cell::Lines(vec![
                                        vec![super::w::Ink::new(cells[1].clone(), t.text_muted)],
                                        vec![super::w::Ink::new(cells[2].clone(), t.text_muted)],
                                    ]),
                                    Cell::text(cells[3].clone(), t.info),
                                    Cell::Actions(oa::key_actions(k)),
                                ],
                            )
                        })
                        .collect();
                    let (c2, id2, tenant2, slot2, path2) = (
                        c.clone(),
                        id.clone(),
                        tenant.clone(),
                        slot.clone(),
                        path.clone(),
                    );
                    let list2 = list.clone();
                    col = col.child(
                        DataTable::new(
                            vec![
                                Col::new("Name", ColW::Flex { weight: 1, min: 10 }),
                                Col::new("Created · Last used", ColW::Flex { weight: 2, min: 14 }),
                                Col::new("Fingerprint", ColW::Fit { min: 12, max: 12 }),
                                Col::new("", ColW::Fit { min: 8, max: 10 }),
                            ],
                            rows,
                            sel,
                        )
                        .width(w)
                        .max_rows(8)
                        .on_action(move |fp, aid| {
                            if aid != "revoke" {
                                return;
                            }
                            let Some(k) = list2
                                .iter()
                                .find(|k| k.get("fingerprint").and_then(Value::as_str) == Some(fp))
                            else {
                                return;
                            };
                            let label = k
                                .get("label")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string();
                            let (c3, id3, tenant3, slot3, path3, fp3) = (
                                c2.clone(),
                                id2.clone(),
                                tenant2.clone(),
                                slot2.clone(),
                                path2.clone(),
                                fp.to_string(),
                            );
                            super::w::Confirm::danger(
                                oa::account_revoke_sentence(&label, &id2),
                                "Revoke",
                                "Cancel",
                            )
                            .open(pcx, c2.ui, move || {
                                revoking.set(label.clone());
                                status.set(None);
                                c3.store
                                    .json
                                    .set_write(oa::KEY_ACCOUNT_REVOKE, Some(WriteState::Pending));
                                c3.send(Cmd::Json(JsonCmd::Send {
                                    key: oa::KEY_ACCOUNT_REVOKE.into(),
                                    method: "DELETE".into(),
                                    path: oa::account_key_path(&id3, &tenant3, &fp3),
                                    body: Value::Null,
                                    slow: false,
                                    label: format!("Accounts: revoke {label} of {id3}"),
                                    reload: vec![(slot3, path3)],
                                    journal: false,
                                }));
                            });
                        })
                        .view(kcx, &t),
                    );
                }
            }
            _ => {
                col = col.child(super::w::form::sentence(
                    &t,
                    "Reading the keys...",
                    w,
                    t.text_muted,
                ));
            }
        }
        if let Some((ok, text)) = status.get() {
            col = col.child(super::w::form::sentence(
                &t,
                &text,
                w,
                if ok { t.ok } else { t.error },
            ));
        }
        col.build()
    })
}

/// Short terminals get the compact card (one line per switch).
fn email_card_compact(cx: Scope) -> bool {
    crate::ui::page_viewport(cx).get().h < 30
}

/// "Email for everyone" (admins): the card under the table — three
/// switches that apply at once, each with its sentence (R8.1: no
/// Advanced).
fn email_card(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    use crate::worker::operator::{EmailAction, OpCmd};
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let form_id = crate::worker::next_form_id();
    let busy = cx.signal(Option::<&'static str>::None);
    cx.effect(move || {
        if let Some((fid, _)) = ui.write_done.get() {
            if fid == form_id {
                ui.write_done.set(None);
                busy.set(None);
            }
        }
    });
    let ctx = ctx.clone();
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |scx| {
        if !store.conn.with(ConnPhase::is_admin) {
            return Element::new().style(LayoutStyle::default().h(0)).build();
        }
        let caps = store.op.email_caps.get();
        let now_busy = busy.get();
        let w = page_w(scx) - 4;
        let compact = email_card_compact(scx);
        let unavailable = match &caps {
            Loadable::Failed(e) => Some(format!("couldn't read the email switches: {e}")),
            Loadable::Ready(_) => None,
            _ => Some("reading…".to_string()),
        };
        let vals = match &caps {
            Loadable::Ready(c) => (c.email, c.agent_tools, c.recovery),
            _ => (false, false, false),
        };
        let mut body = Element::new().style(LayoutStyle::column().shrink(0.0));
        for (key, label, help, on) in [
            ("email", MAILBOXES_LABEL, MAILBOXES_HELP, vals.0),
            (
                "email_agent_tools",
                AGENT_TOOLS_LABEL,
                AGENT_TOOLS_HELP,
                vals.1,
            ),
            ("email_recovery", RECOVERY_LABEL, RECOVERY_HELP, vals.2),
        ] {
            let c = ctx.clone();
            let mut tg = super::w::Toggle::new(on)
                .label(label)
                .busy(now_busy == Some(key))
                .refused(unavailable.clone())
                .on_change(move |want| {
                    if busy.get_untracked().is_some() {
                        return;
                    }
                    busy.set(Some(key));
                    c.send(Cmd::Operator(OpCmd::Email {
                        action: EmailAction::CapsDefaults(json!({ key: want }).into()),
                        form_id: Some(form_id),
                    }));
                });
            if compact {
                tg = tg.tip(help);
            }
            body = body.child(
                Element::new()
                    .style(LayoutStyle::line(1).shrink(0.0))
                    .child(tg.view(scx, &tt))
                    .build(),
            );
            if compact {
                continue;
            }
            for l in super::w::paint::wrap(help, w - 3) {
                body = body.child(super::w::paint::fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![super::w::Ink::new(format!("   {l}"), tt.text_muted)],
                    None,
                ));
            }
        }
        Block::new()
            .border(BorderKind::Rounded)
            .title("Email for everyone")
            .fill(tt.surface)
            .layout(LayoutStyle::column().shrink(0.0).padding(Edges {
                left: 1,
                right: 1,
                top: 0,
                bottom: 0,
            }))
            .child(body.build())
            .element(&tt)
            .build()
    })
}

thread_local! {
    /// A Logs "Open in Observer" is waiting for its one-time link.
    static OBSERVER_PENDING: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// The web's "Show archived" tooltips (console.py renderAccountsArchivedSwitch).
pub const SHOW_ARCHIVED_TIP_ADMIN: &str =
    "Archived accounts can't sign in or act; their runs and history are kept.";
pub const SHOW_ARCHIVED_TIP_MEMBER: &str = "Archived entities you created can't act; their memory, runs and history are kept, and you can unarchive them.";
/// The web's create-user role options (console.py `#new-roles`): exactly two roles.
pub const ROLE_OPTION_MEMBER: &str = "Member — runs workflows on their own runtime";
pub const ROLE_OPTION_ADMIN: &str = "Admin — manages this gateway";

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

/// The Email cell (R8.2: ONE column): the address and the mailbox's
/// connection state — `test@x · connected`, `No address`.
pub fn email_cell(r: &AccountRow) -> String {
    let state = match r.mailbox.state.as_str() {
        "connected" => "connected",
        "receive_only" => "receive only",
        "needs_reconnect" => "needs reconnecting",
        "not_connected" => "not connected",
        "paused" => "paused",
        "unavailable" => "mailbox not available",
        _ => "",
    };
    let address = r.email_address.as_ref().or(r.mailbox.address.as_ref());
    let mut cell = match (address, state) {
        (None, _) => "No address".to_string(),
        (Some(a), "") => a.clone(),
        (Some(a), s) => format!("{a} · {s}"),
    };
    // Receive only / needs reconnecting: the reason on the next line (the web's).
    if r.mailbox.state == "receive_only" || r.mailbox.state == "needs_reconnect" {
        if let Some(why) = &r.mailbox.reason {
            cell.push('\n');
            cell.push_str(why);
        }
    }
    cell
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

/// `<id>: a · b · c` laid out in lines no wider than `width`, breaking
/// only between two actions.
pub fn action_lines(id: &str, actions: &[String], width: i32) -> Vec<String> {
    let w = width.max(20) as usize;
    let mut out = Vec::new();
    let mut cur = format!("{id}:");
    for (i, a) in actions.iter().enumerate() {
        let piece = if i == 0 {
            format!(" {a}")
        } else {
            format!(" · {a}")
        };
        let fits = abstracttui::text::width(&cur) as usize
            + abstracttui::text::width(&piece) as usize
            <= w;
        if fits || i == 0 {
            cur.push_str(&piece);
        } else {
            out.push(format!("{cur} ·"));
            cur = format!("  {a}");
        }
    }
    out.push(cur);
    out
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

/// `w`: the selected account's workspaces (R11.1 account level; every
/// row the gateway marks `actions.workspace` available — humans, entities
/// for admins and their creator, your own row as `me`).
fn workspace_selected(cx: Scope, ctx: &Ctx) {
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — no workspaces to show".into()));
        return;
    };
    if let Some(why) = r.refusal("workspace") {
        ctx.store.notice.set(Some(why));
        return;
    }
    super::workspace_chooser::open(cx, ctx, account_workspace_target(&r));
}

/// The account-level chooser target of a row: `me` for the signed-in
/// principal's own row, else `tenant:id` (the web's `wsAccountKey`).
pub fn account_workspace_target(r: &AccountRow) -> super::workspace_chooser::Target {
    super::workspace_chooser::Target::Account {
        key: if r.own {
            "me".into()
        } else {
            format!("{}:{}", r.tenant_id, r.id)
        },
        id: r.id.clone(),
    }
}

/// `p`: the selected account's Preferences (the default workflow per app;
/// R14.2, the web row's "Default workflows of <id>").
fn preferences_selected(cx: Scope, ctx: &Ctx) {
    let Some(r) = selected_account(ctx) else {
        ctx.store
            .notice
            .set(Some("no account selected — no preferences to show".into()));
        return;
    };
    match &r.preferences_action {
        None => ctx.store.notice.set(Some(
            "this gateway does not offer per-account preferences (GET /accounts/{id}/preferences needs a newer gateway)".into(),
        )),
        Some(a) if !a.available => ctx.store.notice.set(Some(
            a.reason
                .clone()
                .unwrap_or_else(|| format!("Preferences are not available for {}.", r.id)),
        )),
        Some(_) => super::account_preferences::open(cx, ctx, r),
    }
}

/// `E` (admins): "Eligible workspaces" — the gateway level, the web's
/// button at the top of Accounts.
fn eligible_workspaces(cx: Scope, ctx: &Ctx) {
    super::workspace_chooser::open(cx, ctx, super::workspace_chooser::Target::Gateway);
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
    // The web's sentences (console.py, another user's Email modal).
    match r.mailbox.state.as_str() {
        "connected" => format!(
            "Mailbox: connected as {}. You never see anyone's mail.",
            r.mailbox.address.clone().unwrap_or_default()
        ),
        "paused" => format!("Mailbox: paused by {}. You never see anyone's mail.", r.id),
        _ => format!(
            "Mailbox: not connected — only {} can connect a mailbox. You never see anyone's mail.",
            r.id
        ),
    }
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

/// Retained runtime planes (a user moved to another runtime, or a deleted
/// user before 0.11): transfer to a living user. Never purged — accounts
/// and their data are archived, never deleted (round 3).
/// The retained-runtimes dialog's declared width, and what its chrome
/// spends: the Modal's own 1-cell margin plus the dress Block's border
/// and padding, left and right (`ui::open_form_guarded`). Budgeting the
/// grid inside it starts here, not at the viewport.
const RESV_MODAL_W: i32 = 88;
const RESV_MODAL_CHROME: i32 = 4;

pub(crate) fn open_reservations_modal(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    store.reservations.set(crate::store::Loadable::Loading);
    ctx.send(Cmd::LoadReservations);
    let ctx2 = ctx.clone();
    let screen_cx = cx;
    super::w::FormModal::new("Retained runtimes")
        .lead("Transfer one to a user — the data is never deleted.")
        .size(RESV_MODAL_W, 30)
        .open(ctx, cx, move |mcx, close, _guard, _inner_w| {
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
                .style(LayoutStyle::column().gap(0).grow(1.0))
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
                                let vw = RESV_MODAL_W.min(crate::ui::page_viewport(gcx).get().w)
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
                        let transfer = move || {
                            let idx = ctx_t.ui.resv_sel.get_untracked();
                            let row = ctx_t
                                .store
                                .reservations
                                .with_untracked(|d| d.ready().and_then(|r| r.get(idx).cloned()));
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
                        };
                        super::w::form::button_row(vec![
                            button(
                                bcx,
                                &t,
                                &Action::label("transfer", "Transfer to user"),
                                On::Raised,
                                true,
                                transfer,
                            ),
                            button(
                                bcx,
                                &t,
                                &Action::label("close", "Close"),
                                On::Raised,
                                true,
                                move || close_esc(),
                            ),
                        ])
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

/// "Mailboxes for users" — the administrator's one email switch.
pub const MAILBOXES_LABEL: &str = "Mailboxes for users";
pub const MAILBOXES_HELP: &str = "Users may connect their own mailbox for their agents, automations and notifications. You never see anyone's mail.";
pub const AGENT_TOOLS_LABEL: &str = "Agent email tools for users";
pub const AGENT_TOOLS_HELP: &str = "Users may let their agents and workflows use their mailbox. Each user still switches the tools on for themselves.";
pub const RECOVERY_LABEL: &str = "Sign-in by email";
pub const RECOVERY_HELP: &str = "Shows 'Forgot your token?' on the sign-in page. Whoever controls a user's mailbox can then sign in as that user.";

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

/// Create (existing=None) or edit a user.
fn open_user_form(cx: Scope, ctx: &Ctx, existing: Option<UserRow>) {
    let create = existing.is_none();
    let ctx2 = ctx.clone();
    let modal_title = if create {
        "Create user".to_string()
    } else {
        format!(
            "Edit user '{}'",
            existing.as_ref().map(|u| u.user_id.as_str()).unwrap_or("")
        )
    };
    super::w::FormModal::new(modal_title).size(96, 40).open(
        ctx,
        cx,
        move |mcx, close, guard, _inner_w| {
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
            let advanced = mcx.signal(true);
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

            let ctx_save = ctx2.clone();
            let ex_save = ex.clone();
            let close_cancel = close.clone();

            Element::new()
                .style(LayoutStyle::column().gap(0))
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
                    // Two roles (operator ruling 2026-10-08): no viewer / read-only.
                    MultiSelect::new(vec![
                        SelectOption::keyed("user", ROLE_OPTION_MEMBER),
                        SelectOption::keyed("admin", ROLE_OPTION_ADMIN),
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
                    // R15 D1: no "Advanced" — a visible section named by its
                    // content (COORD "R15 SECTION NAMES").
                    dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), move |_dcx| {
                        let t = theme.get().tokens;
                        line(vec![span_bold(RUNTIME_TENANT_SECTION, t.text)])
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

/// R15 D1 section name (shared with the web console).
pub const RUNTIME_TENANT_SECTION: &str = "Runtime and tenant";
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
        crate::ui::page_viewport(cx).get_untracked(),
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

/// Activity — <id> (Logs): the web modal — filter chips as a Segmented,
/// the events as a table with an "Open in Observer" button on rows that
/// carry a run, the API's note under it.
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
    let mine = ctx.store.conn.with_untracked(ConnPhase::is_known_non_admin);
    ctx.send(Cmd::LoadActivity {
        target: target.clone(),
        mine,
        key: key.clone(),
        kind: String::new(),
    });
    let ctx2 = ctx.clone();
    super::w::FormModal::new(format!("Activity — {who}"))
        .size(110, 34)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let store = ctx2.store;
            let t = use_theme(mcx).get().tokens;
            let reload = {
                let ctx3 = ctx2.clone();
                let target = target.clone();
                let key = key.clone();
                move |i: usize| {
                    filter.set(i);
                    ctx3.send(Cmd::LoadActivity {
                        target: target.clone(),
                        mine,
                        key: key.clone(),
                        kind: ACTIVITY_FILTERS[i].1.to_string(),
                    });
                }
            };
            let chips =
                super::w::Segmented::new(ACTIVITY_FILTERS.iter().map(|(l, _)| l.to_string()), None)
                    .bind(filter)
                    .on_pick(reload)
                    .view(mcx, &t);
            let sel = mcx.signal(Option::<String>::None);
            let ctx_o = ctx2.clone();
            let table = dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                let t = use_theme(gcx).get().tokens;
                let vp = crate::ui::page_viewport(gcx).get();
                match store.activity.get().map(|(_, _, d)| d) {
                    None | Some(Loadable::NotAsked) | Some(Loadable::Loading) => {
                        super::w::form::sentence(&t, "Loading…", w, t.text_muted)
                    }
                    Some(Loadable::Failed(e)) => super::w::form::sentence(
                        &t,
                        &crate::worker::skills::refusal_text(&e),
                        w,
                        t.error,
                    ),
                    Some(Loadable::Ready(d)) => {
                        let today = crate::localtime::local_today();
                        let events = d.events.clone();
                        let rows: Vec<WRow> = events
                            .iter()
                            .enumerate()
                            .map(|(i, e)| {
                                let title = if e.ok {
                                    e.title.clone()
                                } else {
                                    format!("✗ {}", e.title)
                                };
                                let mut acts = Vec::new();
                                if e.run_id.is_some() || e.observer_path.is_some() {
                                    acts.push(
                                        Action::label("observer", "Open in Observer")
                                            .key('o')
                                            .tooltip(match &e.run_id {
                                                Some(id) => format!("Open run {id} in Observer"),
                                                None => "Open in Observer".into(),
                                            }),
                                    );
                                }
                                WRow::new(
                                    i.to_string(),
                                    vec![
                                        Cell::text(activity_time(&e.ts, &today), t.text_muted),
                                        Cell::text(title, if e.ok { t.text } else { t.error }),
                                        Cell::text(
                                            e.detail.clone().unwrap_or_default(),
                                            t.text_muted,
                                        ),
                                        Cell::Actions(acts),
                                    ],
                                )
                            })
                            .collect();
                        let ctx_a = ctx_o.clone();
                        let ev2 = events.clone();
                        let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                        col = col.child(
                            DataTable::new(
                                vec![
                                    Col::new("Time", ColW::Fit { min: 5, max: 12 }),
                                    Col::new("Event", ColW::Fit { min: 10, max: 28 }),
                                    Col::new("Detail", ColW::Flex { weight: 1, min: 12 }),
                                    Col::new("", ColW::Fit { min: 0, max: 20 }),
                                ],
                                rows,
                                sel,
                            )
                            .width(w)
                            .max_rows((vp.h - 14).max(4))
                            .empty(ACTIVITY_EMPTY)
                            .autofocus()
                            .on_action(move |k, _id| {
                                if let Some(e) = k.parse::<usize>().ok().and_then(|i| ev2.get(i)) {
                                    open_in_observer(&ctx_a, e);
                                }
                            })
                            .view(gcx, &t),
                        );
                        if let Some(note) = d.note.clone().filter(|n| !n.trim().is_empty()) {
                            col = col.child(super::w::form::sentence(
                                &t,
                                note.trim(),
                                w,
                                t.text_faint,
                            ));
                        }
                        col.build()
                    }
                }
            });
            let close2 = close.clone();
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(chips)
                .child(
                    Element::new()
                        .style(LayoutStyle::line(1).shrink(0.0))
                        .build(),
                )
                .child(table)
                .child(
                    Element::new()
                        .style(LayoutStyle::default().grow(1.0))
                        .build(),
                )
                .child(super::w::form::button_row(vec![button(
                    mcx,
                    &t,
                    &Action::label("close", "Close"),
                    On::Raised,
                    true,
                    move || close2(),
                )]))
                .build()
        });
}

/// "Open in Observer" for one activity event (a one-time signed-in link
/// through the Apps lane; the link modal opens on the Accounts page).
fn open_in_observer(ctx: &Ctx, e: &crate::store::accounts::ActivityEvent) {
    let path = match (&e.observer_path, &e.run_id) {
        (Some(p), _) => p.clone(),
        (None, Some(id)) => format!("/apps/observer/#run/{id}"),
        _ => {
            ctx.store
                .notice
                .set(Some("this event has no run to open".into()));
            return;
        }
    };
    OBSERVER_PENDING.with(|p| p.set(true));
    ctx.send(Cmd::AppAct {
        app_id: "observer".into(),
        name: "AbstractObserver".into(),
        verb: crate::store::apps::AppVerb::Open,
        path: Some(path),
        start_first: false,
    });
}
