//! Connection screen: base URL + admin token → probe → honest state.
//!
//! The probe is `GET /ping` then `GET /me` — never discovery (the
//! gateway's own docstring law: discovery can legitimately time out or
//! skip providers; ping validates reachability + auth, nothing else).

use abstracttui::prelude::*;

use super::util::{badge, esc_releases_focus, field, line, span, span_bold};
use super::w::action::On;
use super::w::Action;
use super::Ctx;
use crate::store::ConnPhase;
use abstracttui::widgets::Tone;

// ---------------------------------------------------------------------------
// R15 Connection (DESIGN-TUI.md §3.3): the TUI's sign-in surface (the web
// has a sign-in card, not a page). Every control is a real button — Sign
// in / Re-probe, Show / Hide on the token, Network — and the sign-in by
// email steps are the web card's words and buttons. One action list
// (`page_actions`) is the single source for the buttons, the hints and
// the click tests.
// ---------------------------------------------------------------------------

/// The field labels (the web sign-in card says "Token").
pub const URL_LABEL: &str = "Gateway address";
pub const TOKEN_LABEL: &str = "Token";
/// The web sign-in card's button.
pub const SIGN_IN: &str = "Sign in";
pub const RE_PROBE: &str = "Re-probe";

/// The page's actions in screen order: Sign in (Re-probe once connected),
/// Show/Hide on the token, Network (once connected).
pub fn page_actions(connected: bool, revealed: bool) -> Vec<Action> {
    let mut out = vec![
        if connected {
            Action::label("probe", RE_PROBE)
                .key('r')
                .tooltip("Check the connection again with the address and token above")
        } else {
            Action::label("probe", SIGN_IN)
                .key('r')
                .tooltip("Sign in to the gateway at this address with this token")
        },
        if revealed {
            Action::label("reveal", "Hide").tooltip("Hide token")
        } else {
            Action::label("reveal", "Show").tooltip("Show token")
        },
    ];
    if connected {
        out.push(Action::label("network", "Network").key('N').tooltip(
            "Who can reach this gateway (this computer, local network, internet) and its addresses",
        ));
    }
    out
}

/// The sign-in-by-email actions for a recovery step (`sent`: a code went
/// out; `can_resend`: a new one may be asked for; `wait`: cooldown s).
pub fn recovery_actions(step: &str, ready: bool, wait: u64, can_resend: bool) -> Vec<Action> {
    match step {
        "idle" => vec![Action::link("recover", RECOVERY_LINK)],
        "code" => {
            let mut v = vec![Action::label("use_code", "Use code")
                .refused((!ready).then(|| "Type the 8 digits from the email first.".to_string()))];
            if can_resend {
                v.push(
                    Action::link(
                        "resend",
                        if wait > 0 {
                            format!("Send a new code (in {wait} s)")
                        } else {
                            "Send a new code".to_string()
                        },
                    )
                    .refused((wait > 0).then(|| format!("A new code can be sent in {wait} s."))),
                );
            }
            v.push(Action::link("back", "Back to token"));
            v
        }
        "token" => vec![
            Action::label("copy", "Copy").tooltip("Copy the new token to the clipboard"),
            Action::label("done", "Done").tooltip("Hide the new token; it is not shown again"),
        ],
        _ => Vec::new(),
    }
}

/// The footer verbs of this page (R15: only what applies). The lead wires
/// it into `screen_hint_pairs` (COORD "R15-A HINTS").
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let connected = ctx.store.conn.with(ConnPhase::is_connected);
    let mut out = vec![
        ("Tab", "next"),
        ("Enter", if connected { RE_PROBE } else { SIGN_IN }),
    ];
    if connected {
        out.push(("r", RE_PROBE));
        out.push(("N", "Network"));
    }
    out
}

/// A button for `a` on the Connection block's ground.
fn btn(cx: Scope, t: &TokenSet, a: &Action, on_press: impl FnMut() + 'static) -> View {
    super::w::action::button(cx, t, a, On::Page, true, on_press)
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ui = ctx.ui;
    let store = ctx.store;
    let tt = *t;

    let ctx_probe = ctx.clone();
    let ctx_submit_url = ctx.clone();
    let ctx_submit_tok = ctx.clone();
    let ctx_net = ctx.clone();

    let env_set = ctx.env_token_set;
    let notice = store.notice;
    // Show / Hide on the token (the web card's reveal button).
    let revealed = cx.signal(false);

    // ≤ TIGHT_ROWS terminal rows (REVIEW-1 M3): the roomy layout (gap 1,
    // padding 1) needs ~32 rows once connected; below that the column
    // flex-shrank its rows to ZERO and the top of the screen went blank.
    // Tight = no gaps, no vertical padding, and the two lines the status
    // block already says (the connected intro, the About hint — F1 is in
    // --help and the footer) step aside. Every row is pinned (`pin`):
    // whatever still does not fit clips at the bottom, never the top.
    let vp = crate::ui::page_viewport(cx);
    let tight = cx.memo(move || vp.get().h <= TIGHT_ROWS);
    // A memo, so the URL slot below re-mounts only when this FLIPS — not
    // on every probe transition (a remount puts the caret back at 0).
    // The URL field takes the caret only before the first connection of
    // the session: after one, a dropped gateway leaves the keyboard to the
    // screen keys (`r` re-probes; Tab reaches the field).
    let live = cx.memo(move || {
        ui.was_connected.get()
            || store
                .conn
                .with(|c| matches!(c, ConnPhase::Connected(_) | ConnPhase::Verifying(_)))
    });
    let connected = cx.memo(move || store.conn.with(ConnPhase::is_connected));

    Block::new()
        .border(BorderKind::Rounded)
        .title("Connection")
        .fill(t.surface)
        .layout(roomy_or_tight(false))
        // State precedes the controls that change it: a first-run
        // operator reads top-down, and two inputs + a button read as
        // "fill me in" even when already connected (review finding).
        .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
            let t = tt;
            if tight.get() && store.conn.get().is_connected() {
                return Element::new().style(LayoutStyle::default().h(0)).build();
            }
            if store.conn.get().is_connected() {
                line(vec![span_bold(
                    "Connected — nothing to change here unless you want a different gateway or identity. Ctrl+N continues.",
                    t.ok,
                )])
            } else {
                line(vec![span(
                    "Point the console at a running AbstractGateway and sign in.",
                    t.text_muted,
                )])
            }
        }))
        // The URL field takes the caret ONLY while there is no live
        // connection (REVIEW-1 M2): connected, a focused URL box turned
        // every screen key (`5`, `q`, …) into typing. The slot re-mounts
        // when the connection lands or drops — a remount is how the
        // engine blurs (the focused node vanished → focus none → the
        // root's keys), and how a lost connection hands the caret back.
        // A flaky read (Verifying) is still "connected" here: it must not
        // yank the caret into the URL box mid-way through another screen.
        .child(dyn_view_scoped(LayoutStyle::default().h(1).shrink(0.0), move |gcx| {
            let t = tt;
            let live = live.get();
            let ctx_u = ctx_submit_url.clone();
            let el = TextInput::new()
                .value(ui.conn_url)
                .placeholder("http://127.0.0.1:8080")
                .placeholder_while_focused(true)
                // Editing the address makes it the person's: never followed.
                .on_change(move |_| ui.url_source.set(crate::pointer::UrlSource::Typed))
                .on_submit(move |_| ctx_u.connect_typed())
                .layout(LayoutStyle::default().w(46).h(1))
                .element(gcx, &t);
            let el = super::w::caret_tracked(gcx, ui.caret, el);
            let el = esc_releases_focus(el, notice);
            field(
                &t,
                URL_LABEL,
                if live { el.build() } else { el.autofocus().build() },
            )
        }))
        // The token field + its Show/Hide button (re-mounted on reveal:
        // the engine's mask is a build-time property).
        .child(pin(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |kcx| {
                let t = tt;
                let shown = revealed.get();
                let ctx_tok = ctx_submit_tok.clone();
                let input = esc_releases_focus(
                    super::w::caret_tracked(
                        kcx,
                        ui.caret,
                        TextInput::new()
                            .value(ui.conn_token)
                            .masked(!shown)
                            .placeholder("paste it, or launch with --token <token>")
                            .placeholder_while_focused(true)
                            .on_submit(move |_| ctx_tok.connect_typed())
                            .layout(LayoutStyle::default().w(46).h(1))
                            .element(kcx, &t),
                    ),
                    notice,
                )
                .build();
                let a = page_actions(false, shown)
                    .into_iter()
                    .find(|a| a.id == "reveal")
                    .expect("reveal action");
                field(
                    &t,
                    TOKEN_LABEL,
                    Element::new()
                        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                        .child(input)
                        .child(btn(kcx, &t, &a, move || revealed.update(|r| *r = !*r)))
                        .build(),
                )
            },
        )))
        .child(pin(field(
            t,
            "",
            // The empty masked field is the universal "not logged in"
            // signal — when the env token is what's actually in use,
            // SAY SO in normal ink (the faint one-liner was missed in
            // the live incident).
            dyn_view(LayoutStyle::line(1), move || {
                let t = tt;
                if env_set && ui.conn_token.get().is_empty() {
                    line(vec![span(
                        "using the legacy ABSTRACTGATEWAY_AUTH_TOKEN — type a token here to switch identity · tokens stay in memory, never on disk",
                        t.text_muted,
                    )])
                } else if env_set {
                    line(vec![span(
                        "legacy ABSTRACTGATEWAY_AUTH_TOKEN is set — the typed token wins while the field is non-empty",
                        t.text_faint,
                    )])
                } else {
                    line(vec![span(
                        "the token stays in memory, never on disk",
                        t.text_faint,
                    )])
                }
            }),
        )))
        .child(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |gcx| {
                let t = tt;
                // The button label answers "what will this do" BEFORE
                // it is pressed — a connected re-probe is a re-check,
                // not a sign-in.
                let a = page_actions(connected.get(), false)
                    .into_iter()
                    .find(|a| a.id == "probe")
                    .expect("probe action");
                let ctx_b = ctx_probe.clone();
                field(&t, "", btn(gcx, &t, &a, move || ctx_b.connect_typed()))
            },
        ))
        .child(dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
            status_view(&tt, &store.conn.get(), ui.token_source.get())
        }))
        // "Forgot your token? Email me a sign-in code" (DESIGN §4): only
        // while not signed in, and only when the gateway offers it.
        .child(pin(recovery_view(cx, ctx, t)))
        // An ignored gateway pointer file (~/.abstractframework/gateway.json):
        // why, in one line; nothing when the file is fine or absent.
        .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
            match ui.pointer_notice.get() {
                Some(w) => line(vec![span(format!("⚠ {w}"), tt.warn)]),
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        }))
        // About (F1 / ? anywhere): this console, the framework it is part
        // of, and the connected gateway's versions. A hint line, not a
        // button: a focusable here would shift the screen's Tab chain.
        .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
            if tight.get() {
                return Element::new().style(LayoutStyle::default().h(0)).build();
            }
            line(vec![
                span("About: ", tt.text_muted),
                span(
                    "F1 — this console, AbstractFramework, the gateway's versions (? lists the keys)",
                    tt.text_faint,
                ),
            ])
        }))
        // The acknowledgment line: EVERY probe updates it (number, UTC
        // time, outcome, latency) — a re-probe that lands on the same
        // state is still visibly a new event. This line is why the
        // Probe button can never feel dead again.
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let t = tt;
            match store.last_probe.get() {
                Some(p) => line(vec![
                    span(
                        format!("last probe #{} at {} — ", p.seq, p.at),
                        t.text_muted,
                    ),
                    if p.ok {
                        span(format!("✓ {} ({}ms)", p.outcome, p.took_ms), t.ok)
                    } else {
                        span(format!("✗ {} ({}ms)", p.outcome, p.took_ms), t.error)
                    },
                ]),
                None => line(vec![span("no probe has run yet", t.text_faint)]),
            }
        }))
        // Who can reach this gateway: one line (saved vs running) and the
        // Network button (the Network screen changes it and lists the
        // addresses).
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(
                    Element::new()
                        .style(LayoutStyle::row().grow(1.0).shrink(1.0).h(1))
                        .child(super::network::summary(cx, ctx, t))
                        .build(),
                )
                .child(dyn_view_scoped(
                    LayoutStyle::row().h(1).shrink(0.0),
                    move |ncx| {
                        let t = tt;
                        let Some(a) = page_actions(connected.get(), false)
                            .into_iter()
                            .find(|a| a.id == "network")
                        else {
                            return Element::new().style(LayoutStyle::default().w(0).h(1)).build();
                        };
                        let c = ctx_net.clone();
                        Element::new()
                            .style(LayoutStyle::row().h(1).w(a.width()).shrink(0.0))
                            .child(btn(ncx, &t, &a, move || {
                                super::shell::go(&c, super::SCREEN_NETWORK)
                            }))
                            .build()
                    },
                ))
                .build(),
        )
        .element(t)
        .style_signal(move || roomy_or_tight(tight.get()))
        .build()
}

/// The quiet link (DESIGN §4.5).
pub const RECOVERY_LINK: &str = "Forgot your token? Email me a sign-in code";
/// A wrong, expired or used code (the gateway's words when it has none).
pub const CODE_REFUSED: &str = "That code is wrong, expired or already used. Send a new one.";

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Sign-in by email: link → "Sending…" → the code step with the gateway's
/// honest answer, an 8-digit code field, "Use code" (unavailable until 8
/// digits), "Send a new code" with its 30 s cooldown, "Back to token".
/// A redeemed code returns a new token: it goes into the token field, the
/// console signs in with it, and the token is shown once to copy.
fn recovery_view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    use crate::store::email::{code_complete, RecoveryStep};
    use crate::worker::operator::{OpCmd, RecoveryAction};
    use crate::worker::Cmd;
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let rec = store.op.recovery;
    let user = cx.signal("admin".to_string());
    let caret = ctx.ui.caret;
    let code = cx.signal(String::new());
    let clock = cx.signal(0u64);
    let signed_out = cx.memo(move || {
        !store.conn.with(|c| {
            matches!(
                c,
                ConnPhase::Connected(_) | ConnPhase::Verifying(_) | ConnPhase::Probing
            )
        })
    });
    // Ask the gateway once per URL whether the link is offered.
    {
        let ctx_chk = ctx.clone();
        cx.effect(move || {
            if !signed_out.get() {
                return;
            }
            let known = store.conn.with(|c| {
                matches!(
                    c,
                    ConnPhase::Unauthorized(_) | ConnPhase::Forbidden(_) | ConnPhase::NotConnected
                )
            });
            if !known {
                return;
            }
            let url = crate::ui::normalize_url(&ui.conn_url.get_untracked());
            if url.is_empty() || rec.with_untracked(|r| r.checked_url == url) {
                return;
            }
            rec.update(|r| r.checked_url = url.clone());
            ctx_chk.send(Cmd::Operator(OpCmd::Recovery(RecoveryAction::Check {
                url,
            })));
        });
    }
    // A redeemed code: the new token signs this console in (once).
    {
        let ctx_in = ctx.clone();
        cx.effect(move || {
            let Some(tok) = rec.with(|r| r.new_token.clone()) else {
                return;
            };
            if ui.conn_token.get_untracked() != tok {
                ui.conn_token.set(tok);
                code.set(String::new());
                ctx_in.connect_typed();
            }
        });
    }
    // Signed in with the redeemed token: say it once the connection is
    // verified (the probe's own acknowledgement would otherwise be the
    // last word in the status line).
    cx.effect(move || {
        let connected = store.conn.with(|c| matches!(c, ConnPhase::Connected(_)));
        let pending = rec.with(|r| r.new_token.is_some() && !r.announced);
        // After the sign-in's own reads settle (their notices come first).
        let settled = store.busy.with(|b| b.is_empty());
        if connected && pending && settled {
            store
                .notice
                .set(Some(crate::store::email::SIGNED_IN_NEW_TOKEN.into()));
            rec.update(|r| r.announced = true);
        }
    });
    // The resend cooldown ticks once a second, only while it runs.
    {
        let ticker: std::rc::Rc<std::cell::RefCell<Option<abstracttui::reactive::IntervalHandle>>> =
            std::rc::Rc::new(std::cell::RefCell::new(None));
        cx.effect(move || {
            let _ = clock.get();
            let waiting = rec.with(|r| match &r.step {
                RecoveryStep::Code(a) | RecoveryStep::Redeeming(a) => a.resend_wait_s(now_ms()) > 0,
                _ => false,
            });
            let mut slot = ticker.borrow_mut();
            match (waiting, slot.is_some()) {
                (true, false) => {
                    *slot = Some(abstracttui::reactive::interval(
                        cx,
                        std::time::Duration::from_secs(1),
                        move || clock.update(|c| *c += 1),
                    ));
                }
                (false, true) => {
                    if let Some(h) = slot.take() {
                        h.cancel();
                    }
                }
                _ => {}
            }
        });
    }
    let request = {
        let ctx = ctx.clone();
        move || {
            let uid = user.get_untracked().trim().to_string();
            if uid.is_empty() {
                rec.update(|r| r.error = Some("Type your gateway user first.".into()));
                return;
            }
            let url = crate::ui::normalize_url(&ui.conn_url.get_untracked());
            rec.update(|r| {
                r.error = None;
                if matches!(r.step, RecoveryStep::Idle) {
                    r.step = RecoveryStep::Sending;
                }
            });
            ctx.send(Cmd::Operator(OpCmd::Recovery(RecoveryAction::Request {
                url,
                user_id: uid,
                tenant_id: "default".into(),
            })));
        }
    };
    let redeem = {
        let ctx = ctx.clone();
        move || {
            let c = code.get_untracked();
            if !code_complete(&c) {
                return;
            }
            let Some(answer) = rec.with_untracked(|r| match &r.step {
                RecoveryStep::Code(a) => Some(a.clone()),
                _ => None,
            }) else {
                return;
            };
            let url = crate::ui::normalize_url(&ui.conn_url.get_untracked());
            rec.update(|r| {
                r.error = None;
                r.step = RecoveryStep::Redeeming(answer.clone());
            });
            ctx.send(Cmd::Operator(OpCmd::Recovery(RecoveryAction::Redeem {
                url,
                user_id: answer.user_id.clone(),
                tenant_id: "default".into(),
                code: c.trim().to_string().into(),
            })));
        }
    };
    dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |rcx| {
        let t = tt;
        let r = rec.get();
        // The token a code returned, shown once (even once signed in).
        if let Some(tok) = r.new_token.clone() {
            let tok_copy = tok.clone();
            return Element::new()
                .style(LayoutStyle::column().gap(0).shrink(0.0))
                .child(line(vec![span_bold(
                    format!("Signed in as {} with a new token.", r.signed_in_user),
                    t.ok,
                )]))
                .child({
                    // Wrapped (never cut): the one time the token is shown.
                    let w = (crate::ui::page_viewport(rcx).get_untracked().w - 6).max(20) as usize;
                    let mut c = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                    for l in super::util::wrap_text(
                        "Your new token — shown once; your old token no longer works. Copy it now:",
                        w,
                    ) {
                        c = c.child(line(vec![span(l, t.warn)]));
                    }
                    c.build()
                })
                .child(line(vec![span_bold(tok, t.text)]))
                .child({
                    let acts = recovery_actions("token", false, 0, false);
                    let mut row = Element::new().style(LayoutStyle::row().gap(1).h(1).shrink(0.0));
                    for a in acts {
                        let tok_copy = tok_copy.clone();
                        row = row.child(btn(rcx, &t, &a, move || match a.id {
                            "copy" => {
                                copy_to_clipboard(tok_copy.clone());
                                store
                                    .notice
                                    .set(Some("token copied to the clipboard".into()));
                            }
                            _ => rec.update(|r| r.new_token = None),
                        }));
                    }
                    row.build()
                })
                .build();
        }
        if !signed_out.get() || r.available != Some(true) {
            return Element::new().style(LayoutStyle::default().h(0)).build();
        }
        let error_line = |t: &TokenSet| match &r.error {
            Some(e) => line(vec![span(format!("✗ {e}"), t.error)]),
            None => Element::new().style(LayoutStyle::default().h(0)).build(),
        };
        match &r.step {
            RecoveryStep::Idle => {
                let request = request.clone();
                Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(field(
                        &t,
                        "Gateway user",
                        super::w::caret_tracked(
                            rcx,
                            caret,
                            TextInput::new()
                                .value(user)
                                .layout(LayoutStyle::default().w(24).h(1))
                                .element(rcx, &t),
                        )
                        .build(),
                    ))
                    .child(field(&t, "", {
                        let a = recovery_actions("idle", false, 0, false).remove(0);
                        let request = request;
                        Element::new()
                            .style(LayoutStyle::row().h(1).w(a.width()).shrink(0.0))
                            .child(btn(rcx, &t, &a, request))
                            .build()
                    }))
                    .child(error_line(&t))
                    .build()
            }
            RecoveryStep::Sending => field(&t, "", line(vec![span("◌ Sending…", t.info)])),
            RecoveryStep::Code(a) | RecoveryStep::Redeeming(a) => {
                let checking = matches!(r.step, RecoveryStep::Redeeming(_));
                let (request, redeem, redeem_enter) =
                    (request.clone(), redeem.clone(), redeem.clone());
                // The gateway's honest answer, wrapped (never cut).
                let msg_w = (crate::ui::page_viewport(rcx).get_untracked().w - 6).max(20) as usize;
                let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                for l in super::util::wrap_text(&a.message, msg_w) {
                    col = col.child(line(vec![span(l, if a.sent { t.ok } else { t.warn })]));
                }
                if a.sent {
                    col = col.child(line(vec![span(
                        crate::store::email::CODE_GIVES_NEW_TOKEN,
                        t.text_muted,
                    )]));
                }
                if a.sent {
                    col = col
                        .child(super::util::field_w(&t, "Code from the email", 20, {
                            let code_el = TextInput::new()
                                .value(code)
                                .placeholder("8 digits")
                                .placeholder_while_focused(true)
                                .on_submit(move |_| redeem_enter())
                                .layout(LayoutStyle::default().w(12).h(1))
                                .element(rcx, &t);
                            super::w::caret_tracked(rcx, caret, code_el)
                                .autofocus()
                                .build()
                        }))
                        .child(error_line(&t))
                        .child(field(
                            &t,
                            "",
                            // Rebuilt as the code is typed: "Use code"
                            // becomes available at the 8th digit.
                            dyn_view_scoped(
                                LayoutStyle::row().gap(2).h(1).shrink(0.0),
                                move |bcx| {
                                    let ready = code_complete(&code.get());
                                    let redeem = redeem.clone();
                                    if checking {
                                        return line(vec![span("Checking the code…", t.info)]);
                                    }
                                    let a = recovery_actions("code", ready, 0, false).remove(0);
                                    btn(bcx, &t, &a, redeem)
                                },
                            ),
                        ));
                } else {
                    col = col.child(error_line(&t));
                }
                // No email address: a new code can't be sent either — the
                // message says so; only "Back to token" is offered.
                let can_resend = a.sent || a.reason_code != "no_email_address";
                let a_clock = a.clone();
                col.child(super::util::field_w(
                    &t,
                    "",
                    1,
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                        // Its own region: the countdown re-renders this
                        // button only, never the code field being typed.
                        .child(dyn_view_scoped(
                            LayoutStyle::row().h(1).shrink(0.0),
                            move |kcx| {
                                if !can_resend {
                                    return Element::new()
                                        .style(LayoutStyle::default().w(0).h(1))
                                        .build();
                                }
                                let _ = clock.get();
                                let wait = if checking {
                                    1
                                } else {
                                    a_clock.resend_wait_s(now_ms())
                                };
                                let Some(a) = recovery_actions("code", false, wait, true)
                                    .into_iter()
                                    .find(|a| a.id == "resend")
                                else {
                                    return Element::new()
                                        .style(LayoutStyle::default().w(0).h(1))
                                        .build();
                                };
                                let request = request.clone();
                                Element::new()
                                    .style(LayoutStyle::row().h(1).w(a.width()).shrink(0.0))
                                    .child(btn(kcx, &t, &a, request))
                                    .build()
                            },
                        ))
                        .child({
                            let a = recovery_actions("code", false, 0, false)
                                .into_iter()
                                .find(|a| a.id == "back")
                                .expect("back action");
                            btn(rcx, &t, &a, move || {
                                code.set(String::new());
                                rec.update(|r| {
                                    r.step = RecoveryStep::Idle;
                                    r.error = None;
                                });
                            })
                        })
                        .build(),
                ))
                .build()
            }
        }
    })
}

/// At or under this many terminal rows the Connection screen drops its
/// gaps and vertical padding (M3: the roomy layout needs ~32 connected).
pub const TIGHT_ROWS: i32 = 32;

/// `.clip()`: pinned rows that still overflow are cut at the content box —
/// never painted over the block's bottom border.
fn roomy_or_tight(tight: bool) -> LayoutStyle {
    if tight {
        LayoutStyle::column()
            .gap(0)
            .grow(1.0)
            .padding(Edges::hv(1, 0))
            .clip()
    } else {
        LayoutStyle::column()
            .gap(1)
            .grow(1.0)
            .padding(Edges::all(1))
            .clip()
    }
}

/// A row that keeps its height under pressure: the column clips what
/// does not fit at the bottom instead of flex-shrinking rows to nothing.
fn pin(v: View) -> View {
    Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(v)
        .build()
}

/// The token-source line of a probe that sent NO Authorization header —
/// the one writer's constant every reader compares against (lib.rs boot
/// probe, `Ctx::effective_credentials_with_source`, the 401 copy).
pub const NO_TOKEN_SENT: &str = "none — no Authorization header sent";

/// Where the admin token comes from — `abstractgateway serve` prints it
/// when it starts. Given directly (`--token <token>`), never as a file or
/// an env var in the instructions (operator rule, wave 2).
pub const ADMIN_TOKEN_HINT: &str =
    "admin token: `abstractgateway serve` prints it when it starts (on the gateway host)";

/// The honest states, visually distinct — never one generic "error".
/// Auth failures name the SOURCE of the token that was rejected (field /
/// env / flag / none) — the one fact that untangles "I can't
/// authenticate" when a stale export or an empty field is the culprit.
fn status_view(t: &TokenSet, conn: &ConnPhase, token_source: Option<String>) -> View {
    let source_line = |t: &TokenSet| {
        line(vec![span(
            format!(
                "  token sent: {}",
                token_source.clone().unwrap_or_else(|| "unknown".into())
            ),
            t.warn,
        )])
    };
    match conn {
        ConnPhase::NotConnected => line(vec![
            span("○ ", t.text_muted),
            span("not connected — enter the URL and token, then probe", t.text_muted),
        ]),
        ConnPhase::Probing => line(vec![span("◌ probing…", t.info)]),
        ConnPhase::Verifying(_) => line(vec![span(
            "◌ a request failed — re-checking the gateway…",
            t.warn,
        )]),
        // Two different 401s (REVIEW-1 M6): nothing was sent (sign-in
        // needed — say WHERE the admin token lives), or a token was sent
        // and refused (say which one, and the same way out).
        ConnPhase::Unauthorized(msg) if token_source.as_deref() == Some(NO_TOKEN_SENT) => {
            Element::new()
                .style(LayoutStyle::column())
                .child(line(vec![span_bold(
                    "✗ sign-in needed (401) — no token was sent",
                    t.error,
                )]))
                .child(line(vec![span(format!("  {msg}"), t.text)]))
                .child(line(vec![span(
                    format!("  {ADMIN_TOKEN_HINT}"),
                    t.warn,
                )]))
                .child(line(vec![span(
                    "  paste it in Token, or launch with --token <token>",
                    t.text_muted,
                )]))
                .build()
        }
        ConnPhase::Unauthorized(msg) => Element::new()
            .style(LayoutStyle::column())
            .child(line(vec![span_bold(
                "✗ unauthorized (401) — the gateway rejected the token sent",
                t.error,
            )]))
            .child(line(vec![span(format!("  {msg}"), t.text)]))
            .child(source_line(t))
            .child(line(vec![span(
                format!("  {ADMIN_TOKEN_HINT}"),
                t.text_muted,
            )]))
            .build(),
        ConnPhase::Forbidden(msg) => Element::new()
            .style(LayoutStyle::column())
            .child(line(vec![span_bold("✗ forbidden (403)", t.error)]))
            .child(line(vec![span(format!("  {msg}"), t.text)]))
            .child(source_line(t))
            .child(line(vec![span(
                "  the token authenticated but lacks access — an admin token is needed here",
                t.text_muted,
            )]))
            .build(),
        ConnPhase::NotGateway(status, msg) => Element::new()
            .style(LayoutStyle::column())
            .child(line(vec![span_bold(
                if *status == 0 {
                    "✗ answered, but not like a gateway".to_string()
                } else {
                    format!("✗ answered HTTP {status}, but not like a gateway")
                },
                t.warn,
            )]))
            .child(line(vec![span(format!("  {msg}"), t.text)]))
            .child(line(vec![span(
                "  something IS listening there — is another service squatting this port?",
                t.text_muted,
            )]))
            .build(),
        ConnPhase::Unreachable(msg) => Element::new()
            .style(LayoutStyle::column())
            .child(line(vec![span_bold("✗ gateway unreachable", t.error)]))
            .child(line(vec![span(format!("  {msg}"), t.text)]))
            .child(line(vec![span(
                "  is the gateway running on that host/port? (a booting gateway can take a minute before it listens)",
                t.text_muted,
            )]))
            .build(),
        ConnPhase::Connected(id) => {
            let roles = if id.roles.is_empty() {
                "—".to_string()
            } else {
                id.roles.join(", ")
            };
            Element::new()
                .style(LayoutStyle::column())
                .child(
                    Element::new()
                        .style(LayoutStyle::row().gap(1).h(1))
                        .child(line_frag(t, "● connected", t.ok, true))
                        .child(badge(
                            t,
                            if id.admin { "admin" } else { "not admin" },
                            if id.admin { Tone::Ok } else { Tone::Warn },
                        ))
                        .child(badge(
                            t,
                            &format!("auth: {}", id.auth_mode),
                            Tone::Info,
                        ))
                        .child(badge(
                            t,
                            &format!("routing: {}", id.routing_mode),
                            Tone::Muted,
                        ))
                        .build(),
                )
                .child(line(vec![span(
                    format!(
                        "  {} @ tenant {} · roles: {}",
                        id.user_id, id.tenant_id, roles
                    ),
                    t.text,
                )]))
                .child(line(vec![span(
                    format!(
                        "  token sent: {}",
                        token_source.clone().unwrap_or_else(|| "unknown".into())
                    ),
                    t.text_faint,
                )]))
                .child(if id.admin {
                    line(vec![span(
                        "  ready — Ctrl+N continues to the next step (] also works outside text fields)",
                        t.text_muted,
                    )])
                } else {
                    line(vec![span(
                        "  not an admin: admin-only actions are refused with the reason; reads still work",
                        t.warn,
                    )])
                })
                .build()
        }
    }
}

fn line_frag(_t: &TokenSet, text: &str, ink: abstracttui::base::Rgba, bold: bool) -> View {
    line(vec![if bold {
        span_bold(text.to_string(), ink)
    } else {
        span(text.to_string(), ink)
    }])
}
