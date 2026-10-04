//! Workspaces (ACCOUNTS, right after Accounts) — R8.2: the web console's
//! new "Workspaces" page in the terminal. Which folders agents may read
//! and write: the gateway-wide policy and the per-account policies.
//!
//! - The page: the effective summary of the highlighted scope in one line,
//!   then one table — "Gateway policy" and every user account ("Own
//!   policy · …" / "Follows the gateway policy"). Enter edits the
//!   highlighted scope. A non-admin sees the gateway policy as one
//!   read-only line and edits their own policy.
//! - The editor (a full-width overlay, Esc closes): the access mode as a
//!   segmented switch (Allow my list / Allow everything except), the
//!   `[x] Trust the launch folder` switch, the allowed and the refused
//!   folders as rows (Enter edits a row in place, `+ Add a folder` adds
//!   one, `x` removes one), and for an account "Follow the gateway
//!   policy". Every change applies at once and says "Saved" in place —
//!   no Save per section. A typed folder is checked by the gateway first
//!   (`POST /workspace/path-check`); a refused one is not saved and its
//!   sentence is shown.
//!
//! Routes and the model: `store_workspaces.rs`; writes:
//! `worker_workspaces.rs`.

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

use super::kit::{self, InlineConfirm, Row, WrapTable};
use super::util::{line, span, span_bold};
use super::widths::ColRule;
use super::Ctx;
use crate::store::accounts::AccountRow;
use crate::store::operator::MyPolicy;
use crate::store::skills::Tone;
use crate::store::workspaces::{
    account_cell, account_policy, mode_label, public_line, summary, Edit, ListKind, Policy, Scope,
    ADD_FOLDER, FOLLOW_GATEWAY, MODE_ALLOW_ALL, MODE_ALLOW_LIST, MODE_HELP_ALL, MODE_HELP_LIST,
    REFUSED_HELP, SUBTITLE, TITLE, TRUST_HELP, TRUST_LABEL,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::OpCmd;
use crate::worker::workspaces::WsCmd;
use crate::worker::Cmd;

/// The page's one-sentence explanation (the web's).
pub const PURPOSE: &str = "The gateway policy applies to everyone; an account with its own policy uses that instead (the gateway's refused folders always apply).";

/// The footer verbs.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let _ = ctx;
    vec![
        ("↑/↓", "choose"),
        ("Enter", "edit policy"),
        ("r", "refresh"),
    ]
}

fn is_admin(ctx: &Ctx) -> bool {
    ctx.store.conn.with_untracked(ConnPhase::is_admin)
}

/// The user accounts listed under the gateway policy (entities set their
/// folders in Manage; archived accounts are left out).
pub fn account_rows(ctx: &Ctx) -> Vec<AccountRow> {
    ctx.store.accounts.with_untracked(|d| {
        d.ready()
            .map(|rows| {
                rows.iter()
                    .filter(|r| !r.is_entity() && !r.archived)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    })
}

/// The scope behind table row `i` (0 = the gateway policy).
fn scope_at(ctx: &Ctx, i: usize) -> Option<Scope> {
    if i == 0 {
        return Some(Scope::Gateway);
    }
    account_rows(ctx).get(i - 1).map(|r| Scope::Account {
        tenant_id: r.tenant_id.clone(),
        user_id: r.id.clone(),
    })
}

/// Read what the page needs (admin: the config + the accounts; anyone:
/// their own policy and the public gateway line).
pub fn refresh(ctx: &Ctx) {
    let store = ctx.store;
    if is_admin(ctx) {
        store.runtime_config.set(Loadable::Loading);
        ctx.send(Cmd::LoadRuntimeConfig);
        ctx.send(Cmd::load_accounts_for(&store));
    } else {
        store.op.my_policy.set(Loadable::Loading);
        ctx.send(Cmd::Operator(OpCmd::LoadMyPolicy));
        ctx.send(Cmd::Workspaces(WsCmd::LoadPublic));
    }
}

/// [`refresh`] for a harness that holds the store and the command channel
/// but no `Ctx` (the live drive tests).
pub fn refresh_for_tests(store: &crate::store::Store, tx: &std::sync::mpsc::Sender<Cmd>) {
    if store.conn.with_untracked(ConnPhase::is_admin) {
        let _ = tx.send(Cmd::LoadRuntimeConfig);
        let _ = tx.send(Cmd::load_accounts_for(store));
    } else {
        let _ = tx.send(Cmd::Operator(OpCmd::LoadMyPolicy));
        let _ = tx.send(Cmd::Workspaces(WsCmd::LoadPublic));
    }
}

/// The Accounts Workspace jump: the page focused on that account's row
/// (the web's `#workspaces?account=<id>`). The caller switches screens.
pub fn focus_account(ctx: &Ctx, tenant_id: &str, user_id: &str) {
    ctx.store
        .ws
        .focus
        .set(Some((tenant_id.to_string(), user_id.to_string())));
}

/// The current policy of `scope` and the gateway's (for inheritance).
fn current(ctx: &Ctx, scope: &Scope) -> Option<(Policy, Policy)> {
    match scope {
        Scope::Own => ctx.store.op.my_policy.with(|p| {
            p.ready().map(|p| {
                let gw = Policy {
                    mode: Some(p.eff_mode.clone()),
                    trust: Some(p.eff_trust),
                    ..Policy::default()
                };
                (own_policy(p), gw)
            })
        }),
        _ => ctx.store.runtime_config.with(|c| {
            c.ready().map(|c| {
                let gw = Policy::gateway(c);
                let p = match scope {
                    Scope::Gateway => gw.clone(),
                    Scope::Account { tenant_id, user_id } => {
                        account_policy(c, tenant_id, user_id).unwrap_or_default()
                    }
                    Scope::Own => unreachable!(),
                };
                (p, gw)
            })
        }),
    }
}

fn own_policy(p: &MyPolicy) -> Policy {
    Policy {
        mode: (!p.mode.is_empty()).then(|| p.mode.clone()),
        trust: match p.trust.as_str() {
            "on" => Some(true),
            "off" => Some(false),
            _ => None,
        },
        allowed: p.allowed.clone(),
        refused: p.blocked.clone(),
        keep: Default::default(),
    }
}

pub fn view(cx: Scope_, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ws = store.ws;
    let tt = *t;
    let keeper = super::util::FocusKeeper::new();
    // First visit (and after a reconnect reset): read what the page shows.
    {
        let c = ctx.clone();
        cx.effect(move || {
            if !c.store.conn.with(ConnPhase::is_connected) {
                return;
            }
            // Each slot loads only while never read (a slot landing must
            // not re-send the others: that would reset them to Loading).
            let admin = c.store.conn.with(ConnPhase::is_admin);
            let not_asked = |l: bool| l;
            if admin {
                if not_asked(
                    c.store
                        .runtime_config
                        .with(|r| matches!(r, Loadable::NotAsked)),
                ) {
                    c.store.runtime_config.set(Loadable::Loading);
                    c.send(Cmd::LoadRuntimeConfig);
                }
                if not_asked(c.store.accounts.with(|r| matches!(r, Loadable::NotAsked))) {
                    c.send(Cmd::load_accounts_for(&c.store));
                }
            } else {
                if not_asked(
                    c.store
                        .op
                        .my_policy
                        .with(|r| matches!(r, Loadable::NotAsked)),
                ) {
                    c.store.op.my_policy.set(Loadable::Loading);
                    c.send(Cmd::Operator(OpCmd::LoadMyPolicy));
                }
                if not_asked(ws.public.with(|r| matches!(r, Loadable::NotAsked))) {
                    ws.public.set(Loadable::Loading);
                    c.send(Cmd::Workspaces(WsCmd::LoadPublic));
                }
            }
        });
    }
    // The Accounts jump: select that account's row once the list is here.
    {
        let c = ctx.clone();
        cx.effect(move || {
            let Some((tenant, user)) = ws.focus.get() else {
                return;
            };
            c.store.accounts.with(|_| ());
            let rows = account_rows(&c);
            if let Some(i) = rows
                .iter()
                .position(|r| r.id == user && r.tenant_id == tenant)
            {
                ws.sel.set(i + 1);
                ws.focus.set(None);
            }
        });
    }
    let keys = ctx.clone();
    let root = Element::new()
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                if k.key == Key::Enter && !is_admin(&keys) {
                    open_editor(cx, &keys, Scope::Own);
                    ectx.stop_propagation();
                }
            }
        });
    let body = ctx.clone();
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
            .child(dyn_view_scoped(
                LayoutStyle::column().gap(0).grow(1.0).min_h(3),
                move |gcx| {
                    if store.conn.with(ConnPhase::is_admin) {
                        admin_page(gcx, cx, &body, &tt, &keeper)
                    } else {
                        own_page(gcx, &body, &tt, &keeper)
                    }
                },
            ))
            .element(t)
            .build(),
    )
    .build()
}

/// The reactive scope type (the page's `Scope` enum shadows the name).
type Scope_ = abstracttui::prelude::Scope;

/// `page` = the page's own scope: the editor opens on it, so a data
/// reload that rebuilds this view never closes an open editor.
fn admin_page(
    cx: Scope_,
    page: Scope_,
    ctx: &Ctx,
    t: &TokenSet,
    keeper: &super::util::FocusKeeper,
) -> View {
    let store = ctx.store;
    let ws = store.ws;
    let width = (abstracttui::app::use_viewport(cx).get().w - 4).max(20);
    let cfg = store.runtime_config.get();
    let _ = store.accounts.get();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    let c = match cfg {
        Loadable::Ready(c) => c,
        Loadable::Failed(e) => {
            return keeper.anchor(kit::sentence(
                t,
                &format!("Could not read the workspace policy. {e}"),
                width,
                t.error,
            ))
        }
        _ => return keeper.anchor(kit::sentence(t, "Loading…", width, t.text_muted)),
    };
    let gw = Policy::gateway(&c);
    let accounts = account_rows(ctx);
    let sel = ws.sel.get();
    // The effective summary of the highlighted scope, one line on top.
    let top = match sel {
        0 => format!("Gateway policy: {}", summary(&gw, &gw)),
        i => match accounts.get(i - 1) {
            Some(a) => {
                let p = account_policy(&c, &a.tenant_id, &a.id).filter(Policy::is_custom);
                match p {
                    Some(p) => format!("{}: {}", a.id, summary(&p, &gw)),
                    None => format!(
                        "{}: follows the gateway policy — {}",
                        a.id,
                        summary(&gw, &gw)
                    ),
                }
            }
            None => String::new(),
        },
    };
    col = col.child(kit::sentence(t, &top, width, t.text));
    col = col.child(kit::sentence(t, PURPOSE, width, t.text_muted));
    let mut rows = vec![Row::new(vec![
        "Gateway policy\nEveryone".into(),
        summary(&gw, &gw),
    ])];
    for a in &accounts {
        let p = account_policy(&c, &a.tenant_id, &a.id);
        let name = if a.tenant_id != "default" {
            format!("{}/{}", a.tenant_id, a.id)
        } else {
            a.id.clone()
        };
        rows.push(Row::new(vec![name, account_cell(p.as_ref(), &gw)]));
    }
    super::util::clamp_selection(cx, ws.sel, {
        let n = rows.len();
        move || n
    });
    let ctx_act = ctx.clone();
    col = col.child(
        keeper.wire(
            WrapTable::new(
                vec![ColRule::tail("Scope", 14), ColRule::head("Policy", 30)],
                rows,
                ws.sel,
            )
            .on_activate(move |i| {
                if let Some(s) = scope_at(&ctx_act, i) {
                    open_editor(page, &ctx_act, s);
                }
            })
            .element(cx, t),
        ),
    );
    col = col.child(kit::sentence(
        t,
        "Enter edits the highlighted policy. Entities set their folders in Accounts → Manage.",
        width,
        t.text_faint,
    ));
    col.build()
}

fn own_page(cx: Scope_, ctx: &Ctx, t: &TokenSet, keeper: &super::util::FocusKeeper) -> View {
    let store = ctx.store;
    let width = (abstracttui::app::use_viewport(cx).get().w - 4).max(20);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    match store.ws.public.get() {
        Loadable::Ready(v) => {
            col = col.child(kit::sentence(t, &public_line(&v), width, t.text_muted))
        }
        Loadable::Failed(e) => {
            col = col.child(kit::sentence(
                t,
                &format!("Could not read the gateway policy. {e}"),
                width,
                t.error,
            ))
        }
        _ => {}
    }
    let body = match store.op.my_policy.get() {
        Loadable::Ready(p) => {
            let own = own_policy(&p);
            let gw = Policy {
                mode: Some(p.eff_mode.clone()),
                trust: Some(p.eff_trust),
                ..Policy::default()
            };
            let state = if own.is_custom() {
                format!("Your policy: {}", summary(&own, &gw))
            } else {
                format!(
                    "Your policy follows the gateway policy — {}",
                    p.effective_text()
                )
            };
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(kit::sentence(t, &state, width, t.text))
                .child(kit::sentence(
                    t,
                    "Enter edits your policy.",
                    width,
                    t.text_faint,
                ))
                .build()
        }
        Loadable::Failed(e) => kit::sentence(
            t,
            &format!("Could not read your workspace policy. {e}"),
            width,
            t.error,
        ),
        _ => kit::sentence(t, "Loading…", width, t.text_muted),
    };
    col = col.child(keeper.anchor(body));
    col.build()
}

// ---------------------------------------------------------------- editor

/// One selectable line of the editor.
#[derive(Clone, Debug, PartialEq)]
pub enum Item {
    Mode,
    Trust,
    Caption(ListKind),
    Folder(ListKind, usize),
    Add(ListKind),
    Follow,
}

/// The editor's lines for `p` (captions are skipped by the selection).
pub fn items(scope: &Scope, p: &Policy) -> Vec<Item> {
    let mut out = vec![Item::Mode, Item::Trust];
    for kind in [ListKind::Allowed, ListKind::Refused] {
        out.push(Item::Caption(kind));
        for i in 0..p.list(kind).len() {
            out.push(Item::Folder(kind, i));
        }
        out.push(Item::Add(kind));
    }
    if !matches!(scope, Scope::Gateway) && p.is_custom() {
        out.push(Item::Follow);
    }
    out
}

fn step(items: &[Item], from: usize, down: bool) -> usize {
    let n = items.len() as i64;
    let mut i = from as i64;
    loop {
        i += if down { 1 } else { -1 };
        if i < 0 || i >= n {
            return from;
        }
        if !matches!(items[i as usize], Item::Caption(_)) {
            return i as usize;
        }
    }
}

/// Send one write for `scope` (the editor holds new writes while one is
/// in flight — the next press re-reads the landed state).
fn save(ctx: &Ctx, scope: &Scope, before: &Policy, after: Option<Policy>, what: &str) {
    let ws = ctx.store.ws;
    if ws.busy.get_untracked() {
        return;
    }
    ws.msg.set(Some(("Saving...".into(), Tone::Plain)));
    ctx.send(Cmd::Workspaces(WsCmd::Save {
        scope: scope.clone(),
        before: before.clone(),
        policy: after,
        what: what.to_string(),
    }));
}

/// The editor overlay for `scope`.
pub fn open_editor(cx: Scope_, ctx: &Ctx, scope: Scope) {
    if matches!(scope, Scope::Own)
        && ctx
            .store
            .op
            .my_policy
            .with_untracked(|p| p.ready().is_none())
    {
        ctx.store
            .notice
            .set(Some("Reading your workspace policy… — one moment".into()));
        return;
    }
    if !matches!(scope, Scope::Own)
        && ctx
            .store
            .runtime_config
            .with_untracked(|p| p.ready().is_none())
    {
        ctx.store
            .notice
            .set(Some("Reading the workspace policy… — one moment".into()));
        return;
    }
    let ws = ctx.store.ws;
    ws.item.set(0);
    ws.editing.set(None);
    ws.msg.set(None);
    let c = ctx.clone();
    let title = format!("Workspace policy — {}", scope.title());
    kit::open_overlay(
        ctx,
        cx,
        title,
        &[
            ("↑/↓", "choose"),
            ("space/Enter", "switch · edit"),
            ("←/→", "access mode"),
            ("x", "remove folder"),
        ],
        move |mcx, _close, guard| {
            let confirm = InlineConfirm::new(mcx);
            // Esc first cancels an open edit (the overlay stays).
            *guard.borrow_mut() = Some(Box::new(move || {
                if ws.editing.get_untracked().is_some() {
                    ws.editing.set(None);
                    ws.msg.set(None);
                    return true;
                }
                false
            }));
            // The keys live on the editor body itself (inside its scroll,
            // so ↑/↓ choose a line instead of scrolling the page).
            let root = Element::new()
                .focusable()
                .style(LayoutStyle::column().gap(0).grow(1.0));
            let root = confirm.keys(root);
            let body_ctx = c.clone();
            let body_scope = scope.clone();
            root.child(dyn_view_scoped(
                LayoutStyle::column().gap(0).grow(1.0),
                move |gcx| editor_body(gcx, &body_ctx, &body_scope, confirm),
            ))
            .child(confirm.view(&use_theme(mcx).get().tokens, 0))
            .build()
        },
    );
}

fn editor_key(ctx: &Ctx, scope: &Scope, confirm: InlineConfirm, key: Key) -> bool {
    let ws = ctx.store.ws;
    let Some((p, gw)) = current(ctx, scope) else {
        return false;
    };
    let list = items(scope, &p);
    let at = ws.item.get_untracked().min(list.len().saturating_sub(1));
    let item = list.get(at).cloned();
    let set_mode = |mode: &str| {
        if p.effective_mode(&gw) == mode && p.mode.is_some() {
            return;
        }
        let mut after = p.clone();
        after.mode = Some(mode.to_string());
        save(ctx, scope, &p, Some(after), "access mode");
    };
    match key {
        Key::Up => ws.item.set(step(&list, at, false)),
        Key::Down => ws.item.set(step(&list, at, true)),
        Key::Left if item == Some(Item::Mode) => set_mode("whitelist"),
        Key::Right if item == Some(Item::Mode) => set_mode("blacklist"),
        Key::Char(' ') | Key::Enter => match item {
            Some(Item::Mode) => {
                let next = if p.effective_mode(&gw) == "blacklist" {
                    "whitelist"
                } else {
                    "blacklist"
                };
                set_mode(next);
            }
            Some(Item::Trust) => {
                let mut after = p.clone();
                after.trust = Some(!p.effective_trust(&gw));
                save(ctx, scope, &p, Some(after), "launch-folder trust");
            }
            Some(Item::Folder(kind, i)) => {
                ws.draft
                    .set(p.list(kind).get(i).cloned().unwrap_or_default());
                ws.msg.set(None);
                ws.editing.set(Some(Edit::Row(kind, i)));
            }
            Some(Item::Add(kind)) => {
                ws.draft.set(String::new());
                ws.msg.set(None);
                ws.editing.set(Some(Edit::Add(kind)));
            }
            Some(Item::Follow) => {
                let c = ctx.clone();
                let s = scope.clone();
                let before = p.clone();
                confirm.ask(
                    format!(
                        "{} follows the gateway policy again? Its own folders and choices are dropped.",
                        match scope {
                            Scope::Account { user_id, .. } => user_id.clone(),
                            _ => "Your account".into(),
                        }
                    ),
                    FOLLOW_GATEWAY,
                    move || save(&c, &s, &before, None, "follow the gateway policy"),
                );
            }
            _ => return false,
        },
        Key::Char('x') | Key::Delete | Key::Backspace => match item {
            Some(Item::Folder(kind, i)) => {
                let mut after = p.clone();
                if i < after.list(kind).len() {
                    after.list_mut(kind).remove(i);
                }
                save(ctx, scope, &p, Some(after), "a folder removed");
                // Stay on the same position (the next row moves up).
            }
            _ => return false,
        },
        _ => return false,
    }
    true
}

fn editor_body(cx: Scope_, ctx: &Ctx, scope: &Scope, confirm: InlineConfirm) -> View {
    let t = use_theme(cx).get().tokens;
    let ws = ctx.store.ws;
    let width = (abstracttui::app::use_viewport(cx).get().w - 8).max(20);
    let Some((p, gw)) = current(ctx, scope) else {
        return kit::sentence(&t, "Loading…", width, t.text_muted);
    };
    let list = items(scope, &p);
    let at = ws.item.get().min(list.len().saturating_sub(1));
    let editing = ws.editing.get();
    let inherit = !matches!(scope, Scope::Gateway);
    let mut col = Element::new().style(LayoutStyle::column().gap(0));
    // The effective summary, one line on top.
    col = col.child(kit::sentence(
        &t,
        &format!("Effective: {}", summary(&p, &gw)),
        width,
        t.text,
    ));
    let mark = |i: usize| if i == at { "▸ " } else { "  " };
    for (i, it) in list.iter().enumerate() {
        let selected = i == at;
        let ink = if selected { t.accent } else { t.text };
        match it {
            Item::Mode => {
                let mode = p.effective_mode(&gw);
                let seg = |label: &str, on: bool| {
                    if on {
                        span_bold(format!("[{label}]"), t.accent)
                    } else {
                        span(format!(" {label} "), t.text_muted)
                    }
                };
                let mut spans = vec![
                    span(mark(i), ink),
                    span_bold("Access  ", ink),
                    seg(MODE_ALLOW_LIST, mode != "blacklist"),
                    span(" ", t.text),
                    seg(MODE_ALLOW_ALL, mode == "blacklist"),
                ];
                if inherit && p.mode.is_none() {
                    spans.push(span("  (the gateway's)", t.text_faint));
                }
                col = col.child(line(spans));
                col = col.child(kit::sentence(
                    &t,
                    &format!(
                        "    {}",
                        if mode == "blacklist" {
                            MODE_HELP_ALL
                        } else {
                            MODE_HELP_LIST
                        }
                    ),
                    width,
                    t.text_faint,
                ));
            }
            Item::Trust => {
                let on = p.effective_trust(&gw);
                let mut spans = vec![
                    span(mark(i), ink),
                    span_bold(
                        super::switch::switch_text(TRUST_LABEL, on, None, false),
                        ink,
                    ),
                ];
                if inherit && p.trust.is_none() {
                    spans.push(span("  (the gateway's)", t.text_faint));
                }
                col = col.child(line(spans));
                col = col.child(kit::sentence(
                    &t,
                    &format!("    {TRUST_HELP}"),
                    width,
                    t.text_faint,
                ));
            }
            Item::Caption(kind) => {
                let mut spans = vec![span_bold(format!("  {}", kind.title()), t.text)];
                if *kind == ListKind::Refused {
                    spans.push(span(format!("  {REFUSED_HELP}"), t.text_faint));
                } else if p.effective_mode(&gw) == "blacklist" {
                    spans.push(span(
                        "  Not used while everything is allowed.",
                        t.text_faint,
                    ));
                }
                col = col.child(line(spans));
                if p.list(*kind).is_empty() {
                    col = col.child(line(vec![span("      None.", t.text_faint)]));
                }
            }
            Item::Folder(kind, n) => {
                if editing == Some(Edit::Row(*kind, *n)) {
                    col = col.child(folder_input(cx, ctx, scope, &p, Edit::Row(*kind, *n)));
                } else {
                    let path = p.list(*kind).get(*n).cloned().unwrap_or_default();
                    col = col.child(line(vec![
                        span(format!("  {}", mark(i)), ink),
                        span(path, ink),
                    ]));
                }
            }
            Item::Add(kind) => {
                if editing == Some(Edit::Add(*kind)) {
                    col = col.child(folder_input(cx, ctx, scope, &p, Edit::Add(*kind)));
                } else {
                    col = col.child(line(vec![
                        span(format!("  {}", mark(i)), ink),
                        span(ADD_FOLDER, if selected { t.accent } else { t.text_muted }),
                    ]));
                }
            }
            Item::Follow => {
                col = col.child(line(vec![span(String::new(), t.text)]));
                col = col.child(line(vec![
                    span(mark(i), ink),
                    span_bold(FOLLOW_GATEWAY, ink),
                ]));
            }
        }
    }
    // The outcome line is its own reactive view: a new message must not
    // rebuild the editor (an open input would lose its caret).
    col = col.child(dyn_view(
        LayoutStyle::column().gap(0).shrink(0.0),
        move || {
            let t = use_theme(cx).get().tokens;
            match ws.msg.get() {
                Some((text, tone)) => {
                    let ink = match tone {
                        Tone::Ok => t.ok,
                        Tone::Error => t.error,
                        Tone::Plain => t.text_muted,
                    };
                    kit::sentence(&t, &text, width, ink)
                }
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        },
    ));
    let _ = mode_label;
    // Not editing: the editor takes the keyboard back (an in-place input
    // that held it has just closed).
    if editing.is_none() {
        col = col.focusable().autofocus();
    }
    let keys_ctx = ctx.clone();
    let keys_scope = scope.clone();
    let col = col.on(Phase::Bubble, move |ectx, ev| {
        if let UiEvent::Key(k) = ev {
            if k.mods.0 != 0 || ws.editing.get_untracked().is_some() || confirm.is_open() {
                return;
            }
            if editor_key(&keys_ctx, &keys_scope, confirm, k.key) {
                ectx.stop_propagation();
            }
        }
    });
    Scroll::new(col.build())
        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
        .scrollbar_auto_hide(true)
        .view(cx)
}

/// The in-place folder input: Enter checks then saves; Esc keeps.
fn folder_input(cx: Scope_, ctx: &Ctx, scope: &Scope, p: &Policy, edit: Edit) -> View {
    let t = use_theme(cx).get().tokens;
    let ws = ctx.store.ws;
    let c = ctx.clone();
    let s = scope.clone();
    let before = p.clone();
    let e = edit.clone();
    kit::inline_input(
        cx,
        &t,
        "    Folder:",
        ws.draft,
        "/absolute/path",
        move |typed| {
            let typed = typed.trim().to_string();
            if typed.is_empty() {
                ws.editing.set(None);
                return;
            }
            if ws.busy.get_untracked() {
                return;
            }
            ws.msg
                .set(Some(("Checking the folder...".into(), Tone::Plain)));
            c.send(Cmd::Workspaces(WsCmd::PutFolder {
                scope: s.clone(),
                before: before.clone(),
                edit: e.clone(),
                path: typed,
            }));
        },
        move || {
            ws.editing.set(None);
            ws.msg.set(None);
        },
    )
}
