//! "My email" — the caller's own account page for email (DESIGN §6; web:
//! the gateway console's "My account"). Opened with `@` on the Users
//! screen, open to every signed-in human. Top to bottom, the words of the
//! web console:
//!
//! 1. **Email address** — one field with its own inline Save (the only
//!    Save on the page): where sign-in codes and notifications go.
//! 2. **Mailbox** — not connected: tabs IMAP (first, default) | Google |
//!    Microsoft (DESIGN-v2 §3). IMAP shows every field: the mailbox
//!    address, the password and both servers, pre-filled with the standard
//!    values the moment the address has a domain, then with the gateway's
//!    discovery `defaults` — never over a field the person edited. No user
//!    name or display name; Ctrl+O reveals one Login field for the few
//!    providers that sign in with another name. ONE Connect (save + test).
//!    Connected: one status line, the **Active** switch, Test and Disconnect
//!    (inline confirmation).
//! 3. **Notifications** — two switches, "Job failed" and "Approval needed",
//!    and "Send a test", whose result is the gateway's sentence.
//! 4. **Agent email tools** — one switch, with the reason when unavailable.
//! 5. **Advanced** (folded) — compact sentences: who your agents may send
//!    to, "At most N per hour and M per day.", the watched folder.
//!
//! Switches apply at once (worker write law: write → verify by GET →
//! journal); the status line under the page names the new state. The
//! administrator's gateway-wide email switches live on the Users screen.

use abstracttui::prelude::*;
use abstracttui::widgets::{Scroll, Tabs};
use serde_json::json;

use super::switch::Switch;
use super::util::{ellipsize, field, line, span, span_bold, wrap_text};
use super::{open_form, Ctx};
use crate::store::email::{
    address_domain, imap_connect_body, limits_body, policy_body, MyEmail, ServerDefaults,
    ServerSettings, DISCOVERY_NO_DEFAULTS,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::{EmailAction, OpCmd};
use crate::worker::Cmd;

/// The Mailbox card's tabs (DESIGN-v2 §3): IMAP first and the default.
pub const TABS: [&str; 3] = ["IMAP", "Google", "Microsoft"];
const TAB_IMAP: usize = 0;

pub const ADDRESS_HELP: &str = "Where sign-in codes and notifications go.";
pub const MAILBOX_ADDRESS_HELP: &str =
    "The account your agents read and send from \u{2014} usually your own address.";
/// The admin's own page opens with this sentence (DESIGN-v2 §2.3).
pub const ADMIN_SENTENCE: &str = "You are also a user of this gateway: this address receives your sign-in codes and notifications, and your mailbox serves your own agents.";
pub const DIFFERENT_LOGIN: &str = "My provider uses a different login name";
pub const LOGIN_HELP: &str =
    "The name your provider signs you in with, when it is not your address.";
/// The key that reveals / hides the Login field (the only way: no button).
pub const LOGIN_KEY_LABEL: &str = "Ctrl+O";
pub const ACTIVE_HELP: &str =
    "Off pauses watching, sending and notifications; your settings are kept.";
pub const PASSWORD_HELP: &str = "Use an app password if your provider needs one.";
pub const JOB_FAILED_HELP: &str =
    "An automation of yours, or a run you asked to be emailed about, failed after its retries.";
pub const APPROVAL_HELP: &str = "A run is waiting for your answer.";
pub const AGENT_TOOLS_HELP: &str = "Your agents and workflows may list, search, read, send and reply to your mail. Every send still follows your recipient rules, your limits and the approval gate.";
pub const DISCONNECT_CONFIRM: &str = "Disconnect this mailbox? Your agents lose email until you connect again. Policy and limits are kept.";

/// The page's shared state (modal scope: survives every re-render).
#[derive(Clone, Copy)]
struct Page {
    form_id: u64,
    /// The key of the control whose write is in flight (busy marker).
    busy: Signal<Option<&'static str>>,
    /// The one status line: `Ok(new state)` / `Err(what failed)`.
    status: Signal<Option<Result<String, String>>>,
    /// The text a landed write says (set when the write is sent).
    pending_ok: Signal<Option<String>>,
    address: Signal<String>,
    address_saved: Signal<bool>,
    tab: Signal<usize>,
    other_address: Signal<String>,
    password: Signal<String>,
    servers: ServerSignals,
    /// The Login field is shown (Ctrl+O).
    login_shown: Signal<bool>,
    /// Bumped per address edit: only the newest arms the discovery call.
    addr_gen: Signal<u64>,
    /// The last "Send a test" sentence, shown under the button.
    test_result: Signal<Option<Result<String, String>>>,
    confirm_disconnect: Signal<bool>,
    advanced: Signal<bool>,
    new_entry: Signal<String>,
    per_hour: Signal<String>,
    per_day: Signal<String>,
    folder: Signal<String>,
    oauth_advanced: Signal<bool>,
    client_id: Signal<String>,
    client_secret: Signal<String>,
    tenant: Signal<String>,
    flow: Signal<String>,
    seeded: Signal<bool>,
    /// The four switches' shown states, synced from the gateway's answer
    /// (a switch press never rebuilds the page: the focus stays put).
    sw_job: Signal<bool>,
    sw_approval: Signal<bool>,
    sw_tools: Signal<bool>,
    sw_use: Signal<bool>,
    /// Width available for wrapped helper text.
    wrap_w: usize,
}

// Which server fields the person edited (never overwritten by a fill).
const E_IMAP_HOST: u8 = 1;
const E_IMAP_PORT: u8 = 2;
const E_IMAP_SEC: u8 = 4;
const E_SMTP_HOST: u8 = 8;
const E_SMTP_PORT: u8 = 16;
const E_SMTP_SEC: u8 = 32;
const E_LOGIN: u8 = 64;

#[derive(Clone, Copy)]
struct ServerSignals {
    imap_host: Signal<String>,
    imap_port: Signal<String>,
    imap_security: Signal<String>,
    smtp_host: Signal<String>,
    smtp_port: Signal<String>,
    smtp_security: Signal<String>,
    login: Signal<String>,
    edited: Signal<u8>,
}

impl ServerSignals {
    fn new(cx: Scope) -> ServerSignals {
        ServerSignals {
            imap_host: cx.signal(String::new()),
            imap_port: cx.signal(String::new()),
            imap_security: cx.signal("ssl".to_string()),
            smtp_host: cx.signal(String::new()),
            smtp_port: cx.signal(String::new()),
            smtp_security: cx.signal("ssl".to_string()),
            login: cx.signal(String::new()),
            edited: cx.signal(0),
        }
    }

    fn get(&self) -> ServerSettings {
        ServerSettings {
            imap_host: self.imap_host.get_untracked(),
            imap_port: self.imap_port.get_untracked(),
            imap_security: self.imap_security.get_untracked(),
            smtp_host: self.smtp_host.get_untracked(),
            smtp_port: self.smtp_port.get_untracked(),
            smtp_security: self.smtp_security.get_untracked(),
        }
    }

    fn mark(&self, bit: u8) {
        self.edited.update(|e| *e |= bit);
    }

    /// Fill every field the person has not edited from `d` (the standard
    /// values first, then the gateway's discovery `defaults`).
    fn fill(&self, d: &ServerDefaults) {
        let edited = self.edited.get_untracked();
        let put = |bit: u8, sig: Signal<String>, v: String| {
            if edited & bit == 0 && sig.with_untracked(|c| *c != v) {
                sig.set(v);
            }
        };
        let port = |p: Option<i64>| p.map(|p| p.to_string()).unwrap_or_default();
        put(E_IMAP_HOST, self.imap_host, d.imap.host.clone());
        put(E_IMAP_PORT, self.imap_port, port(d.imap.port));
        if !d.imap.security.is_empty() {
            put(E_IMAP_SEC, self.imap_security, d.imap.security.clone());
        }
        put(E_SMTP_HOST, self.smtp_host, d.smtp.host.clone());
        put(E_SMTP_PORT, self.smtp_port, port(d.smtp.port));
        if !d.smtp.security.is_empty() {
            put(E_SMTP_SEC, self.smtp_security, d.smtp.security.clone());
        }
        if !d.login.is_empty() {
            put(E_LOGIN, self.login, d.login.clone());
        }
    }
}

/// The mailbox address changed: the standard values at once (for the
/// fields nobody edited), the discovery call ~400 ms after the last edit.
fn address_changed(ctx: &Ctx, p: Page, address: &str) {
    if let Some(st) = ServerDefaults::standard(address) {
        p.servers.fill(&st);
    }
    let gen = p.addr_gen.get_untracked() + 1;
    p.addr_gen.set(gen);
    let ctx = ctx.clone();
    abstracttui::reactive::after(std::time::Duration::from_millis(400), move || {
        if p.addr_gen.get_untracked() == gen {
            discover(&ctx, &p.other_address.get_untracked());
        }
    });
}

/// Route this form's write outcomes into the one status line.
fn install_done(mcx: Scope, ctx: &Ctx, p: Page) {
    let ui = ctx.ui;
    mcx.effect(move || {
        if let Some((fid, outcome)) = ui.write_done.get() {
            if fid == p.form_id {
                ui.write_done.set(None);
                let landed = p.busy.get_untracked();
                p.busy.set(None);
                match outcome.clone() {
                    Ok(_) => {
                        let text = p
                            .pending_ok
                            .get_untracked()
                            .unwrap_or_else(|| "Saved.".into());
                        if landed == Some("address") {
                            p.address_saved.set(true);
                        }
                        if landed == Some("connect") || landed == Some("disconnect") {
                            p.password.set(String::new());
                            p.confirm_disconnect.set(false);
                        }
                        if landed == Some("test_notification") {
                            // The gateway's sentence ("Sent to x@y."), not ours.
                            p.test_result.set(Some(Ok(outcome.clone().unwrap_or(text))));
                            p.status.set(None);
                            return;
                        }
                        p.status.set(Some(Ok(text)));
                    }
                    Err(e) => {
                        if landed == Some("test_notification") {
                            p.test_result.set(Some(Err(e)));
                            p.status.set(None);
                            return;
                        }
                        p.status.set(Some(Err(e)));
                    }
                }
            }
        }
    });
}

/// Send one write: busy on `key`, the status line cleared, `ok` said when
/// the verified write lands. A second write waits for the first.
fn send(ctx: &Ctx, p: Page, key: &'static str, action: EmailAction, ok: String) {
    if p.busy.get_untracked().is_some() {
        p.status.set(Some(Err(
            "Still saving the last change — try again in a moment.".into(),
        )));
        return;
    }
    p.status.set(None);
    p.pending_ok.set(Some(ok));
    p.busy.set(Some(key));
    ctx.send(Cmd::Operator(OpCmd::Email {
        action,
        form_id: Some(p.form_id),
    }));
}

/// Ask discovery about `address` (the Other tab), once per address.
fn discover(ctx: &Ctx, address: &str) {
    let a = address.trim().to_string();
    if address_domain(&a).is_none() {
        return;
    }
    let asked = ctx
        .store
        .op
        .email_discovery
        .with_untracked(|d| d.as_ref().map(|(x, _)| x.clone()));
    if asked.as_deref() == Some(a.as_str()) {
        return;
    }
    ctx.send(Cmd::Operator(OpCmd::Email {
        action: EmailAction::Discover(a),
        form_id: None,
    }));
}

/// Open the page; it reads the settings first and fills in when they land.
pub fn open(cx: Scope, ctx: &Ctx) {
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        ctx.store.notice.set(Some(
            "not connected — probe on the Connection screen first".into(),
        ));
        return;
    }
    ctx.send(Cmd::Operator(OpCmd::LoadMyEmail));
    let vw = abstracttui::app::use_viewport(cx).get_untracked().w;
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(100, 44), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let store = ctx2.store;
        let p = Page {
            form_id: crate::worker::next_form_id(),
            busy: mcx.signal(None),
            status: mcx.signal(None),
            pending_ok: mcx.signal(None),
            address: mcx.signal(String::new()),
            address_saved: mcx.signal(false),
            tab: mcx.signal(0usize),
            other_address: mcx.signal(String::new()),
            password: mcx.signal(String::new()),
            servers: ServerSignals::new(mcx),
            login_shown: mcx.signal(false),
            addr_gen: mcx.signal(0),
            test_result: mcx.signal(None),
            confirm_disconnect: mcx.signal(false),
            advanced: mcx.signal(false),
            new_entry: mcx.signal(String::new()),
            per_hour: mcx.signal(String::new()),
            per_day: mcx.signal(String::new()),
            folder: mcx.signal(String::new()),
            oauth_advanced: mcx.signal(false),
            client_id: mcx.signal(String::new()),
            client_secret: mcx.signal(String::new()),
            tenant: mcx.signal(String::new()),
            flow: mcx.signal(String::new()),
            seeded: mcx.signal(false),
            sw_job: mcx.signal(true),
            sw_approval: mcx.signal(true),
            sw_tools: mcx.signal(false),
            sw_use: mcx.signal(true),
            // Modal inner width: the modal (≤ 100) minus its margin,
            // border and padding, minus the scrollbar.
            wrap_w: (vw.min(100) - 8).max(20) as usize,
        };
        install_done(mcx, &ctx2, p);
        // Seed the typed fields ONCE from the first answer (a republish
        // after a write never overwrites what the person is typing).
        let ctx_seed = ctx2.clone();
        mcx.effect(move || {
            if p.seeded.get_untracked() {
                return;
            }
            if let Loadable::Ready(e) = store.op.my_email.get() {
                p.seeded.set(true);
                p.address.set(e.email_address());
                let other = if e.address.is_empty() {
                    e.email_address()
                } else {
                    e.address.clone()
                };
                p.other_address.set(other);
                p.per_hour
                    .set(e.per_hour.map(|v| v.to_string()).unwrap_or_default());
                p.per_day
                    .set(e.per_day.map(|v| v.to_string()).unwrap_or_default());
                if let Some(i) = &e.imap {
                    p.folder.set(if i.folder.is_empty() {
                        "INBOX".to_string()
                    } else {
                        i.folder.clone()
                    });
                }
                // The IMAP pane (the default tab) shows its servers filled
                // at once for the address it starts with.
                if !e.configured {
                    let a = p.other_address.get_untracked();
                    if let Some(st) = ServerDefaults::standard(&a) {
                        p.servers.fill(&st);
                        discover(&ctx_seed, &a);
                    }
                }
            }
        });
        // The switches follow the gateway's verified answer.
        mcx.effect(move || {
            if let Loadable::Ready(e) = store.op.my_email.get() {
                let legacy = match store.op.my_notifications.get() {
                    Loadable::Ready(n) => n.two_switches(),
                    _ => (true, true),
                };
                p.sw_job.set(e.notify_job_failed.unwrap_or(legacy.0));
                p.sw_approval
                    .set(e.notify_approval_needed.unwrap_or(legacy.1));
                p.sw_tools.set(e.agent_tools_enabled);
                p.sw_use.set(e.enabled);
            }
        });
        // The page is rebuilt only when its SHAPE changes (connected or
        // not, a reason appearing, the rules list…), never for a switch.
        let shape = mcx.memo(move || store.op.my_email.with(shape_key));
        // Discovery's `defaults` for the address still typed replace the
        // standard values — field by field, never over an edited one.
        mcx.effect(move || {
            if let Some((addr, Loadable::Ready(d))) = store.op.email_discovery.get() {
                let typed = p.other_address.get_untracked();
                if addr.trim().eq_ignore_ascii_case(typed.trim()) {
                    if let Some(def) = &d.defaults {
                        p.servers.fill(def);
                    }
                }
            }
        });
        let ctx_body = ctx2.clone();
        let close_cancel = close.clone();
        let page_id: std::rc::Rc<std::cell::Cell<Option<abstracttui::ui::ViewId>>> =
            std::rc::Rc::new(std::cell::Cell::new(None));
        let page_id_c = page_id.clone();
        let body = dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |bcx| {
            let t = theme.get().tokens;
            let _ = shape.get();
            match store.op.my_email.get_untracked() {
                Loadable::Ready(e) => page_body(bcx, &ctx_body, &t, &e, p),
                Loadable::Failed(err) => line(vec![span(
                    format!("✗ {}", crate::store::email::email_error_text(&err)),
                    t.error,
                )]),
                _ => line(vec![span("◌ reading your email settings…", t.info)]),
            }
        });
        Element::new()
            .style(LayoutStyle::column().gap(0).grow(1.0))
            // Focusable: Ctrl+O below anchors the focus here when it hides
            // the focused Login field (the page's own id, recorded as keys
            // pass through it on the way down).
            .focusable()
            .on(abstracttui::ui::Phase::Capture, move |ectx, _| {
                if page_id_c.get().is_none() {
                    page_id_c.set(ectx.current());
                }
            })
            // "My provider uses a different login name": a key, never a
            // button (DESIGN-v2 §3) — only on the IMAP pane of a mailbox
            // not connected yet.
            .shortcut(KeyChord::new(Mods::CTRL, Key::Char('o')), move |ectx| {
                let connectable = store
                    .op
                    .my_email
                    .with_untracked(|e| e.ready().map(|e| !e.configured).unwrap_or(false));
                if connectable && p.tab.get_untracked() == TAB_IMAP {
                    let hide = p.login_shown.get_untracked();
                    p.login_shown.set(!hide);
                    // Hiding disposes the focused Login field: anchor the
                    // focus on the page so the keys (Ctrl+O again) stay live.
                    if hide {
                        if let Some(id) = page_id.get() {
                            ectx.request_focus(id);
                        }
                    }
                }
            })
            .child(line(vec![span_bold("My account — email", t0.accent)]))
            .child(
                Scroll::new(body)
                    .layout(LayoutStyle::default().grow(1.0).min_h(3))
                    .element(mcx, &t0)
                    .build(),
            )
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                let w = p.wrap_w + 2;
                match (p.busy.get(), p.status.get()) {
                    (Some(_), _) => line(vec![span("◌ saving…", t.info)]),
                    (None, Some(Ok(v))) => line(vec![span(ellipsize(&format!("✓ {v}"), w), t.ok)]),
                    (None, Some(Err(e))) => {
                        line(vec![span(ellipsize(&format!("✗ {e}"), w), t.error)])
                    }
                    (None, None) => line(vec![span(
                        ellipsize(
                            "space switch · Tab next · Enter on a field saves it · Esc close",
                            w,
                        ),
                        t.text_faint,
                    )]),
                }
            }))
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_cancel())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

/// What the page's layout depends on (see `shape` in `open`).
fn shape_key(l: &Loadable<MyEmail>) -> String {
    match l {
        Loadable::Ready(e) => format!(
            "ready|{}|{}|{}|{:?}|{:?}|{:?}|{}|{}|{}|{:?}|{:?}|{:?}|{:?}",
            e.configured,
            e.mailboxes_off(),
            e.enabled,
            e.agent_tools_unavailable(),
            e.notifications_unavailable(),
            e.policy_entries,
            e.policy_mode,
            e.connected_text(),
            e.email_address(),
            e.oauth_providers,
            e.usage_text(),
            e.imap.as_ref().map(|i| i.folder.clone()),
            e.per_hour
        ),
        Loadable::Failed(err) => format!("failed|{err}"),
        _ => "loading".into(),
    }
}

/// Helper text, wrapped to the page width (never cut). A leading run of
/// spaces indents every wrapped line (a switch's description sits under
/// its label).
fn helper(t: &TokenSet, text: &str, w: usize) -> View {
    let body = text.trim_start_matches(' ');
    let indent = text.len() - body.len();
    let pad = " ".repeat(indent);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for l in wrap_text(body, w.saturating_sub(indent).max(10)) {
        col = col.child(line(vec![span(format!("{pad}{l}"), t.text_faint)]));
    }
    col.build()
}

fn heading(t: &TokenSet, text: &str) -> View {
    line(vec![span_bold(text.to_string(), t.text)])
}

fn gap() -> View {
    Element::new()
        .style(LayoutStyle::default().h(1).shrink(0.0))
        .build()
}

fn input(value: Signal<String>, w: i32, masked: bool) -> TextInput {
    TextInput::new()
        .value(value)
        .masked(masked)
        .placeholder(String::new())
        .layout(LayoutStyle::default().w(w).h(1))
}

/// A cycling choice (`label: value  change`), applied by the caller.
fn cycle(
    cx: Scope,
    t: &TokenSet,
    label: &'static str,
    value: Signal<String>,
    options: &'static [(&'static str, &'static str)],
    on_pick: impl FnMut(String) + 'static,
) -> View {
    cycle_w(cx, t, label, value, options, 34, on_pick)
}

/// [`cycle`] with the label+value column `w` cells wide.
fn cycle_w(
    cx: Scope,
    t: &TokenSet,
    label: &'static str,
    value: Signal<String>,
    options: &'static [(&'static str, &'static str)],
    w: i32,
    mut on_pick: impl FnMut(String) + 'static,
) -> View {
    let t0 = *t;
    Element::new()
        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
        .child(dyn_view(LayoutStyle::line(1).w(w), move || {
            let cur = value.get();
            let shown = options
                .iter()
                .find(|(k, _)| *k == cur)
                .map(|(_, v)| *v)
                .unwrap_or("?");
            line(vec![
                span(format!("{label}: "), t0.text_muted),
                span(shown, t0.text),
            ])
        }))
        .child(
            Button::new("change")
                .on_click(move || {
                    let mut next = String::new();
                    value.update(|v| {
                        let i = options
                            .iter()
                            .position(|(k, _)| *k == v.as_str())
                            .unwrap_or(0);
                        *v = options[(i + 1) % options.len()].0.to_string();
                        next = v.clone();
                    });
                    on_pick(next);
                })
                .element(cx, &t0)
                .build(),
        )
        .build()
}

fn page_body(cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    // The admin is also a user of the gateway: one sentence says what this
    // page is to them (DESIGN-v2 §2.3).
    if ctx.store.conn.with_untracked(ConnPhase::is_admin) {
        col = col
            .child(helper(&t0, ADMIN_SENTENCE, p.wrap_w))
            .child(gap());
    }
    col.child(address_card(cx, ctx, &t0, e, p))
        .child(gap())
        .child(mailbox_card(cx, ctx, &t0, e, p))
        .child(gap())
        .child(notifications_card(cx, ctx, &t0, e, p))
        .child(gap())
        .child(agent_tools_card(cx, ctx, &t0, e, p))
        .child(gap())
        .child(advanced_card(cx, ctx, &t0, e, p))
        .build()
}

// ---------------------------------------------------------------- 1

fn address_card(cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let saved_value = e.email_address();
    let save = {
        let ctx = ctx.clone();
        move || {
            let a = p.address.get_untracked();
            let ok = if a.trim().is_empty() {
                "Email address cleared.".to_string()
            } else {
                "Email address saved.".to_string()
            };
            send(
                &ctx,
                p,
                "address",
                EmailAction::SetAddress(a.trim().to_string()),
                ok,
            );
        }
    };
    let save_enter = save.clone();
    let field_w = (p.wrap_w as i32 - 28).clamp(16, 40);
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(field(
            &t0,
            "Email address",
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(
                    input(p.address, field_w, false)
                        .on_change(move |_| p.address_saved.set(false))
                        .on_submit(move |_| save_enter())
                        .element(cx, &t0)
                        .build(),
                )
                .child(dyn_view_scoped(
                    LayoutStyle::row().h(1).w(8).shrink(0.0),
                    move |scx| {
                        let t = t0;
                        // "Save" only while the field differs from the saved
                        // address; "Saved" once it landed.
                        let unchanged = p.address.get().trim() == saved_value.as_str();
                        if unchanged {
                            return if p.address_saved.get() {
                                line(vec![span("Saved", t.ok)])
                            } else {
                                Element::new().style(LayoutStyle::default().h(1)).build()
                            };
                        }
                        let save = save.clone();
                        Button::new("Save").on_click(save).element(scx, &t).build()
                    },
                ))
                .build(),
        ))
        .child(helper(&t0, ADDRESS_HELP, p.wrap_w))
        .child(match different_mailbox(e) {
            Some(m) => helper(
                &t0,
                &format!("Your mailbox is a different account: {m}."),
                p.wrap_w,
            ),
            None => Element::new().style(LayoutStyle::default().h(0)).build(),
        })
        .build()
}

/// The connected mailbox's address when it is not "Your email address"
/// (only then does the page say so).
pub fn different_mailbox(e: &MyEmail) -> Option<String> {
    let own = e.email_address();
    let mb = e.address.trim();
    (e.configured
        && !mb.is_empty()
        && !own.trim().is_empty()
        && !mb.eq_ignore_ascii_case(own.trim()))
    .then(|| mb.to_string())
}

// ---------------------------------------------------------------- 2

fn mailbox_card(cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(heading(&t0, "Mailbox"));
    if e.mailboxes_off() && !e.configured {
        return col
            .child(helper(
                &t0,
                &format!(
                    "{} Ask your gateway admin to allow \u{201c}Mailboxes for users\u{201d}.",
                    crate::store::email::REASON_ADMIN_MAILBOXES_OFF
                ),
                p.wrap_w,
            ))
            .build();
    }
    if e.configured {
        return col.child(connected_view(cx, ctx, &t0, e, p)).build();
    }
    col = col.child(
        Tabs::new()
            .tab(TABS[0], || {
                Element::new().style(LayoutStyle::default().h(0)).build()
            })
            .tab(TABS[1], || {
                Element::new().style(LayoutStyle::default().h(0)).build()
            })
            .tab(TABS[2], || {
                Element::new().style(LayoutStyle::default().h(0)).build()
            })
            .active(p.tab)
            .on_change({
                let ctx = ctx.clone();
                move |i| {
                    if i == TAB_IMAP {
                        discover(&ctx, &p.other_address.get_untracked());
                    }
                }
            })
            .layout(LayoutStyle::column().h(2).shrink(0.0))
            .element(cx, &t0)
            .build(),
    );
    let ctx_tab = ctx.clone();
    let e_tab = e.clone();
    col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |tcx| {
            let t = t0;
            match p.tab.get() {
                TAB_IMAP => imap_tab(tcx, &ctx_tab, &t, p),
                i => oauth_tab(
                    tcx,
                    &ctx_tab,
                    &t,
                    &e_tab,
                    p,
                    if i == 1 { "google" } else { "microsoft" },
                ),
            }
        },
    ))
    .build()
}

fn connected_view(_cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let text = e.connected_text();
    let ink = if e.last_error.is_some() {
        t0.warn
    } else {
        t0.text
    };
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for l in wrap_text(&text, p.wrap_w) {
        col = col.child(line(vec![span(l, ink)]));
    }
    if e.mailboxes_off() {
        col = col.child(helper(
            &t0,
            "Your admin turned mailboxes off: your agents, automations and notifications can't use it. Your settings are kept.",
            p.wrap_w,
        ));
    }
    // Active (relocated from Advanced's "Use this mailbox", DESIGN-v2 §3).
    let ctx_use = ctx.clone();
    col = col
        .child(
            Switch::new("Active", p.sw_use)
                .fill()
                .busy_when(move || p.busy.get() == Some("enabled"))
                .notice(ctx.store.notice)
                .on_request(move |want| {
                    send(
                        &ctx_use,
                        p,
                        "enabled",
                        EmailAction::Enabled(want),
                        if want {
                            "Mailbox Active: on.".into()
                        } else {
                            "Mailbox Active: off \u{2014} no watching, sending or notifications; your settings are kept.".into()
                        },
                    )
                })
                .element(_cx, &t0)
                .build(),
        )
        .child(helper(&t0, &format!("    {ACTIVE_HELP}"), p.wrap_w));
    let (c_test, c_disc) = (ctx.clone(), ctx.clone());
    col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |dcx| {
            let t = t0;
            if p.confirm_disconnect.get() {
                let c_disc = c_disc.clone();
                return Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(helper(&t, DISCONNECT_CONFIRM, p.wrap_w))
                    .child(
                        Element::new()
                            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                            .child(
                                Button::new("Disconnect")
                                    .on_click(move || {
                                        send(
                                            &c_disc,
                                            p,
                                            "disconnect",
                                            EmailAction::Disconnect,
                                            "Mailbox disconnected. Policy and limits are kept."
                                                .into(),
                                        )
                                    })
                                    .element(dcx, &t)
                                    .build(),
                            )
                            .child(
                                Button::new("Cancel")
                                    .on_click(move || p.confirm_disconnect.set(false))
                                    .element(dcx, &t)
                                    .build(),
                            )
                            .build(),
                    )
                    .build();
            }
            let c_test = c_test.clone();
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Test")
                        .on_click(move || {
                            send(
                                &c_test,
                                p,
                                "test",
                                EmailAction::Test,
                                "Mailbox test passed: signed in to both servers.".into(),
                            )
                        })
                        .element(dcx, &t)
                        .build(),
                )
                .child(
                    Button::new("Disconnect")
                        .on_click(move || p.confirm_disconnect.set(true))
                        .element(dcx, &t)
                        .build(),
                )
                .build()
        },
    ))
    .build()
}

fn imap_tab(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let store = ctx.store;
    let w = (p.wrap_w as i32 - 22).clamp(16, 40);
    let ctx_addr = ctx.clone();
    let ctx_disc = ctx.clone();
    let ctx_connect = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(field(
            &t0,
            "Mailbox address",
            input(p.other_address, w, false)
                .on_change(move |a| address_changed(&ctx_addr, p, a))
                .on_submit(move |a| discover(&ctx_disc, a))
                .element(cx, &t0)
                .build(),
        ))
        .child(helper(&t0, MAILBOX_ADDRESS_HELP, p.wrap_w))
        .child(field(
            &t0,
            "Password",
            input(p.password, w, true).element(cx, &t0).build(),
        ))
        .child(helper(&t0, PASSWORD_HELP, p.wrap_w))
        .child(server_rows(cx, &t0, p))
        // Where the values come from: one muted sentence.
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |_| {
                let t = t0;
                let typed = p.other_address.get();
                let Some(domain) = address_domain(&typed) else {
                    return helper(
                        &t,
                        "Type the mailbox address: its servers fill in by themselves.",
                        p.wrap_w,
                    );
                };
                let standard = || {
                    ServerDefaults::standard(&typed)
                        .map(|d| d.message)
                        .unwrap_or_default()
                };
                match store.op.email_discovery.get() {
                    Some((addr, state)) if addr.trim().eq_ignore_ascii_case(typed.trim()) => {
                        match state {
                            Loadable::Ready(d) => match &d.defaults {
                                Some(def) if !def.message.is_empty() => {
                                    helper(&t, &def.message, p.wrap_w)
                                }
                                Some(_) => helper(&t, &standard(), p.wrap_w),
                                None => line(vec![span(DISCOVERY_NO_DEFAULTS, t.warn)]),
                            },
                            Loadable::Failed(err) => helper(
                                &t,
                                &format!(
                                    "{} (Couldn't look up the servers for {domain}: {}.)",
                                    standard(),
                                    crate::store::email::email_error_text(&err)
                                        .trim_end_matches('.')
                                ),
                                p.wrap_w,
                            ),
                            _ => line(vec![span(
                                format!("\u{25cc} looking up the servers for {domain}\u{2026}"),
                                t.info,
                            )]),
                        }
                    }
                    _ => helper(&t, &standard(), p.wrap_w),
                }
            },
        ))
        // The different login: one line, a key (Ctrl+O) reveals the field.
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |lcx| {
                let t = t0;
                if !p.login_shown.get() {
                    return line(vec![
                        span(format!("{LOGIN_KEY_LABEL}  "), t.accent),
                        span(DIFFERENT_LOGIN, t.text_faint),
                    ]);
                }
                if p.servers.login.with_untracked(String::is_empty) {
                    p.servers
                        .login
                        .set(p.other_address.get_untracked().trim().to_string());
                }
                Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(field(
                        &t,
                        "Login",
                        input(p.servers.login, w, false)
                            .on_change(move |_| p.servers.mark(E_LOGIN))
                            .element(lcx, &t)
                            .autofocus()
                            .build(),
                    ))
                    .child(helper(
                        &t,
                        &format!("{LOGIN_HELP} {LOGIN_KEY_LABEL} hides it."),
                        p.wrap_w,
                    ))
                    .build()
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::row().h(1).shrink(0.0),
            move |bcx| {
                let t = t0;
                let busy = p.busy.get() == Some("connect");
                let ctx_connect = ctx_connect.clone();
                Button::new(if busy { "Connecting…" } else { "Connect" })
                    .on_click(move || {
                        if p.busy.get_untracked().is_some() {
                            return;
                        }
                        let address = p.other_address.get_untracked();
                        let login = (p.login_shown.get_untracked()
                            && p.servers.edited.get_untracked() & E_LOGIN != 0)
                            .then(|| p.servers.login.get_untracked());
                        let body = imap_connect_body(
                            &address,
                            &p.password.get_untracked(),
                            &p.servers.get(),
                            login.as_deref(),
                        );
                        match body {
                            Ok(b) => send(
                                &ctx_connect,
                                p,
                                "connect",
                                EmailAction::Connect(b.into()),
                                format!("Mailbox connected as {}.", address.trim()),
                            ),
                            Err(msg) => p.status.set(Some(Err(msg))),
                        }
                    })
                    .element(bcx, &t)
                    .build()
            },
        ))
        .build()
}

const SECURITY: &[(&str, &str)] = &[("ssl", "SSL"), ("starttls", "STARTTLS")];

/// Incoming / outgoing servers: always visible, pre-filled (no disclosure).
fn server_rows(cx: Scope, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let sv = p.servers;
    let host_w = (p.wrap_w as i32 - 22).clamp(12, 34);
    let leg = |title: &'static str,
               host: Signal<String>,
               port: Signal<String>,
               sec: Signal<String>,
               bits: (u8, u8, u8)| {
        Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(line(vec![span_bold(title, t0.text_muted)]))
            .child(field(
                &t0,
                "  Server",
                input(host, host_w, false)
                    .on_change(move |_| sv.mark(bits.0))
                    .element(cx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "  Port",
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        input(port, 6, false)
                            .on_change(move |_| sv.mark(bits.1))
                            .element(cx, &t0)
                            .build(),
                    )
                    .child(cycle_w(cx, &t0, "Security", sec, SECURITY, 20, move |_| {
                        sv.mark(bits.2)
                    }))
                    .build(),
            ))
            .build()
    };
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(leg(
            "Incoming mail (IMAP)",
            sv.imap_host,
            sv.imap_port,
            sv.imap_security,
            (E_IMAP_HOST, E_IMAP_PORT, E_IMAP_SEC),
        ))
        .child(leg(
            "Outgoing mail (SMTP)",
            sv.smtp_host,
            sv.smtp_port,
            sv.smtp_security,
            (E_SMTP_HOST, E_SMTP_PORT, E_SMTP_SEC),
        ))
        .build()
}

fn oauth_tab(
    _cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    e: &MyEmail,
    p: Page,
    provider: &'static str,
) -> View {
    const FLOWS: &[(&str, &str)] = &[
        ("", "provider default"),
        ("device", "device code"),
        ("loopback", "browser on the gateway's computer"),
    ];
    let t0 = *t;
    let store = ctx.store;
    let label = if provider == "google" {
        "Google"
    } else {
        "Microsoft"
    };
    // Unavailable with the reason when the gateway has no client for it
    // (and none is given under Advanced).
    let reason = e
        .oauth_provider(provider)
        .and_then(|(ok, why)| (!ok).then_some(why))
        .filter(|why| !why.is_empty());
    let ctx_start = ctx.clone();
    let ctx_cancel = ctx.clone();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    let hint = p.address.get_untracked();
    if !hint.is_empty() {
        col = col.child(helper(
            &t0,
            &format!("Signs in as {hint} (your email address; the provider may ask for another)."),
            p.wrap_w,
        ));
    }
    col = col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |bcx| {
            let t = t0;
            let own_client = !p.client_id.get().trim().is_empty();
            let why = if own_client { None } else { reason.clone() };
            let ctx_start = ctx_start.clone();
            let button_label = format!("Sign in with {label}");
            let mut c = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
            c = c.child(
                Button::new(button_label)
                    .on_click({
                        let why = why.clone();
                        move || {
                            if let Some(w) = &why {
                                p.status.set(Some(Err(w.clone())));
                                return;
                            }
                            let body = json!({
                                "provider": provider,
                                "address": p.address.get_untracked().trim(),
                                "client_id": p.client_id.get_untracked().trim(),
                                "client_secret": p.client_secret.get_untracked(),
                                "tenant": p.tenant.get_untracked().trim(),
                                "flow": p.flow.get_untracked(),
                            });
                            send(
                                &ctx_start,
                                p,
                                "oauth",
                                EmailAction::OAuthStart(body.into()),
                                format!("{label} sign-in started: follow the instructions below."),
                            );
                        }
                    })
                    .element(bcx, &t)
                    .build(),
            );
            if let Some(w) = why {
                c = c.child(helper(&t, &format!("Unavailable — {w}"), p.wrap_w));
            }
            c.build()
        },
    ));
    col = col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |ocx| {
            let t = t0;
            match store.op.email_oauth.get() {
                Some((id, prompt)) => {
                    let c = ctx_cancel.clone();
                    Element::new()
                        .style(LayoutStyle::column().gap(0).shrink(0.0))
                        .child(helper(&t, &prompt, p.wrap_w))
                        .child(
                            Button::new("Cancel sign-in")
                                .on_click(move || {
                                    c.send(Cmd::Operator(OpCmd::Email {
                                        action: EmailAction::OAuthCancel(id.clone()),
                                        form_id: None,
                                    }))
                                })
                                .element(ocx, &t)
                                .build(),
                        )
                        .build()
                }
                None => Element::new().style(LayoutStyle::default().h(0)).build(),
            }
        },
    ));
    let w = (p.wrap_w as i32 - 22).clamp(16, 44);
    col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |acx| {
            let t = t0;
            let open = p.oauth_advanced.get();
            let mut c = Element::new()
                .style(LayoutStyle::column().gap(0).shrink(0.0))
                .child(
                    Button::new(if open { "Advanced ▾" } else { "Advanced ▸" })
                        .on_click(move || p.oauth_advanced.update(|v| *v = !*v))
                        .element(acx, &t)
                        .build(),
                );
            if open {
                c = c
                    .child(helper(
                        &t,
                        "Your own OAuth client, when the gateway has none or you prefer yours.",
                        p.wrap_w,
                    ))
                    .child(field(
                        &t,
                        "Client ID",
                        input(p.client_id, w, false).element(acx, &t).build(),
                    ))
                    .child(field(
                        &t,
                        "Client secret",
                        input(p.client_secret, w, true).element(acx, &t).build(),
                    ));
                if provider == "microsoft" {
                    c = c
                        .child(field(
                            &t,
                            "Tenant",
                            input(p.tenant, w, false).element(acx, &t).build(),
                        ))
                        .child(field(
                            &t,
                            "",
                            cycle(acx, &t, "Sign-in flow", p.flow, FLOWS, |_| {}),
                        ));
                }
            }
            c.build()
        },
    ))
    .build()
}

// ---------------------------------------------------------------- 3

fn notifications_card(cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let reason = e.notifications_unavailable();
    let row = |key: &'static str, label: &'static str, help: &'static str, sig: Signal<bool>| {
        let ctx = ctx.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(
                Switch::new(label, sig)
                    .fill()
                    .unavailable(reason.clone())
                    .busy_when(move || p.busy.get() == Some(key))
                    .notice(ctx.store.notice)
                    .on_request(move |want| {
                        send(
                            &ctx,
                            p,
                            key,
                            EmailAction::NotificationSwitch {
                                key: key.to_string(),
                                on: want,
                            },
                            format!(
                                "\u{201c}{label}\u{201d} notifications are {}.",
                                if want { "on" } else { "off" }
                            ),
                        )
                    })
                    .element(cx, &t0)
                    .build(),
            )
            .child(helper(&t0, &format!("    {help}"), p.wrap_w))
            .build()
    };
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(heading(&t0, "Notifications"))
        .child(row("job_failed", "Job failed", JOB_FAILED_HELP, p.sw_job))
        .child(row(
            "approval_needed",
            "Approval needed",
            APPROVAL_HELP,
            p.sw_approval,
        ))
        .child(test_notification(cx, ctx, &t0, p))
        .build()
}

/// "Send a test": its result is ALWAYS the gateway's sentence ("Sent to
/// x@y." / "Not sent: hourly limit reached …"), wrapped under the button.
fn test_notification(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let ctx_test = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(
            Button::new("Send a test")
                .on_click(move || {
                    p.test_result.set(None);
                    send(
                        &ctx_test,
                        p,
                        "test_notification",
                        EmailAction::TestNotification,
                        String::new(),
                    )
                })
                .element(cx, &t0)
                .build(),
        )
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |_| {
                let t = t0;
                let busy = p.busy.get() == Some("test_notification");
                let (text, ink) = match p.test_result.get() {
                    _ if busy => ("\u{25cc} sending a test\u{2026}".to_string(), t.info),
                    Some(Ok(m)) => (m, t.ok),
                    Some(Err(m)) => (m, t.warn),
                    None => return Element::new().style(LayoutStyle::default().h(0)).build(),
                };
                let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                for l in wrap_text(&text, p.wrap_w.saturating_sub(4).max(10)) {
                    col = col.child(line(vec![span(format!("    {l}"), ink)]));
                }
                col.build()
            },
        ))
        .build()
}

// ---------------------------------------------------------------- 4

fn agent_tools_card(cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let ctx2 = ctx.clone();
    let sig = p.sw_tools;
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(heading(&t0, "Agent email tools"))
        .child(
            Switch::new("Agent email tools", sig)
                .fill()
                .unavailable(e.agent_tools_unavailable())
                .busy_when(move || p.busy.get() == Some("agent_tools"))
                .notice(ctx.store.notice)
                .on_request(move |want| {
                    send(
                        &ctx2,
                        p,
                        "agent_tools",
                        EmailAction::AgentTools(want),
                        if want {
                            "Agent email tools are on.".into()
                        } else {
                            "Agent email tools are off.".into()
                        },
                    )
                })
                .element(cx, &t0)
                .build(),
        )
        .child(helper(&t0, &format!("    {AGENT_TOOLS_HELP}"), p.wrap_w))
        .build()
}

// ---------------------------------------------------------------- 5

fn advanced_card(_cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let e2 = e.clone();
    let ctx2 = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(dyn_view_scoped(
            LayoutStyle::row().h(1).shrink(0.0),
            move |bcx| {
                let t = t0;
                let open = p.advanced.get();
                Button::new(if open {
                    "Advanced ▾"
                } else {
                    "Advanced ▸  who your agents may send to, limits, folder"
                })
                .on_click(move || p.advanced.update(|v| *v = !*v))
                .element(bcx, &t)
                .build()
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |acx| {
                if !p.advanced.get() {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                advanced_body(acx, &ctx2, &t0, &e2, p)
            },
        ))
        .build()
}

const MODES: &[(&str, &str)] = &[
    ("allowlist", "only these recipients"),
    ("denylist", "everyone except these"),
];

fn advanced_body(cx: Scope, ctx: &Ctx, t: &TokenSet, e: &MyEmail, p: Page) -> View {
    let t0 = *t;
    let entries = e.policy_entries.clone();
    let mode_sig = cx.signal(e.policy_mode.clone());
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    // "Your agents may send to [only these recipients]" + the entries;
    // every add/remove applies at once.
    let policy_send = {
        let ctx = ctx.clone();
        move |mode: String, list: Vec<String>, ok: String| {
            let body = policy_body(&mode, &list.join("\n"));
            send(&ctx, p, "policy", EmailAction::Policy(body.into()), ok);
        }
    };
    {
        let entries = entries.clone();
        let policy_send = policy_send.clone();
        col = col.child(cycle_w(
            cx,
            &t0,
            "Your agents may send to",
            mode_sig,
            MODES,
            (p.wrap_w as i32 - 10).clamp(20, 50),
            move |m| {
                let words = if m == "denylist" {
                    "Your agents may send to everyone except the listed addresses."
                } else {
                    "Your agents may send only to the listed addresses."
                };
                policy_send(m, entries.clone(), words.into())
            },
        ));
    }
    if entries.is_empty() {
        col = col.child(line(vec![span("    no addresses yet", t0.text_faint)]));
    }
    for (i, entry) in entries.iter().enumerate() {
        let entries = entries.clone();
        let policy_send = policy_send.clone();
        let entry_txt = entry.clone();
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(line(vec![span(
                    format!("    {}", ellipsize(entry, 40)),
                    t0.text,
                )]))
                .child(
                    Button::new("Remove")
                        .on_click(move || {
                            let mut list = entries.clone();
                            list.remove(i);
                            policy_send(
                                mode_sig.get_untracked(),
                                list,
                                format!("{entry_txt} removed."),
                            )
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .build(),
        );
    }
    {
        let entries = entries.clone();
        let policy_send = policy_send.clone();
        let add = move || {
            let v = p.new_entry.get_untracked().trim().to_string();
            if v.is_empty() {
                return;
            }
            let mut list = entries.clone();
            list.push(v.clone());
            p.new_entry.set(String::new());
            policy_send(mode_sig.get_untracked(), list, format!("{v} added."))
        };
        let add2 = add.clone();
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(line_w(&t0, "    add", 8))
                .child(
                    input(p.new_entry, 28, false)
                        .on_submit(move |_| add())
                        .element(cx, &t0)
                        .build(),
                )
                .child(Button::new("Add").on_click(add2).element(cx, &t0).build())
                .build(),
        );
    }
    // "At most [20] per hour and [100] per day." — Enter in either saves.
    let save_limits = {
        let ctx = ctx.clone();
        move || match limits_body(&p.per_hour.get_untracked(), &p.per_day.get_untracked()) {
            Ok(b) => send(
                &ctx,
                p,
                "limits",
                EmailAction::Limits(b.into()),
                "Send limits saved.".into(),
            ),
            Err(msg) => p.status.set(Some(Err(msg))),
        }
    };
    let (sl1, sl2) = (save_limits.clone(), save_limits);
    col = col.child(gap()).child(
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(line_w(&t0, "At most", 7))
            .child(
                input(p.per_hour, 5, false)
                    .on_submit(move |_| sl1())
                    .element(cx, &t0)
                    .build(),
            )
            .child(line_w(&t0, "per hour and", 12))
            .child(
                input(p.per_day, 5, false)
                    .on_submit(move |_| sl2())
                    .element(cx, &t0)
                    .build(),
            )
            .child(line_w(&t0, "per day.", 8))
            .build(),
    );
    let usage = e.usage_text();
    if !usage.is_empty() {
        col = col.child(helper(&t0, &format!("    {usage}."), p.wrap_w));
    }
    // "Watch folder [INBOX]" (PUT /me/email/folder, Enter saves; empty =
    // INBOX). Unavailable, with the reason, without a mailbox.
    col = col.child(gap());
    if e.configured {
        let ctx_f = ctx.clone();
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(line_w(&t0, "Watch folder", 12))
                .child(
                    input(p.folder, 20, false)
                        .on_submit(move |v: &str| {
                            let v = v.trim().to_string();
                            let shown = if v.is_empty() {
                                "INBOX".to_string()
                            } else {
                                v.clone()
                            };
                            send(
                                &ctx_f,
                                p,
                                "folder",
                                EmailAction::Folder(v),
                                format!("Watching the folder {shown}."),
                            )
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .build(),
        );
    } else {
        col = col.child(line(vec![
            span("Watch folder \u{2014} ", t0.text_faint),
            span(crate::store::email::REASON_CONNECT_MAILBOX, t0.text_faint),
        ]));
    }
    col.build()
}

fn line_w(t: &TokenSet, text: &str, w: i32) -> View {
    super::util::line_styled(
        LayoutStyle::line(1).w(w).shrink(0.0),
        vec![span(text.to_string(), t.text_muted)],
    )
}

/// The helper under another user's Email address (DESIGN-v2 §2.3).
pub fn other_address_help(user_id: &str) -> String {
    format!("Where {user_id}'s sign-in codes and notifications go.")
}

/// The read-only mailbox line for another user when the caller has none to
/// show: only that user can connect a mailbox (round-1 rule).
pub fn other_not_connected(user_id: &str) -> String {
    format!(
        "Not connected \u{2014} only {user_id} can connect a mailbox. You never see anyone's mail."
    )
}

/// Another user's email, as an admin sees it (Accounts screen, DESIGN-v2
/// §2.3): the Email address field with its inline Save (`PATCH
/// /admin/users/{id}` {email}) and a read-only mailbox status line — never
/// a mailbox form: the admin never touches another user's mailbox.
/// `mailbox_line` is the status as the Accounts row says it ("Connected as
/// …"); empty = [`other_not_connected`].
pub fn open_other(
    cx: Scope,
    ctx: &Ctx,
    user_id: String,
    tenant_id: String,
    email_address: Option<String>,
    mailbox_line: String,
) {
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(84, 17), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let ui = ctx2.ui;
        let saved = mcx.signal(email_address.clone().unwrap_or_default());
        let value = mcx.signal(email_address.clone().unwrap_or_default());
        let status = mcx.signal(Option::<Result<String, String>>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        let vw = abstracttui::app::use_viewport(mcx).get_untracked().w;
        let wrap_w = (vw.min(84) - 6).max(20) as usize;
        mcx.effect(move || {
            if let Some((fid, outcome)) = ui.write_done.get() {
                if fid == form_id {
                    ui.write_done.set(None);
                    in_flight.set(false);
                    match outcome {
                        Ok(_) => {
                            saved.set(value.get_untracked().trim().to_string());
                            status.set(Some(Ok("Saved".into())));
                        }
                        Err(e) => status.set(Some(Err(e))),
                    }
                }
            }
        });
        let save = {
            let ctx = ctx2.clone();
            let (user_id, tenant_id) = (user_id.clone(), tenant_id.clone());
            move || {
                if in_flight.get_untracked() {
                    return;
                }
                status.set(None);
                in_flight.set(true);
                ctx.send(Cmd::PatchUser {
                    user_id: user_id.clone(),
                    tenant_id: tenant_id.clone(),
                    body: json!({ "email": value.get_untracked().trim() }).into(),
                    form_id: Some(form_id),
                });
            }
        };
        let save_enter = save.clone();
        let mailbox = if mailbox_line.trim().is_empty() {
            other_not_connected(&user_id)
        } else {
            mailbox_line.clone()
        };
        let close_b = close.clone();
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0).grow(1.0))
            .child(line(vec![span_bold(
                format!("Email \u{2014} {user_id}"),
                t0.accent,
            )]))
            .child(gap())
            .child(field(
                &t0,
                "Email address",
                Element::new()
                    .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                    .child(
                        input(value, 36, false)
                            .on_change(move |_| status.set(None))
                            .on_submit(move |_| save_enter())
                            .element(mcx, &t0)
                            .autofocus()
                            .build(),
                    )
                    .child(dyn_view_scoped(
                        LayoutStyle::row().h(1).w(10).shrink(0.0),
                        move |scx| {
                            let t = t0;
                            if in_flight.get() {
                                return line(vec![span("saving…", t.info)]);
                            }
                            if value.get().trim() == saved.get().trim() {
                                return match status.get() {
                                    Some(Ok(_)) => line(vec![span("Saved", t.ok)]),
                                    _ => Element::new().style(LayoutStyle::default().h(1)).build(),
                                };
                            }
                            Button::new("Save")
                                .on_click(save.clone())
                                .element(scx, &t)
                                .build()
                        },
                    ))
                    .build(),
            ))
            .child(helper(&t0, &other_address_help(&user_id), wrap_w))
            .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
                let t = theme.get().tokens;
                match status.get() {
                    Some(Err(e)) => line(vec![span(format!("✗ {e}"), t.error)]),
                    _ => Element::new().style(LayoutStyle::default().h(0)).build(),
                }
            }))
            .child(gap())
            .child(heading(&t0, "Mailbox"));
        for l in wrap_text(&mailbox, wrap_w) {
            col = col.child(line(vec![span(l, t0.text_muted)]));
        }
        col.child(gap())
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_b())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}
