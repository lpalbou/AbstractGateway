//! Connection screen: base URL + admin token → probe → honest state.
//!
//! The probe is `GET /ping` then `GET /me` — never discovery (the
//! gateway's own docstring law: discovery can legitimately time out or
//! skip providers; ping validates reachability + auth, nothing else).

use abstracttui::prelude::*;

use super::util::{badge, esc_releases_focus, field, line, span, span_bold};
use super::Ctx;
use crate::store::ConnPhase;
use abstracttui::widgets::Tone;

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ui = ctx.ui;
    let store = ctx.store;
    let tt = *t;

    let ctx_probe = ctx.clone();
    let ctx_submit_url = ctx.clone();
    let ctx_submit_tok = ctx.clone();

    let env_set = ctx.env_token_set;
    let notice = store.notice;

    // ≤ TIGHT_ROWS terminal rows (REVIEW-1 M3): the roomy layout (gap 1,
    // padding 1) needs ~32 rows once connected; below that the column
    // flex-shrank its rows to ZERO and the top of the screen went blank.
    // Tight = no gaps, no vertical padding, and the two lines the status
    // block already says (the connected intro, the About hint — F1 is in
    // --help and the footer) step aside. Every row is pinned (`pin`):
    // whatever still does not fit clips at the bottom, never the top.
    let vp = abstracttui::app::use_viewport(cx);
    let tight = cx.memo(move || vp.get().h <= TIGHT_ROWS);
    // A memo, so the URL slot below re-mounts only when this FLIPS — not
    // on every probe transition (a remount puts the caret back at 0).
    let live = cx.memo(move || {
        store
            .conn
            .with(|c| matches!(c, ConnPhase::Connected(_) | ConnPhase::Verifying(_)))
    });

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
                    "Point the console at a running AbstractGateway and probe it.",
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
                .on_submit(move |_| ctx_u.connect_now())
                .layout(LayoutStyle::default().w(46).h(1))
                .element(gcx, &t);
            let el = esc_releases_focus(el, notice);
            field(
                &t,
                "Gateway URL",
                if live { el.build() } else { el.autofocus().build() },
            )
        }))
        .child(pin(field(
            t,
            "Admin token",
            esc_releases_focus(
                TextInput::new()
                    .value(ui.conn_token)
                    .masked(true)
                    .placeholder("bearer token — blank uses $ABSTRACTGATEWAY_AUTH_TOKEN")
                    .placeholder_while_focused(true)
                    .on_submit(move |_| ctx_submit_tok.connect_now())
                    .layout(LayoutStyle::default().w(46).h(1))
                    .element(cx, t),
                notice,
            )
            .build(),
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
                        "using $ABSTRACTGATEWAY_AUTH_TOKEN — type here only to switch identity · tokens stay in memory, never on disk",
                        t.text_muted,
                    )])
                } else if env_set {
                    line(vec![span(
                        "env ABSTRACTGATEWAY_AUTH_TOKEN: set — the typed token wins while the field is non-empty",
                        t.text_faint,
                    )])
                } else {
                    line(vec![span(
                        "env ABSTRACTGATEWAY_AUTH_TOKEN: not set · the token stays in memory, never on disk",
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
                // not a login.
                let label = if store.conn.get().is_connected() {
                    "Re-probe (connected ✓)"
                } else {
                    "Probe gateway"
                };
                let ctx_b = ctx_probe.clone();
                field(
                    &t,
                    "",
                    Button::new(label)
                        .on_click(move || ctx_b.connect_now())
                        .element(gcx, &t)
                        .build(),
                )
            },
        ))
        .child(dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
            status_view(&tt, &store.conn.get(), ui.token_source.get())
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
                    "F1 (or ?) — this console, AbstractFramework, the gateway's versions",
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
        // Who can reach this gateway (localhost / LAN / internet) and the
        // addresses to copy — the gateway's `gateway_network_v1` verdicts.
        .child(super::network::panel(cx, ctx, t))
        .element(t)
        .style_signal(move || roomy_or_tight(tight.get()))
        .build()
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

/// Where the gateway keeps its admin token — the gateway's own words
/// (`abstractgateway serve --help`, `--print-token`).
pub const ADMIN_TOKEN_PATH: &str = "<data dir>/auth/bootstrap-admin-token";

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
                    format!("  admin token: {ADMIN_TOKEN_PATH} on the gateway host"),
                    t.warn,
                )]))
                .child(line(vec![span(
                    "  paste it in Admin token, or launch with --token-file PATH",
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
                format!("  admin token: {ADMIN_TOKEN_PATH} on the gateway host"),
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
