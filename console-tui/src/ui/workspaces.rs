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
    account_entries, account_policy, admin_summary, gateway_sentence, list_help, own_sentence,
    self_summary, Edit, ListKind, Policy, Scope, ACCOUNTS_NOTE, ADD_FOLDER, ENTITIES_NOTE,
    GATEWAY_NOTE, LEGACY_HELP, LEGACY_LABEL, MODE_ALLOW_ALL, MODE_ALLOW_LIST, MODE_HELP,
    MODE_HELP_ALL, MODE_HELP_LIST, MODE_LABEL, OWN_LABEL, ROOT_HELP, ROOT_LABEL, SELF_NOTE,
    SUBTITLE, TITLE, TRUST_HELP, TRUST_LABEL,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::OpCmd;
use crate::worker::workspaces::WsCmd;
use crate::worker::Cmd;

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
        ..Policy::default()
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
    // The summary in one line on top (the web's): the gateway policy and
    // how many accounts have their own.
    let own = account_entries(&c).len();
    col = col.child(kit::sentence(t, &admin_summary(&gw, own), width, t.text));
    col = col.child(kit::sentence(
        t,
        &format!("{GATEWAY_NOTE} {ACCOUNTS_NOTE}"),
        width,
        t.text_muted,
    ));
    let mut rows = vec![Row::new(vec![
        "Gateway policy".into(),
        gateway_sentence(&gw),
    ])];
    for a in &accounts {
        let p = account_policy(&c, &a.tenant_id, &a.id);
        let name = if a.tenant_id != "default" {
            format!("{}/{}", a.tenant_id, a.id)
        } else {
            a.id.clone()
        };
        let cell = match p.as_ref().filter(|p| p.is_custom()) {
            Some(_) => format!("[x] {OWN_LABEL} · {}", own_sentence(p.as_ref(), &gw)),
            None => format!("[ ] {OWN_LABEL} · {}", own_sentence(None, &gw)),
        };
        rows.push(Row::new(vec![name, cell]));
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
        &format!("Enter edits the highlighted policy. {ENTITIES_NOTE}"),
        width,
        t.text_faint,
    ));
    col.build()
}

fn own_page(cx: Scope_, ctx: &Ctx, t: &TokenSet, keeper: &super::util::FocusKeeper) -> View {
    let store = ctx.store;
    let width = (abstracttui::app::use_viewport(cx).get().w - 4).max(20);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    let body = match store.op.my_policy.get() {
        Loadable::Ready(p) => {
            let own = own_policy(&p);
            let gw = Policy {
                mode: Some(p.eff_mode.clone()),
                trust: Some(p.eff_trust),
                ..Policy::default()
            };
            let _ = &gw;
            let state = self_summary(&own, p.customized, &p.eff_mode, p.eff_trust);
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(kit::sentence(t, &state, width, t.text))
                .child(kit::sentence(t, SELF_NOTE, width, t.text_muted))
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
    /// An account's / your "Own policy" switch (off = follows the gateway).
    Own,
    Mode,
    Trust,
    Caption(ListKind),
    Folder(ListKind, usize),
    Add(ListKind),
    /// The gateway's Default folder.
    Root,
    /// "Any folder (old clients)" (gateway, accounts — never self-service).
    Legacy,
}

/// The editor's lines for `p` (captions are skipped by the selection),
/// in the web editor's order.
pub fn items(scope: &Scope, p: &Policy) -> Vec<Item> {
    let mut out = Vec::new();
    if !matches!(scope, Scope::Gateway) {
        out.push(Item::Own);
        if !p.is_custom() {
            return out;
        }
    }
    out.push(Item::Mode);
    out.push(Item::Trust);
    for kind in [ListKind::Allowed, ListKind::Refused] {
        out.push(Item::Caption(kind));
        for i in 0..p.list(kind).len() {
            out.push(Item::Folder(kind, i));
        }
        out.push(Item::Add(kind));
    }
    if matches!(scope, Scope::Gateway) {
        out.push(Item::Root);
    }
    if !matches!(scope, Scope::Own) {
        out.push(Item::Legacy);
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
            Some(Item::Own) if !p.is_custom() => {
                // An own policy starts as a copy of the gateway's mode and
                // trust, then diverges (the web's).
                let after = Policy {
                    mode: gw.mode.clone(),
                    trust: gw.trust,
                    ..Policy::default()
                };
                save(ctx, scope, &p, Some(after), "own policy on");
            }
            Some(Item::Own) => {
                let c = ctx.clone();
                let s = scope.clone();
                let before = p.clone();
                confirm.ask(
                    match scope {
                        Scope::Account { user_id, .. } => format!(
                            "Drop {user_id}'s own policy? Their agents follow the gateway policy again."
                        ),
                        _ => "Drop your own policy? Your agents follow the gateway policy again."
                            .to_string(),
                    },
                    "Drop",
                    move || save(&c, &s, &before, None, "own policy dropped"),
                );
            }
            Some(Item::Root) => {
                ws.draft.set(p.root.clone().unwrap_or_default());
                ws.msg.set(None);
                ws.editing.set(Some(Edit::Root));
            }
            Some(Item::Legacy) => {
                let mut after = p.clone();
                after.legacy = Some(p.legacy != Some(true));
                save(ctx, scope, &p, Some(after), "any folder (old clients)");
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
        &match scope {
            Scope::Gateway => gateway_sentence(&p),
            _ => own_sentence(Some(&p), &gw),
        },
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
                col = col.child(line(vec![
                    span(mark(i), ink),
                    span_bold(format!("{MODE_LABEL}  "), ink),
                    span(MODE_HELP, t.text_faint),
                ]));
                let mut spans = vec![
                    span("    ", ink),
                    seg(MODE_ALLOW_LIST, mode != "blacklist"),
                    span(" ", t.text),
                    seg(MODE_ALLOW_ALL, mode == "blacklist"),
                ];
                if inherit && p.mode.is_none() {
                    spans.push(span("  (the gateway's)", t.text_faint));
                }
                col = col.child(line(spans));
                col = col.child(kit::sentence_indent(
                    &t,
                    if mode == "blacklist" {
                        MODE_HELP_ALL
                    } else {
                        MODE_HELP_LIST
                    },
                    width,
                    4,
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
                col = col.child(kit::sentence_indent(&t, TRUST_HELP, width, 4, t.text_faint));
            }
            Item::Caption(kind) => {
                col = col.child(line(vec![span_bold(format!("  {}", kind.title()), t.text)]));
                col = col.child(kit::sentence_indent(
                    &t,
                    list_help(scope, *kind),
                    width,
                    4,
                    t.text_faint,
                ));
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
            Item::Own => {
                col = col.child(line(vec![
                    span(mark(i), ink),
                    span_bold(
                        super::switch::switch_text(OWN_LABEL, p.is_custom(), None, false),
                        ink,
                    ),
                ]));
                if !p.is_custom() {
                    col = col.child(kit::sentence_indent(
                        &t,
                        &format!("{} {}", own_sentence(None, &gw), gateway_sentence(&gw)),
                        width,
                        4,
                        t.text_faint,
                    ));
                }
            }
            Item::Root => {
                if editing == Some(Edit::Root) {
                    col = col.child(folder_input(cx, ctx, scope, &p, Edit::Root));
                } else {
                    let saved = p.root.clone().unwrap_or_default();
                    let shown = if saved.is_empty() {
                        if p.root_in_use.is_empty() {
                            "(the gateway's own folder)".to_string()
                        } else {
                            format!("(the gateway's own folder) {}", p.root_in_use)
                        }
                    } else {
                        saved
                    };
                    col = col.child(line(vec![
                        span(mark(i), ink),
                        span_bold(format!("{ROOT_LABEL}: "), ink),
                        span(shown, t.text),
                    ]));
                }
                col = col.child(kit::sentence_indent(&t, ROOT_HELP, width, 4, t.text_faint));
            }
            Item::Legacy => {
                col = col.child(line(vec![
                    span(mark(i), ink),
                    span_bold(
                        super::switch::switch_text(
                            LEGACY_LABEL,
                            p.legacy == Some(true),
                            None,
                            false,
                        ),
                        ink,
                    ),
                ]));
                col = col.child(kit::sentence_indent(
                    &t,
                    LEGACY_HELP,
                    width,
                    4,
                    t.text_faint,
                ));
            }
        }
    }
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
    let scroll = Scroll::new(col.build())
        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
        .scrollbar_auto_hide(true)
        .view(cx);
    // The outcome line sits under the scroll (always visible) as its own reactive view: a new message must not
    // rebuild the editor (an open input would lose its caret).
    let msg_line = dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
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
    });
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(scroll)
        .child(msg_line)
        .build()
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
            if ws.busy.get_untracked() {
                return;
            }
            if typed.is_empty() {
                if e == Edit::Root && before.root.as_deref().is_some_and(|r| !r.is_empty()) {
                    // Empty Default folder = the gateway's own folder.
                    let mut after = before.clone();
                    after.root = Some(String::new());
                    ws.msg.set(Some(("Saving...".into(), Tone::Plain)));
                    c.send(Cmd::Workspaces(WsCmd::Save {
                        scope: s.clone(),
                        before: before.clone(),
                        policy: Some(after),
                        what: "default folder".into(),
                    }));
                    ws.editing.set(None);
                } else {
                    ws.editing.set(None);
                }
                return;
            }
            ws.msg.set(Some(("Checking…".into(), Tone::Plain)));
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
