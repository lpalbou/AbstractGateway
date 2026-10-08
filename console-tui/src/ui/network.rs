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

use std::cell::RefCell;
use std::rc::Rc;

use abstracttui::prelude::*;
use abstracttui::widgets::TextInput;
use serde_json::Value;

use super::util::{line, span, span_bold, wrap_text, SpanSpec};
use super::w::action::{button, button_focused, On};
use super::w::{Action, Cell, Col, ColW, DataTable, Row as WRow};
use super::widths::BLOCK_CHROME;
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
    /// The selected address row (by key).
    addr_key: Signal<Option<String>>,
    origin_draft: Signal<String>,
    origin_error: Signal<String>,
    /// A refused mode: (reason, fix).
    refused: Signal<Option<(String, String)>>,
    /// The last mode outcome line: (tone, text).
    notice: Signal<Option<(&'static str, String)>>,
    /// Which proxy field the last save was for.
    proxy_field: Signal<String>,
    warnings_open: Signal<Option<bool>>,
    scroll: Signal<i32>,
    /// Which control (by id) held the keyboard: a rebuild (fresh data)
    /// gives it back, so a save never sends the focus home.
    focus: Signal<String>,
    /// Where the focused control wants the page scrolled: re-applied when
    /// the rebuilt page's extent is known.
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
    if !d.writable {
        ctx.store.notice.set(Some(NON_ADMIN_MODE.into()));
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
        nu.origin_error.set(EMPTY_ORIGIN.into());
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

/// The web's `netOriginRemove`.
fn origin_remove(ctx: &Ctx, nu: NetUi, d: &NetworkData, gone: &str) {
    if !d.proxy.origins.iter().any(|x| x == gone) {
        return;
    }
    let cur: Vec<String> = d
        .proxy
        .origins
        .iter()
        .filter(|x| *x != gone)
        .cloned()
        .collect();
    post_proxy(
        ctx,
        nu,
        "allowed_origins",
        serde_json::json!({"allowed_origins": cur}),
    );
}

/// The sentence a non-admin reads under the mode choice (and on a press).
pub const NON_ADMIN_MODE: &str = "Only an admin can change who can reach this gateway.";
/// The proxy fields' sentence for a non-admin.
pub const NON_ADMIN_PROXY: &str = "Only an admin can change these.";
/// The web's sentence for an empty origin.
pub const EMPTY_ORIGIN: &str = "Type an origin, for example https://gateway.example.com.";
/// The Internet confirmation's heading (the web's `ui-net-confirm`).
pub const INTERNET_CONFIRM: &str = "Before you open the gateway to the internet";
/// Its action button.
pub const INTERNET_GO: &str = "I understand, use Internet mode";

/// The mode choice's segment labels: the mode's label, "Saving..." on the
/// one being saved, and the web's lock tag on a mode that needs accounts.
pub fn mode_labels(d: &NetworkData, saving: Option<&str>) -> Vec<String> {
    d.modes
        .iter()
        .map(|m| {
            let base = if saving == Some(m.id.as_str()) {
                "Saving...".to_string()
            } else if m.label.is_empty() {
                m.id.clone()
            } else {
                m.label.clone()
            };
            if m.allowed {
                base
            } else {
                format!("{base}  Needs accounts")
            }
        })
        .collect()
}

/// The Internet confirmation's sentence: the heading, the gateway's
/// reason and each of its warnings (one bullet per line).
pub fn internet_question(reason: &str, warnings: &[String]) -> String {
    let mut s = INTERNET_CONFIRM.to_string();
    if !reason.is_empty() {
        s.push('\n');
        s.push_str(reason);
    }
    for w in warnings {
        s.push_str("\n• ");
        s.push_str(w);
    }
    s
}

/// The Keep button of the Internet confirmation ("Keep <saved mode>").
pub fn keep_label(d: &NetworkData) -> String {
    format!(
        "Keep {}",
        if d.configured_label.is_empty() {
            "the current mode".to_string()
        } else {
            d.configured_label.clone()
        }
    )
}

/// An address row's stable key.
pub fn address_key(a: &crate::store::NetworkAddress) -> String {
    format!("{}|{}", a.kind, a.url)
}

/// An address row's actions (the single source for the cell, the key and
/// the tests): Copy on every address a client can use — the web shows no
/// Copy on an address this mode does not serve.
pub fn address_actions(a: &crate::store::NetworkAddress) -> Vec<Action> {
    if a.url.is_empty() || a.reachable == Some(false) {
        return Vec::new();
    }
    vec![Action::label("copy", "Copy")
        .tooltip(format!("Copy {}", a.url))
        .key('c')]
}

/// The page's own buttons, in page order (the single source for the
/// buttons, the keys, the hint bar and the tests).
pub fn page_actions(d: &NetworkData) -> Vec<Action> {
    let mut out = Vec::new();
    if d.restart_required && d.restart_available && d.restart_applies && d.writable {
        out.push(Action::label("restart", "Restart now").key('R'));
    }
    if crate::store::operator::offers_public_lookup(d) {
        out.push(Action::label("lookup", "Look up my public address").key('p'));
    }
    out.push(Action::label("check", "Check again").key('r'));
    out.push(
        Action::label("openai", "OpenAI API")
            .tooltip("The OpenAI-compatible API has its own page: OpenAI API")
            .key('o'),
    );
    if d.proxy.present && d.writable {
        out.push(Action::label("add_origin", "Add origin").key('a'));
    }
    out
}

/// One Remove button per allowed origin (admins; the web's chip ×, named
/// as its aria-label says: "Remove <origin>").
pub fn origin_actions(d: &NetworkData) -> Vec<(String, Action)> {
    if !d.proxy.present || !d.writable {
        return Vec::new();
    }
    d.proxy
        .origins
        .iter()
        .map(|o| {
            (
                o.clone(),
                Action::label("remove_origin", "Remove").tooltip(format!("Remove {o}")),
            )
        })
        .collect()
}

/// The Network page's hint pairs.
pub fn hints() -> Vec<(&'static str, &'static str)> {
    vec![
        ("Tab", "who can reach it · addresses · buttons"),
        ("Enter", "choose / press"),
        ("c", "Copy"),
        ("w", "What to know"),
        ("o", "OpenAI API"),
        ("r", "Check again"),
    ]
}

/// The Network page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    load_on_connect(cx, ctx);
    let store = ctx.store;
    let tt = *t;
    let nu = NetUi {
        addr_key: cx.signal(None),
        origin_draft: cx.signal(String::new()),
        origin_error: cx.signal(String::new()),
        refused: cx.signal(None),
        notice: cx.signal(None),
        proxy_field: cx.signal(String::new()),
        warnings_open: cx.signal(None),
        scroll: cx.signal(0),
        // The address table takes the keyboard first (its keys work at
        // once; Shift+Tab reaches the mode choice).
        // ("first": the page opens at its top — that first focus scrolls nothing.)
        focus: cx.signal("addresses-first".to_string()),
        target: cx.signal(0),
        last: cx.signal(None),
    };
    // Keep the last read: a refresh never blanks the page.
    cx.effect(move || {
        if let Loadable::Ready(d) = store.network.get() {
            nu.last.set(Some(d));
        }
    });
    // A mode write's answer: the confirmation (opened on the PAGE scope),
    // the refusal, or the saved line.
    {
        let ctx_m = ctx.clone();
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
                    store.json.set_write(MODE_KEY, None);
                    if e.status() == Some(409) && text("reason_code") == "acknowledgement_required"
                    {
                        let warnings: Vec<String> = body
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
                        let keep = nu
                            .last
                            .with_untracked(|d| d.as_ref().map(keep_label))
                            .unwrap_or_else(|| "Keep the current mode".into());
                        let c = ctx_m.clone();
                        super::w::Confirm::danger(
                            internet_question(&text("refused_reason"), &warnings),
                            INTERNET_GO,
                            &keep,
                        )
                        .open(cx, ctx_m.ui, move || post_mode(&c, nu, &mode, true));
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
    let ctx_k = ctx.clone();
    let root = Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .on(Phase::Bubble, move |ectx, ev| {
            let UiEvent::Key(k) = ev else { return };
            if k.mods.0 != 0 && !matches!(k.key, Key::Char(c) if c.is_ascii_uppercase()) {
                return;
            }
            let Some(d) = nu.last.get_untracked() else {
                return;
            };
            let handled = match k.key {
                Key::Char('c') => {
                    let a = selected_address(&d, nu);
                    copy_address(ctx_k.store, a);
                    true
                }
                Key::Char('w') if !d.warnings.is_empty() => {
                    toggle_warnings(nu, &d);
                    true
                }
                Key::Char(c) => match page_actions(&d).into_iter().find(|a| a.key == Some(c)) {
                    // `r` stays the console's refresh (the same read).
                    Some(a) if a.id != "check" => {
                        page_action(cx, &ctx_k, nu, &d, a.id);
                        true
                    }
                    _ => false,
                },
                _ => false,
            };
            if handled {
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
                        nu.warnings_open.get(),
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
                            let c = ctx_body.clone();
                            col = col.child(
                                Element::new()
                                    .style(LayoutStyle::row().h(1).shrink(0.0))
                                    .child(button_focused(
                                        gcx,
                                        &t,
                                        &Action::label("check", "Try again").key('r'),
                                        On::Page,
                                        move || {
                                            c.store.network.set(Loadable::Loading);
                                            c.send(Cmd::LoadNetwork);
                                        },
                                    ))
                                    .build(),
                            );
                            col.build()
                        }
                        (_, Some(d)) => {
                            let w = (vp.get_untracked().w - BLOCK_CHROME - 3).max(20) as usize;
                            ready_view(gcx, cx, &ctx_body, &t, d, w, nu)
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

fn toggle_warnings(nu: NetUi, d: &NetworkData) {
    let open = nu
        .warnings_open
        .get_untracked()
        .unwrap_or_else(|| default_warnings_open(d));
    nu.warnings_open.set(Some(!open));
}

fn selected_address(d: &NetworkData, nu: NetUi) -> Option<crate::store::NetworkAddress> {
    let key = nu.addr_key.get_untracked();
    d.addresses
        .iter()
        .find(|a| Some(address_key(a)) == key)
        .or_else(|| d.addresses.first())
        .cloned()
}

/// Run page action `id` (a click or its key).
fn page_action(pcx: Scope, ctx: &Ctx, nu: NetUi, d: &NetworkData, id: &str) {
    let _ = pcx;
    match id {
        "restart" => ctx.send(Cmd::Operator(
            crate::worker::operator::OpCmd::RestartNetwork,
        )),
        "lookup" => ctx.send(Cmd::Operator(crate::worker::operator::OpCmd::LookupPublic)),
        "check" => {
            nu.notice.set(None);
            nu.refused.set(None);
            ctx.store.network.set(Loadable::Loading);
            ctx.send(Cmd::LoadNetwork);
        }
        "openai" => ctx.ui.screen.set(super::SCREEN_OPENAI),
        "add_origin" if !store_pending(ctx, PROXY_KEY) => origin_add(ctx, nu, d),
        _ => {}
    }
}

fn store_pending(ctx: &Ctx, key: &str) -> bool {
    ctx.store
        .json
        .write_untracked(key)
        .is_some_and(|w| w.is_pending())
}

/// Scroll the page so row `y` is visible — never when it already is (a
/// click focuses its control: the page must not move under the pointer).
fn reveal(nu: NetUi, y: i32, visible: i32) {
    let top = nu.scroll.get_untracked();
    let want = if y < top {
        (y - 1).max(0)
    } else if y >= top + visible.max(3) - 1 {
        (y - visible.max(3) + 3).max(0)
    } else {
        top
    };
    nu.target.set(want);
    if want != top {
        nu.scroll.set(want);
    }
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
/// Controls register their focus name (their tooltip text, which the
/// focused-control line carries) with their row: focusing one scrolls the
/// page to it, and a rebuild hands the keyboard back to it.
struct Page {
    col: Element,
    y: i32,
    w: usize,
    /// Rows the page's scroll window shows.
    vis: i32,
    nu: NetUi,
    spots: Rc<RefCell<Vec<(String, String, i32)>>>,
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
    /// Register control `id` (focus name `tip`) at the current row; true
    /// when it held the keyboard before this rebuild.
    fn spot(&mut self, id: &str, tip: &str) -> bool {
        self.spots
            .borrow_mut()
            .push((tip.to_string(), id.to_string(), self.y));
        self.nu.focus.with_untracked(|f| f == id)
    }
    /// A focusable ELEMENT (the text field): FocusIn is delivered to it.
    fn tracked(&mut self, id: &'static str, el: Element) -> Element {
        let y = self.y;
        let nu = self.nu;
        let vis = self.vis;
        let el = el.on(Phase::Target, move |_, ev| {
            if matches!(ev, UiEvent::FocusIn) {
                nu.focus.set(id.to_string());
                reveal(nu, y, vis);
            }
        });
        if nu.focus.with_untracked(|f| f == id) {
            el.autofocus()
        } else {
            el
        }
    }
    /// Place a row `h` lines tall.
    fn place(&mut self, el: Element, h: i32) {
        let c = std::mem::replace(&mut self.col, Element::new());
        self.col = c.child(el.build());
        self.y += h;
    }
    fn place_view(&mut self, v: View, h: i32) {
        let c = std::mem::replace(&mut self.col, Element::new());
        self.col = c.child(v);
        self.y += h;
    }
}

/// A row of action buttons (labelled), each registered as a focus spot.
fn button_row(
    cx: Scope,
    t: &TokenSet,
    p: &mut Page,
    actions: Vec<Action>,
    lead: Option<View>,
    on: Rc<dyn Fn(&'static str)>,
) {
    let mut row = Element::new().style(LayoutStyle::row().gap(1).h(1).shrink(0.0));
    if let Some(l) = lead {
        row = row.child(l);
    }
    for a in actions {
        let id = a.id;
        let cb = on.clone();
        let refocus = p.spot(id, &a.tip_text());
        row = row.child(if refocus {
            button_focused(cx, t, &a, On::Page, move || cb(id))
        } else {
            button(cx, t, &a, On::Page, true, move || cb(id))
        });
    }
    p.place(row, 1);
}

#[allow(clippy::too_many_arguments)]
fn ready_view(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: NetworkData,
    w: usize,
    nu: NetUi,
) -> View {
    let store = ctx.store;
    let raw = d.raw.clone();
    let admin = d.writable;
    let null = Value::Null;
    let eff = raw.get("effective").unwrap_or(&null);
    let conf = raw.get("configured").unwrap_or(&null);
    let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let bl = |v: &Value, k: &str| v.get(k).and_then(Value::as_bool).unwrap_or(false);
    let spots: Rc<RefCell<Vec<(String, String, i32)>>> = Rc::new(RefCell::new(Vec::new()));
    let vis = (crate::ui::page_viewport(cx).get_untracked().h - 2).max(3);
    let mut p = Page {
        col: Element::new().style(LayoutStyle::column().gap(0).shrink(0.0).w(w as i32)),
        y: 0,
        w,
        vis,
        nu,
        spots: spots.clone(),
    };
    let on_page: Rc<dyn Fn(&'static str)> = {
        let ctx = ctx.clone();
        let d = d.clone();
        Rc::new(move |id| page_action(pcx, &ctx, nu, &d, id))
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
    let mode_busy = store
        .json
        .write_untracked(MODE_KEY)
        .is_some_and(|w| w.is_pending());
    let saving = if mode_busy {
        nu.focus
            .with_untracked(|f| f.strip_prefix("mode:").map(str::to_string))
    } else {
        None
    };
    let labels = mode_labels(&d, saving.as_deref());
    let chosen = d.modes.iter().position(|m| m.id == d.configured_mode);
    let mut seg = super::w::Segmented::new(labels.clone(), chosen);
    let mut refocus_mode = false;
    for (i, m) in d.modes.iter().enumerate() {
        let mut tip = mode_text(&m.id).to_string();
        if !m.allowed {
            if let Some(r) = &m.reason {
                tip = format!("{tip} {r}");
            }
        }
        if !admin {
            seg = seg.disable(i, NON_ADMIN_MODE);
        } else if mode_busy {
            seg = seg.disable(i, "Saving...");
        } else {
            seg = seg.tip(i, tip.clone());
        }
        let shown = if !admin {
            NON_ADMIN_MODE.to_string()
        } else {
            tip
        };
        refocus_mode |= p.spot("modes", &shown);
    }
    {
        let ctx_m = ctx.clone();
        let d_m = d.clone();
        let seg = seg
            .autofocus_chosen(refocus_mode && !mode_busy)
            .on_pick(move |i| {
                if let Some(m) = d_m.modes.get(i) {
                    nu.focus.set(format!("mode:{}", m.id));
                }
                choose(&ctx_m, nu, &d_m, i);
                nu.focus.set("modes".into());
            });
        let wseg = seg.width();
        let h = if wseg as usize > w {
            labels.len() as i32
        } else {
            1
        };
        let seg = if wseg as usize > w {
            seg.vertical(true)
        } else {
            seg
        };
        p.place_view(seg.view(cx, t), h);
    }
    // Every mode's sentence (the web shows them inside the segments).
    for m in &d.modes {
        let txt = format!("{}: {}", m.label, mode_text(&m.id));
        p.wrap(&txt, t.text_faint);
    }
    if !admin {
        p.wrap(NON_ADMIN_MODE, t.text_muted);
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
    if mode_busy {
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
        let restart: Vec<Action> = page_actions(&d)
            .into_iter()
            .filter(|a| a.id == "restart")
            .collect();
        if !restart.is_empty() {
            button_row(cx, t, &mut p, restart, None, on_page.clone());
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
    if d.addresses.is_empty() {
        p.wrap("The gateway found no address to show.", t.text_muted);
    } else {
        let rows: Vec<WRow> = d
            .addresses
            .iter()
            .map(|a| {
                let mut label = vec![super::w::Ink::new(kind_label(a), t.text)];
                if !a.url.is_empty() && a.url == primary {
                    label.push(super::w::Ink::new(" · Primary", t.info));
                }
                let pill = reach_pill(a);
                WRow::new(
                    address_key(a),
                    vec![
                        Cell::Text(label),
                        Cell::text(a.url.clone(), t.text),
                        Cell::Badge {
                            label: pill.to_string(),
                            ink: if a.reachable == Some(true) {
                                t.ok
                            } else {
                                t.text_muted
                            },
                            action: None,
                            tip: None,
                        },
                        Cell::Actions(address_actions(a)),
                    ],
                )
                .dim(a.reachable == Some(false))
                .note((!a.note.is_empty()).then(|| (a.note.clone(), t.text_muted)))
            })
            .collect();
        let cols = vec![
            Col::new("Address", ColW::Fit { min: 12, max: 26 }),
            Col::new("URL", ColW::Flex { weight: 1, min: 18 }),
            Col::new("Status", ColW::Fit { min: 8, max: 24 }),
            Col::new("Actions", ColW::Fit { min: 7, max: 8 }),
        ];
        let widths = DataTable::solve(&cols, &rows, w as i32);
        let _ = widths;
        let y = p.y;
        let refocus =
            p.nu.focus
                .with_untracked(|f| f == "addresses" || f == "addresses-first");
        let addrs = d.addresses.clone();
        let store_a = store;
        let addrs2 = d.addresses.clone();
        let store_b = store;
        let mut table = DataTable::new(cols, rows, nu.addr_key)
            .width(w as i32)
            .on_focus(move || {
                let first = nu.focus.with_untracked(|f| f == "addresses-first");
                nu.focus.set("addresses".into());
                if !first {
                    reveal(nu, y, vis);
                }
            })
            .on_action(move |key, id| {
                nu.addr_key.set(Some(key.to_string()));
                if id == "copy" {
                    let a = addrs.iter().find(|a| address_key(a) == key).cloned();
                    copy_address(store_a, a);
                }
            })
            .on_activate(move |key| {
                let a = addrs2.iter().find(|a| address_key(a) == key).cloned();
                copy_address(store_b, a);
            });
        if refocus {
            table = table.autofocus();
        }
        let view = table.view(cx, t);
        // The table's height: measured by its own layout; estimate for the
        // page's row count (headers + rule + rows + notes).
        let h = 2 + d
            .addresses
            .iter()
            .map(|a| {
                let note = if a.note.is_empty() {
                    0
                } else {
                    wrap_text(&a.note, w.saturating_sub(2).max(10)).len() as i32
                };
                1 + note
            })
            .sum::<i32>();
        p.place_view(view, h);
    }
    // Toolbar: Look up my public address · Check again · checked HH:MM:SS.
    let tools: Vec<Action> = page_actions(&d)
        .into_iter()
        .filter(|a| a.id == "lookup" || a.id == "check")
        .collect();
    {
        let mut row = Element::new().style(LayoutStyle::row().gap(1).h(1).shrink(0.0));
        for a in tools {
            let id = a.id;
            let cb = on_page.clone();
            let refocus = p.spot(id, &a.tip_text());
            row = row.child(if refocus {
                button_focused(cx, t, &a, On::Page, move || cb(id))
            } else {
                button(cx, t, &a, On::Page, true, move || cb(id))
            });
        }
        let checked = s(&raw, "checked_at");
        if checked.len() >= 19 {
            row = row.child(line(vec![span(
                format!("checked {} UTC", &checked[11..19]),
                t.text_faint,
            )]));
        }
        p.place(row, 1);
    }
    if let Some(note) = &d.public_note {
        p.wrap(&format!("public address: {note}"), t.text_faint);
    }

    // ---- What to know ----
    if !d.warnings.is_empty() {
        let open = nu
            .warnings_open
            .get_untracked()
            .unwrap_or_else(|| default_warnings_open(&d));
        let head = Action::plain(
            "warnings",
            format!(
                "{}What to know about {} ({})",
                if open { "▾ " } else { "▸ " },
                if d.configured_label.is_empty() {
                    "this mode".to_string()
                } else {
                    d.configured_label.clone()
                },
                d.warnings.len()
            ),
        )
        .key('w');
        let d_w = d.clone();
        let refocus = p.spot("warnings", &head.tip_text());
        let b = if refocus {
            button_focused(cx, t, &head, On::Page, move || toggle_warnings(nu, &d_w))
        } else {
            button(cx, t, &head, On::Page, true, move || {
                toggle_warnings(nu, &d_w)
            })
        };
        p.place(
            Element::new()
                .style(LayoutStyle::row().h(1).shrink(0.0))
                .child(b),
            1,
        );
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

    // ---- Reached through another address? (allowed origins and proxy
    // trust are shown IN this card — R15 D1, no "Advanced" disclosure) ----
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
    if d.proxy.present {
        proxy_view(cx, ctx, t, &d, &mut p, nu, on_page.clone());
    }
    // The OpenAI pointer (after the card, as on the web).
    let openai: Vec<Action> = page_actions(&d)
        .into_iter()
        .filter(|a| a.id == "openai")
        .collect();
    p.blank(t);
    button_row(
        cx,
        t,
        &mut p,
        openai,
        Some(super::util::line_styled(
            LayoutStyle::line(1).w(43).shrink(0.0),
            vec![span(
                "The OpenAI-compatible API has its own page:",
                t.text_muted,
            )],
        )),
        on_page.clone(),
    );

    // Focus follows the keyboard: the focused control's name (its tooltip,
    // carried by the focused-control line) → its row → scroll there.
    if let Some(fl) = super::w::tip::focus_line() {
        let spots = spots.clone();
        cx.effect(move || {
            let Some(text) = fl.get() else { return };
            let hit = spots
                .borrow()
                .iter()
                .find(|(tip, _, _)| *tip == text)
                .map(|(_, id, y)| (id.clone(), *y));
            if let Some((id, y)) = hit {
                if !id.starts_with("mode:") && nu.focus.get_untracked() != id {
                    nu.focus.set(id);
                }
                reveal(nu, y, vis);
            }
        });
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

/// Allowed origins + proxies on other machines (`netProxyMarkup`), inside
/// "Reached through another address?".
fn proxy_view(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: &NetworkData,
    p: &mut Page,
    nu: NetUi,
    on_page: Rc<dyn Fn(&'static str)>,
) {
    let store = ctx.store;
    let pr = d.proxy.clone();
    let admin = d.writable;
    let null = Value::Null;
    let o_raw = d
        .raw
        .get("reverse_proxy")
        .and_then(|r| r.get("allowed_origins"))
        .unwrap_or(&null);
    let pending = store
        .json
        .write_untracked(PROXY_KEY)
        .is_some_and(|w| w.is_pending());
    let field = nu.proxy_field.get_untracked();
    if pr.origins_overridden || pr.trust_overridden {
        p.text(vec![span("[Environment override]", t.warn)]);
    }

    // Allowed origins.
    p.blank(t);
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
        let removes = origin_actions(d);
        for o in &pr.origins {
            let lead = super::util::line_styled(
                LayoutStyle::line(1)
                    .w((abstracttui::text::width(o) + 2).min(p.w as i32 - 10))
                    .shrink(0.0),
                vec![span(format!("  {o}"), t.text)],
            );
            match removes.iter().find(|(x, _)| x == o) {
                Some((_, a)) => {
                    let mut a = a.clone();
                    if pending {
                        a = a.refused(Some("Saving...".into()));
                    }
                    let ctx_x = ctx.clone();
                    let d_x = d.clone();
                    let gone = o.clone();
                    let tip = a.tip_text();
                    let refocus = p.spot(&format!("remove:{o}"), &tip);
                    let b = if refocus {
                        button_focused(cx, t, &a, On::Page, move || {
                            origin_remove(&ctx_x, nu, &d_x, &gone)
                        })
                    } else {
                        button(cx, t, &a, On::Page, true, move || {
                            origin_remove(&ctx_x, nu, &d_x, &gone)
                        })
                    };
                    p.place(
                        Element::new()
                            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                            .child(lead)
                            .child(b),
                        1,
                    );
                }
                None => p.place(
                    Element::new()
                        .style(LayoutStyle::row().h(1).shrink(0.0))
                        .child(lead),
                    1,
                ),
            }
        }
    }
    if admin {
        let ctx_add = ctx.clone();
        let d_add = d.clone();
        let input = p.tracked(
            "origin-input",
            super::util::esc_releases_focus(
                TextInput::new()
                    .value(nu.origin_draft)
                    .placeholder("https://gateway.example.com")
                    .layout(LayoutStyle::default().w(40).h(1).shrink(1.0))
                    .on_submit(move |_text: &str| {
                        if !store_pending(&ctx_add, PROXY_KEY) {
                            origin_add(&ctx_add, nu, &d_add)
                        }
                    })
                    .element(cx, t),
                ctx.store.notice,
            ),
        );
        let input = super::w::caret_tracked(cx, ctx.ui.caret, input);
        let mut add: Vec<Action> = page_actions(d)
            .into_iter()
            .filter(|a| a.id == "add_origin")
            .collect();
        if pending && field == "allowed_origins" {
            for a in &mut add {
                a.label = "Saving...".into();
            }
        }
        button_row(cx, t, p, add, Some(input.build()), on_page.clone());
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
    let ctx_t = ctx.clone();
    let label = if pending && field == "trust_proxy" {
        "Saving..."
    } else {
        TRUST_LABEL
    };
    let tip = format!("{TRUST_LABEL}: {TRUST_TEXT}");
    let refocus = p.spot(
        "trust",
        &if admin {
            tip.clone()
        } else {
            NON_ADMIN_PROXY.to_string()
        },
    );
    let toggle = super::w::Toggle::new(pr.trust_proxy)
        .label(label)
        .tip(tip)
        .refused((!admin).then(|| NON_ADMIN_PROXY.to_string()))
        .busy(pending)
        .autofocus(refocus)
        .on_change(move |on| {
            post_proxy(
                &ctx_t,
                nu,
                "trust_proxy",
                serde_json::json!({"trust_proxy": on}),
            )
        });
    p.place_view(toggle.view(cx, t), 1);
    p.wrap(TRUST_TEXT, t.text_muted);
    p.wrap(TRUST_DANGER, t.warn);
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
        p.text(vec![span(NON_ADMIN_PROXY, t.text_muted)]);
    } else {
        p.text(vec![span(
            "Changes apply to the next request: no restart.",
            t.text_muted,
        )]);
    }
}

/// The trust switch's label and sentence (the web's).
pub const TRUST_LABEL: &str = "Trust proxies on other machines";
pub const TRUST_TEXT: &str = "A proxy on this computer always names the real client (X-Forwarded-For), for sign-in lockouts and the audit log. On: a proxy on another machine may too.";
pub const TRUST_DANGER: &str = "Only when your own proxy on that machine sits in front of every request: otherwise anyone can choose the address the gateway sees.";

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
