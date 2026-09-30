//! "My email" — the caller's OWN mailbox (framework backlog 0992; web: the
//! Users tab's "My email" section, open to every signed-in human). Same
//! fields and words as the web console and AbstractCore's Email screens:
//! account (IMAP/SMTP, password), OAuth2 sign-in, recipient policy, send
//! limits, notification preferences.
//!
//! Opened with `@` on the Users screen. Four sections, one at a time (the
//! form never outgrows an 80x24 terminal): Account · OAuth2 · Policy &
//! limits · Notifications. Every write goes through the worker's write law
//! (write → verify by GET → journal) and the form stays open.

use abstracttui::prelude::*;
use abstracttui::widgets::{SubmitPolicy, TextArea, TextAreaState};
use serde_json::json;

use super::util::{ellipsize, field, line, span, span_bold};
use super::{open_form, Ctx};
use crate::store::email::{
    connect_body, limits_body, notifications_body, policy_body, MyEmail, MyNotifications,
    NotifyEvent,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::{EmailAction, OpCmd};
use crate::worker::Cmd;

const SECTIONS: [&str; 4] = [
    "Account",
    "OAuth2 sign-in",
    "Policy, limits & tools",
    "Notifications",
];

/// Route this form's write outcomes: a landed write keeps the form OPEN
/// (several writes per visit) and says what landed; a refusal shows its
/// cause and fix.
fn install_email_done(
    mcx: Scope,
    ctx: &Ctx,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    ok_note: Signal<Option<String>>,
) {
    let ui = ctx.ui;
    mcx.effect(move || {
        if let Some((fid, outcome)) = ui.write_done.get() {
            if fid == form_id {
                ui.write_done.set(None);
                in_flight.set(false);
                match outcome {
                    Ok(v) => {
                        form_error.set(None);
                        ok_note.set(Some(v));
                    }
                    Err(e) => {
                        ok_note.set(None);
                        form_error.set(Some(e));
                    }
                }
            }
        }
    });
}

/// Open the form; it reads the settings first and fills in when they land.
pub fn open(cx: Scope, ctx: &Ctx) {
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        ctx.store.notice.set(Some(
            "not connected — probe on the Connection screen first".into(),
        ));
        return;
    }
    ctx.send(Cmd::Operator(OpCmd::LoadMyEmail));
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(100, 34), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let store = ctx2.store;
        let form_error = mcx.signal(Option::<String>::None);
        let ok_note = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let section = mcx.signal(0usize);
        let form_id = crate::worker::next_form_id();
        install_email_done(mcx, &ctx2, form_id, in_flight, form_error, ok_note);
        let ctx_body = ctx2.clone();
        let close_cancel = close.clone();
        let tabs = {
            let mut row = Element::new().style(LayoutStyle::row().gap(2).h(1).shrink(0.0));
            for (i, name) in SECTIONS.iter().enumerate() {
                row = row.child(
                    Button::new(*name)
                        .on_click(move || section.set(i))
                        .element(mcx, &t0)
                        .build(),
                );
            }
            row.build()
        };
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("My email", t0.accent)]))
            .child(line(vec![span(
                "Your own mailbox, stored encrypted in your data home; only read (never marked read, moved or deleted). \
                 Administrators can turn email on or off for you, never read it.",
                t0.text_faint,
            )]))
            .child(dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |_scx| {
                let t = theme.get().tokens;
                match store.op.my_email.get() {
                    Loadable::Ready(e) => status_block(&t, &e),
                    Loadable::Failed(e) => line(vec![span(format!("✗ {}", crate::store::email::email_error_text(&e)), t.error)]),
                    _ => line(vec![span("◌ reading my email…", t.info)]),
                }
            }))
            .child(tabs)
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                line(vec![span_bold(format!("— {} —", SECTIONS[section.get().min(3)]), t.text)])
            }))
            .child(dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |bcx| {
                let t = theme.get().tokens;
                let sec = section.get();
                let email = store.op.my_email.get();
                let Loadable::Ready(e) = email else {
                    return line(vec![span("◌ waiting for the settings…", t.info)]);
                };
                match sec {
                    0 => account_section(bcx, &ctx_body, &t, &e, form_id, in_flight, form_error, ok_note),
                    1 => oauth_section(bcx, &ctx_body, &t, &e, form_id, in_flight, form_error, ok_note),
                    2 => policy_section(bcx, &ctx_body, &t, &e, form_id, in_flight, form_error, ok_note),
                    _ => match store.op.my_notifications.get() {
                        Loadable::Ready(n) => notifications_section(bcx, &ctx_body, &t, &n, form_id, in_flight, form_error, ok_note),
                        Loadable::Failed(err) => line(vec![span(format!("✗ {err}"), t.error)]),
                        _ => line(vec![span("◌ reading my notifications…", t.info)]),
                    },
                }
            }))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                match ok_note.get() {
                    Some(v) => line(vec![span(format!("✓ {}", ellipsize(&v, 94)), t.ok)]),
                    None => line(vec![]),
                }
            }))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_cancel())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

fn status_block(t: &TokenSet, e: &MyEmail) -> View {
    let t0 = *t;
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    col = col.child(line(vec![
        span("state: ", t0.text_muted),
        span_bold(
            e.state_label(),
            if e.effective_enabled { t0.ok } else { t0.warn },
        ),
        span(
            if e.configured {
                format!("  ·  {}  ·  sign-in {}", e.address, e.auth_kind)
            } else {
                String::new()
            },
            t0.text,
        ),
    ]));
    if e.configured {
        col = col.child(line(vec![span(
            ellipsize(
                &format!(
                    "IMAP (read) {}  ·  SMTP (send) {}",
                    e.imap
                        .as_ref()
                        .map(|s| s.text())
                        .unwrap_or_else(|| "not configured".into()),
                    e.smtp
                        .as_ref()
                        .map(|s| s.text())
                        .unwrap_or_else(|| "not configured".into()),
                ),
                94,
            ),
            t0.text_faint,
        )]));
        col = col.child(line(vec![span(
            ellipsize(
                &format!(
                    "last test {} (IMAP {}, SMTP {})",
                    if e.last_test.is_empty() {
                        "never"
                    } else {
                        &e.last_test
                    },
                    e.imap_test,
                    e.smtp_test,
                ),
                94,
            ),
            t0.text_faint,
        )]));
        col = col.child(line(vec![span(
            format!("credentials: {}", e.credentials_text()),
            t0.text_faint,
        )]));
        col = col.child(line(vec![span(
            format!(
                "watcher: {}{}",
                e.watcher_state,
                if e.watcher_last_poll.is_empty() {
                    String::new()
                } else {
                    format!(" · last check {}", e.watcher_last_poll)
                }
            ),
            t0.text_faint,
        )]));
    }
    if let Some(err) = &e.last_error {
        col = col.child(line(vec![span(ellipsize(&err.text(), 96), t0.error)]));
    }
    if !e.admin_disabled.is_empty() {
        col = col.child(line(vec![span(ellipsize(&e.admin_disabled, 96), t0.error)]));
    }
    for n in &e.notices {
        col = col.child(line(vec![span(ellipsize(n, 96), t0.warn)]));
    }
    col.build()
}

fn input(
    cx: Scope,
    t: &TokenSet,
    value: Signal<String>,
    placeholder: &str,
    w: i32,
    masked: bool,
) -> View {
    TextInput::new()
        .value(value)
        .masked(masked)
        .placeholder(placeholder.to_string())
        .placeholder_while_focused(true)
        .layout(LayoutStyle::default().w(w).h(1))
        .element(cx, t)
        .build()
}

/// A cycling choice (the my_policy.rs pattern): label + the current value.
fn cycle(
    cx: Scope,
    t: &TokenSet,
    label: &'static str,
    value: Signal<String>,
    options: &'static [(&'static str, &'static str)],
) -> View {
    let t0 = *t;
    Element::new()
        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
        .child(dyn_view(LayoutStyle::line(1).w(44), move || {
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
                    value.update(|v| {
                        let i = options
                            .iter()
                            .position(|(k, _)| *k == v.as_str())
                            .unwrap_or(0);
                        *v = options[(i + 1) % options.len()].0.to_string();
                    })
                })
                .element(cx, &t0)
                .build(),
        )
        .build()
}

fn send_write(
    ctx: &Ctx,
    action: EmailAction,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    ok_note: Signal<Option<String>>,
) {
    if in_flight.get_untracked() {
        return;
    }
    form_error.set(None);
    ok_note.set(None);
    in_flight.set(true);
    ctx.send(Cmd::Operator(OpCmd::Email {
        action,
        form_id: Some(form_id),
    }));
}

const SECURITY: &[(&str, &str)] = &[("ssl", "SSL"), ("starttls", "STARTTLS")];

#[allow(clippy::too_many_arguments)]
fn account_section(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    e: &MyEmail,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    ok_note: Signal<Option<String>>,
) -> View {
    let t0 = *t;
    let imap = e.imap.clone().unwrap_or_default();
    let smtp = e.smtp.clone().unwrap_or_default();
    let address = cx.signal(e.address.clone());
    let display_name = cx.signal(e.display_name.clone());
    let username = cx.signal(if e.username == e.address {
        String::new()
    } else {
        e.username.clone()
    });
    let password = cx.signal(String::new());
    let imap_host = cx.signal(imap.host.clone());
    let imap_port = cx.signal(imap.port.map(|p| p.to_string()).unwrap_or_default());
    let imap_sec = cx.signal(if imap.security.is_empty() {
        "ssl".to_string()
    } else {
        imap.security.clone()
    });
    let imap_folder = cx.signal(imap.folder.clone());
    let smtp_host = cx.signal(smtp.host.clone());
    let smtp_port = cx.signal(smtp.port.map(|p| p.to_string()).unwrap_or_default());
    let smtp_sec = cx.signal(if smtp.security.is_empty() {
        "ssl".to_string()
    } else {
        smtp.security.clone()
    });
    let confirming = cx.signal(false);
    let enabled = e.enabled;
    let (c_save, c_test, c_toggle, c_disc) = (ctx.clone(), ctx.clone(), ctx.clone(), ctx.clone());
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(field(&t0, "address", input(cx, &t0, address, "me@example.com", 40, false)))
        .child(field(&t0, "display name", input(cx, &t0, display_name, "", 40, false)))
        .child(field(&t0, "user name", input(cx, &t0, username, "(the address)", 40, false)))
        .child(field(&t0, "password", input(cx, &t0, password, "app password — stored encrypted, never shown again", 52, true)))
        .child(field(&t0, "IMAP host", input(cx, &t0, imap_host, "imap.example.com", 40, false)))
        .child(field(&t0, "IMAP port", input(cx, &t0, imap_port, "993", 8, false)))
        .child(field(&t0, "", cycle(cx, &t0, "IMAP security", imap_sec, SECURITY)))
        .child(field(&t0, "folder", input(cx, &t0, imap_folder, "INBOX", 24, false)))
        .child(field(&t0, "SMTP host", input(cx, &t0, smtp_host, "smtp.example.com", 40, false)))
        .child(field(&t0, "SMTP port", input(cx, &t0, smtp_port, "465", 8, false)))
        .child(field(&t0, "", cycle(cx, &t0, "SMTP security", smtp_sec, SECURITY)))
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Save and test")
                        .on_click(move || {
                            let body = connect_body(
                                &address.get_untracked(),
                                &display_name.get_untracked(),
                                &username.get_untracked(),
                                &password.get_untracked(),
                                (&imap_host.get_untracked(), &imap_port.get_untracked(), &imap_sec.get_untracked(), &imap_folder.get_untracked()),
                                (&smtp_host.get_untracked(), &smtp_port.get_untracked(), &smtp_sec.get_untracked()),
                            );
                            match body {
                                Ok(b) => send_write(&c_save, EmailAction::Connect(b.into()), form_id, in_flight, form_error, ok_note),
                                Err(msg) => form_error.set(Some(msg)),
                            }
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .child(
                    Button::new("Test")
                        .on_click(move || send_write(&c_test, EmailAction::Test, form_id, in_flight, form_error, ok_note))
                        .element(cx, &t0)
                        .build(),
                )
                .child(
                    Button::new(if enabled { "Turn off" } else { "Turn on" })
                        .on_click(move || send_write(&c_toggle, EmailAction::Enabled(!enabled), form_id, in_flight, form_error, ok_note))
                        .element(cx, &t0)
                        .build(),
                )
                .child(dyn_view_scoped(LayoutStyle::row().h(1).w(34), move |dcx| {
                    let c_disc = c_disc.clone();
                    let armed = confirming.get();
                    Button::new(if armed { "Disconnect now (deletes the credentials)" } else { "Disconnect" })
                        .on_click(move || {
                            if confirming.get_untracked() {
                                confirming.set(false);
                                send_write(&c_disc, EmailAction::Disconnect, form_id, in_flight, form_error, ok_note);
                            } else {
                                confirming.set(true);
                            }
                        })
                        .element(dcx, &t0)
                        .build()
                }))
                .build(),
        )
        .child(line(vec![span(
            "Many providers need an app password when two-step verification is on. Disconnect deletes the stored password or tokens (policy and limits are kept).",
            t0.text_faint,
        )]))
        .build()
}

#[allow(clippy::too_many_arguments)]
fn oauth_section(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    e: &MyEmail,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    ok_note: Signal<Option<String>>,
) -> View {
    const PROVIDERS: &[(&str, &str)] = &[
        ("microsoft", "Microsoft (Outlook, Microsoft 365)"),
        ("google", "Google (Gmail)"),
    ];
    const FLOWS: &[(&str, &str)] = &[
        ("", "provider default"),
        ("device", "device code"),
        ("loopback", "browser on the gateway's computer"),
    ];
    let t0 = *t;
    let store = ctx.store;
    let provider = cx.signal("microsoft".to_string());
    let address = cx.signal(e.address.clone());
    let client_id = cx.signal(String::new());
    let client_secret = cx.signal(String::new());
    let tenant = cx.signal(String::new());
    let flow = cx.signal(String::new());
    let (c_start, c_cancel) = (ctx.clone(), ctx.clone());
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(field(&t0, "", cycle(cx, &t0, "provider", provider, PROVIDERS)))
        .child(field(&t0, "address", input(cx, &t0, address, "me@outlook.com", 40, false)))
        .child(field(&t0, "client id", input(cx, &t0, client_id, "(the gateway's or the built-in client)", 44, false)))
        .child(field(&t0, "client secret", input(cx, &t0, client_secret, "(none)", 44, true)))
        .child(field(&t0, "tenant", input(cx, &t0, tenant, "common (Microsoft only)", 30, false)))
        .child(field(&t0, "", cycle(cx, &t0, "sign-in flow", flow, FLOWS)))
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Start sign-in")
                        .on_click(move || {
                            let body = json!({
                                "provider": provider.get_untracked(),
                                "address": address.get_untracked().trim(),
                                "client_id": client_id.get_untracked().trim(),
                                "client_secret": client_secret.get_untracked(),
                                "tenant": tenant.get_untracked().trim(),
                                "flow": flow.get_untracked(),
                            });
                            send_write(&c_start, EmailAction::OAuthStart(body.into()), form_id, in_flight, form_error, ok_note);
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .child(
                    Button::new("Cancel sign-in")
                        .on_click(move || {
                            if let Some((id, _)) = store.op.email_oauth.get_untracked() {
                                c_cancel.send(Cmd::Operator(OpCmd::Email { action: EmailAction::OAuthCancel(id), form_id: None }));
                            }
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .build(),
        )
        .child(dyn_view(LayoutStyle::line(2).shrink(0.0), move || {
            let t = t0;
            match store.op.email_oauth.get() {
                Some((_, prompt)) => line(vec![span(ellipsize(&prompt, 190), t.info)]),
                None => line(vec![]),
            }
        }))
        .child(line(vec![span(
            "Without a client id, the gateway's OAuth client (set by an administrator) or the built-in AbstractFramework client signs in. \
             Microsoft defaults to a device code (any browser, any machine); Google to a browser on the gateway's own computer.",
            t0.text_faint,
        )]))
        .build()
}

#[allow(clippy::too_many_arguments)]
fn policy_section(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    e: &MyEmail,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    ok_note: Signal<Option<String>>,
) -> View {
    const MODES: &[(&str, &str)] = &[
        ("allowlist", "allowlist: only these recipients"),
        ("denylist", "denylist: everyone except these"),
    ];
    let t0 = *t;
    let mode = cx.signal(e.policy_mode.clone());
    let entries0 = e.policy_entries.join("\n");
    let entries = cx.signal(entries0.clone());
    let entries_state = TextAreaState::new(cx);
    entries_state.set_text(entries0);
    let per_hour = cx.signal(e.per_hour.map(|v| v.to_string()).unwrap_or_default());
    let per_day = cx.signal(e.per_day.map(|v| v.to_string()).unwrap_or_default());
    let usage = e.usage_text();
    let (c_pol, c_lim, c_tools) = (ctx.clone(), ctx.clone(), ctx.clone());
    let agent_on = e.agent_tools_enabled;
    let agent_text = e.agent_tools_text();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(field(&t0, "", cycle(cx, &t0, "mode", mode, MODES)))
        .child(field(
            &t0,
            "entries",
            TextArea::new()
                .state(&entries_state)
                .placeholder("one address or domain per line — me@example.com, example.org")
                .on_change(move |s: &str| {
                    if entries.with_untracked(|cur| cur != s) {
                        entries.set(s.to_string());
                    }
                })
                .submit_policy(SubmitPolicy::EnterInserts)
                .rows(3, 5)
                .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                .element(cx, &t0)
                .build(),
        ))
        .child(
            Button::new("Save policy")
                .on_click(move || {
                    let body = policy_body(&mode.get_untracked(), &entries.get_untracked());
                    send_write(&c_pol, EmailAction::Policy(body.into()), form_id, in_flight, form_error, ok_note);
                })
                .element(cx, &t0)
                .build(),
        )
        .child(line(vec![span(
            "Entries are exact addresses or domains (a subdomain only when written as its own entry); To, Cc and Bcc of every send.",
            t0.text_faint,
        )]))
        .child(field(&t0, "per hour", input(cx, &t0, per_hour, "20", 8, false)))
        .child(field(&t0, "per day", input(cx, &t0, per_day, "100", 8, false)))
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Save limits")
                        .on_click(move || match limits_body(&per_hour.get_untracked(), &per_day.get_untracked()) {
                            Ok(b) => send_write(&c_lim, EmailAction::Limits(b.into()), form_id, in_flight, form_error, ok_note),
                            Err(msg) => form_error.set(Some(msg)),
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .child(line(vec![span(usage, t0.text_faint)]))
                .build(),
        )
        .child(line(vec![
            span("agent email tools: ", t0.text_muted),
            span(ellipsize(&agent_text, 76), t0.text),
        ]))
        .child(
            Button::new(if agent_on {
                "Turn agent email tools off"
            } else {
                "Turn agent email tools on"
            })
            .on_click(move || {
                send_write(&c_tools, EmailAction::AgentTools(!agent_on), form_id, in_flight, form_error, ok_note)
            })
            .element(cx, &t0)
            .build(),
        )
        .child(line(vec![span(
            "Off by default: agents and workflows get list, search, read, send, reply and attachments only when your account is connected, allowed by an administrator and this is on.",
            t0.text_faint,
        )]))
        .build()
}

#[allow(clippy::too_many_arguments)]
fn notifications_section(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    n: &MyNotifications,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
    ok_note: Signal<Option<String>>,
) -> View {
    let t0 = *t;
    let rows: Vec<(NotifyEvent, Signal<bool>)> = n
        .events
        .iter()
        .map(|e| (e.clone(), cx.signal(e.email)))
        .collect();
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(line(vec![span(
            ellipsize(&n.channel_text(), 190),
            if n.available { t0.text } else { t0.warn },
        )]));
    for (ev, sig) in &rows {
        col = col.child(field(
            &t0,
            "",
            Checkbox::new(ev.label.clone())
                .checked(*sig)
                .element(cx, &t0)
                .build(),
        ));
    }
    let rows_save = rows.clone();
    let (c_save, c_test) = (ctx.clone(), ctx.clone());
    col.child(
        Element::new()
            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
            .child(
                Button::new("Save notifications")
                    .on_click(move || {
                        let evs: Vec<NotifyEvent> = rows_save
                            .iter()
                            .map(|(e, s)| NotifyEvent {
                                email: s.get_untracked(),
                                ..e.clone()
                            })
                            .collect();
                        send_write(
                            &c_save,
                            EmailAction::Notifications(notifications_body(&evs).into()),
                            form_id,
                            in_flight,
                            form_error,
                            ok_note,
                        );
                    })
                    .element(cx, &t0)
                    .build(),
            )
            .child(
                Button::new("Send test notification")
                    .on_click(move || {
                        send_write(
                            &c_test,
                            EmailAction::TestNotification,
                            form_id,
                            in_flight,
                            form_error,
                            ok_note,
                        )
                    })
                    .element(cx, &t0)
                    .build(),
            )
            .build(),
    )
    .child(line(vec![span(
        ellipsize(&n.outbox_text, 96),
        t0.text_faint,
    )]))
    .build()
}
