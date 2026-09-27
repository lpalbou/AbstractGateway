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

use std::rc::Rc;

use abstracttui::prelude::*;

use super::util::{ellipsize, line, span, span_bold};
use super::{open_form, Ctx};
use crate::store::operator::{paused_banner_text, HostRunner};
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
}

fn toggle_pause(ctx: &Ctx) {
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
fn restart(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
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
    close();
    let c = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        "Restart AbstractGateway? Running workflows pause at their next step and continue after the \
         restart. The console is unavailable for a few seconds and reconnects by itself."
            .to_string(),
        "Restart",
        "Keep it running",
        move || c.send(Cmd::Operator(OpCmd::Restart)),
    );
}

fn quit(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
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
    close();
    let c = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        "Quit AbstractGateway? Workflows stop and this console goes offline until you start \
         AbstractGateway again."
            .to_string(),
        "Quit the gateway",
        "Keep it running",
        move || c.send(Cmd::Operator(OpCmd::Shutdown)),
    );
}

fn check_update(ctx: &Ctx) {
    if guard(ctx) {
        ctx.send(Cmd::Operator(OpCmd::UpdateCheck));
    }
}

fn start_update(cx: Scope, ctx: &Ctx, close: &dyn Fn()) {
    if !guard(ctx) {
        return;
    }
    let Some(u) = ctx.store.op.update.with_untracked(|u| u.ready().cloned()) else {
        ctx.store
            .notice
            .set(Some("check for an update first (u)".into()));
        return;
    };
    if !u.can_start() {
        let why = if u.job_state == "running" {
            "an update is already being installed".to_string()
        } else if !u.update_available {
            "no update is available — u checks again".to_string()
        } else {
            format!("this install cannot update itself: {}", u.install_reason)
        };
        ctx.store.notice.set(Some(why));
        return;
    }
    close();
    let c = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "AbstractGateway {} is available (you have {}). Installing takes a minute or two; \
             workflows keep running until you restart.",
            u.latest, u.current
        ),
        "Update now",
        "Not now",
        move || c.send(Cmd::Operator(OpCmd::UpdateStart)),
    );
}

/// Open the panel (F3). `cx` is the ROOT scope: confirms open there.
pub fn open(ctx: &Ctx, cx: Scope) {
    refresh(ctx);
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(104, 20), move |mcx, close| {
        let theme = use_theme(mcx);
        let store = ctx2.store;
        let op = store.op;
        let is_admin = admin(&ctx2);

        let c_p = ctx2.clone();
        let c_r = ctx2.clone();
        let c_q = ctx2.clone();
        let c_u = ctx2.clone();
        let c_s = ctx2.clone();
        let c_ref = ctx2.clone();
        let (k_r, k_q, k_s) = (close.clone(), close.clone(), close.clone());

        let body = dyn_view(LayoutStyle::column().gap(0).grow(1.0), move || {
            let t = theme.get().tokens;
            let _ = store.tick.get();
            let mut rows: Vec<View> = Vec::new();
            let conn = store.conn.get();
            if !conn.is_connected() {
                rows.push(line(vec![span(
                    "not connected — the host state needs a live gateway",
                    t.warn,
                )]));
            }
            match op.runner.get() {
                Loadable::Ready(r) => {
                    let ink = if r.paused { t.warn } else { t.ok };
                    rows.push(line(vec![
                        span(format!("{:>14}: ", "workflows"), t.text_muted),
                        span_bold(r.state_text(), ink),
                    ]));
                    let detail = r.detail_text();
                    if !detail.is_empty() {
                        rows.push(line(vec![span(
                            format!("{:>14}  {}", "", ellipsize(&detail, 86)),
                            t.text_faint,
                        )]));
                    }
                    let caps = match (r.cap_restart, r.cap_shutdown) {
                        (true, true) => "restart and quit available".to_string(),
                        (false, true) => {
                            format!("quit available · restart unavailable: {}", r.cap_reason)
                        }
                        (true, false) => {
                            format!("restart available · quit unavailable: {}", r.cap_reason)
                        }
                        (false, false) => format!("restart/quit unavailable: {}", r.cap_reason),
                    };
                    rows.push(line(vec![
                        span(format!("{:>14}: ", "process"), t.text_muted),
                        span(ellipsize(&caps, 86), t.text),
                    ]));
                }
                Loadable::Failed(e) => rows.push(line(vec![
                    span(format!("{:>14}: ", "workflows"), t.text_muted),
                    span(format!("gateway state unavailable: {e}"), t.error),
                ])),
                Loadable::Loading => {
                    rows.push(line(vec![span("◌ reading the gateway host…", t.info)]))
                }
                Loadable::NotAsked => {
                    rows.push(line(vec![span("— not read yet (r)", t.text_muted)]))
                }
            }
            match op.tray.get() {
                Loadable::Ready(note) => rows.push(line(vec![
                    span(format!("{:>14}: ", "desktop tray"), t.text_muted),
                    span(ellipsize(&note, 86), t.text),
                ])),
                Loadable::Failed(e) => rows.push(line(vec![
                    span(format!("{:>14}: ", "desktop tray"), t.text_muted),
                    span(format!("unavailable: {e}"), t.warn),
                ])),
                _ => {}
            }
            if is_admin {
                match op.update.get() {
                    Loadable::Ready(u) => {
                        rows.push(line(vec![
                            span(format!("{:>14}: ", "version"), t.text_muted),
                            span(
                                ellipsize(&u.version_text(), 86),
                                if u.update_available { t.accent } else { t.text },
                            ),
                        ]));
                        let hint = u.hint_text();
                        if !hint.is_empty() {
                            rows.push(line(vec![span(
                                format!("{:>14}  {}", "", ellipsize(&hint, 86)),
                                t.text_faint,
                            )]));
                        }
                    }
                    Loadable::Failed(e) => rows.push(line(vec![
                        span(format!("{:>14}: ", "version"), t.text_muted),
                        span(format!("update state unavailable: {e}"), t.warn),
                    ])),
                    Loadable::Loading => {
                        rows.push(line(vec![span("◌ reading the update state…", t.info)]))
                    }
                    Loadable::NotAsked => {}
                }
            }
            if let Some(l) = op.lifecycle.get() {
                rows.push(line(vec![span(ellipsize(&l, 100), t.info)]));
            }
            rows.push(line(vec![span(
                if is_admin {
                    "p pause/resume · R restart · Q quit · u check for update · U install update · r reload · Esc close"
                } else {
                    "only an admin can pause, restart, quit or update this gateway · r reload · Esc close"
                },
                t.text_faint,
            )]));
            let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
            for r in rows {
                col = col.child(r);
            }
            col.build()
        });

        let t0 = theme.get().tokens;
        let buttons: View = if is_admin {
            let (b_p, b_r, b_q, b_u, b_s) = (
                c_p.clone(),
                c_r.clone(),
                c_q.clone(),
                c_u.clone(),
                c_s.clone(),
            );
            let (kb_r, kb_q, kb_s) = (k_r.clone(), k_q.clone(), k_s.clone());
            // Rebuilt when the runner answer changes: the pause button
            // names the verb it will perform (web parity).
            dyn_view_scoped(LayoutStyle::row().h(1).shrink(0.0), move |bcx| {
                let t = theme.get().tokens;
                let label = match op.runner.get() {
                    Loadable::Ready(r) if r.paused => "Resume workflows",
                    _ => "Pause workflows",
                };
                let (b_p, b_r, b_q, b_u, b_s) = (
                    b_p.clone(),
                    b_r.clone(),
                    b_q.clone(),
                    b_u.clone(),
                    b_s.clone(),
                );
                let (kb_r, kb_q, kb_s) = (kb_r.clone(), kb_q.clone(), kb_s.clone());
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new(label)
                            .on_click(move || toggle_pause(&b_p))
                            .element(bcx, &t)
                            .build(),
                    )
                    .child(
                        Button::new("Restart…")
                            .on_click(move || restart(cx, &b_r, &*kb_r))
                            .element(bcx, &t)
                            .build(),
                    )
                    .child(
                        Button::new("Quit…")
                            .on_click(move || quit(cx, &b_q, &*kb_q))
                            .element(bcx, &t)
                            .build(),
                    )
                    .child(
                        Button::new("Check for update")
                            .on_click(move || check_update(&b_u))
                            .element(bcx, &t)
                            .build(),
                    )
                    .child(
                        Button::new("Install update…")
                            .on_click(move || start_update(cx, &b_s, &*kb_s))
                            .element(bcx, &t)
                            .build(),
                    )
                    .build()
            })
        } else {
            Element::new().style(LayoutStyle::default().h(0)).build()
        };

        let close_esc: Rc<dyn Fn()> = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .shortcut(KeyChord::plain(Key::Char('p')), move |_| toggle_pause(&c_p))
            .shortcut(KeyChord::plain(Key::Char('R')), move |_| {
                restart(cx, &c_r, &*k_r)
            })
            .shortcut(KeyChord::plain(Key::Char('Q')), move |_| {
                quit(cx, &c_q, &*k_q)
            })
            .shortcut(KeyChord::plain(Key::Char('u')), move |_| check_update(&c_u))
            .shortcut(KeyChord::plain(Key::Char('U')), move |_| {
                start_update(cx, &c_s, &*k_s)
            })
            .shortcut(KeyChord::plain(Key::Char('r')), move |_| refresh(&c_ref))
            .child(line(vec![
                span_bold("Gateway host", t0.accent),
                span(
                    "  — this gateway process, as the web console's Gateway card shows it",
                    t0.text_faint,
                ),
            ]))
            .child(body)
            .child(buttons)
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_esc())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
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
