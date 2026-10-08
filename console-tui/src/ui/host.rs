//! Gateway host panel (F3 anywhere) + the "Workflows are paused" banner.
//!
//! Parity with the web console's Gateway card (Resources tab) and its
//! paused banner (every tab): the workflow runner's state (running /
//! pausing / paused, by whom, since when), the desktop tray note, the
//! installed version and the update check — and the verbs, admin-gated
//! exactly like the web: pause / resume, restart, quit, check for an
//! update, install it.
//!
//! Restart and quit take the gateway AWAY on purpose. The worker's
//! watcher (`worker::operator`) probes until it has seen the gateway go
//! down (and, for a restart, come back), then re-probes the connection —
//! the header then tells the truth (connected again / unreachable). The
//! watcher's line rides in the chrome banner, so it stays visible after
//! this panel closes for the confirm.
//!
//! A panel, not a numbered screen: the digit row is full (1-9, 0) and
//! host control is a verb set every screen may need (the web shows the
//! paused banner on every tab). The lead settles final placement.
//!
//! R15: the panel IS the web's ◎ Gateway card — its title and note, the
//! Workflows paused and Start at login switches (Toggles), the Version /
//! Desktop icon / Last restart / Start at login rows, and its buttons
//! (Check now, Update, Restart gateway…, Quit gateway…) with the web's
//! questions; `host_actions` is the single source.

use std::rc::Rc;

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

use super::util::{line, span, span_bold};
use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Toggle};
use super::Ctx;
use crate::store::operator::{paused_banner_text, HostRunner, StartAtLogin};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::OpCmd;
use crate::worker::Cmd;

/// The key that opens this panel from any screen (function keys survive
/// focused text fields — the F1 About precedent).
pub const OPEN_KEY: Key = Key::F(3);
pub const OPEN_KEY_LABEL: &str = "F3";

/// How long a FINISHED restart/quit line stays in the chrome banner (the
/// journal keeps it for good).
const LIFECYCLE_LINGER: std::time::Duration = std::time::Duration::from_secs(30);

fn admin(ctx: &Ctx) -> bool {
    ctx.store
        .conn
        .with_untracked(|c| matches!(c, ConnPhase::Connected(id) if id.admin))
}

/// Why a verb is refused before anything is sent, or None when it may go.
pub fn refusal(connected: bool, is_admin: bool) -> Option<&'static str> {
    if !connected {
        Some("not connected — probe on the Connection screen first")
    } else if !is_admin {
        Some("only an admin can pause, restart, quit or update this gateway")
    } else {
        None
    }
}

fn guard(ctx: &Ctx) -> bool {
    let connected = ctx.store.conn.with_untracked(ConnPhase::is_connected);
    match refusal(connected, admin(ctx)) {
        Some(why) => {
            ctx.store.notice.set(Some(why.into()));
            false
        }
        None => true,
    }
}

fn runner_now(ctx: &Ctx) -> Option<HostRunner> {
    ctx.store.op.runner.with_untracked(|r| r.ready().cloned())
}

/// Load (or reload) everything the panel shows.
pub fn refresh(ctx: &Ctx) {
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        return;
    }
    let is_admin = admin(ctx);
    // A held answer stays on screen while the reload runs (the banner
    // reads the same slot: blanking it would flicker "paused" away).
    let op = ctx.store.op;
    if op.runner.with_untracked(|r| r.ready().is_none()) {
        op.runner.set(Loadable::Loading);
    }
    if op.tray.with_untracked(|r| r.ready().is_none()) {
        op.tray.set(Loadable::Loading);
    }
    if is_admin && op.update.with_untracked(|r| r.ready().is_none()) {
        op.update.set(Loadable::Loading);
    }
    ctx.send(Cmd::Operator(OpCmd::LoadHost { admin: is_admin }));
    if is_admin {
        load_start_at_login(ctx);
    }
}

// ---- Start at login (GET/PUT /host/start-at-login, admin) -------------
// One toggle, two homes: this panel (F3, key `L`) and the setup guide's
// Finish step. Every change is confirmed, then verified by a GET (the
// worker's write law); the text shows the read-back, never the wish.

/// Read (or re-read) the start-at-login state (admins only).
pub fn load_start_at_login(ctx: &Ctx) {
    if !admin(ctx) {
        return;
    }
    let slot = ctx.store.op.start_at_login;
    if slot.with_untracked(|r| r.ready().is_none()) {
        slot.set(Loadable::Loading);
    }
    ctx.send(Cmd::Operator(OpCmd::LoadStartAtLogin));
}

/// The one line both homes show.
pub fn start_at_login_text(slot: &Loadable<StartAtLogin>, is_admin: bool) -> String {
    if !is_admin {
        return "only an admin can see or change it".into();
    }
    match slot {
        Loadable::Ready(st) => st.text(),
        Loadable::Failed(e) => format!("unavailable: {e}"),
        Loadable::Loading => "reading…".into(),
        Loadable::NotAsked => "not read yet".into(),
    }
}

/// The toggle: confirm (on the ROOT scope `cx`), then PUT + verify.
pub fn toggle_start_at_login(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
    if !guard(ctx) {
        return;
    }
    let Some(st) = ctx
        .store
        .op
        .start_at_login
        .with_untracked(|r| r.ready().cloned())
    else {
        ctx.store
            .notice
            .set(Some("start at login is not read yet — r reloads it".into()));
        return;
    };
    let Some(verb) = st.verb() else {
        ctx.store.notice.set(Some(format!(
            "start at login can't be changed here: {}",
            st.reason
        )));
        return;
    };
    // R15 (adversary H1): the confirm opens OVER the panel; Cancel returns
    // to it. `close` stays in the signature for the callers that pass one.
    let _ = close;
    let c = ctx.clone();
    let enabled = !st.enabled;
    let replace_other = st.state == "other";
    super::confirm_danger(cx, ctx.ui, st.confirm_text(), verb, "Leave it", move || {
        c.send(Cmd::Operator(OpCmd::SetStartAtLogin {
            enabled,
            replace_other,
        }))
    });
}

pub(crate) fn toggle_pause(ctx: &Ctx) {
    if !guard(ctx) {
        return;
    }
    let Some(r) = runner_now(ctx) else {
        ctx.store.notice.set(Some(
            "the runner state is not loaded yet — r reloads it".into(),
        ));
        return;
    };
    ctx.send(Cmd::Operator(OpCmd::SetPaused { pause: !r.paused }));
}

/// Restart: capability-gated, confirmed on the ROOT scope after this
/// panel closes (a prompt over a modal is the stacking hazard).
pub(crate) fn restart(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
    if !guard(ctx) {
        return;
    }
    match runner_now(ctx) {
        Some(r) if r.cap_restart => {}
        Some(r) => {
            ctx.store.notice.set(Some(format!(
                "restart is not available: {}",
                if r.cap_reason.is_empty() {
                    "not available for this launch"
                } else {
                    r.cap_reason.as_str()
                }
            )));
            return;
        }
        None => {
            ctx.store.notice.set(Some(
                "the runner state is not loaded yet — r reloads it".into(),
            ));
            return;
        }
    }
    // R15 (adversary H1): the confirm opens OVER the panel; Cancel returns
    // to it. `close` stays in the signature for the callers that pass one.
    let _ = close;
    let c = ctx.clone();
    super::w::Confirm::danger(RESTART_QUESTION, "Restart", "Cancel")
        .open(cx, ctx.ui, move || c.send(Cmd::Operator(OpCmd::Restart)));
}

pub(crate) fn quit(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
    if !guard(ctx) {
        return;
    }
    match runner_now(ctx) {
        Some(r) if r.cap_shutdown => {}
        Some(r) => {
            ctx.store.notice.set(Some(format!(
                "quit is not available: {}",
                if r.cap_reason.is_empty() {
                    "not available for this launch"
                } else {
                    r.cap_reason.as_str()
                }
            )));
            return;
        }
        None => {
            ctx.store.notice.set(Some(
                "the runner state is not loaded yet — r reloads it".into(),
            ));
            return;
        }
    }
    // R15 (adversary H1): the confirm opens OVER the panel; Cancel returns
    // to it. `close` stays in the signature for the callers that pass one.
    let _ = close;
    let c = ctx.clone();
    super::w::Confirm::danger(QUIT_QUESTION, "Quit", "Cancel")
        .open(cx, ctx.ui, move || c.send(Cmd::Operator(OpCmd::Shutdown)));
}

pub(crate) fn check_update(ctx: &Ctx) {
    if guard(ctx) {
        ctx.send(Cmd::Operator(OpCmd::UpdateCheck));
    }
}

pub(crate) fn start_update(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
    if !guard(ctx) {
        return;
    }
    let Some(u) = ctx.store.op.update.with_untracked(|u| u.ready().cloned()) else {
        ctx.store
            .notice
            .set(Some("check for an update first (Check now)".into()));
        return;
    };
    if let Some(why) = u.start_refusal() {
        ctx.store.notice.set(Some(why));
        return;
    }
    // R15 (adversary H1): the confirm opens OVER the panel; Cancel returns
    // to it. `close` stays in the signature for the callers that pass one.
    let _ = close;
    let c = ctx.clone();
    let installer_sha256 = u.installer_sha256();
    // The web's update question (`startGatewayUpdate`): the gateway's
    // own sentence, Update now / Cancel.
    super::w::Confirm::plain(u.confirm_text(), "Update now", "Cancel").open(
        cx,
        ctx.ui,
        move || {
            c.send(Cmd::Operator(OpCmd::UpdateStart {
                installer_sha256: installer_sha256.clone(),
            }))
        },
    );
}

// ---- R15: the panel is the web's ◎ Gateway card ----------------------

pub const TITLE: &str = "Gateway";
pub const NOTE: &str = "How this gateway is running right now. Pausing stops new workflow steps; the console and connected apps keep answering.";
pub const PAUSE_LABEL: &str = "Workflows paused";
pub const PAUSE_TIP: &str = "On: no new workflow step starts until you switch it off; work already inside a call finishes first";
pub const CHECK_TIP: &str = "Check for a newer release: an AbstractFramework installer install compares with the newest AbstractFramework release, any other install with the newest AbstractGateway on PyPI (needs internet)";
pub const UPDATE_TIP: &str = "Install it in the background (an installer install runs the AbstractFramework installer); restart to finish";
pub const RESTART_QUESTION: &str = "Restart AbstractGateway? Running workflows pause at their next step and continue after the restart. The console is unavailable for a few seconds.";
pub const QUIT_QUESTION: &str = "Quit AbstractGateway? Workflows stop and this console goes offline until you start AbstractGateway again.";
const NON_ADMIN: &str = "only an admin can pause, restart, quit or update this gateway";

/// The card's buttons (admins; the web hides them otherwise): Check now,
/// Update (when the check found one), Restart gateway…, Quit gateway…
/// (refused with the gateway's reason when this launch cannot).
pub fn host_actions(
    r: Option<&HostRunner>,
    u: Option<&crate::store::operator::HostUpdate>,
    is_admin: bool,
) -> Vec<Action> {
    if !is_admin {
        return Vec::new();
    }
    let mut out = vec![Action::label("check", "Check now")
        .key('u')
        .tooltip(CHECK_TIP)];
    if let Some(u) = u.filter(|u| u.update_available) {
        out.push(
            Action::label("update", "Update")
                .key('U')
                .tooltip(UPDATE_TIP)
                .refused(u.start_refusal()),
        );
    }
    let cap_why = |ok: bool| -> Option<String> {
        match r {
            None => Some("the runner state is not loaded yet".into()),
            Some(_) if ok => None,
            Some(r) => Some(if r.cap_reason.is_empty() {
                "not available for this launch".to_string()
            } else {
                r.cap_reason.clone()
            }),
        }
    };
    out.push(
        Action::label("restart", "Restart gateway…")
            .key('R')
            .refused(cap_why(r.is_some_and(|r| r.cap_restart))),
    );
    out.push(
        Action::label("quit", "Quit gateway…")
            .key('Q')
            .refused(cap_why(r.is_some_and(|r| r.cap_shutdown)))
            .danger(),
    );
    out
}

/// One label · value row of the card (the value wraps under itself).
fn kv(t: &TokenSet, label: &str, value: &str, ink: Rgba, w: i32) -> Vec<View> {
    let body_w = (w - 17).max(16) as usize;
    super::util::wrap_text(value, body_w)
        .into_iter()
        .enumerate()
        .map(|(i, l)| {
            line(vec![
                span(
                    if i == 0 {
                        format!("{label:<16} ")
                    } else {
                        " ".repeat(17)
                    },
                    t.text_muted,
                ),
                span(l, ink),
            ])
        })
        .collect()
}

/// Open the panel (F3). `cx` is the ROOT scope: confirms open there.
pub fn open(ctx: &Ctx, cx: Scope) {
    refresh(ctx);
    let c = ctx.clone();
    super::w::FormModal::new(TITLE)
        .lead(NOTE)
        .size(104, 26)
        .open(ctx, cx, move |mcx, close, _guard, inner_w| {
            let store = c.store;
            let op = store.op;
            let is_admin = admin(&c);
            // The rows scroll (wheel) when the terminal is too short for
            // them (80x24 with a "Last restart" row): never clipped.
            let scroll_y = mcx.signal(0);
            let body = dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |bcx| {
                let t = abstracttui::app::current_theme().tokens;
                let _ = store.tick.get();
                let w = inner_w;
                let mut rows: Vec<View> = Vec::new();
                if !store.conn.get().is_connected() {
                    rows.push(sentence(
                        &t,
                        "not connected — the host state needs a live gateway",
                        w,
                        t.warn,
                    ));
                }
                match op.runner.get() {
                    Loadable::Ready(r) => {
                        let ink = if r.paused { t.warn } else { t.ok };
                        rows.extend(kv(&t, "Workflows", &r.state_text(), ink, w));
                        let detail = r.detail_text();
                        if !detail.is_empty() {
                            rows.extend(kv(&t, "", &detail, t.text_faint, w));
                        }
                        // R13.1: the "Last restart" row (admins; absent =
                        // no watchdog incident), wrapped, never cut.
                        if let Some(hang) = &r.last_hang {
                            rows.extend(kv(&t, "Last restart", &hang.text(), t.warn, w));
                            for extra in hang.dump_text().into_iter().chain(hang.detail_lines()) {
                                rows.extend(kv(&t, "", &extra, t.text_faint, w));
                            }
                        }
                    }
                    Loadable::Failed(e) => rows.extend(kv(
                        &t,
                        "Workflows",
                        &format!("gateway state unavailable: {e}"),
                        t.error,
                        w,
                    )),
                    Loadable::Loading => {
                        rows.push(sentence(&t, "◌ reading the gateway host…", w, t.info))
                    }
                    Loadable::NotAsked => {
                        rows.push(sentence(&t, "— not read yet (r)", w, t.text_muted))
                    }
                }
                if is_admin {
                    match op.update.get() {
                        Loadable::Ready(u) => {
                            let ink = if u.update_available { t.accent } else { t.text };
                            rows.extend(kv(&t, "Version", &u.version_text(), ink, w));
                            let hint = u.hint_text();
                            if !hint.is_empty() {
                                rows.extend(kv(&t, "", &hint, t.text_faint, w));
                            }
                        }
                        Loadable::Failed(e) => rows.extend(kv(
                            &t,
                            "Version",
                            &format!("update state unavailable: {e}"),
                            t.warn,
                            w,
                        )),
                        Loadable::Loading => rows.extend(kv(&t, "Version", "reading…", t.info, w)),
                        Loadable::NotAsked => {}
                    }
                }
                match op.tray.get() {
                    Loadable::Ready(note) => rows.extend(kv(&t, "Desktop icon", &note, t.text, w)),
                    Loadable::Failed(e) => rows.extend(kv(
                        &t,
                        "Desktop icon",
                        &format!("unavailable: {e}"),
                        t.warn,
                        w,
                    )),
                    _ => {}
                }
                rows.extend(kv(
                    &t,
                    "Start at login",
                    &start_at_login_text(&op.start_at_login.get(), is_admin),
                    t.text,
                    w,
                ));
                if let Some(l) = op.lifecycle.get() {
                    rows.push(sentence(&t, &l, w, t.info));
                }
                if !is_admin {
                    rows.push(sentence(&t, NON_ADMIN, w, t.text_faint));
                }
                let mut col = Element::new().style(LayoutStyle::column().gap(0));
                for r in rows {
                    col = col.child(r);
                }
                Scroll::new(col.build())
                    .axes(false, true)
                    .offset_y(scroll_y)
                    .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                    .scrollbar_auto_hide(true)
                    .view(bcx)
            });

            // The switches and the buttons: rebuilt when the runner, the
            // update check or the start-at-login answer changes.
            let cc = c.clone();
            let close_c = close.clone();
            let controls = dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |bcx| {
                let t = abstracttui::app::current_theme().tokens;
                let runner = op.runner.get();
                let update = op.update.get();
                let login = op.start_at_login.get();
                let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                if is_admin {
                    let paused_na = match &runner {
                        Loadable::Ready(_) => None,
                        Loadable::Failed(e) => Some(format!("unavailable: {e}")),
                        _ => Some("reading…".to_string()),
                    };
                    let login_na = match &login {
                        Loadable::Ready(st) => st.switch_unavailable(),
                        Loadable::Failed(e) => Some(format!("unavailable: {e}")),
                        _ => Some("reading…".to_string()),
                    };
                    let (c1, c2) = (cc.clone(), cc.clone());
                    let k2 = close_c.clone();
                    let mut switches = Element::new()
                        .style(LayoutStyle::row().gap(3).h(1).shrink(0.0))
                        .child(
                            Toggle::new(matches!(&runner, Loadable::Ready(r) if r.paused))
                                .label(PAUSE_LABEL)
                                .tip(format!("{PAUSE_TIP}  (p)"))
                                .refused(paused_na)
                                .on_change(move |_| toggle_pause(&c1))
                                .view(bcx, &t),
                        )
                        .child(
                            Toggle::new(matches!(&login, Loadable::Ready(st) if st.enabled))
                                .label("Start at login")
                                .tip("Start the gateway when you log in to this computer  (L)")
                                .refused(login_na)
                                .on_change(move |_| toggle_start_at_login(cx, &c2, &*k2))
                                .view(bcx, &t),
                        );
                    if let Some(label) = login.ready().and_then(|st| st.repair_label()) {
                        let (c3, k3) = (cc.clone(), close_c.clone());
                        switches = switches.child(button(
                            bcx,
                            &t,
                            &Action::label("repair", label),
                            On::Raised,
                            true,
                            move || toggle_start_at_login(cx, &c3, &*k3),
                        ));
                    }
                    col = col.child(switches.build());
                }
                let mut buttons = Vec::new();
                for a in host_actions(runner.ready(), update.ready(), is_admin) {
                    let (c4, k4) = (cc.clone(), close_c.clone());
                    let id = a.id;
                    buttons.push(button(bcx, &t, &a, On::Raised, true, move || {
                        host_action(cx, &c4, &*k4, id)
                    }));
                }
                let k5 = close_c.clone();
                buttons.push(button(
                    bcx,
                    &t,
                    &Action::label("close", "Close"),
                    On::Raised,
                    true,
                    move || k5(),
                ));
                col.child(super::w::form::button_row(buttons)).build()
            });

            let (ck, kk) = (c.clone(), close.clone());
            Element::new()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .focusable()
                .autofocus()
                .on(Phase::Bubble, move |ectx, ev| {
                    let UiEvent::Key(k) = ev else { return };
                    if k.mods.0 != 0 && !matches!(k.key, Key::Char(ch) if ch.is_ascii_uppercase()) {
                        return;
                    }
                    let id = match k.key {
                        Key::Char('p') => "pause",
                        Key::Char('L') => "login",
                        Key::Char('R') => "restart",
                        Key::Char('Q') => "quit",
                        Key::Char('u') => "check",
                        Key::Char('U') => "update",
                        Key::Char('r') => "reload",
                        _ => return,
                    };
                    ectx.stop_propagation();
                    host_action(cx, &ck, &*kk, id);
                })
                .child(body)
                .child(controls)
                .build()
        });
}

/// One verb of the panel (a button, a switch or its key).
fn host_action(cx: Scope, ctx: &Ctx, close: &dyn Fn(), id: &str) {
    match id {
        "pause" => toggle_pause(ctx),
        "login" => toggle_start_at_login(cx, ctx, close),
        "restart" => restart(cx, ctx, close),
        "quit" => quit(cx, ctx, close),
        "check" => check_update(ctx),
        "update" => start_update(cx, ctx, close),
        "reload" => refresh(ctx),
        _ => {}
    }
}

/// The chrome banner: the paused sentence (web: every tab), and the
/// restart/quit watcher line while one is in flight. Zero rows otherwise
/// (the 80x24 law: a reserved empty row pushes screens off).
pub fn banner(ctx: &Ctx, theme: Signal<&'static abstracttui::theme::Theme>) -> View {
    let store = ctx.store;
    let ctx_b = ctx.clone();
    dyn_view_scoped(LayoutStyle::default().shrink(0.0), move |_| {
        let t = theme.get().tokens;
        let connected = store.conn.with(ConnPhase::is_connected);
        let paused = if connected {
            store
                .op
                .runner
                .with(|r| r.ready().and_then(paused_banner_text))
        } else {
            None
        };
        let lifecycle = store.op.lifecycle.get();
        let mut rows: Vec<View> = Vec::new();
        if let Some(p) = paused {
            // The admin hint goes FIRST: the line truncates at its end.
            let head = if admin(&ctx_b) {
                format!(" ⏸ [{OPEN_KEY_LABEL} → p resumes] ")
            } else {
                " ⏸ ".to_string()
            };
            rows.push(line(vec![span_bold(format!("{head}{p}"), t.warn)]));
        }
        if let Some(l) = lifecycle {
            rows.push(line(vec![span_bold(format!(" {l}"), t.info)]));
        }
        if rows.is_empty() {
            return Element::new().style(LayoutStyle::default().h(0)).build();
        }
        let n = rows.len() as i32;
        let mut col = Element::new().style(LayoutStyle::column().h(n).shrink(0.0));
        for r in rows {
            col = col.child(r);
        }
        col.build()
    })
}

/// The paused-banner poll lifecycle (web: `/host/runner` every 15 s while
/// signed in): connected → a fresh generation-gated chain; disconnected
/// → the generation bumps and the live chain dies. Also retires a
/// FINISHED watcher line after a while (the journal keeps it).
pub fn install(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    {
        let ctx = ctx.clone();
        let was_on = Rc::new(std::cell::Cell::new(false));
        cx.effect(move || {
            let on = store.conn.with(ConnPhase::is_connected);
            if on && !was_on.get() {
                let gen = store.op.runner_poll_gen.get_untracked() + 1;
                store.op.runner_poll_gen.set(gen);
                ctx.send(Cmd::Operator(OpCmd::PollRunner { gen }));
            }
            if !on && was_on.get() {
                store.op.runner_poll_gen.update(|g| *g += 1);
            }
            was_on.set(on);
        });
    }
    {
        let pending: Rc<std::cell::RefCell<Option<abstracttui::reactive::IntervalHandle>>> =
            Rc::new(std::cell::RefCell::new(None));
        cx.effect(move || {
            let line = store.op.lifecycle.get();
            if let Some(h) = pending.borrow_mut().take() {
                h.cancel();
            }
            let Some(l) = line else { return };
            if l.starts_with('⟳') {
                return; // in flight: the watcher owns it
            }
            let handle = abstracttui::reactive::interval(cx, LIFECYCLE_LINGER, move || {
                if store
                    .op
                    .lifecycle
                    .with_untracked(|cur| cur.as_deref() == Some(l.as_str()))
                {
                    store.op.lifecycle.set(None);
                }
            });
            *pending.borrow_mut() = Some(handle);
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verbs_refuse_with_a_reason_before_sending() {
        assert!(refusal(false, true).unwrap().contains("not connected"));
        assert!(refusal(true, false).unwrap().contains("only an admin"));
        assert_eq!(refusal(true, true), None);
    }
}
