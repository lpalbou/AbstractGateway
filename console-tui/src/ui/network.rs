//! Network screen (key `N`): who can reach the gateway — Localhost only
//! / Local network / Internet — what is SAVED versus what is RUNNING now,
//! and every address a client can use, one keypress from the clipboard.
//! The Connection screen keeps a one-line summary ([`summary`]).
//!
//! Contract `gateway_network_v1` (`GET/POST /api/gateway/network`,
//! `POST /api/gateway/network/restart`). The gateway owns every verdict:
//! which modes are allowed (user auth), whether a restart is required and
//! whether one can apply the setting (`restart.reason`). This screen
//! renders them and never guesses.
//!
//! The modes are a LIST, not radio buttons: `(•)` marks the SAVED mode
//! only, the cursor is the list's highlight, and Enter saves the
//! highlighted mode (Internet asks for the acknowledgement first). When a
//! save leaves the gateway needing a restart that can apply it, the
//! screen offers the restart (a confirm); the restart watcher then
//! reconnects and the screen reads the network again.
//!
//! Keys (focus inside the screen): ↑/↓ + Enter the mode · Tab to the
//! address list · ↑/↓ pick · c or Enter copies the URL · Tab to the
//! reverse-proxy origins line (comma-separated, Enter saves, empty
//! clears) · Tab to the trust-proxy checkbox (Space toggles and saves).

use abstracttui::prelude::*;
use abstracttui::widgets::{List, TextInput};

use super::util::{field_w, line, span, span_bold, wrap_text};
use super::widths::BLOCK_CHROME;
use super::Ctx;
use crate::store::{ConnPhase, Loadable, NetworkData};
use crate::worker::Cmd;

/// Ask for `GET /network` once per connection (a reconnect resets the
/// slot to NotAsked; `r` reloads explicitly). Shared by the screen and
/// the Connection summary.
fn load_on_connect(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let ctx_load = ctx.clone();
    cx.effect(move || {
        let connected = store.conn.with(ConnPhase::is_connected);
        if connected
            && store
                .network
                .with_untracked(|n| matches!(n, Loadable::NotAsked))
        {
            store.network.set(Loadable::Loading);
            ctx_load.send(Cmd::LoadNetwork);
        }
    });
}

/// Where a saved value comes from, in words.
fn saved_word(source: &str) -> &'static str {
    if source == "stored" {
        "saved"
    } else {
        "not saved — the default"
    }
}

/// Where a running value came from, in words.
fn running_word(source: &str) -> &'static str {
    match source {
        "cli" => "from the command line",
        "setting" => "from the saved setting",
        "default" => "the default",
        _ => "source unknown",
    }
}

/// "Local network · port 8080 (saved)" — the SAVED exposure.
pub fn saved_text(d: &NetworkData) -> String {
    let port = if d.configured_port_source == "stored" {
        format!("port {}", d.configured_port)
    } else {
        "port not saved".to_string()
    };
    format!(
        "{} ({}) · {port}",
        d.configured_label,
        saved_word(&d.configured_source)
    )
}

/// "Localhost only 127.0.0.1:18882 (port from the command line)" — what
/// RUNS now.
pub fn running_text(d: &NetworkData) -> String {
    match d.effective_port {
        Some(p) if !d.effective_bind.is_empty() => format!(
            "{} {}:{p} (address {}, port {})",
            d.effective_label,
            d.effective_bind,
            running_word(&d.effective_host_source),
            running_word(&d.effective_port_source)
        ),
        _ => "unknown (no running gateway recorded)".to_string(),
    }
}

/// One mode row: `(•)` marks the SAVED mode only; the list highlight is
/// the cursor.
pub fn mode_row(m: &crate::store::NetworkMode) -> String {
    let mark = if m.selected { "(•)" } else { "( )" };
    let label = if m.id == "internet" {
        format!("{}…", m.label)
    } else {
        m.label.clone()
    };
    let refusal = if m.allowed { "" } else { " (needs user auth)" };
    format!("{mark} {label}{refusal}")
}

/// The Connection screen's one line: saved vs running, and where to go.
pub fn summary(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    load_on_connect(cx, ctx);
    let store = ctx.store;
    let tt = *t;
    dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
        let t = tt;
        if !store.conn.get().is_connected() {
            return line(vec![span("Network: probe the gateway first", t.text_faint)]);
        }
        match store.network.get() {
            Loadable::Ready(d) => line(vec![
                span_bold("Network: ", t.accent),
                span(
                    format!(
                        "{}{} · running {} {}:{} — N opens Network",
                        d.configured_label,
                        if d.restart_required {
                            " (saved, restart needed)"
                        } else {
                            ""
                        },
                        d.effective_label,
                        d.effective_bind,
                        d.effective_port
                            .map(|p| p.to_string())
                            .unwrap_or_else(|| "?".into()),
                    ),
                    if d.restart_required {
                        t.warn
                    } else {
                        t.text_muted
                    },
                ),
            ]),
            Loadable::Failed(e) => line(vec![span(
                format!("Network: {e} (N opens Network)"),
                t.warn,
            )]),
            _ => line(vec![span("Network: reading…", t.info)]),
        }
    })
}

/// The Network screen.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    load_on_connect(cx, ctx);
    let store = ctx.store;
    let tt = *t;

    // A save that needs a restart which can apply it: offer it (the
    // worker sets the offer from the verified read-back).
    {
        let ctx_offer = ctx.clone();
        cx.effect(move || {
            let Some(offer) = store.op.network_restart_offer.get() else {
                return;
            };
            store.op.network_restart_offer.set(None);
            confirm_restart(cx, &ctx_offer, offer);
        });
    }

    let ctx_body = ctx.clone();
    let vp = abstracttui::app::use_viewport(cx);
    Block::new()
        .border(BorderKind::Rounded)
        .title("Network — who can reach this gateway")
        .fill(t.surface)
        .layout(
            LayoutStyle::column()
                .gap(0)
                .grow(1.0)
                .padding(Edges::hv(1, 0))
                .clip(),
        )
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).grow(1.0),
            move |gcx| {
                let t = tt;
                if !store.conn.get().is_connected() {
                    return line(vec![span(
                        "not connected — probe the gateway on 1 Connection first",
                        t.text_faint,
                    )]);
                }
                match store.network.get() {
                    Loadable::NotAsked | Loadable::Loading => {
                        line(vec![span("◌ reading network exposure…", t.info)])
                    }
                    Loadable::Failed(e) => line(vec![span(
                        format!("Network exposure: {e} (r retries)"),
                        if e.status() == Some(404) {
                            t.text_muted
                        } else {
                            t.warn
                        },
                    )]),
                    Loadable::Ready(d) => {
                        let wrap_w = (vp.get_untracked().w - BLOCK_CHROME - 2).max(20) as usize;
                        ready_view(gcx, &ctx_body, &t, d, wrap_w)
                    }
                }
            },
        ))
        .element(t)
        .build()
}

fn address_row(a: &crate::store::NetworkAddress) -> String {
    let mark = match a.reachable {
        Some(true) => "●",
        Some(false) => "○",
        None => "?",
    };
    let label = if a.label.is_empty() {
        a.kind.clone()
    } else {
        format!("{} · {}", a.kind, a.label)
    };
    format!("{mark} {:<40} {label}", a.url)
}

/// Wrapped lines in one ink (notes are the gateway's own sentences:
/// never cut).
fn wrapped(col: Element, text: &str, width: usize, ink: Rgba) -> Element {
    let mut col = col;
    for l in wrap_text(text, width) {
        col = col.child(line(vec![span(l, ink)]));
    }
    col
}

fn ready_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: NetworkData, wrap_w: usize) -> View {
    let store = ctx.store;
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));

    // Saved vs running now, each labelled.
    col = col.child(line(vec![
        span_bold(format!("{:<13}", "Saved:"), t.accent),
        span_bold(
            saved_text(&d),
            if d.configured_mode == "localhost" {
                t.ok
            } else {
                t.warn
            },
        ),
    ]));
    col = col.child(line(vec![
        span_bold(format!("{:<13}", "Running now:"), t.accent),
        span(running_text(&d), t.text),
    ]));

    // The restart story — required, possible, or impossible and why (the
    // gateway's `restart.reason`, wrapped).
    if d.restart_required {
        let port = d.restart_port.unwrap_or(d.configured_port);
        if d.restart_applies && d.restart_available {
            let ctx_r = ctx.clone();
            let offer = crate::worker::network_restart_offer(&d).unwrap_or_default();
            col = col.child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1))
                    .child(line(vec![span_bold(
                        format!(
                            "⚠ saved, not running yet: a restart applies '{}' on port {port}",
                            d.configured_mode
                        ),
                        t.warn,
                    )]))
                    .child(
                        Button::new("Restart to apply")
                            .on_click(move || confirm_restart(cx, &ctx_r, offer.clone()))
                            .element(cx, t)
                            .build(),
                    )
                    .build(),
            );
        } else {
            let why = d
                .restart_reason
                .clone()
                .unwrap_or_else(|| d.restart_how.clone());
            col = wrapped(
                col,
                &format!(
                    "⚠ saved, not running yet{}: {why}",
                    if d.overridden_by_cli {
                        " (the command line overrides the setting)"
                    } else {
                        ""
                    }
                ),
                wrap_w,
                t.warn,
            );
        }
    }
    if !d.auth_ok {
        if let Some(fix) = d.auth_fix.clone() {
            col = wrapped(col, &format!("auth: {fix}"), wrap_w, t.warn);
        }
    }

    // Mode list (Enter saves the highlighted mode) + addresses (c copies).
    let rows: Vec<String> = d.modes.iter().map(mode_row).collect();
    let saved_ix = d.modes.iter().position(|m| m.selected).unwrap_or(0);
    let mode_sel = cx.signal(saved_ix);
    let ctx_apply = ctx.clone();
    let d_apply = d.clone();
    let legend = if d.writable {
        "(•) saved · Enter applies the highlighted mode"
    } else {
        "(•) saved · changing it needs an admin token"
    };
    let modes = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0).w(36))
        .child(line(vec![span_bold("Who can reach it", t.accent)]))
        .child(
            List::new(rows.clone())
                .selection(mode_sel)
                .on_activate(move |i| apply_mode(cx, &ctx_apply, &d_apply, i))
                .layout(
                    LayoutStyle::default()
                        .h(rows.len().max(1) as i32)
                        .shrink(0.0),
                )
                .element(cx, t)
                .build(),
        )
        .build();

    let urls: Vec<String> = d.addresses.iter().map(|a| a.url.clone()).collect();
    let addr_rows: Vec<String> = d.addresses.iter().map(address_row).collect();
    let addr_sel = cx.signal(0usize);
    let visible = addr_rows.len().clamp(1, 6) as i32;
    let urls_c = urls.clone();
    let urls_a = urls.clone();
    let addresses = Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .shortcut(KeyChord::plain(Key::Char('c')), move |_| {
            copy_url(store, urls_c.get(addr_sel.get_untracked()).cloned());
        })
        .child(line(vec![span_bold("Addresses", t.accent)]))
        .child(
            List::new(addr_rows)
                .selection(addr_sel)
                .on_activate(move |i| copy_url(store, urls_a.get(i).cloned()))
                .layout(LayoutStyle::default().h(visible).grow(1.0))
                .element(cx, t)
                .build(),
        )
        .build();

    col = col.child(line(vec![span(String::new(), t.text)]));
    col = col.child(
        Element::new()
            .style(LayoutStyle::row().gap(3).shrink(0.0))
            .child(modes)
            .child(addresses)
            .build(),
    );
    col = col.child(line(vec![span(legend, t.text_faint)]));
    col = col.child(line(vec![span(
        format!(
            "addresses: c/Enter copies · ● listening ○ not yet · primary {}",
            d.copy_hint
        ),
        t.text_faint,
    )]));
    if d.proxy.present {
        col = col.child(line(vec![span(String::new(), t.text)]));
        col = col.child(proxy_view(cx, ctx, t, &d));
    }
    // After the reverse proxy so the screen's Tab order stays put.
    // WAN address (web: "Look up my public address" — admin, internet
    // mode, no public address listed yet). One outbound HTTPS call made
    // BY THE GATEWAY; the answer lands as a `public` address row.
    if crate::store::operator::offers_public_lookup(&d) {
        let ctx_l = ctx.clone();
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Look up my public address")
                        .on_click(move || {
                            ctx_l.send(Cmd::Operator(crate::worker::operator::OpCmd::LookupPublic))
                        })
                        .element(cx, t)
                        .build(),
                )
                .child(line(vec![span(
                    "asks a public service which address your network shows the internet",
                    t.text_faint,
                )]))
                .build(),
        );
    }
    if let Some(note) = &d.public_note {
        col = wrapped(
            col,
            &format!("public address: {note}"),
            wrap_w,
            t.text_faint,
        );
    }
    col.build()
}

fn source_word(source: &str, overridden: bool) -> String {
    if overridden {
        "environment override".to_string()
    } else {
        match source {
            "setting" => "saved setting".to_string(),
            "" => "default".to_string(),
            other => other.to_string(),
        }
    }
}

/// Split the edit line exactly as `abstractgateway network set
/// --allowed-origins` does: commas, trimmed, empty pieces dropped.
pub fn parse_origins_line(text: &str) -> Vec<String> {
    text.split(',')
        .map(str::trim)
        .filter(|x| !x.is_empty())
        .map(str::to_string)
        .collect()
}

/// Reverse proxy (mission Z): the browser origins edit line and the
/// trust-proxy checkbox, with where each value comes from and the
/// environment override said in words. The gateway validates; a refusal
/// comes back as a notice with its exact sentence.
fn proxy_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: &NetworkData) -> View {
    let p = d.proxy.clone();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    col = col.child(line(vec![
        span_bold("Reverse proxy", t.accent),
        span(
            format!(
                " · origins: {} [{}] · trust proxy: {} [{}] · applies to the next request",
                if p.origins.is_empty() {
                    "none".to_string()
                } else {
                    p.origins.join(", ")
                },
                source_word(&p.origins_source, p.origins_overridden),
                if p.trust_proxy { "on" } else { "off" },
                source_word(&p.trust_source, p.trust_overridden),
            ),
            t.text_muted,
        ),
    ]));
    if p.origins_overridden {
        col = col.child(line(vec![span(
            format!(
                "⚠ this gateway was started with {} in its environment: that list decides ({}); the origins saved here apply once it starts without it",
                p.origins_env_name,
                if p.origins_env_value.is_empty() {
                    "none".to_string()
                } else {
                    p.origins_env_value.join(", ")
                }
            ),
            t.warn,
        )]));
    }
    if p.trust_overridden {
        col = col.child(line(vec![span(
            format!(
                "⚠ this gateway was started with {} in its environment: trust proxy is {}; the switch saved here applies once it starts without it",
                p.trust_env_name,
                if p.trust_effective { "on" } else { "off" }
            ),
            t.warn,
        )]));
    }
    for w in &p.origins_warnings {
        col = col.child(line(vec![span(format!("⚠ {w}"), t.warn)]));
    }
    if !d.writable {
        col = col.child(line(vec![span(
            "changing the reverse proxy needs an admin token",
            t.text_faint,
        )]));
        return col.build();
    }
    let origins_text = cx.signal(p.origins.join(", "));
    let ctx_o = ctx.clone();
    col = col.child(field_w(
        t,
        "browser origins",
        16,
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(
                super::util::esc_releases_focus(
                    TextInput::new()
                        .value(origins_text)
                        .placeholder("https://gateway.example.com (comma-separated)")
                        .layout(LayoutStyle::default().w(60).h(1).shrink(0.0))
                        .on_submit(move |text: &str| {
                            ctx_o.send(Cmd::SetNetworkProxy {
                                allowed_origins: Some(parse_origins_line(text)),
                                trust_proxy: None,
                            })
                        })
                        .element(cx, t),
                    ctx.store.notice,
                )
                .build(),
            )
            .child(line(vec![span("Enter saves · empty clears", t.text_faint)]))
            .build(),
    ));
    // A persistent setting = a switch labelled by the feature; it applies
    // at once and the shown state is the gateway's read-back.
    let trust = cx.signal(p.trust_proxy);
    let ctx_t = ctx.clone();
    col = col
        .child(
            super::switch::Switch::new("Trust proxies on other machines (X-Forwarded-For)", trust)
                .notice(ctx.store.notice)
                .on_request(move |on| {
                    ctx_t.send(Cmd::SetNetworkProxy {
                        allowed_origins: None,
                        trust_proxy: Some(on),
                    })
                })
                .element(cx, t)
                .build(),
        )
        .child(line(vec![span(
            "    only when your own proxy sits in front of every request · space switch",
            t.text_faint,
        )]));
    col.build()
}

fn copy_url(store: crate::store::Store, url: Option<String>) {
    match url {
        Some(u) if !u.is_empty() => {
            copy_to_clipboard(u.clone());
            store.notice.set(Some(format!("copied {u}")));
        }
        _ => store
            .notice
            .set(Some("no address selected — nothing to copy".into())),
    }
}

fn apply_mode(cx: Scope, ctx: &Ctx, d: &NetworkData, idx: usize) {
    let store = ctx.store;
    let Some(m) = d.modes.get(idx).cloned() else {
        return;
    };
    if m.selected {
        store
            .notice
            .set(Some(format!("'{}' is already the saved mode", m.label)));
        return;
    }
    if !d.writable {
        store.notice.set(Some(
            "changing the network exposure needs an admin token".into(),
        ));
        return;
    }
    if !m.allowed {
        store.notice.set(Some(format!(
            "'{}' refused: {}{}",
            m.label,
            m.reason.clone().unwrap_or_default(),
            m.fix.map(|f| format!(" — fix: {f}")).unwrap_or_default()
        )));
        return;
    }
    if m.id == "internet" {
        let ctx2 = ctx.clone();
        super::confirm_danger(
            cx,
            ctx.ui,
            "Expose the gateway to the internet? It speaks plain HTTP: put a TLS reverse proxy or a tunnel \
             (Caddy, Cloudflare Tunnel, Tailscale Funnel) in front and never forward the raw port. Port \
             forwarding and firewalls are yours to configure. Applied at the next restart."
                .to_string(),
            "I understand — allow Internet",
            "Keep the current mode",
            move || {
                ctx2.send(Cmd::SetNetwork {
                    mode: "internet".into(),
                    acknowledge_internet: true,
                })
            },
        );
        return;
    }
    ctx.send(Cmd::SetNetwork {
        mode: m.id,
        acknowledge_internet: false,
    });
}

fn confirm_restart(cx: Scope, ctx: &Ctx, message: String) {
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        message,
        "Restart the gateway",
        "Not now",
        move || {
            ctx2.send(Cmd::Operator(
                crate::worker::operator::OpCmd::RestartNetwork,
            ))
        },
    );
}
