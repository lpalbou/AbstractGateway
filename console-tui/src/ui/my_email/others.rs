//! My email → Mailboxes, your own page: the person's OTHER mailboxes (the web's
//! Mailboxes list after the primary, `console_mailboxes.py`) and "Add mailbox".
//!
//! - Each other mailbox is ONE line "address · provider · state · Read only|Read
//!   and write", an **Active** switch (`PUT /me/email/accounts/{id}/enabled`) and a
//!   **Read and write** switch (`PUT /me/email/accounts/{id}/access {write}`; turning
//!   it on adds and tests the outgoing server with the stored sign-in, so slow).
//! - **Add mailbox** (once the primary is connected): Email address (the standard
//!   servers at once, then the gateway's discovery), Password, the **Read and write**
//!   switch (default off), Server settings (incoming always, outgoing only when Read
//!   and write is on, Sign-in name), ONE "Add mailbox" → `PUT /me/email/accounts/{id}`
//!   `{address, password, test: true, imap, smtp?, write, name}` with the id derived
//!   from the address as the web does. Password sign-in only: Google/Microsoft
//!   sign-in of another mailbox is the web's.
//!
//! Writes go through the JSON lane under [`K_ACCOUNTS_WRITE`], re-read
//! [`K_ACCOUNTS`], and land on the page's one status line (the gateway's cause
//! when refused).

use abstracttui::prelude::*;
use serde_json::{json, Value};

use super::{
    discover, field, gap, helper, input, line, server_rows, span, span_bold, Page, ServerSignals,
    E_LOGIN, PASSWORD_HELP, SERVER_SETTINGS, SERVER_SETTINGS_KEY_LABEL, SIGN_IN_NAME_HELP,
};
use crate::store::email::{
    access_words, add_mailbox_body, email_error_text, mailbox_access, mailbox_id_from_address,
    ServerDefaults,
};
use crate::store::json::WriteState;
use crate::store::Loadable;
use crate::ui::Ctx;
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

/// The JSON lane's read of `GET /me/email/accounts` (this page's own key).
pub const K_ACCOUNTS: &str = "my_email.accounts";
/// The outcome of this page's mailbox writes (switches, Add mailbox).
pub const K_ACCOUNTS_WRITE: &str = "my_email.accounts.write";
const ACCOUNTS_PATH: &str = "/me/email/accounts";
pub const ADD_MAILBOX: &str = "Add mailbox";
/// The Read and write switch's description (the web's words).
pub const WRITE_HELP: &str = "Off: automations read this mailbox and never change it or send from it. On: they can mark, move, label, delete and send as it.";
pub(super) const SENDS_AS_YOU: &str = crate::ui::triggers::SENDS_AS_YOU;

/// One of your other mailboxes (a configured, non-primary `GET /me/email/accounts` item).
#[derive(Clone, Debug, PartialEq)]
pub struct OtherMailbox {
    pub id: String,
    pub name: String,
    pub address: String,
    pub provider: String,
    pub state: String,
    pub enabled: bool,
    /// Read and write (else Read only).
    pub write: bool,
}

fn st(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

/// Your other mailboxes, in the gateway's order (the primary and the not
/// configured ones left out).
pub fn other_mailboxes(v: &Value) -> Vec<OtherMailbox> {
    v.get("items")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|m| m.get("configured").and_then(Value::as_bool) == Some(true))
                .filter(|m| m.get("primary").and_then(Value::as_bool) != Some(true))
                .map(|m| {
                    let mailbox = m.get("mailbox").cloned().unwrap_or(Value::Null);
                    OtherMailbox {
                        id: st(m, "account_id"),
                        name: st(m, "name"),
                        address: st(m, "address"),
                        provider: st(&mailbox, "provider"),
                        state: st(&mailbox, "state"),
                        enabled: m.get("enabled").and_then(Value::as_bool) == Some(true),
                        write: mailbox_access(m) == "write",
                    }
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Every mailbox id the gateway knows (an added id never reuses one).
fn taken_ids(v: &Value) -> Vec<String> {
    v.get("items")
        .and_then(Value::as_array)
        .map(|items| items.iter().map(|m| st(m, "account_id")).collect())
        .unwrap_or_default()
}

impl OtherMailbox {
    /// The web's state words (`MBX_STATE`); "not in use" when its Active is off.
    pub fn state_words(&self) -> String {
        if !self.enabled {
            return "not in use".into();
        }
        match self.state.as_str() {
            "connected" => "connected".into(),
            "receive_only" => "connected, can't send".into(),
            "paused" => "paused".into(),
            "needs_reconnect" => "needs reconnecting".into(),
            "unavailable" => "turned off by your admin".into(),
            "not_connected" => "not connected".into(),
            "" => "unknown".into(),
            other => other.to_string(),
        }
    }

    /// ONE line: "address · provider · state · Read only|Read and write".
    pub fn line(&self) -> String {
        let who = if !self.name.is_empty() && !self.name.eq_ignore_ascii_case(&self.address) {
            format!("{} ({})", self.name, self.address)
        } else if self.address.is_empty() {
            self.id.clone()
        } else {
            self.address.clone()
        };
        let mut parts = vec![who];
        let provider = match self.provider.as_str() {
            "google" => "Gmail",
            "microsoft" => "Outlook",
            "yahoo" => "Yahoo Mail",
            "icloud" => "iCloud Mail",
            "fastmail" => "Fastmail",
            _ => "",
        };
        if !provider.is_empty() {
            parts.push(provider.into());
        }
        parts.push(self.state_words());
        parts.push(access_words(if self.write { "write" } else { "read" }).into());
        parts.join(" · ")
    }

    fn label(&self) -> String {
        if self.address.is_empty() {
            self.id.clone()
        } else {
            self.address.clone()
        }
    }
}

/// "Add mailbox"'s fields (modal scope: they survive every re-render).
#[derive(Clone, Copy)]
pub(super) struct AddForm {
    pub(super) open: Signal<bool>,
    pub(super) address: Signal<String>,
    password: Signal<String>,
    write: Signal<bool>,
    pub(super) servers: ServerSignals,
    pub(super) servers_shown: Signal<bool>,
    addr_gen: Signal<u64>,
}

impl AddForm {
    pub(super) fn new(cx: Scope) -> AddForm {
        AddForm {
            open: cx.signal(false),
            address: cx.signal(String::new()),
            password: cx.signal(String::new()),
            write: cx.signal(false),
            servers: ServerSignals::new(cx),
            servers_shown: cx.signal(false),
            addr_gen: cx.signal(0),
        }
    }

    /// A fresh form (opened again after an add or a Cancel).
    fn reset(&self) {
        self.address.set(String::new());
        self.password.set(String::new());
        self.write.set(false);
        self.servers_shown.set(false);
        self.servers.edited.set(0);
        for s in [
            self.servers.imap_host,
            self.servers.imap_port,
            self.servers.smtp_host,
            self.servers.smtp_port,
            self.servers.login,
        ] {
            s.set(String::new());
        }
        self.servers.imap_security.set("ssl".into());
        self.servers.smtp_security.set("ssl".into());
    }
}

/// Read the list once the page opens, and route this page's mailbox writes
/// into its one status line.
pub(super) fn install(mcx: Scope, ctx: &Ctx, p: Page) {
    ctx.store.json.set_write(K_ACCOUNTS_WRITE, None);
    ctx.send(Cmd::Json(JsonCmd::get(K_ACCOUNTS, ACCOUNTS_PATH)));
    let store = ctx.store;
    mcx.effect(move || {
        let out = match store.json.write(K_ACCOUNTS_WRITE) {
            Some(WriteState::Done(v)) => Ok(v),
            Some(WriteState::Failed(e)) => Err(email_error_text(&e)),
            _ => return,
        };
        store.json.set_write(K_ACCOUNTS_WRITE, None);
        let landed = p.busy.get_untracked();
        p.busy.set(None);
        match out {
            // A 2xx that says it did not work is said as the failure it is.
            Ok(v) if v.get("ok").and_then(Value::as_bool) == Some(false) => {
                let why = ["message", "cause", "error"]
                    .iter()
                    .map(|k| st(&v, k))
                    .find(|m| !m.is_empty())
                    .unwrap_or_else(|| "The gateway did not save it.".into());
                p.status.set(Some(Err(why)));
            }
            Ok(_) => {
                if landed == Some("add") {
                    p.add.open.set(false);
                    p.add.reset();
                }
                let text = p
                    .pending_ok
                    .get_untracked()
                    .unwrap_or_else(|| "Saved.".into());
                p.status.set(Some(Ok(text)));
            }
            Err(e) => p.status.set(Some(Err(e))),
        }
    });
}

/// Send one of this section's writes (one at a time, like the page's own).
fn write(
    ctx: &Ctx,
    p: Page,
    key: &'static str,
    path: String,
    body: Value,
    label: String,
    ok: String,
) {
    if p.busy.get_untracked().is_some() {
        p.status.set(Some(Err(
            "Still saving the last change — try again in a moment.".into(),
        )));
        return;
    }
    p.status.set(None);
    p.pending_ok.set(Some(ok));
    p.busy.set(Some(key));
    ctx.store
        .json
        .set_write(K_ACCOUNTS_WRITE, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: K_ACCOUNTS_WRITE.into(),
        method: "PUT".into(),
        path,
        body,
        // A connect tests the servers; turning Read and write on tests the outgoing one.
        slow: key != "enabled",
        label,
        reload: vec![(K_ACCOUNTS.into(), ACCOUNTS_PATH.into())],
        journal: true,
    }));
}

fn account_path(id: &str, sub: &str) -> String {
    format!("{ACCOUNTS_PATH}/{}{sub}", crate::api::urlencode(id))
}

/// The other mailboxes, then (when `can_add`) "Add mailbox" or its form.
pub(super) fn section(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page, can_add: bool) -> View {
    let t0 = *t;
    let store = ctx.store;
    // Rebuilt only when the set of mailboxes changes: a switch press keeps its focus.
    let ids = cx.memo(move || match store.json.get(K_ACCOUNTS) {
        Loadable::Ready(v) => Ok(other_mailboxes(&v)
            .into_iter()
            .map(|m| m.id)
            .collect::<Vec<_>>()),
        Loadable::Failed(e) => Err(Some(email_error_text(&e))),
        _ => Err(None),
    });
    let ctx_rows = ctx.clone();
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |rcx| {
                let t = t0;
                match ids.get() {
                    Ok(ids) if ids.is_empty() => {
                        Element::new().style(LayoutStyle::default().h(0)).build()
                    }
                    Ok(ids) => {
                        let mut c = Element::new()
                            .style(LayoutStyle::column().gap(0).shrink(0.0))
                            .child(gap());
                        for id in ids {
                            c = c.child(row(rcx, &ctx_rows, &t, p, id));
                        }
                        c.build()
                    }
                    Err(Some(e)) => {
                        helper(&t, &format!("Could not list your mailboxes: {e}"), p.wrap_w)
                    }
                    Err(None) => line(vec![span("\u{25cc} Reading your mailboxes...", t.info)]),
                }
            },
        ));
    if !can_add {
        return col.build();
    }
    let ctx_add = ctx.clone();
    col = col.child(gap()).child(dyn_view_scoped(
        LayoutStyle::column().gap(0).shrink(0.0),
        move |acx| {
            let t = t0;
            if p.add.open.get() {
                return add_form(acx, &ctx_add, &t, p);
            }
            Button::new(ADD_MAILBOX)
                .on_click(move || {
                    p.add.reset();
                    p.status.set(None);
                    p.add.open.set(true);
                })
                .element(acx, &t)
                .build()
        },
    ));
    col.build()
}

/// One other mailbox: its line, then Active and Read and write.
fn row(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page, id: String) -> View {
    let t0 = *t;
    let store = ctx.store;
    let find = {
        let id = id.clone();
        move |tracked: bool| -> Option<OtherMailbox> {
            let v = if tracked {
                store.json.get(K_ACCOUNTS)
            } else {
                store.json.get_untracked(K_ACCOUNTS)
            };
            match v {
                Loadable::Ready(v) => other_mailboxes(&v).into_iter().find(|m| m.id == id),
                _ => None,
            }
        }
    };
    let first = find(false);
    let active = cx.signal(first.as_ref().map(|m| m.enabled).unwrap_or(false));
    let rw = cx.signal(first.as_ref().map(|m| m.write).unwrap_or(false));
    // The switches follow the gateway's answer (also after a refused write).
    {
        let find = find.clone();
        cx.effect(move || {
            if let Some(m) = find(true) {
                active.set(m.enabled);
                rw.set(m.write);
            }
        });
    }
    let text = {
        let find = find.clone();
        dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
            let t = t0;
            let m = find(true);
            let warn = m
                .as_ref()
                .map(|m| {
                    m.enabled && matches!(m.state.as_str(), "receive_only" | "needs_reconnect")
                })
                .unwrap_or(false);
            let words = m.map(|m| m.line()).unwrap_or_default();
            line(vec![span_bold(words, if warn { t.warn } else { t.text })])
        })
    };
    let (c_use, c_rw) = (ctx.clone(), ctx.clone());
    let (id_use, id_rw) = (id.clone(), id);
    let (f_use, f_rw) = (find.clone(), find);
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(text)
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(3).h(1).shrink(0.0))
                .child(Element::new().style(LayoutStyle::default().w(1).h(1).shrink(0.0)).build())
                .child(
                    crate::ui::w::Toggle::switch("Active", active)
                        .busy_when(move || p.busy.get() == Some("enabled"))
                        .on_change(move |want| {
                            let who = f_use(false).map(|m| m.label()).unwrap_or_else(|| id_use.clone());
                            write(
                                &c_use,
                                p,
                                "enabled",
                                account_path(&id_use, "/enabled"),
                                json!({ "enabled": want }),
                                format!("Mailbox active: {who}"),
                                format!("{who}: Active {}.", if want { "on" } else { "off" }),
                            )
                        })
                        .view(cx, &t0),
                )
                .child(
                    crate::ui::w::Toggle::switch(crate::store::email::ACCESS_WRITE, rw)
                        .busy_when(move || p.busy.get() == Some("access"))
                        .on_change(move |want| {
                            let who = f_rw(false).map(|m| m.label()).unwrap_or_else(|| id_rw.clone());
                            write(
                                &c_rw,
                                p,
                                "access",
                                account_path(&id_rw, "/access"),
                                json!({ "write": want }),
                                format!("Mailbox read and write: {who}"),
                                if want {
                                    format!("{who}: Read and write \u{2014} automations can change it and send from it.")
                                } else {
                                    format!("{who}: Read only \u{2014} automations read it and never change it or send from it.")
                                },
                            )
                        })
                        .view(cx, &t0),
                )
                .build(),
        )
        .build()
}

/// The address changed: the standard servers at once (fields nobody edited),
/// the gateway's discovery ~400 ms after the last edit.
fn address_changed(ctx: &Ctx, p: Page, address: &str) {
    if let Some(st) = ServerDefaults::standard(address) {
        p.add.servers.fill(&st);
    }
    let gen = p.add.addr_gen.get_untracked() + 1;
    p.add.addr_gen.set(gen);
    let ctx = ctx.clone();
    abstracttui::reactive::after(std::time::Duration::from_millis(400), move || {
        if p.add.addr_gen.get_untracked() == gen && p.add.open.get_untracked() {
            discover(&ctx, &p.add.address.get_untracked());
        }
    });
}

/// "Add mailbox": another mailbox, by password.
fn add_form(cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let a = p.add;
    let w = (p.wrap_w as i32 - 22).clamp(16, 40);
    let ctx_addr = ctx.clone();
    let ctx_disc = ctx.clone();
    let ctx_go = ctx.clone();
    let store = ctx.store;
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(vec![span_bold(ADD_MAILBOX, t0.text)]))
        .child(field(
            &t0,
            "Email address",
            input(a.address, w, false)
                .on_change(move |s| address_changed(&ctx_addr, p, s))
                .on_submit(move |s| discover(&ctx_disc, s))
                .element(cx, &t0)
                .build(),
        ))
        .child(super::found_line(store, &t0, p, a.address))
        .child(field(
            &t0,
            "Password",
            input(a.password, w, true).element(cx, &t0).build(),
        ))
        .child(helper(&t0, PASSWORD_HELP, p.wrap_w))
        .child(
            crate::ui::w::Toggle::switch(crate::store::email::ACCESS_WRITE, a.write)
                .on_change(move |want| a.write.set(want))
                .view(cx, &t0),
        )
        .child(helper(&t0, &format!("    {WRITE_HELP}"), p.wrap_w))
        .child(crate::ui::w::action::button(
            cx,
            &t0,
            &crate::ui::w::Action::link("add_server_settings", SERVER_SETTINGS)
                .tooltip(format!("{SERVER_SETTINGS}  ({SERVER_SETTINGS_KEY_LABEL})")),
            crate::ui::w::action::On::Page,
            true,
            move || a.servers_shown.set(!a.servers_shown.get_untracked()),
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |lcx| {
                let t = t0;
                if !a.servers_shown.get() {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                // The outgoing server only when it may send (Read and write).
                let smtp = a.write.get();
                if a.servers.login.with_untracked(String::is_empty) {
                    a.servers
                        .login
                        .set(a.address.get_untracked().trim().to_string());
                }
                Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .child(server_rows(lcx, &t, a.servers, p.wrap_w, smtp))
                    .child(field(
                        &t,
                        "Sign-in name",
                        input(a.servers.login, w, false)
                            .on_change(move |_| a.servers.mark(E_LOGIN))
                            .element(lcx, &t)
                            .build(),
                    ))
                    .child(helper(&t, SIGN_IN_NAME_HELP, p.wrap_w))
                    .build()
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::row().gap(2).h(1).shrink(0.0),
            move |bcx| {
                let t = t0;
                let busy = p.busy.get() == Some("add");
                let ctx_go = ctx_go.clone();
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new(if busy { "Adding…" } else { ADD_MAILBOX })
                            .on_click(move || add(&ctx_go, p))
                            .element(bcx, &t)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel")
                            .on_click(move || {
                                if p.busy.get_untracked() == Some("add") {
                                    return;
                                }
                                a.open.set(false);
                                a.reset();
                            })
                            .element(bcx, &t)
                            .build(),
                    )
                    .build()
            },
        ))
        .build()
}

/// ONE "Add mailbox": test, then store, under an id derived from the address.
fn add(ctx: &Ctx, p: Page) {
    if p.busy.get_untracked().is_some() {
        return;
    }
    let a = p.add;
    let address = a.address.get_untracked().trim().to_string();
    let write_on = a.write.get_untracked();
    // The sign-in name only when the person changed it (otherwise the address signs in).
    let login =
        (a.servers.edited.get_untracked() & E_LOGIN != 0).then(|| a.servers.login.get_untracked());
    let body = match add_mailbox_body(
        &address,
        &a.password.get_untracked(),
        &a.servers.get(),
        login.as_deref(),
        write_on,
    ) {
        Ok(b) => b,
        Err(msg) => {
            p.status.set(Some(Err(msg)));
            return;
        }
    };
    let taken = match ctx.store.json.get_untracked(K_ACCOUNTS) {
        Loadable::Ready(v) => taken_ids(&v),
        _ => Vec::new(),
    };
    let id = mailbox_id_from_address(&address, &taken);
    write(
        ctx,
        p,
        "add",
        account_path(&id, ""),
        body,
        format!("Add mailbox {address}"),
        format!(
            "Mailbox added: {address} \u{00b7} {}.",
            access_words(if write_on { "write" } else { "read" })
        ),
    );
}
