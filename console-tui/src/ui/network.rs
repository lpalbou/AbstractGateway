//! Network exposure panel (Connection screen): who can reach the
//! gateway — Localhost only / Local network / Internet — and every
//! address a client can use, one keypress from the clipboard.
//!
//! Contract `gateway_network_v1` (`GET/POST /api/gateway/network`,
//! `POST /api/gateway/network/restart`). The gateway owns every verdict:
//! which modes are allowed (user auth), whether a restart is required and
//! whether one can apply the setting (`serve --host` pins the bind). This
//! panel renders them and never guesses.
//!
//! Keys (focus inside the panel only, so the URL/token fields keep their
//! letters): ↑/↓ choose a mode · Enter applies it (Internet asks first) ·
//! Tab to the address list · ↑/↓ pick · c or Enter copies the URL ·
//! Tab to the reverse-proxy origins line (comma-separated, Enter saves,
//! empty clears) · Tab to the trust-proxy checkbox (Space toggles and
//! saves). Both reverse-proxy fields apply to the next request.

use abstracttui::prelude::*;
use abstracttui::widgets::{Checkbox, List, RadioGroup, TextInput};

use super::util::{field_w, line, span, span_bold};
use super::Ctx;
use crate::store::{ConnPhase, Loadable, NetworkData};
use crate::worker::Cmd;

pub fn panel(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let tt = *t;

    // Load once per connection (reset_domains puts the slot back to
    // NotAsked on a new probe; `r` on this screen reloads explicitly).
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

    let ctx_body = ctx.clone();
    dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |gcx| {
        let t = tt;
        if !store.conn.get().is_connected() {
            return line(vec![span(
                "Network exposure: probe the gateway first",
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
            Loadable::Ready(d) => ready_view(gcx, &ctx_body, &t, d),
        }
    })
}

fn mode_index(d: &NetworkData) -> usize {
    d.modes.iter().position(|m| m.selected).unwrap_or(0)
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

fn ready_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: NetworkData) -> View {
    let store = ctx.store;
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));

    // Header: configured vs running, in one line.
    let running = match d.effective_port {
        Some(p) if !d.effective_bind.is_empty() => format!("{}:{}", d.effective_bind, p),
        _ => "unknown".to_string(),
    };
    col = col.child(line(vec![
        span_bold("Network exposure: ", t.accent),
        span_bold(
            d.configured_label.clone(),
            if d.configured_mode == "localhost" {
                t.ok
            } else {
                t.warn
            },
        ),
        span(
            format!(
                " · port {} · running: {} {}",
                d.configured_port, d.effective_label, running
            ),
            t.text_muted,
        ),
    ]));

    // The restart story — required, possible, or impossible and why.
    if d.restart_required {
        if d.restart_applies && d.restart_available {
            let ctx_r = ctx.clone();
            col = col.child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1))
                    .child(line(vec![span_bold(
                        format!(
                            "⚠ restart required to apply '{}' on port {}",
                            d.configured_mode, d.configured_port
                        ),
                        t.warn,
                    )]))
                    .child(
                        Button::new("Restart to apply")
                            .on_click(move || confirm_restart(cx, &ctx_r))
                            .element(cx, t)
                            .build(),
                    )
                    .build(),
            );
        } else {
            col = col.child(line(vec![span_bold(
                format!(
                    "⚠ not applied{}: {}",
                    if d.overridden_by_cli {
                        " (command line overrides the setting)"
                    } else {
                        ""
                    },
                    d.restart_reason
                        .clone()
                        .unwrap_or_else(|| d.restart_how.clone())
                ),
                t.warn,
            )]));
        }
    }
    if !d.auth_ok {
        if let Some(fix) = d.auth_fix.clone() {
            col = col.child(line(vec![span(format!("auth: {fix}"), t.warn)]));
        }
    }

    // Mode picker (one tab stop; Enter applies) + addresses (c copies).
    let labels: Vec<String> = d
        .modes
        .iter()
        .map(|m| {
            let base = if m.id == "internet" {
                format!("{}…", m.label)
            } else {
                m.label.clone()
            };
            if m.allowed {
                base
            } else {
                format!("{base} (needs user auth)")
            }
        })
        .collect();
    let mode_sel = cx.signal(mode_index(&d));
    let ctx_apply = ctx.clone();
    let d_apply = d.clone();
    let modes = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .shortcut(KeyChord::plain(Key::Enter), move |_| {
            apply_mode(cx, &ctx_apply, &d_apply, mode_sel.get_untracked());
        })
        .child(
            RadioGroup::new(labels)
                .selection(mode_sel)
                .element(cx, t)
                .build(),
        )
        .build();

    let urls: Vec<String> = d.addresses.iter().map(|a| a.url.clone()).collect();
    let rows: Vec<String> = d.addresses.iter().map(address_row).collect();
    let addr_sel = cx.signal(0usize);
    let visible = rows.len().clamp(1, 5) as i32;
    let urls_c = urls.clone();
    let urls_a = urls.clone();
    let addresses = Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .shortcut(KeyChord::plain(Key::Char('c')), move |_| {
            copy_url(store, urls_c.get(addr_sel.get_untracked()).cloned());
        })
        .child(
            List::new(rows)
                .selection(addr_sel)
                .on_activate(move |i| copy_url(store, urls_a.get(i).cloned()))
                .layout(LayoutStyle::default().h(visible).grow(1.0))
                .element(cx, t)
                .build(),
        )
        .build();

    col = col.child(
        Element::new()
            .style(LayoutStyle::row().gap(3).shrink(0.0))
            .child(modes)
            .child(addresses)
            .build(),
    );
    col = col.child(line(vec![span(
        format!(
            "↑↓+Enter mode · Tab, c copies · ● listening ○ not yet · primary {}",
            d.copy_hint
        ),
        t.text_faint,
    )]));
    if d.proxy.present {
        col = col.child(proxy_view(cx, ctx, t, &d));
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
                    .element(cx, t)
                    .build(),
            )
            .child(line(vec![span("Enter saves · empty clears", t.text_faint)]))
            .build(),
    ));
    let trust = cx.signal(p.trust_proxy);
    let ctx_t = ctx.clone();
    col = col.child(
        Checkbox::new(
            "trust the proxy's client address (X-Forwarded-For): only when your own proxy sits in front of every request",
        )
        .checked(trust)
        .on_change(move |on| {
            ctx_t.send(Cmd::SetNetworkProxy {
                allowed_origins: None,
                trust_proxy: Some(on),
            })
        })
        .element(cx, t)
        .build(),
    );
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
        store.notice.set(Some(format!(
            "'{}' is already the configured mode",
            m.label
        )));
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

fn confirm_restart(cx: Scope, ctx: &Ctx) {
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        "Restart the gateway now to apply the network exposure? Running work pauses for a few seconds; \
         probe again afterwards."
            .to_string(),
        "Restart the gateway",
        "Not now",
        move || ctx2.send(Cmd::RestartNetwork),
    );
}
