//! "My email" — the caller's own account page for email (DESIGN §6; web:
//! the gateway console's "My account"). Opened with `@` on the Users
//! screen, open to every signed-in human. Top to bottom, the words of the
//! web console:
//!
//! 1. **Email address** — one field with its own inline Save (the only
//!    Save on the page): where sign-in codes and notifications go.
//! 2. **Mailbox** — not connected: tabs Google | Microsoft | Other; Other
//!    asks the address and the password only, shows the servers found for
//!    the address in one line, and keeps "Server settings" folded unless the
//!    lookup found nothing; ONE Connect (save + test). Connected: one status
//!    line, Test and Disconnect (inline confirmation).
//! 3. **Notifications** — two switches, "Job failed" and "Approval needed".
//! 4. **Agent email tools** — one switch, with the reason when unavailable.
//! 5. **Advanced** (folded) — recipient rules, send limits, folder, the
//!    "Use this mailbox" switch, "Send a test notification".
//!
//! Switches apply at once (worker write law: write → verify by GET →
//! journal); the status line under the page names the new state. The
//! administrator's gateway-wide email switches live on the Users screen.

use abstracttui::prelude::*;
use abstracttui::widgets::{Scroll, Tabs};
use serde_json::json;

use super::switch::Switch;
use super::util::{ellipsize, field, field_w, line, span, span_bold, wrap_text};
use super::{open_form, Ctx};
use crate::store::email::{
    address_domain, limits_body, other_connect_body, policy_body, security_label, Discovery,
    MyEmail, ServerSettings,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::{EmailAction, OpCmd};
use crate::worker::Cmd;

/// The Mailbox card's tabs (DESIGN §6.2).
pub const TABS: [&str; 3] = ["Google", "Microsoft", "Other"];
const TAB_OTHER: usize = 2;

pub const ADDRESS_HELP: &str =
    "Where sign-in codes and notifications go, and the first address your agents may write to.";
pub const PASSWORD_HELP: &str = "Use an app password if your provider needs one.";
pub const JOB_FAILED_HELP: &str =
    "An automation of yours, or a run you asked to be emailed about, failed after its retries.";
pub const APPROVAL_HELP: &str = "A run is waiting for your answer.";
pub const AGENT_TOOLS_HELP: &str = "Your agents and workflows may list, search, read, send and reply to your mail. Every send still follows your recipient rules, your limits and the approval gate.";
pub const DISCONNECT_CONFIRM: &str = "Disconnect this mailbox? Your agents lose email until you connect again. Policy and limits are kept.";
pub const USE_MAILBOX_HELP: &str =
    "Off keeps the settings but stops watching, sending and notifications.";

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
    servers_open: Signal<bool>,
    servers: ServerSignals,
    confirm_disconnect: Signal<bool>,
    advanced: Signal<bool>,
    new_entry: Signal<String>,
    per_hour: Signal<String>,
    per_day: Signal<String>,
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

#[derive(Clone, Copy)]
struct ServerSignals {
    imap_host: Signal<String>,
    imap_port: Signal<String>,
    imap_security: Signal<String>,
    smtp_host: Signal<String>,
    smtp_port: Signal<String>,
    smtp_security: Signal<String>,
    username: Signal<String>,
    display_name: Signal<String>,
    folder: Signal<String>,
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
            username: cx.signal(String::new()),
            display_name: cx.signal(String::new()),
            folder: cx.signal(String::new()),
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
            username: self.username.get_untracked(),
            display_name: self.display_name.get_untracked(),
            folder: self.folder.get_untracked(),
        }
    }

    /// Fill the fields from what discovery found (the user edits from there).
    fn fill(&self, d: &Discovery) {
        if let Some(i) = &d.imap {
            self.imap_host.set(i.host.clone());
            self.imap_port
                .set(i.port.map(|p| p.to_string()).unwrap_or_default());
            if !i.security.is_empty() {
                self.imap_security.set(i.security.clone());
            }
        }
        if let Some(s) = &d.smtp {
            self.smtp_host.set(s.host.clone());
            self.smtp_port
                .set(s.port.map(|p| p.to_string()).unwrap_or_default());
            if !s.security.is_empty() {
                self.smtp_security.set(s.security.clone());
            }
        }
        if !d.username.is_empty() {
            self.username.set(d.username.clone());
        }
    }
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
                match outcome {
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
                        p.status.set(Some(Ok(text)));
                    }
                    Err(e) => {
                        // (A connect refused for want of servers opens
                        // Server settings through `email_discovery`, set by
                        // the worker from the gateway's reason code.)
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
            servers_open: mcx.signal(false),
            servers: ServerSignals::new(mcx),
            confirm_disconnect: mcx.signal(false),
            advanced: mcx.signal(false),
            new_entry: mcx.signal(String::new()),
            per_hour: mcx.signal(String::new()),
            per_day: mcx.signal(String::new()),
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
                    p.servers.folder.set(i.folder.clone());
                }
                if e.auth_kind == "password" {
                    p.tab.set(TAB_OTHER);
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
        // Discovery that found nothing opens Server settings by itself.
        mcx.effect(move || {
            if let Some((_, Loadable::Ready(d))) = store.op.email_discovery.get() {
                if d.found {
                    p.servers.fill(&d);
                } else {
                    p.servers_open.set(true);
                }
            }
        });
        let ctx_body = ctx2.clone();
        let close_cancel = close.clone();
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
    mut on_pick: impl FnMut(String) + 'static,
) -> View {
    let t0 = *t;
    Element::new()
        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
        .child(dyn_view(LayoutStyle::line(1).w(34), move || {
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
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(address_card(cx, ctx, &t0, e, p))
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
        .build()
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
                    if i == TAB_OTHER {
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
                TAB_OTHER => other_tab(tcx, &ctx_tab, &t, p),
                i => oauth_tab(
                    tcx,
                    &ctx_tab,
                    &t,
                    &e_tab,
                    p,
                    if i == 0 { "google" } else { "microsoft" },
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
    } else if !e.enabled {
        col = col.child(helper(
            &t0,
            "Not in use: switch on \u{201c}Use this mailbox\u{201d} under Advanced.",
            p.wrap_w,
        ));
    }
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

fn other_tab(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let store = ctx.store;
    let w = (p.wrap_w as i32 - 22).clamp(16, 40);
    let ctx_disc = ctx.clone();
    let ctx_connect = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(field(
            &t0,
            "Email address",
            input(p.other_address, w, false)
                .on_submit(move |a| discover(&ctx_disc, a))
                .element(cx, &t0)
                .build(),
        ))
        .child(field(
            &t0,
            "Password",
            input(p.password, w, true).element(cx, &t0).build(),
        ))
        .child(helper(&t0, PASSWORD_HELP, p.wrap_w))
        // The servers found for the address: one line, or the reason.
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |scx| {
                let t = t0;
                let typed = p.other_address.get();
                let found = store.op.email_discovery.get();
                let row = |text: String, ink, scx: Scope| {
                    let shown = ellipsize(&text, p.wrap_w.saturating_sub(8));
                    let w = abstracttui::text::width(&shown);
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                        .child(super::util::line_styled(
                            LayoutStyle::line(1).w(w).shrink(0.0),
                            vec![span(shown, ink)],
                        ))
                        .child(
                            Button::new(if p.servers_open.get_untracked() {
                                "Hide"
                            } else {
                                "Edit"
                            })
                            .on_click(move || p.servers_open.update(|v| *v = !*v))
                            .element(scx, &t)
                            .build(),
                        )
                        .build()
                };
                match found {
                    Some((addr, state)) if addr.trim() == typed.trim() => match state {
                        Loadable::Ready(d) => match d.summary() {
                            Some(s) => row(s, t.text_muted, scx),
                            None => helper(&t, &d.not_found_text(), p.wrap_w),
                        },
                        Loadable::Failed(err) => helper(
                            &t,
                            &format!(
                                "Couldn't look up the mail servers: {}",
                                crate::store::email::email_error_text(&err)
                            ),
                            p.wrap_w,
                        ),
                        _ => line(vec![span("◌ looking up the mail servers…", t.info)]),
                    },
                    _ if address_domain(&typed).is_some() => row(
                        "Enter in the address looks up the servers (Connect does too).".into(),
                        t.text_faint,
                        scx,
                    ),
                    _ => Element::new().style(LayoutStyle::default().h(0)).build(),
                }
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |scx| {
                if !p.servers_open.get() {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                server_settings(scx, &t0, p)
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
                        let servers = p.servers.get();
                        let body = other_connect_body(
                            &address,
                            &p.password.get_untracked(),
                            p.servers_open.get_untracked().then_some(&servers),
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

fn server_settings(cx: Scope, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let sv = p.servers;
    let host_w = (p.wrap_w as i32 - 22).clamp(12, 34);
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(vec![span_bold("Server settings", t0.text_muted)]))
        .child(field(&t0, "IMAP server", input(sv.imap_host, host_w, false).element(cx, &t0).build()))
        .child(field(&t0, "IMAP port", input(sv.imap_port, 6, false).element(cx, &t0).build()))
        .child(field(&t0, "", cycle(cx, &t0, "IMAP security", sv.imap_security, SECURITY, |_| {})))
        .child(field(&t0, "SMTP server", input(sv.smtp_host, host_w, false).element(cx, &t0).build()))
        .child(field(&t0, "SMTP port", input(sv.smtp_port, 6, false).element(cx, &t0).build()))
        .child(field(&t0, "", cycle(cx, &t0, "SMTP security", sv.smtp_security, SECURITY, |_| {})))
        .child(field(&t0, "User name", input(sv.username, host_w, false).element(cx, &t0).build()))
        .child(field(&t0, "Display name", input(sv.display_name, host_w, false).element(cx, &t0).build()))
        .child(field(&t0, "Folder", input(sv.folder, 16, false).element(cx, &t0).build()))
        .child(helper(
            &t0,
            &format!(
                "Empty user name = the address; empty folder = INBOX. {} and {} are the two security modes.",
                security_label("ssl"),
                security_label("starttls")
            ),
            p.wrap_w,
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
                    "Advanced ▸  recipient rules, send limits, folder, Use this mailbox"
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
    // Recipient rules: mode + entries; every add/remove applies at once.
    col = col.child(line(vec![span_bold("Recipient rules", t0.text_muted)]));
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
        col = col.child(field_w(
            &t0,
            "",
            3,
            cycle(cx, &t0, "Mode", mode_sig, MODES, move |m| {
                let words = if m == "denylist" {
                    "Recipient rules: everyone except the listed addresses."
                } else {
                    "Recipient rules: only the listed addresses."
                };
                policy_send(m, entries.clone(), words.into())
            }),
        ));
    }
    if entries.is_empty() {
        col = col.child(line(vec![span("    no entries yet", t0.text_faint)]));
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
                                format!("{entry_txt} removed from the recipient rules."),
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
            policy_send(
                mode_sig.get_untracked(),
                list,
                format!("{v} added to the recipient rules."),
            )
        };
        let add2 = add.clone();
        col = col.child(field(
            &t0,
            "Add address/domain",
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(
                    input(p.new_entry, 28, false)
                        .on_submit(move |_| add())
                        .element(cx, &t0)
                        .build(),
                )
                .child(Button::new("Add").on_click(add2).element(cx, &t0).build())
                .build(),
        ));
    }
    // Send limits: per hour · per day side by side, saved on Enter.
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
    col = col
        .child(line(vec![span_bold("Send limits", t0.text_muted)]))
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                .child(line_w(&t0, "    per hour", 12))
                .child(
                    input(p.per_hour, 6, false)
                        .on_submit(move |_| sl1())
                        .element(cx, &t0)
                        .build(),
                )
                .child(line_w(&t0, "  per day", 9))
                .child(
                    input(p.per_day, 6, false)
                        .on_submit(move |_| sl2())
                        .element(cx, &t0)
                        .build(),
                )
                .build(),
        );
    let usage = e.usage_text();
    col = col.child(helper(
        &t0,
        &format!(
            "    {}Enter in a field saves both.",
            if usage.is_empty() {
                String::new()
            } else {
                format!("{usage}. ")
            }
        ),
        p.wrap_w,
    ));
    // Folder (read from the connected mailbox; set under Server settings).
    let folder = e
        .imap
        .as_ref()
        .map(|i| {
            if i.folder.is_empty() {
                "INBOX".to_string()
            } else {
                i.folder.clone()
            }
        })
        .unwrap_or_else(|| "INBOX".into());
    col = col.child(line(vec![
        span_bold("Folder  ", t0.text_muted),
        span(folder, t0.text),
    ]));
    // Use this mailbox (the user's own on/off).
    let ctx_use = ctx.clone();
    let use_sig = p.sw_use;
    col = col
        .child(
            Switch::new("Use this mailbox", use_sig)
                .fill()
                .unavailable((!e.configured).then(|| crate::store::email::REASON_CONNECT_MAILBOX.to_string()))
                .busy_when(move || p.busy.get() == Some("enabled"))
                .notice(ctx.store.notice)
                .on_request(move |want| {
                    send(
                        &ctx_use,
                        p,
                        "enabled",
                        EmailAction::Enabled(want),
                        if want {
                            "\u{201c}Use this mailbox\u{201d} is on.".into()
                        } else {
                            "\u{201c}Use this mailbox\u{201d} is off: no watching, sending or notifications.".into()
                        },
                    )
                })
                .element(cx, &t0)
                .build(),
        )
        .child(helper(&t0, &format!("    {USE_MAILBOX_HELP}"), p.wrap_w));
    let ctx_test = ctx.clone();
    col.child(
        Button::new("Send a test notification")
            .on_click(move || {
                send(
                    &ctx_test,
                    p,
                    "test_notification",
                    EmailAction::TestNotification,
                    "Test notification sent.".into(),
                )
            })
            .element(cx, &t0)
            .build(),
    )
    .build()
}

fn line_w(t: &TokenSet, text: &str, w: i32) -> View {
    super::util::line_styled(
        LayoutStyle::line(1).w(w).shrink(0.0),
        vec![span(text.to_string(), t.text_muted)],
    )
}
