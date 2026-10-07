//! Network page (key `N`): who can reach the gateway, every address it
//! answers on, and the Advanced block (allowed origins, proxies on other
//! machines) — the web console's Network page (console_ui.py
//! `netViewMarkup`, `netAddressRow`, `netOtherAddressMarkup`,
//! `netProxyMarkup`) in the terminal, same sentences, same routes.
//! The Connection screen keeps a one-line summary ([`summary`]).
//!
//! Contract `gateway_network_v1`: `GET /network`, `POST /network {mode,
//! acknowledge_internet?}` (409 `acknowledgement_required` → the Internet
//! confirm with the gateway's warnings; other 409 → its reason + fix),
//! `POST /network {allowed_origins} | {trust_proxy}` (400 → the gateway's
//! validation sentence verbatim), `POST /network/restart`. The gateway
//! owns every verdict; this page renders them and never guesses.
//!
//! Keys: ↑/↓ + Enter choose who can reach the gateway · Tab to the
//! addresses (↑/↓, Enter shows the note, c copies a working address) ·
//! w What to know · a Advanced (origins: type + Enter adds, x removes the
//! selected one; space switches "Trust proxies on other machines") ·
//! r Check again. The page scrolls to whatever holds the keyboard.

use abstracttui::prelude::*;
use abstracttui::widgets::{List, TextInput};
use serde_json::Value;

use super::kit::{Row, WrapTable};
use super::util::{line, span, span_bold, wrap_text, SpanSpec};
use super::widths::{ColRule, BLOCK_CHROME};
use super::Ctx;
use crate::store::json::WriteState;
use crate::store::{ConnPhase, Loadable, NetworkData};
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;
use abstracttui::ui::{Phase, UiEvent};

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

/// The web's sentence under each mode (`NET_MODE_TEXT`).
pub fn mode_text(id: &str) -> &'static str {
    match id {
        "localhost" => "Only this computer can connect: apps and browsers running here. Nothing on your network sees the gateway.",
        "lan" => "Phones, tablets and other computers on the same Wi-Fi or office network can connect. Everyone signs in with their own account.",
        "internet" => "Reachable from anywhere, through a secure proxy or tunnel that you set up. The gateway itself speaks plain HTTP and never touches your router.",
        _ => "",
    }
}

/// The web's address label (`NET_KIND_LABEL`, LAN rows name the interface).
pub fn kind_label(a: &crate::store::NetworkAddress) -> String {
    let base = match a.kind.as_str() {
        "loopback" => "This computer",
        "lan" => "Local network",
        "hostname" => "Network name",
        "tailscale" => "Tailscale",
        "public" => "Public address",
        "" => "Address",
        other => other,
    };
    if a.kind == "lan" && !a.label.is_empty() {
        format!("{base} · {}", a.label)
    } else {
        base.to_string()
    }
}

/// The address's pill: "Works now" / "Not in this mode" / "Through your
/// proxy only" (public) / "Unknown".
pub fn reach_pill(a: &crate::store::NetworkAddress) -> &'static str {
    match a.reachable {
        Some(true) => "Works now",
        Some(false) => "Not in this mode",
        None if a.kind == "public" => "Through your proxy only",
        None => "Unknown",
    }
}

/// One mode row: `(•)` marks the SAVED mode only; a locked mode says
/// "Needs accounts" (the web's lock tag).
pub fn mode_row(m: &crate::store::NetworkMode) -> String {
    let mark = if m.selected { "(•)" } else { "( )" };
    let lock = if m.allowed { "" } else { "  Needs accounts" };
    format!("{mark} {}{lock}", m.label)
}

/// "Saved setting" / "Set by the environment" / "Default" (the web pills).
fn source_pill(source: &str, overridden: bool) -> &'static str {
    if overridden {
        "Set by the environment"
    } else if source == "setting" {
        "Saved setting"
    } else {
        "Default"
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

/// The result line of a reverse-proxy save (the web's `netProxySave`):
/// (tone ok|warn|err, head, text).
pub fn proxy_saved_line(field: &str, out: &WriteState) -> Option<(&'static str, String, String)> {
    let label = if field == "trust_proxy" {
        "Trust proxy"
    } else {
        "Origins"
    };
    match out {
        WriteState::Pending => None,
        WriteState::Done(v) => {
            let ch = v.get("changed").and_then(|c| c.get(field));
            Some(match ch.and_then(|c| c.get("applies")).and_then(Value::as_str) {
                None if ch.is_none() => ("ok", "Saved".into(), format!("{label}: no change.")),
                Some("overridden_by_env") => (
                    "warn",
                    "Saved · not in effect".into(),
                    "The environment this gateway was started with decides until it starts without it.".into(),
                ),
                Some("restart") => (
                    "warn",
                    "Saved · restart required".into(),
                    format!("{label} applies when the gateway restarts."),
                ),
                _ => (
                    "ok",
                    "Saved · applies now".into(),
                    format!("{label} applies to the next request."),
                ),
            })
        }
        WriteState::Failed(e) => {
            let reason = e
                .body
                .as_ref()
                .and_then(|b| b.get("refused_reason"))
                .and_then(Value::as_str)
                .map(str::to_string);
            if field == "allowed_origins" && e.status() == Some(400) && reason.is_some() {
                // Said under the input instead (the gateway's words).
                return None;
            }
            Some((
                "err",
                "Not saved".into(),
                reason.unwrap_or_else(|| e.to_string()),
            ))
        }
    }
}

/// The page-local state that survives a data refresh (the page region is
/// rebuilt when `GET /network` lands; these signals live outside it).
#[derive(Clone, Copy)]
struct NetUi {
    mode_sel: Signal<usize>,
    addr_sel: Signal<usize>,
    addr_open: Signal<Option<usize>>,
    origin_sel: Signal<usize>,
    origin_draft: Signal<String>,
    origin_error: Signal<String>,
    /// The Internet confirm: (mode, reason, warnings).
    confirm: Signal<Option<(String, String, Vec<String>)>>,
    /// A refused mode: (reason, fix).
    refused: Signal<Option<(String, String)>>,
    /// The last mode outcome line: (tone, text).
    notice: Signal<Option<(&'static str, String)>>,
    /// Which proxy field the last save was for.
    proxy_field: Signal<String>,
    warnings_open: Signal<Option<bool>>,
    advanced_open: Signal<Option<bool>>,
    scroll: Signal<i32>,
    /// Which control (in page order) held the keyboard: a rebuild (fresh
    /// data, a fold) gives it back, so a save never sends the caret home.
    focus: Signal<&'static str>,
    /// Where the focused control wants the page scrolled: re-applied when
    /// the rebuilt page's extent is known (a fresh Scroll clamps its offset
    /// to the content it has measured so far).
    target: Signal<i32>,
    /// The last Ready read (shown while a refresh is in flight).
    last: Signal<Option<NetworkData>>,
}

const MODE_KEY: &str = "network.mode";
const PROXY_KEY: &str = "network.proxy";

/// Re-read `GET /network` after a write (the serial worker runs it after
/// the write) without blanking the page: the last read stays on screen.
fn reread(ctx: &Ctx) {
    ctx.send(Cmd::LoadNetwork);
}

fn post_mode(ctx: &Ctx, nu: NetUi, mode: &str, ack: bool) {
    nu.notice.set(None);
    nu.refused.set(None);
    nu.confirm.set(None);
    let mut body = serde_json::json!({"mode": mode});
    if ack {
        body["acknowledge_internet"] = Value::Bool(true);
    }
    ctx.store
        .json
        .set_write(MODE_KEY, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: MODE_KEY.into(),
        method: "POST".into(),
        path: "/network".into(),
        body,
        slow: false,
        label: format!("Who can reach this gateway: {mode}"),
        reload: Vec::new(),
        journal: true,
    }));
    reread(ctx);
}

fn post_proxy(ctx: &Ctx, nu: NetUi, field: &str, body: Value) {
    nu.proxy_field.set(field.to_string());
    ctx.store
        .json
        .set_write(PROXY_KEY, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: PROXY_KEY.into(),
        method: "POST".into(),
        path: "/network".into(),
        body,
        slow: false,
        label: if field == "trust_proxy" {
            "Trust proxies on other machines".into()
        } else {
            "Allowed origins".into()
        },
        reload: Vec::new(),
        journal: true,
    }));
    reread(ctx);
}

/// The web's `netChoose`.
fn choose(ctx: &Ctx, nu: NetUi, d: &NetworkData, idx: usize) {
    let Some(m) = d.modes.get(idx).cloned() else {
        return;
    };
    nu.notice.set(None);
    nu.refused.set(None);
    nu.confirm.set(None);
    if !d.writable {
        ctx.store.notice.set(Some(
            "Only an admin can change who can reach this gateway.".into(),
        ));
        return;
    }
    if m.id == d.configured_mode {
        return;
    }
    if !m.allowed {
        nu.refused.set(Some((
            m.reason
                .unwrap_or_else(|| "This mode is not available right now.".into()),
            m.fix.unwrap_or_default(),
        )));
        return;
    }
    post_mode(ctx, nu, &m.id, false);
}

/// The web's `netOriginAdd`.
fn origin_add(ctx: &Ctx, nu: NetUi, d: &NetworkData) {
    let typed = nu.origin_draft.get_untracked().trim().to_string();
    if typed.is_empty() {
        nu.origin_error
            .set("Type an origin, for example https://gateway.example.com.".into());
        return;
    }
    let mut cur = d.proxy.origins.clone();
    if cur.contains(&typed) {
        nu.origin_error.set(format!("{typed} is already allowed."));
        return;
    }
    nu.origin_error.set(String::new());
    cur.push(typed);
    post_proxy(
        ctx,
        nu,
        "allowed_origins",
        serde_json::json!({"allowed_origins": cur}),
    );
}

fn origin_remove(ctx: &Ctx, nu: NetUi, d: &NetworkData, idx: usize) {
    let Some(gone) = d.proxy.origins.get(idx).cloned() else {
        return;
    };
    let cur: Vec<String> = d
        .proxy
        .origins
        .iter()
        .filter(|x| **x != gone)
        .cloned()
        .collect();
    post_proxy(
        ctx,
        nu,
        "allowed_origins",
        serde_json::json!({"allowed_origins": cur}),
    );
}

/// The Network page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    load_on_connect(cx, ctx);
    let store = ctx.store;
    let tt = *t;
    let nu = NetUi {
        mode_sel: cx.signal(usize::MAX),
        addr_sel: cx.signal(0),
        addr_open: cx.signal(None),
        origin_sel: cx.signal(0),
        origin_draft: cx.signal(String::new()),
        origin_error: cx.signal(String::new()),
        confirm: cx.signal(None),
        refused: cx.signal(None),
        notice: cx.signal(None),
        proxy_field: cx.signal(String::new()),
        warnings_open: cx.signal(None),
        advanced_open: cx.signal(None),
        scroll: cx.signal(0),
        focus: cx.signal("modes"),
        target: cx.signal(0),
        last: cx.signal(None),
    };
    // Keep the last read: a refresh never blanks the page.
    cx.effect(move || {
        if let Loadable::Ready(d) = store.network.get() {
            nu.last.set(Some(d));
        }
    });
    // A mode write's answer: the confirm, the refusal, or the saved line.
    {
        cx.effect(move || {
            let Some(w) = store.json.write(MODE_KEY) else {
                return;
            };
            match w {
                WriteState::Pending => {}
                WriteState::Done(v) => {
                    let conf = v.get("configured").cloned().unwrap_or(Value::Null);
                    let label = conf
                        .get("label")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    let restart = v
                        .get("restart_required")
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                    let eff = v.get("effective");
                    let pinned = eff
                        .map(|e| {
                            e.get("pinned_by_cli")
                                .and_then(Value::as_bool)
                                .unwrap_or(false)
                                || e.get("overridden_by_cli")
                                    .and_then(Value::as_bool)
                                    .unwrap_or(false)
                        })
                        .unwrap_or(false);
                    nu.notice.set(if restart || pinned {
                        None
                    } else {
                        Some((
                            "ok",
                            format!("Saved: {label}. The gateway already runs this way."),
                        ))
                    });
                    store.json.set_write(MODE_KEY, None);
                }
                WriteState::Failed(e) => {
                    let body = e.body.clone().unwrap_or(Value::Null);
                    let text = |k: &str| {
                        body.get(k)
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string()
                    };
                    if e.status() == Some(409) && text("reason_code") == "acknowledgement_required"
                    {
                        let warnings = body
                            .get("warnings")
                            .and_then(Value::as_array)
                            .map(|a| {
                                a.iter()
                                    .filter_map(Value::as_str)
                                    .map(str::to_string)
                                    .collect()
                            })
                            .unwrap_or_default();
                        let mode = body
                            .get("mode")
                            .and_then(Value::as_str)
                            .unwrap_or("internet")
                            .to_string();
                        nu.confirm
                            .set(Some((mode, text("refused_reason"), warnings)));
                    } else if e.status() == Some(409) {
                        let reason = if text("refused_reason").is_empty() {
                            e.to_string()
                        } else {
                            text("refused_reason")
                        };
                        nu.refused.set(Some((reason, text("fix"))));
                    } else {
                        nu.notice.set(Some((
                            "err",
                            format!("Could not change who can reach this gateway: {e}"),
                        )));
                    }
                    store.json.set_write(MODE_KEY, None);
                }
            }
        });
    }
    // An origins save refused with the gateway's sentence: under the input.
    cx.effect(move || {
        if let Some(WriteState::Failed(e)) = store.json.write(PROXY_KEY) {
            if nu.proxy_field.get_untracked() == "allowed_origins" && e.status() == Some(400) {
                if let Some(r) = e
                    .body
                    .as_ref()
                    .and_then(|b| b.get("refused_reason"))
                    .and_then(Value::as_str)
                {
                    nu.origin_error.set(r.to_string());
                }
            }
        }
        if let Some(WriteState::Done(_)) = store.json.write(PROXY_KEY) {
            if nu.proxy_field.get_untracked() == "allowed_origins" {
                nu.origin_draft.set(String::new());
                nu.origin_error.set(String::new());
            }
        }
    });

    let ctx_body = ctx.clone();
    let vp = crate::ui::page_viewport(cx);
    let ctx_w = ctx.clone();
    let ctx_a = ctx.clone();
    let root = Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .shortcut(KeyChord::plain(Key::Char('w')), move |_| {
            let open = nu.warnings_open.get_untracked().unwrap_or_else(|| {
                nu.last
                    .with_untracked(|d| d.as_ref().map(default_warnings_open).unwrap_or(false))
            });
            nu.warnings_open.set(Some(!open));
            let _ = &ctx_w;
        })
        .shortcut(KeyChord::plain(Key::Char('a')), move |_| {
            let open = nu.advanced_open.get_untracked().unwrap_or_else(|| {
                nu.last
                    .with_untracked(|d| d.as_ref().map(default_advanced_open).unwrap_or(false))
            });
            nu.advanced_open.set(Some(!open));
            let _ = &ctx_a;
        });
    // The Internet confirm answers first (y / n / Esc), like a dialog.
    let ctx_y = ctx.clone();
    let root = root.on(Phase::Capture, move |ectx, ev| {
        if let UiEvent::Key(k) = ev {
            let Some((mode, _, _)) = nu.confirm.get_untracked() else {
                return;
            };
            match k.key {
                Key::Char('y') | Key::Char('Y') => post_mode(&ctx_y, nu, &mode, true),
                Key::Char('n') | Key::Char('N') | Key::Escape => nu.confirm.set(None),
                _ => return,
            }
            ectx.stop_propagation();
        }
    });
    Block::new()
        .border(BorderKind::Rounded)
        .title("Network")
        .fill(t.surface)
        .layout(
            LayoutStyle::column()
                .gap(0)
                .grow(1.0)
                .padding(Edges::hv(1, 0))
                .clip(),
        )
        .child(
            root.child(dyn_view_scoped(
                LayoutStyle::column().gap(0).grow(1.0),
                move |gcx| {
                    let t = tt;
                    if !store.conn.get().is_connected() {
                        return line(vec![span(
                            "not connected — probe the gateway on 1 Connection first",
                            t.text_faint,
                        )]);
                    }
                    let state = store.network.get();
                    let last = nu.last.get();
                    // A rebuild on these too (the focused control comes back).
                    let _ = (
                        nu.advanced_open.get(),
                        nu.warnings_open.get(),
                        nu.confirm.get(),
                        nu.refused.get(),
                        nu.notice.get(),
                        nu.origin_error.get(),
                        store.json.writes.get(),
                    );
                    match (state, last) {
                        (Loadable::Failed(e), None) => {
                            let w = (vp.get_untracked().w - BLOCK_CHROME - 2).max(20) as usize;
                            let msg = if e.status() == Some(404) {
                                "This gateway has no network settings yet (GET /api/gateway/network answered 404): it needs the version with network exposure.".to_string()
                            } else {
                                e.to_string()
                            };
                            let mut col = Element::new().style(LayoutStyle::column().gap(0));
                            col = col.child(line(vec![span_bold(
                                "Could not read this gateway's network settings.",
                                t.error,
                            )]));
                            col = wrapped(col, &msg, w, t.warn);
                            col = col.child(line(vec![span("r Try again", t.text_faint)]));
                            Element::new()
                                .focusable()
                                .autofocus()
                                .style(LayoutStyle::column().grow(1.0))
                                .child(col.build())
                                .build()
                        }
                        (_, Some(d)) => {
                            let w = (vp.get_untracked().w - BLOCK_CHROME - 3).max(20) as usize;
                            ready_view(gcx, &ctx_body, &t, d, w, nu)
                        }
                        _ => Element::new()
                            .focusable()
                            .autofocus()
                            .style(LayoutStyle::column().grow(1.0))
                            .child(line(vec![span(
                                "Looking at this computer's network...",
                                t.info,
                            )]))
                            .build(),
                    }
                },
            ))
            .build(),
        )
        .element(t)
        .build()
}

fn default_warnings_open(d: &NetworkData) -> bool {
    d.restart_required || d.configured_mode == "internet"
}

fn default_advanced_open(d: &NetworkData) -> bool {
    let p = &d.proxy;
    d.configured_mode == "internet"
        || !p.origins.is_empty()
        || p.trust_proxy
        || p.origins_overridden
        || p.trust_overridden
}

/// Wrapped lines in one ink (the gateway's sentences are never cut).
fn wrapped(col: Element, text: &str, width: usize, ink: Rgba) -> Element {
    let mut col = col;
    for l in wrap_text(text, width) {
        col = col.child(line(vec![span(l, ink)]));
    }
    col
}

/// The page body as a column with a running row count, so a focused
/// control can scroll itself into view (the page is taller than 80×24).
struct Page {
    col: Element,
    y: i32,
    w: usize,
    scroll: Signal<i32>,
    target: Signal<i32>,
    focus: Signal<&'static str>,
}

impl Page {
    fn text(&mut self, spans: Vec<SpanSpec>) {
        let c = std::mem::replace(&mut self.col, Element::new());
        // shrink(0): a squeezed line paints over its neighbours.
        self.col = c.child(super::util::line_styled(
            LayoutStyle::line(1).shrink(0.0),
            spans,
        ));
        self.y += 1;
    }
    fn blank(&mut self, t: &TokenSet) {
        self.text(vec![span(String::new(), t.text)]);
    }
    fn wrap(&mut self, text: &str, ink: Rgba) {
        for l in wrap_text(text, self.w) {
            self.text(vec![span(l, ink)]);
        }
    }
    fn wrap_bold(&mut self, text: &str, ink: Rgba) {
        for l in wrap_text(text, self.w) {
            self.text(vec![span_bold(l, ink)]);
        }
    }
    /// A focusable control `h` rows tall: focusing it scrolls it in view.
    /// Give a FOCUSABLE element (the widget itself — FocusIn is delivered
    /// to its target only) its place in the page's focus order: focusing it
    /// scrolls the page to it, and a rebuild hands the keyboard back to it.
    fn tracked(&mut self, key: &'static str, el: Element) -> Element {
        let y = self.y;
        let scroll = self.scroll;
        let target = self.target;
        let focus = self.focus;
        let idx = key;
        let el = el.on(Phase::Target, move |_, ev| {
            if matches!(ev, UiEvent::FocusIn) {
                focus.set(idx);
                target.set((y - 2).max(0));
                scroll.set((y - 2).max(0));
            }
        });
        if focus.get_untracked() == idx {
            el.autofocus()
        } else {
            el
        }
    }
    /// Place a row `h` lines tall (its focusables already `tracked`).
    fn place(&mut self, el: Element, h: i32) {
        let c = std::mem::replace(&mut self.col, Element::new());
        self.col = c.child(el.build());
        self.y += h;
    }
    /// A single focusable widget `h` rows tall.
    fn control(&mut self, key: &'static str, el: Element, h: i32) {
        let el = self.tracked(key, el);
        self.place(el, h);
    }
}

fn ready_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: NetworkData, w: usize, nu: NetUi) -> View {
    let store = ctx.store;
    let raw = d.raw.clone();
    let admin = d.writable;
    let null = Value::Null;
    let eff = raw.get("effective").unwrap_or(&null);
    let conf = raw.get("configured").unwrap_or(&null);
    let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let bl = |v: &Value, k: &str| v.get(k).and_then(Value::as_bool).unwrap_or(false);
    let mut p = Page {
        col: Element::new().style(LayoutStyle::column().gap(0).shrink(0.0).w(w as i32)),
        y: 0,
        w,
        scroll: nu.scroll,
        target: nu.target,
        focus: nu.focus,
    };

    // ---- Who can reach this gateway ----
    let running = format!(
        "Running now: {}{}",
        if d.effective_label.is_empty() {
            "unknown".to_string()
        } else {
            d.effective_label.clone()
        },
        d.effective_port
            .map(|p| format!(" · port {p}"))
            .unwrap_or_default()
    );
    p.text(vec![
        span_bold("Who can reach this gateway", t.accent),
        span(format!("  {running}"), t.text_muted),
    ]);
    let rows: Vec<String> = d.modes.iter().map(mode_row).collect();
    if nu.mode_sel.get_untracked() >= rows.len() {
        nu.mode_sel
            .set(d.modes.iter().position(|m| m.selected).unwrap_or(0));
    }
    let ctx_m = ctx.clone();
    let d_m = d.clone();
    let busy = store
        .json
        .write_untracked(MODE_KEY)
        .is_some_and(|w| w.is_pending());
    p.control(
        "modes",
        List::new(rows.clone())
            .selection(nu.mode_sel)
            .on_activate(move |i| {
                if !busy {
                    choose(&ctx_m, nu, &d_m, i)
                }
            })
            .layout(
                LayoutStyle::default()
                    .h(rows.len().max(1) as i32)
                    .shrink(0.0),
            )
            .element(cx, t),
        rows.len().max(1) as i32,
    );
    // Every mode's sentence (the web shows them in the segmented choice).
    for m in &d.modes {
        let txt = format!("{}: {}", m.label, mode_text(&m.id));
        p.wrap(&txt, t.text_faint);
    }
    if !admin {
        p.wrap(
            "Only an admin can change who can reach this gateway.",
            t.text_muted,
        );
    }
    if (bl(eff, "overridden_by_cli") || bl(eff, "pinned_by_cli")) && !s(eff, "bind_host").is_empty()
    {
        let saved = if s(conf, "source") == "stored" {
            "Saved"
        } else {
            "Default"
        };
        let label = if s(conf, "label").is_empty() {
            s(conf, "mode")
        } else {
            s(conf, "label")
        };
        let eff_label = if s(eff, "label").is_empty() {
            s(eff, "bind_host")
        } else {
            s(eff, "label")
        };
        p.wrap_bold(
            &format!(
                "{saved}: {label}. This gateway listens on {eff_label} because it was started with --host {}.",
                s(eff, "bind_host")
            ),
            t.warn,
        );
        p.wrap(
            "It keeps that address until it is started without the flag; a restart from here keeps it too.",
            t.warn,
        );
    }
    if let Some((reason, fix)) = nu.refused.get_untracked() {
        p.wrap_bold(&reason, t.warn);
        if !fix.is_empty() {
            p.wrap(&format!("How to fix it: {fix}"), t.warn);
        }
    }
    if let Some((_, reason, warnings)) = nu.confirm.get_untracked() {
        p.text(vec![span_bold(
            "Before you open the gateway to the internet",
            t.warn,
        )]);
        if !reason.is_empty() {
            p.wrap(&reason, t.text_muted);
        }
        for wv in &warnings {
            for (i, l) in wrap_text(wv, w.saturating_sub(2).max(10))
                .into_iter()
                .enumerate()
            {
                p.text(vec![span(
                    format!("{}{l}", if i == 0 { "• " } else { "  " }),
                    t.text,
                )]);
            }
        }
        p.text(vec![
            span_bold("[y] ", t.accent),
            span_bold("I understand, use Internet mode", t.error),
            span("  ", t.text),
            span_bold("[n] ", t.accent),
            span(
                format!(
                    "Keep {}",
                    if d.configured_label.is_empty() {
                        "the current mode".to_string()
                    } else {
                        d.configured_label.clone()
                    }
                ),
                t.text,
            ),
        ]);
    }
    if busy {
        p.text(vec![span("Saving...", t.info)]);
    }
    if let Some((tone, text)) = nu.notice.get_untracked() {
        p.wrap_bold(&text, if tone == "err" { t.error } else { t.ok });
    }
    if d.restart_required {
        let can = d.restart_available && d.restart_applies;
        let label = if d.configured_label.is_empty() {
            d.configured_mode.clone()
        } else {
            d.configured_label.clone()
        };
        p.wrap_bold(
            &if can {
                format!("Restart to apply: {label} on port {}.", d.configured_port)
            } else {
                format!(
                    "Saved: {label} on port {}, not applied yet.",
                    d.configured_port
                )
            },
            if can { t.info } else { t.warn },
        );
        p.wrap(
            &format!(
                "The gateway keeps running as {}{} until it restarts.",
                if d.effective_label.is_empty() {
                    "before".to_string()
                } else {
                    d.effective_label.clone()
                },
                if d.effective_bind.is_empty() {
                    String::new()
                } else {
                    format!(
                        " ({}:{})",
                        d.effective_bind,
                        d.effective_port.map(|x| x.to_string()).unwrap_or_default()
                    )
                }
            ),
            t.text_muted,
        );
        if !can {
            let why = d.restart_reason.clone().unwrap_or_default();
            let txt = [why.as_str(), d.restart_how.as_str()]
                .iter()
                .filter(|x| !x.is_empty())
                .copied()
                .collect::<Vec<_>>()
                .join(" ");
            if !txt.is_empty() {
                p.wrap(&txt, t.text_muted);
            }
        }
        if can && admin {
            let ctx_r = ctx.clone();
            let btn = p.tracked(
                "restart",
                Button::new("Restart now")
                    .on_click(move || {
                        ctx_r.send(Cmd::Operator(
                            crate::worker::operator::OpCmd::RestartNetwork,
                        ))
                    })
                    .element(cx, t),
            );
            p.place(
                Element::new()
                    .style(LayoutStyle::row().h(1).shrink(0.0))
                    .child(btn.build()),
                1,
            );
        }
    }
    let auth = raw.get("auth").unwrap_or(&null);
    if auth.get("ok_for_mode").and_then(Value::as_bool) == Some(false)
        && nu.refused.get_untracked().is_none()
    {
        let reason = s(auth, "reason");
        p.wrap_bold(
            if reason.is_empty() {
                "This mode's sign-in requirement is not met."
            } else {
                &reason
            },
            t.warn,
        );
        if !s(auth, "fix").is_empty() {
            p.wrap(&format!("How to fix it: {}", s(auth, "fix")), t.warn);
        }
    } else if bl(auth, "will_enable_user_auth") && d.configured_mode != "localhost" {
        p.wrap(
            "Sign-in with accounts stays on for this mode: every person uses their own account (Users tab).",
            t.text_muted,
        );
    }

    // ---- Addresses ----
    p.blank(t);
    p.text(vec![
        span_bold("Addresses", t.accent),
        span("  Other devices use one of these to connect.", t.text_muted),
    ]);
    let primary = d.copy_hint.clone();
    let rows: Vec<Row> = d
        .addresses
        .iter()
        .map(|a| {
            let mut label = kind_label(a);
            if !a.url.is_empty() && a.url == primary {
                label.push_str(" · Primary");
            }
            let mut detail = Vec::new();
            if !a.note.is_empty() {
                detail.push(a.note.clone());
            }
            Row::new(vec![label, a.url.clone(), reach_pill(a).to_string()])
                .detail(detail)
                .dim(a.reachable == Some(false))
        })
        .collect();
    let rules = vec![
        ColRule::head("Address", 12),
        ColRule::tail("URL", 18),
        ColRule::head("Status", 8),
    ];
    let lines = super::kit::wrap_layout(&rules, &rows, w as i32, nu.addr_open.get_untracked());
    let table_h = (lines.len() as i32).clamp(2, 14);
    let addrs = d.addresses.clone();
    let addr_sel = nu.addr_sel;
    if rows.is_empty() {
        p.wrap("The gateway found no address to show.", t.text_muted);
    } else {
        p.control(
            "addresses",
            WrapTable::new(rules, rows, nu.addr_sel)
                .expanded(nu.addr_open)
                .layout(LayoutStyle::default().w(w as i32).h(table_h).shrink(0.0))
                .element(cx, t)
                .shortcut(KeyChord::plain(Key::Char('c')), move |_| {
                    let a = addrs.get(addr_sel.get_untracked()).cloned();
                    copy_address(store, a);
                }),
            table_h,
        );
        p.text(vec![span(
            "Enter shows the note · c copies an address that works now",
            t.text_faint,
        )]);
    }
    // Toolbar: Look up my public address · Check again · checked HH:MM:SS.
    let mut bar = Element::new().style(LayoutStyle::row().gap(2).h(1).shrink(0.0));
    if crate::store::operator::offers_public_lookup(&d) {
        let ctx_l = ctx.clone();
        let b = p.tracked(
            "lookup",
            Button::new("Look up my public address")
                .on_click(move || {
                    ctx_l.send(Cmd::Operator(crate::worker::operator::OpCmd::LookupPublic))
                })
                .element(cx, t),
        );
        bar = bar.child(b.build());
    }
    let ctx_c = ctx.clone();
    let b = p.tracked(
        "check",
        Button::new("Check again")
            .on_click(move || {
                ctx_c.store.network.set(Loadable::Loading);
                ctx_c.send(Cmd::LoadNetwork);
            })
            .element(cx, t),
    );
    bar = bar.child(b.build());
    let checked = s(&raw, "checked_at");
    if checked.len() >= 19 {
        bar = bar.child(line(vec![span(
            format!("checked {} UTC", &checked[11..19]),
            t.text_faint,
        )]));
    }
    p.place(bar, 1);
    if let Some(note) = &d.public_note {
        p.wrap(&format!("public address: {note}"), t.text_faint);
    }

    // ---- What to know ----
    if !d.warnings.is_empty() {
        let open = nu
            .warnings_open
            .get_untracked()
            .unwrap_or_else(|| default_warnings_open(&d));
        p.text(vec![
            span(if open { "▾ " } else { "▸ " }, t.accent),
            span_bold(
                format!(
                    "What to know about {} ({})",
                    if d.configured_label.is_empty() {
                        "this mode".to_string()
                    } else {
                        d.configured_label.clone()
                    },
                    d.warnings.len()
                ),
                t.text,
            ),
            span("  w", t.text_faint),
        ]);
        if open {
            for wv in &d.warnings {
                for (i, l) in wrap_text(wv, w.saturating_sub(2).max(10))
                    .into_iter()
                    .enumerate()
                {
                    p.text(vec![span(
                        format!("{}{l}", if i == 0 { "• " } else { "  " }),
                        t.text_muted,
                    )]);
                }
            }
        }
    }

    // ---- Reached through another address? ----
    p.blank(t);
    let ts_name = raw
        .get("tailscale")
        .and_then(|x| x.get("dns_name"))
        .and_then(Value::as_str)
        .map(str::to_string);
    p.text(vec![
        span_bold("Reached through another address?", t.accent),
        span("  Tailscale, a reverse proxy", t.text_muted),
    ]);
    p.wrap(
        &format!(
            "Nothing to set up: the gateway accepts every address above{}, and a proxy on this computer passes the real client address.",
            if ts_name.is_some() {
                ", including its Tailscale name"
            } else {
                ""
            }
        ),
        t.text_muted,
    );
    if let Some(name) = &ts_name {
        let port = d
            .effective_port
            .or(Some(d.configured_port).filter(|p| *p > 0))
            .unwrap_or(8080);
        p.wrap(
            &format!(
                "Voice and camera need https: on this computer run tailscale serve --bg http://127.0.0.1:{port}, then open https://{name}. tailscale serve reset undoes it."
            ),
            t.text_muted,
        );
    }
    let ui = ctx.ui;
    let b = p.tracked(
        "openai",
        Button::new("OpenAI API")
            .on_click(move || ui.screen.set(super::SCREEN_OPENAI))
            .element(cx, t),
    );
    p.place(
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(super::util::line_styled(
                LayoutStyle::line(1).w(44).shrink(0.0),
                vec![span(
                    "The OpenAI-compatible API has its own page:",
                    t.text_muted,
                )],
            ))
            .child(b.build()),
        1,
    );

    // ---- Advanced ----
    if d.proxy.present {
        proxy_view(cx, ctx, t, &d, &mut p, nu);
    }
    let scroll = nu.scroll;
    let target = nu.target;
    let extent = cx.signal((0, 0));
    cx.effect(move || {
        let (_, h) = extent.get();
        if h > 0 {
            let want = target.get_untracked();
            if scroll.get_untracked() != want {
                scroll.set(want);
            }
        }
    });
    let content = p.col.build();
    Scroll::new(content)
        .axes(false, true)
        .offset_y(scroll)
        .extent_signal(extent)
        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
        .scrollbar_auto_hide(true)
        .view(cx)
}

/// Advanced: allowed origins + proxies on other machines (`netProxyMarkup`).
fn proxy_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: &NetworkData, p: &mut Page, nu: NetUi) {
    let store = ctx.store;
    let pr = d.proxy.clone();
    let admin = d.writable;
    let null = Value::Null;
    let o_raw = d
        .raw
        .get("reverse_proxy")
        .and_then(|r| r.get("allowed_origins"))
        .unwrap_or(&null);
    let open = nu
        .advanced_open
        .get_untracked()
        .unwrap_or_else(|| default_advanced_open(d));
    let sum = format!(
        "{} · {}",
        if pr.origins.is_empty() {
            "no manual origin".to_string()
        } else {
            format!(
                "{} origin{}",
                pr.origins.len(),
                if pr.origins.len() == 1 { "" } else { "s" }
            )
        },
        if pr.trust_effective {
            "proxies elsewhere trusted"
        } else {
            "local proxy only"
        }
    );
    p.blank(t);
    let mut head = vec![
        span(if open { "▾ " } else { "▸ " }, t.accent),
        span_bold("Advanced", t.accent),
        span(format!("  {sum}"), t.text_muted),
    ];
    if pr.origins_overridden || pr.trust_overridden {
        head.push(span("  [Environment override]", t.warn));
    }
    head.push(span("  a", t.text_faint));
    p.text(head);
    if !open {
        return;
    }
    let pending = store
        .json
        .write_untracked(PROXY_KEY)
        .is_some_and(|w| w.is_pending());
    let field = nu.proxy_field.get_untracked();

    // Allowed origins.
    p.text(vec![
        span_bold("Allowed origins", t.text),
        span(
            format!(
                "  [{}]",
                source_pill(&pr.origins_source, pr.origins_overridden)
            ),
            t.text_muted,
        ),
    ]);
    p.wrap(
        "Other web addresses whose pages may use this gateway: a proxy or tunnel with its own name, for example https://gateway.example.com. The addresses above need no entry.",
        t.text_muted,
    );
    if pr.origins_overridden {
        p.wrap_bold(
            &format!(
                "This gateway was started with {} in its environment, so that list decides:",
                if pr.origins_env_name.is_empty() {
                    "an origins list".to_string()
                } else {
                    pr.origins_env_name.clone()
                }
            ),
            t.warn,
        );
        p.wrap(
            &format!(
                "{}. The origins below are saved and apply once the gateway is started without it.",
                if pr.origins_env_value.is_empty() {
                    "none".to_string()
                } else {
                    pr.origins_env_value.join(", ")
                }
            ),
            t.warn,
        );
    }
    if pr.origins.is_empty() {
        p.text(vec![span(
            if pr.origins_overridden {
                "None saved"
            } else {
                "None yet: only this computer's own pages"
            },
            t.text_faint,
        )]);
    } else {
        let rows: Vec<String> = pr.origins.iter().map(|x| format!("{x}  ×")).collect();
        let h = rows.len() as i32;
        let ctx_x = ctx.clone();
        let d_x = d.clone();
        let sel = nu.origin_sel;
        let mut list = List::new(rows)
            .selection(nu.origin_sel)
            .layout(LayoutStyle::default().h(h).shrink(0.0))
            .element(cx, t);
        if admin {
            list = list.shortcut(KeyChord::plain(Key::Char('x')), move |_| {
                if !pending {
                    origin_remove(&ctx_x, nu, &d_x, sel.get_untracked())
                }
            });
        }
        p.control("origins", list, h);
        if admin {
            p.text(vec![span("x removes the selected origin", t.text_faint)]);
        }
    }
    if admin {
        let ctx_add = ctx.clone();
        let d_add = d.clone();
        let ctx_btn = ctx.clone();
        let d_btn = d.clone();
        let input = p.tracked(
            "origin-input",
            super::util::esc_releases_focus(
                TextInput::new()
                    .value(nu.origin_draft)
                    .placeholder("https://gateway.example.com")
                    .layout(LayoutStyle::default().w(40).h(1).shrink(1.0))
                    .on_submit(move |_text: &str| {
                        if !pending {
                            origin_add(&ctx_add, nu, &d_add)
                        }
                    })
                    .element(cx, t),
                ctx.store.notice,
            ),
        );
        let input = super::w::caret_tracked(cx, ctx.ui.caret, input);
        let label = if pending && field == "allowed_origins" {
            "Saving..."
        } else {
            "Add origin"
        };
        let btn = p.tracked(
            "origin-add",
            Button::new(label)
                .on_click(move || {
                    if !pending {
                        origin_add(&ctx_btn, nu, &d_btn)
                    }
                })
                .element(cx, t),
        );
        p.place(
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(input.build())
                .child(btn.build()),
            1,
        );
    }
    let err = nu.origin_error.get_untracked();
    if !err.is_empty() {
        p.wrap(&err, t.error);
    }
    for wv in &pr.origins_warnings {
        p.wrap(wv, t.warn);
    }
    let strs = |k: &str| -> Vec<String> {
        o_raw
            .get(k)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    let mut always = strs("builtin");
    always.extend(strs("self_origins"));
    if !always.is_empty() && !pr.origins_overridden {
        p.wrap(
            &format!("Always allowed: {}", always.join(" ")),
            t.text_faint,
        );
    }

    // Client address.
    p.blank(t);
    p.text(vec![
        span_bold("Client address", t.text),
        span(
            format!("  [{}]", source_pill(&pr.trust_source, pr.trust_overridden)),
            t.text_muted,
        ),
    ]);
    let trust = cx.signal(pr.trust_proxy);
    let ctx_t = ctx.clone();
    let label = if pending && field == "trust_proxy" {
        "Saving..."
    } else {
        "Trust proxies on other machines"
    };
    p.control(
        "trust",
        super::switch::Switch::new(label, trust)
            .unavailable((!admin).then(|| "Only an admin can change these.".to_string()))
            .busy(pending)
            .notice(ctx.store.notice)
            .on_request(move |on| {
                post_proxy(
                    &ctx_t,
                    nu,
                    "trust_proxy",
                    serde_json::json!({"trust_proxy": on}),
                )
            })
            .element(cx, t),
        1,
    );
    p.wrap(
        "A proxy on this computer always names the real client (X-Forwarded-For), for sign-in lockouts and the audit log. On: a proxy on another machine may too.",
        t.text_muted,
    );
    p.wrap(
        "Only when your own proxy on that machine sits in front of every request: otherwise anyone can choose the address the gateway sees.",
        t.warn,
    );
    if pr.trust_overridden {
        p.wrap_bold(
            &format!(
                "This gateway was started with {} in its environment: trust is {}.",
                if pr.trust_env_name.is_empty() {
                    "a proxy setting".to_string()
                } else {
                    pr.trust_env_name.clone()
                },
                if pr.trust_effective { "on" } else { "off" }
            ),
            t.warn,
        );
        p.wrap(
            "The switch is saved and applies once the gateway is started without it.",
            t.warn,
        );
    }
    // The saved line.
    let saved = store
        .json
        .write_untracked(PROXY_KEY)
        .and_then(|wr| proxy_saved_line(&field, &wr));
    if let Some((tone, head, text)) = saved {
        let ink = match tone {
            "err" => t.error,
            "warn" => t.warn,
            _ => t.ok,
        };
        p.text(vec![
            span_bold(head, ink),
            span(format!("  {text}"), t.text_muted),
        ]);
    } else if !admin {
        p.text(vec![span("Only an admin can change these.", t.text_muted)]);
    } else {
        p.text(vec![span(
            "Changes apply to the next request: no restart.",
            t.text_muted,
        )]);
    }
}

fn copy_address(store: crate::store::Store, a: Option<crate::store::NetworkAddress>) {
    match a {
        Some(a) if !a.url.is_empty() && a.reachable != Some(false) => {
            copy_to_clipboard(a.url.clone());
            store.notice.set(Some(format!("copied {}", a.url)));
        }
        Some(a) if !a.url.is_empty() => store.notice.set(Some(format!(
            "{} is not in this mode: nothing copied",
            a.url
        ))),
        _ => store
            .notice
            .set(Some("no address selected — nothing to copy".into())),
    }
}
