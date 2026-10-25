//! Triggers (WORK) — the web console's page of the same name
//! (`console_triggers.py`, root backlog 1041), same routes, same words.
//!
//! A trigger source is a type (email received, feed item, webhook
//! received); a CONNECTION is one person's instance of it (this mailbox,
//! this feed, this webhook), created once here and reused by every
//! automation and flow of its owner.
//!
//! The page: the web's purpose sentence; [New connection] [Refresh] and,
//! for admins, the **All accounts** toggle; a `DataTable` Name (the name and
//! the trigger sentence) · (Account) · Status · Listening · **Handled by**
//! (own view only: the flows of the automations handling it, a paused one
//! "<flow> (paused)" muted, "Nothing yet" when none, empty while the
//! automations are not read) · Last event (a row note below 100 columns) ·
//! [Use in a flow] [Events] [Test] [Delete]. Use in a flow (`f`) is the web's
//! menu: "New flow for this trigger…" (Flow's link, with Copy), then every
//! published triggered flow, "✓ <flow>" when it already handles the trigger
//! (plus every handler whose flow is not published, by its flow name). An
//! unchecked pick creates the automation; a checked one opens that flow's
//! automations on this trigger, each with Pause (Resume when paused) and
//! Remove (asked first, inline), sent as `POST /automations/{id}/commands`
//! (`automation.pause|resume|archive`), then the automations are read again.
//! Events opens
//! the connection's 20 most recent events (newest first, each with its
//! whole JSON); Test sends a test event and says "Test event #N sent." in
//! the row's note; Delete asks the web's question first. New connection is
//! a form modal: the sources whose descriptor says `needs_connection`, a
//! Name, and the source's settings from its `connection_schema` (the web's
//! form subset); a webhook's address and one-time signing secret are shown
//! once with Copy buttons.
//!
//! Every read and write goes through the plain JSON lane (`api_json.rs`) on
//! the web page's routes: GET /trigger-sources, GET|POST
//! /triggers/connections (`?all=true` for an admin), DELETE
//! /triggers/connections/{id}, GET …/{id}/events?limit=20, POST
//! …/{id}/test-fire; GET /automations, POST /automations, POST
//! /automations/{id}/commands; GET /bundles.

use std::cell::RefCell;
use std::rc::Rc;

use abstracttui::app::select::{Select, SelectOption};
use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use serde_json::{json, Value};

use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Cell, Col, ColW, DataTable, Ink, Row as WRow, Toggle};
use super::Ctx;
use crate::store::json::WriteState;
use crate::store::{ConnPhase, Loadable};
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

pub const TITLE: &str = "Triggers";
pub const SUBTITLE: &str =
    "Connections that can start your automations: mailboxes, feeds, webhooks";
pub const NAV_TIP: &str = "Mailboxes, feeds and webhooks that can start your automations";
pub const PURPOSE: &str = "A connection is something of yours the gateway listens to: a mailbox, a feed, a webhook address. Flows and automations use it by name.";
/// The webhook setup recipe (the web's setup panel).
pub const WEBHOOK_RECIPE: &str = "GitHub, Zapier and other services: paste the webhook address as the webhook URL, the signing secret as the secret, and choose JSON as the content type.";
pub const WEBHOOK_SHELL: &str =
    "Any other sender signs each call per Standard Webhooks. From a shell (bash, openssl):";
/// The primary mailbox's badge (the web's, as in My email).
pub const SENDS_AS_YOU: &str = "Sends as you";
/// The mailbox rows' link (the web's words).
pub const MANAGE_MAILBOX: &str = "Manage in My email";
/// The line under the list (the web's).
pub const LISTEN_HELP: &str =
    "A connection only fetches while an automation uses it. Pause it to stop fetching.";
pub const K_PAUSE: &str = "triggers.pause";
pub const K_MAILBOXES: &str = "triggers.mailboxes";
pub const K_AUTOS: &str = "triggers.automations";
/// Active AND paused: Handled by lists a paused automation with "(paused)".
pub const AUTOS_PATH: &str = "/automations?status=active,paused&limit=200";
/// Your published flows (Use in a flow lists the triggered ones).
pub const K_FLOWS: &str = "triggers.flows";
pub const FLOWS_PATH: &str = "/bundles";
/// The automation a Use in a flow pick creates (`POST /automations`).
pub const K_USE: &str = "triggers.use";
pub const USE_IN_FLOW: &str = "Use in a flow";
pub const NEW_FLOW: &str = "New flow for this trigger…";
/// The Handled by column (own view) and its empty cell.
pub const HANDLED_BY: &str = "Handled by";
pub const NOTHING_YET: &str = "Nothing yet";
/// A Pause / Resume / Remove on a handler (`POST /automations/{id}/commands`), the web's words:
/// `<title>` is the automation's title, `<label>` the trigger's, `<reason>` the refusal.
pub const K_CMD: &str = "triggers.command";
pub const SAID_PAUSED: &str = "<title> is paused.";
pub const SAID_RESUMED: &str = "<title> is on again.";
pub const SAID_REMOVED: &str = "<title> no longer handles <label>.";
pub const NOT_PAUSED: &str = "<title> was not paused: <reason>";
pub const NOT_RESUMED: &str = "<title> was not resumed: <reason>";
pub const NOT_REMOVED: &str = "<title> was not removed: <reason>";
pub const SESSION_EXPIRED: &str = "Your session expired: sign in again, then try again.";
pub const REMOVE_QUESTION: &str =
    "Remove <title>? It stops handling <label>; its history stays in Observer.";
/// The second re-read after a command's receipt (the runner applies it shortly after).
pub const REREAD_AFTER: std::time::Duration = std::time::Duration::from_millis(1500);
pub const FLOWS_HEAD: &str = "Or let one of your triggered flows handle it";
pub const NO_FLOWS: &str = "No triggered flow yet: start with a new one.";
pub const NEW_FLOW_HELP: &str = "A terminal cannot open Flow: open this link in your browser.";
const MAILBOXES_PATH: &str = "/me/email/accounts";
pub const NEW: &str = "New connection";
pub const REFRESH: &str = "Refresh";
pub const REFRESH_TIP: &str = "Read the connections again";
pub const ALL: &str = "All accounts";
pub const EMPTY: &str = "Nothing to listen to yet. Press n to add a feed or a webhook; add a mailbox in My email (Accounts, @).";
pub const EMPTY_ALL: &str = "No account has a connection yet.";
pub const LOADING: &str = "Reading the connections...";
/// The own view waits for the connections AND the mailboxes (never an empty list meanwhile).
pub const LOADING_OWN: &str = "Reading your connections and mailboxes...";
pub const EVENTS_EMPTY: &str = "No event yet. Send a test event to see one here.";
pub const EVENTS_LOADING: &str = "Reading the events...";
pub const SECRET_ONCE: &str = "The signing secret is shown once. Copy it now: the gateway keeps it sealed and never shows it again.";
pub const WEBHOOK_HELP: &str = "Senders sign each request with this connection's secret (Standard Webhooks). The secret was shown once, when the connection was created.";
pub const NO_SOURCE: &str = "No trigger source on this gateway needs a connection.";
pub const CREATED_HELP: &str = "Your automations and flows can now listen to it.";
pub const NAME_HELP: &str = "How you find it in Flow and in your automations.";
pub const NAME_PLACEHOLDER: &str = "Release feed, Orders webhook…";
pub const EVENTS_LIMIT: usize = 20;

/// Page keys only an admin has.
pub const ADMIN_KEYS: &[&str] = &["a"];

pub const K_SOURCES: &str = "triggers.sources";
pub const K_LIST: &str = "triggers.list";
pub const K_EVENTS: &str = "triggers.events";
pub const K_TEST: &str = "triggers.test";
pub const K_DELETE: &str = "triggers.delete";
pub const K_CREATE: &str = "triggers.create";

const SOURCES_PATH: &str = "/trigger-sources";

/// The well-known feeds New connection → Feed proposes (the web's picker):
/// the shipped catalogue merged with the gateway's own
/// `<data_dir>/catalog/feeds.json`.
pub const K_CATALOG: &str = "triggers.feeds";
pub const CATALOG_PATH: &str = "/triggers/feeds/catalog";
pub const FEED_SOURCE: &str = "feed.item";
pub const CATALOG_LABEL: &str = "Choose a well-known feed";
pub const CATALOG_HELP: &str =
    "Pick one to fill in the name and the address, or choose Another address and type it.";
pub const CATALOG_LOADING: &str = "Reading the well-known feeds...";
pub const ANOTHER_ADDRESS: &str = "Another address";
/// Under Check every after a pick filled it with the feed's `check_every`.
pub const EVERY_RECOMMENDED: &str = "Recommended for this feed";

/// The list route (`?all=true`: an admin's All accounts view).
pub fn list_path(all: bool) -> String {
    if all {
        "/triggers/connections?all=true".into()
    } else {
        "/triggers/connections".into()
    }
}

pub fn conn_path(id: &str) -> String {
    format!("/triggers/connections/{}", crate::api::urlencode(id))
}

pub fn events_path(id: &str) -> String {
    format!("{}/events?limit={EVENTS_LIMIT}", conn_path(id))
}

pub fn test_path(id: &str) -> String {
    format!("{}/test-fire", conn_path(id))
}

/// The web's Delete question.
pub fn delete_question(label: &str) -> String {
    format!("Delete {label}? Automations that listen to it stop receiving its events.")
}

/// A row action's tooltip (the web's `data-af-tip`).
pub fn action_tip(id: &str, label: &str) -> String {
    match id {
        "events" => format!("Recent events of {label}"),
        "test" => format!("Send a test event to {label}"),
        _ => format!("Delete {label}"),
    }
}

// ------------------------------------------------------------------ data

/// One connection row (`GET /triggers/connections`).
#[derive(Clone, Debug, PartialEq)]
pub struct ConnRow {
    pub id: String,
    pub source_id: String,
    pub source_version: i64,
    pub label: String,
    pub status: String,
    pub last_event_at: Option<String>,
    pub ingress_url: Option<String>,
    /// The owner string `"<tenant>:<user>"` (All accounts view).
    pub principal: Option<String>,
    /// Runtime state: paused by its owner (None: a gateway that does not report it).
    pub paused: Option<bool>,
    /// Keep fetching even when nothing uses it (Advanced).
    pub always_listen: Option<bool>,
    /// How many automations use it (None: not reported).
    pub used_by: Option<i64>,
    /// The problem the gateway recorded (`{code, cause, fix}`).
    pub last_error: Option<Value>,
}

/// The operator's three states, in the web's words: the Listening switch (`paused`) is the
/// person's and the only control; whether an automation uses it never moves the switch.
pub fn listening_words(
    paused: bool,
    used_by: Option<i64>,
    always_listen: bool,
    names: &[String],
) -> String {
    if paused {
        return "Paused".into();
    }
    // Who uses it is the row's Handled by (names and flows): the status counts the active ones.
    if !names.is_empty() {
        let n = names.len();
        return format!(
            "Listening · used by {n} automation{}",
            if n == 1 { "" } else { "s" }
        );
    }
    match used_by {
        None => "Listening".into(),
        Some(n) if n > 0 => format!(
            "Listening · used by {n} automation{}",
            if n == 1 { "" } else { "s" }
        ),
        Some(_) if always_listen => "Listening · kept on even when nothing uses it".into(),
        Some(_) => "Waiting for an automation to use it".into(),
    }
}

/// What a connection is doing, in the web's words (None: not reported by the gateway).
pub fn doing_text(r: &ConnRow) -> Option<String> {
    doing_text_with(r, &[])
}

/// [`doing_text`] with the names of the active automations that use it.
pub fn doing_text_with(r: &ConnRow, names: &[String]) -> Option<String> {
    let paused = r.paused?;
    Some(listening_words(
        paused,
        r.used_by,
        r.always_listen == Some(true),
        names,
    ))
}

/// The titles of your active automations whose trigger matches (`GET /automations?status=active`).
pub fn automation_users(autos: &Value, matches: impl Fn(&Value) -> bool) -> Vec<String> {
    autos
        .get("items")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|a| a.get("status").and_then(Value::as_str) == Some("active"))
                .filter(|a| a.get("trigger").is_some_and(&matches))
                .map(|a| {
                    st(a, "title")
                        .filter(|t| !t.is_empty())
                        .unwrap_or_else(|| "an automation".into())
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Does an automation's trigger listen to this connection?
pub fn connection_match(id: &str, t: &Value) -> bool {
    t.get("config")
        .and_then(|c| c.get("connection_id"))
        .and_then(Value::as_str)
        == Some(id)
}

/// Does an automation's trigger read this mailbox? ("self", "default" and no account are the
/// primary mailbox, as the gateway's `validate_trigger_account` says.)
pub fn mailbox_match(m: &MailboxRow, t: &Value) -> bool {
    account_match(&m.id, m.primary, t)
}

fn account_match(id: &str, primary: bool, t: &Value) -> bool {
    if t.get("source_id").and_then(Value::as_str) != Some("email.received") {
        return false;
    }
    let account = t
        .get("config")
        .and_then(|c| c.get("account"))
        .and_then(Value::as_str)
        .filter(|a| !a.is_empty())
        .unwrap_or("self");
    account == id || ((account == "self" || account == "default") && primary)
}

pub fn connection_users(r: &ConnRow, autos: &Value) -> Vec<String> {
    automation_users(autos, |t| connection_match(&r.id, t))
}

pub fn mailbox_users(m: &MailboxRow, autos: &Value) -> Vec<String> {
    automation_users(autos, |t| mailbox_match(m, t))
}

// ------------------------------------------- Handled by + Use in a flow

/// One automation that handles a trigger's events (the web's Handled by line).
#[derive(Clone, Debug, PartialEq)]
pub struct Handler {
    pub id: String,
    pub title: String,
    /// The summary's `flow_name` (else the target's flow id; empty when neither).
    pub flow: String,
    pub paused: bool,
    /// The target's bundle id (no version) and flow id: "already handles it" in the menu.
    pub bundle: String,
    pub flow_id: String,
}

impl Handler {
    /// "<title> · <flow>" (the title alone when it IS the flow's name), "(paused)" after a paused one.
    pub fn text(&self) -> String {
        let base = if self.flow.is_empty() || self.flow == self.title {
            self.title.clone()
        } else {
            format!("{} · {}", self.title, self.flow)
        };
        if self.paused {
            format!("{base} (paused)")
        } else {
            base
        }
    }
    /// The flow's name (the title when the automation names no flow).
    pub fn name(&self) -> String {
        if self.flow.is_empty() {
            self.title.clone()
        } else {
            self.flow.clone()
        }
    }
    /// Does this automation run that published flow?
    pub fn runs(&self, f: &TriggeredFlow) -> bool {
        self.bundle == f.bundle && self.flow_id == f.flow_id
    }
}

/// Your automations (active and paused) on a trigger; None when the list was not read.
pub fn handlers(autos: &Value, matches: impl Fn(&Value) -> bool) -> Option<Vec<Handler>> {
    let items = autos.get("items").and_then(Value::as_array)?;
    Some(
        items
            .iter()
            .filter(|a| {
                matches!(
                    a.get("status").and_then(Value::as_str),
                    Some("active" | "paused")
                )
            })
            .filter(|a| a.get("trigger").is_some_and(&matches))
            .map(|a| {
                let target = a.get("target").cloned().unwrap_or(Value::Null);
                let flow_id = st(&target, "flow_id").unwrap_or_default();
                Handler {
                    id: st(a, "automation_id").unwrap_or_default(),
                    title: st(a, "title")
                        .filter(|t| !t.is_empty())
                        .unwrap_or_else(|| "an automation".into()),
                    flow: st(a, "flow_name")
                        .filter(|f| !f.is_empty())
                        .unwrap_or_else(|| flow_id.clone()),
                    paused: a.get("status").and_then(Value::as_str) == Some("paused"),
                    bundle: st(&target, "bundle_ref")
                        .unwrap_or_default()
                        .split('@')
                        .next()
                        .unwrap_or("")
                        .to_string(),
                    flow_id,
                }
            })
            .collect(),
    )
}

/// The Handled by cell: one line per handler (its flow's name, "<flow> (paused)" for a paused
/// one), `(text, muted)`; "Nothing yet" (muted) when none; no line at all when the automations
/// were not read (never a false "Nothing yet").
pub fn handled_by(handlers: Option<&[Handler]>) -> Vec<(String, bool)> {
    let Some(hs) = handlers else {
        return Vec::new();
    };
    if hs.is_empty() {
        return vec![(NOTHING_YET.to_string(), true)];
    }
    hs.iter()
        .map(|h| {
            if h.paused {
                (format!("{} (paused)", h.name()), true)
            } else {
                (h.name(), false)
            }
        })
        .collect()
}

/// One choice of the Use in a flow menu after "New flow for this trigger…": a published
/// triggered flow (`flow`), or a flow that handles the trigger but is not published (`None`);
/// checked when `handlers` (its automations on this trigger) is not empty.
#[derive(Clone, Debug, PartialEq)]
pub struct MenuItem {
    pub name: String,
    pub flow: Option<TriggeredFlow>,
    pub handlers: Vec<Handler>,
}

impl MenuItem {
    pub fn checked(&self) -> bool {
        !self.handlers.is_empty()
    }
    /// "✓ <name>" when it already handles the trigger.
    pub fn label(&self) -> String {
        if self.checked() {
            format!("✓ {}", self.name)
        } else {
            self.name.clone()
        }
    }
}

/// The menu's flows: every published triggered flow (by name), then every handler whose flow is
/// not among them, grouped by its flow's name, so every handler is reachable.
pub fn menu_items(flows: &[TriggeredFlow], hs: &[Handler]) -> Vec<MenuItem> {
    let mut out: Vec<MenuItem> = flows
        .iter()
        .map(|f| MenuItem {
            name: f.name.clone(),
            flow: Some(f.clone()),
            handlers: hs.iter().filter(|h| h.runs(f)).cloned().collect(),
        })
        .collect();
    let mut rest: Vec<MenuItem> = Vec::new();
    for h in hs.iter().filter(|h| !flows.iter().any(|f| h.runs(f))) {
        let name = h.name();
        match rest.iter_mut().find(|m| m.name == name) {
            Some(m) => m.handlers.push(h.clone()),
            None => rest.push(MenuItem {
                name,
                flow: None,
                handlers: vec![h.clone()],
            }),
        }
    }
    rest.sort_by(|a, b| a.name.cmp(&b.name));
    out.extend(rest);
    out
}

/// What a checked item's automation can be asked (`POST /automations/{id}/commands`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HandlerCmd {
    Pause,
    Resume,
    /// Remove = archive: the gateway has no DELETE /automations (Observer says Archive).
    Remove,
}

impl HandlerCmd {
    /// The command's `type`.
    pub fn kind(self) -> &'static str {
        match self {
            HandlerCmd::Pause => "automation.pause",
            HandlerCmd::Resume => "automation.resume",
            HandlerCmd::Remove => "automation.archive",
        }
    }
}

/// `/automations/{id}/commands`.
pub fn command_path(automation_id: &str) -> String {
    format!(
        "/automations/{}/commands",
        crate::api::urlencode(automation_id)
    )
}

/// The command body (the web's): `{command_id, type, payload: {}}`.
pub fn command_body(cmd: HandlerCmd, command_id: &str) -> Value {
    json!({"command_id": command_id, "type": cmd.kind(), "payload": {}})
}

fn words(template: &str, title: &str, label: &str, reason: &str) -> String {
    template
        .replace("<title>", title)
        .replace("<label>", label)
        .replace("<reason>", reason)
}

/// The sentence after a command's receipt.
pub fn command_said(cmd: HandlerCmd, title: &str, label: &str) -> String {
    let t = match cmd {
        HandlerCmd::Pause => SAID_PAUSED,
        HandlerCmd::Resume => SAID_RESUMED,
        HandlerCmd::Remove => SAID_REMOVED,
    };
    words(t, title, label, "")
}

/// A refused command, in words: a 401 is the expired session; otherwise the gateway's own
/// sentence (a 409 `invalid_state` says why) is the reason.
pub fn command_refusal(e: &crate::api::ApiError, cmd: HandlerCmd, title: &str) -> String {
    if e.kind == crate::api::ApiErrorKind::Unauthorized {
        return SESSION_EXPIRED.into();
    }
    let t = match cmd {
        HandlerCmd::Pause => NOT_PAUSED,
        HandlerCmd::Resume => NOT_RESUMED,
        HandlerCmd::Remove => NOT_REMOVED,
    };
    words(t, title, "", &refusal(e))
}

/// Remove's question (asked inline, in the menu).
pub fn remove_question(title: &str, label: &str) -> String {
    words(REMOVE_QUESTION, title, label, "")
}

/// A checked item's automation actions: Pause (Resume when paused) and Remove.
pub fn handler_actions(h: &Handler) -> Vec<Action> {
    vec![
        if h.paused {
            Action::label("resume", "Resume").tooltip(format!("Turn {} on again", h.title))
        } else {
            Action::label("pause", "Pause").tooltip(format!("Pause {}", h.title))
        },
        Action::label("remove", "Remove").danger().tooltip(format!(
            "Remove {}: it stops handling this trigger",
            h.title
        )),
    ]
}

/// Remove's inline answer buttons.
pub fn remove_actions() -> Vec<Action> {
    vec![
        Action::label("remove_yes", "Remove").danger(),
        Action::label("remove_no", "Cancel"),
    ]
}

/// A row's key as Flow's `?trigger=` value: the connection id, `mail:<mailbox id>` for a mailbox.
pub fn trigger_key(kind_mailbox: bool, id: &str) -> String {
    if kind_mailbox {
        format!("mail:{id}")
    } else {
        id.to_string()
    }
}

/// Flow's "Open Flow from a trigger" link: `<flow url>/?trigger=<connection id>` or
/// `<flow url>/?trigger=mail:<mailbox id>` (the Flow app served by the gateway at `/apps/flow`).
pub fn new_flow_link(gateway_url: &str, key: &str) -> String {
    let q = match key.strip_prefix("mail:") {
        Some(id) => format!("mail:{}", crate::api::urlencode(id)),
        None => crate::api::urlencode(key),
    };
    format!(
        "{}/apps/flow/?trigger={q}",
        gateway_url.trim_end_matches('/')
    )
}

/// One published triggered flow (`GET /bundles` entrypoint with `triggerable`).
#[derive(Clone, Debug, PartialEq)]
pub struct TriggeredFlow {
    pub workflow_id: String,
    pub bundle: String,
    pub bundle_ref: String,
    pub flow_id: String,
    pub name: String,
    /// The flow's published `automation_defaults` (Null when none).
    pub defaults: Value,
}

/// Your published triggered flows, by name (the web's `trgTriggeredFlows`).
pub fn triggered_flows(v: &Value) -> Result<Vec<TriggeredFlow>, String> {
    let items = v
        .get("items")
        .and_then(Value::as_array)
        .ok_or("the gateway's answer has no items list.")?;
    let mut out = Vec::new();
    for b in items {
        let bundle = st(b, "bundle_id").unwrap_or_default();
        let bundle_ref = st(b, "bundle_ref")
            .unwrap_or_else(|| format!("{bundle}@{}", st(b, "bundle_version").unwrap_or_default()));
        for ep in b
            .get("entrypoints")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if ep.get("triggerable").and_then(Value::as_bool) != Some(true) {
                continue;
            }
            let flow_id = st(ep, "flow_id").unwrap_or_default();
            out.push(TriggeredFlow {
                workflow_id: st(ep, "workflow_id").unwrap_or_default(),
                bundle: bundle.clone(),
                bundle_ref: bundle_ref.clone(),
                name: st(ep, "name")
                    .filter(|n| !n.is_empty())
                    .unwrap_or_else(|| flow_id.clone()),
                defaults: b
                    .get("automation_defaults")
                    .and_then(|d| d.get(&flow_id))
                    .cloned()
                    .unwrap_or(Value::Null),
                flow_id,
            });
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

/// The trigger a pick binds the flow to.
#[derive(Clone, Debug, PartialEq)]
pub struct UseTarget {
    pub mailbox: bool,
    /// The primary mailbox (an automation on "self" reads it).
    pub primary: bool,
    pub id: String,
    pub label: String,
    pub source_id: String,
    pub source_version: i64,
}

impl UseTarget {
    pub fn key(&self) -> String {
        trigger_key(self.mailbox, &self.id)
    }
    /// Does an automation's trigger listen to this target?
    pub fn matches(&self, t: &Value) -> bool {
        if self.mailbox {
            account_match(&self.id, self.primary, t)
        } else {
            connection_match(&self.id, t)
        }
    }
}

/// The automation a pick creates (the web's `trgUseFlowBody`): named after the flow, the flow's
/// defaults' inputs and context (and its trigger settings when made for this kind of trigger),
/// THIS trigger.
pub fn use_flow_body(flow: &TriggeredFlow, target: &UseTarget, request_id: &str) -> Value {
    let d = &flow.defaults;
    let dt = d.get("trigger").cloned().unwrap_or(Value::Null);
    let same = st(&dt, "source_id").as_deref() == Some(target.source_id.as_str());
    let mut config = match (same, dt.get("config")) {
        (true, Some(Value::Object(c))) => c.clone(),
        _ => serde_json::Map::new(),
    };
    config.remove("connection_id");
    config.remove("account");
    let mut trigger =
        json!({"source_id": target.source_id, "source_version": target.source_version});
    if target.mailbox {
        config.insert("account".into(), json!(target.id));
        trigger["config"] = Value::Object(config);
    } else {
        trigger["config"] = Value::Object(config);
        trigger["connection_id"] = json!(target.id);
    }
    let mut body = json!({
        "request_id": request_id,
        "title": flow.name,
        "target": {"bundle_ref": flow.bundle_ref, "flow_id": flow.flow_id,
                   "input_data": d.get("input_data").filter(|v| v.is_object()).cloned().unwrap_or(json!({}))},
        "trigger": trigger,
    });
    if let Some(c) = d.get("context").filter(|c| c.is_object()) {
        body["context"] = c.clone();
    }
    body
}

/// A refused pick, in the web's words (`trgUseFlowRefusal`).
pub fn use_refusal(e: &crate::api::ApiError, flow: &str, label: &str) -> String {
    use crate::api::ApiErrorKind;
    let code = e
        .body
        .as_ref()
        .and_then(|b| b.get("reason_code"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if e.kind == ApiErrorKind::Unauthorized {
        return SESSION_EXPIRED.into();
    }
    match code {
        "email_account_cannot_read" => format!("{flow} cannot read {label}: it has no incoming server. Connect it again with its incoming server (My email)."),
        "email_account_not_found" => format!("{label} is not connected any more: add it again in My email."),
        "connection_not_found" | "connection_not_yours" => format!("{label} is not one of your connections any more."),
        "workflow_refused" | "forbidden" => format!("You may not run {flow} on this gateway: ask its administrator."),
        "unknown_trigger_source" | "unsupported_feature" => format!("This gateway cannot run that trigger: {}", refusal(e)),
        _ if e.kind == ApiErrorKind::Forbidden => format!("You may not run {flow} on this gateway: ask its administrator."),
        _ => format!("{flow} was not turned on: {}", refusal(e)),
    }
}

/// UX #9: the stored error (`last_error {code, cause, fix}`), in the web's words.
pub fn error_text(r: &ConnRow) -> Option<String> {
    let e = r.last_error.as_ref()?;
    let code = st(e, "code").unwrap_or_default();
    let cause = st(e, "cause")
        .or_else(|| st(e, "message"))
        .unwrap_or_default();
    let fix = st(e, "fix").unwrap_or_default();
    if r.source_id == "feed.item" || code.starts_with("feed_") {
        if let Some(i) = cause.find("HTTP ") {
            let n: String = cause[i + 5..].chars().take(3).collect();
            let words = match n.as_str() {
                "400" => " (bad request)",
                "401" => " (unauthorized)",
                "403" => " (forbidden)",
                "404" => " (not found)",
                "410" => " (gone)",
                "429" => " (too many requests)",
                "500" => " (server error)",
                "502" => " (bad gateway)",
                "503" => " (unavailable)",
                _ => "",
            };
            return Some(format!(
                "Can't read the feed: the address answered {n}{words}. Check the feed address."
            ));
        }
        if code == "feed_unreachable" {
            return Some(
                "Can't read the feed: it could not be reached. Check the address and the network."
                    .into(),
            );
        }
        if code == "feed_unreadable" {
            return Some("Can't read the feed: the address does not answer with an RSS or Atom feed. Check the feed address.".into());
        }
        return Some(
            format!("Can't read the feed: {cause} {fix}")
                .trim()
                .to_string(),
        );
    }
    if code.starts_with("webhook_") {
        let why = if code.contains("signature") {
            "the signature did not match the signing secret."
        } else if code.contains("unsigned") {
            "the call was not signed."
        } else if code.contains("stale") {
            "its time stamp is too far from now."
        } else if code.contains("replayed") {
            "this call was already received."
        } else {
            "the gateway did not say why."
        };
        return Some(format!("Last call refused: {why}"));
    }
    let out = format!("{cause} {fix}").trim().to_string();
    (!out.is_empty()).then_some(out)
}

fn st(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).map(str::to_string)
}

/// The rows, or the contract break as a sentence.
pub fn rows_from(v: &Value) -> Result<Vec<ConnRow>, String> {
    let list = v
        .get("connections")
        .and_then(Value::as_array)
        .ok_or("the gateway's answer has no connections list.")?;
    list.iter()
        .map(|r| {
            let id = st(r, "connection_id")
                .filter(|s| !s.is_empty())
                .ok_or("a connection has no connection_id.")?;
            let principal = match r.get("principal") {
                Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
                Some(Value::Object(o)) => o.get("user_id").and_then(Value::as_str).map(|u| {
                    format!(
                        "{}:{u}",
                        o.get("tenant_id")
                            .and_then(Value::as_str)
                            .unwrap_or("default")
                    )
                }),
                _ => None,
            };
            Ok(ConnRow {
                label: st(r, "label")
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| id.clone()),
                id,
                source_id: st(r, "source_id").unwrap_or_default(),
                source_version: r.get("source_version").and_then(Value::as_i64).unwrap_or(0),
                status: st(r, "status").unwrap_or_default(),
                last_event_at: st(r, "last_event_at").filter(|s| !s.is_empty()),
                ingress_url: st(r, "ingress_url").filter(|s| !s.is_empty()),
                principal,
                paused: r.get("paused").and_then(Value::as_bool),
                always_listen: r.get("always_listen").and_then(Value::as_bool),
                used_by: used_by_of(r),
                last_error: r.get("last_error").filter(|e| e.is_object()).cloned(),
            })
        })
        .collect()
}

fn used_by_of(v: &Value) -> Option<i64> {
    match v.get("used_by") {
        Some(Value::Array(a)) => Some(a.len() as i64),
        Some(x) => x.as_i64(),
        None => None,
    }
}

/// The owner as the web shows it (the default tenant implied).
pub fn owner_text(p: Option<&str>) -> String {
    match p {
        Some(s) => s.strip_prefix("default:").unwrap_or(s).to_string(),
        None => "owner not reported".into(),
    }
}

/// A source descriptor (`GET /trigger-sources` item).
#[derive(Clone, Debug, PartialEq)]
pub struct SourceDesc {
    pub id: String,
    pub version: i64,
    pub label: String,
    pub connection_schema: Value,
}

/// The sources a connection can be created for (the web's
/// `trgConnectableSources`): an available source WITHOUT the boolean is a
/// gateway older than connections — said, never read as "needs none".
pub fn connectable_sources(v: &Value) -> Result<Vec<SourceDesc>, String> {
    let items = v
        .get("items")
        .and_then(Value::as_array)
        .ok_or("the gateway's trigger sources have no items list.")?;
    let mut out = Vec::new();
    let mut missing = Vec::new();
    for s in items {
        if s.get("available").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let id = st(s, "id").unwrap_or_default();
        let version = s.get("version").and_then(Value::as_i64).unwrap_or(0);
        match s.get("needs_connection").and_then(Value::as_bool) {
            None => missing.push(format!("{id}@{version}")),
            Some(false) => {}
            // A mailbox-backed source reads the person's mail accounts: its mailboxes are rows of
            // the page, managed under Account → My email (never a New connection).
            Some(true) if st(s, "connection_provider").as_deref() == Some("email_accounts") => {}
            Some(true) => {
                let schema = s.get("connection_schema").filter(|x| x.is_object()).ok_or(format!(
                    "Trigger source {id}@{version} needs a connection but serves no connection_schema object."
                ))?;
                out.push(SourceDesc {
                    label: st(s, "label")
                        .filter(|l| !l.is_empty())
                        .unwrap_or_else(|| id.clone()),
                    id,
                    version,
                    connection_schema: schema.clone(),
                });
            }
        }
    }
    if !missing.is_empty() {
        return Err(format!(
            "This gateway's trigger sources do not say whether they need a connection (needs_connection missing on {}). Update the gateway.",
            missing.join(", ")
        ));
    }
    Ok(out)
}

/// A source's label for a row (its id and version when not served).
pub fn source_label(row: &ConnRow, sources: &Value) -> String {
    sources
        .get("items")
        .and_then(Value::as_array)
        .and_then(|items| {
            items.iter().find(|s| {
                st(s, "id").as_deref() == Some(row.source_id.as_str())
                    && s.get("version").and_then(Value::as_i64) == Some(row.source_version)
            })
        })
        .and_then(|s| st(s, "label"))
        .unwrap_or_else(|| format!("{} v{}", row.source_id, row.source_version))
}

/// The web's status words; anything else verbatim (warn tone).
pub fn status_word(s: &str) -> (String, &'static str) {
    match s.trim().to_ascii_lowercase().as_str() {
        "active" => ("Active".into(), "ok"),
        "paused" => ("Paused".into(), "muted"),
        "error" | "failed" => ("Error".into(), "error"),
        "disabled" => ("Off".into(), "muted"),
        "" => ("unknown".into(), "warn"),
        other => (other.to_string(), "warn"),
    }
}

/// "just now", "3 min ago", "2 h ago", "4 d ago" (the web's `trgAgo`).
pub fn ago(iso: &str, now_s: i64) -> Option<String> {
    let t = crate::localtime::parse_iso_epoch(iso)?;
    let s = (now_s - t).max(0);
    if s < 60 {
        return Some("just now".into());
    }
    let m = (s as f64 / 60.0).round() as i64;
    if m < 60 {
        return Some(format!("{m} min ago"));
    }
    let h = (m as f64 / 60.0).round() as i64;
    if h < 24 {
        return Some(format!("{h} h ago"));
    }
    Some(format!("{} d ago", (h as f64 / 24.0).round() as i64))
}

pub fn last_event_text(r: &ConnRow, now_s: i64) -> String {
    match &r.last_event_at {
        None => "Never".into(),
        Some(t) => ago(t, now_s).unwrap_or_else(|| t.clone()),
    }
}

/// A row's actions (the web's Use in a flow and three buttons).
pub fn row_actions(r: &ConnRow) -> Vec<Action> {
    vec![
        Action::label("flow", USE_IN_FLOW)
            .key('f')
            .tooltip(format!("Handle {}'s events with a flow", r.label)),
        Action::label("events", "Events")
            .key('e')
            .tooltip(action_tip("events", &r.label)),
        Action::label("test", "Test")
            .key('t')
            .tooltip(action_tip("test", &r.label)),
        Action::label("delete", "Delete")
            .key('d')
            .danger()
            .tooltip(action_tip("delete", &r.label)),
    ]
}

pub const LOCAL_WEBHOOK: &str =
    "GitHub can't reach this computer yet. Turn on internet access under Network first.";

/// UX #17: what to do after creating a connection.
pub fn next_step(label: &str) -> String {
    format!("Next: in Flow, + → When something arrives → {label}.")
}

/// UX #15: an address only this computer (or its network) can reach.
pub fn local_address(url: &str) -> bool {
    let rest = url.split("://").nth(1).unwrap_or(url);
    let hostport = rest.split('/').next().unwrap_or("");
    let host = if hostport.starts_with('[') {
        hostport
            .trim_start_matches('[')
            .split(']')
            .next()
            .unwrap_or("")
            .to_string()
    } else {
        hostport.split(':').next().unwrap_or("").to_lowercase()
    };
    if host == "localhost"
        || host == "::1"
        || host == "0.0.0.0"
        || host.ends_with(".local")
        || host.ends_with(".localhost")
    {
        return true;
    }
    let parts: Vec<u32> = host.split('.').filter_map(|x| x.parse().ok()).collect();
    if parts.len() != 4 {
        return false;
    }
    let (a, b) = (parts[0], parts[1]);
    a == 127
        || a == 10
        || (a == 192 && b == 168)
        || (a == 172 && (16..=31).contains(&b))
        || (a == 169 && b == 254)
}

/// The one-line hint per option of "What to listen to" (the web's `TRG_SOURCE_HINTS`).
pub fn source_hint(id: &str) -> &'static str {
    match id {
        "feed.item" => "Runs when a feed (RSS or Atom) publishes a new item.",
        "webhook.received" => {
            "Runs when another service (GitHub, Zapier…) calls an address you give it."
        }
        _ => "",
    }
}

/// A mailbox row's one action (the web's link).
pub fn mailbox_actions() -> Vec<Action> {
    vec![
        Action::label("flow", USE_IN_FLOW)
            .key('f')
            .tooltip("Handle this mailbox's mail with a flow"),
        Action::label("manage", MANAGE_MAILBOX).tooltip("Mailboxes are managed with your account"),
    ]
}

/// The head buttons.
pub fn panel_actions() -> Vec<Action> {
    vec![
        Action::label("refresh", REFRESH).tooltip(REFRESH_TIP),
        Action::label("new", NEW).key('n'),
    ]
}

/// One event's summary line (`#seq type subject [test] when`) and its JSON.
/// UX #10: an event as a sentence ("Test event", "New item: <title>", "Call received").
pub fn event_sentence(e: &Value) -> String {
    let d = e.get("data").cloned().unwrap_or(Value::Null);
    let ty = st(e, "type").unwrap_or_default();
    let subject = st(e, "subject").filter(|s| !s.is_empty());
    if d.get("test").and_then(Value::as_bool) == Some(true) {
        return "Test event".into();
    }
    if ty.contains("feed") {
        let title = st(&d, "title")
            .or(subject)
            .unwrap_or_else(|| "untitled".into());
        return format!("New item: {title}");
    }
    if ty.contains("webhook") {
        return "Call received".into();
    }
    if ty.contains("email") {
        return format!(
            "New mail: {}",
            subject.unwrap_or_else(|| "no subject".into())
        );
    }
    match subject {
        Some(s) => format!("{}: {s}", if ty.is_empty() { "Event" } else { &ty }),
        None => {
            if ty.is_empty() {
                "Event".into()
            } else {
                ty
            }
        }
    }
}

/// "just now" / "12 min ago" within the hour, else "09:00" today, else "2026-10-24 · 09:00".
pub fn event_when(iso: &str, now_s: i64) -> String {
    let Some(t) = crate::localtime::parse_iso_epoch(iso) else {
        return iso.to_string();
    };
    if now_s - t < 3600 {
        return ago(iso, now_s).unwrap_or_default();
    }
    let (day, hm) = crate::localtime::local_parts(iso).unwrap_or_default();
    if day == crate::localtime::local_today() {
        hm
    } else {
        format!("{day} · {hm}")
    }
}

/// One event's sentence line (with its time) and its JSON (shown under it, the web's Details).
pub fn event_lines(events: &[Value], now_s: i64) -> Vec<(String, String)> {
    let mut list: Vec<&Value> = events.iter().collect();
    list.sort_by_key(|e| std::cmp::Reverse(e.get("sequence").and_then(Value::as_i64).unwrap_or(0)));
    list.into_iter()
        .take(EVENTS_LIMIT)
        .map(|e| {
            let mut line = event_sentence(e);
            if let Some(t) = st(e, "time") {
                line = format!("{line} · {}", event_when(&t, now_s));
            }
            (line, serde_json::to_string_pretty(e).unwrap_or_default())
        })
        .collect()
}

/// The result sentence of a test-fire answer.
pub fn test_result(out: &WriteState) -> Option<(String, bool)> {
    match out {
        WriteState::Pending => None,
        WriteState::Done(v) => match (v.get("sequence"), v.get("event_id")) {
            (Some(seq), Some(_)) => Some((format!("Test event #{seq} sent."), true)),
            _ => Some((
                "Test not sent: the gateway's answer has no event_id and sequence.".into(),
                false,
            )),
        },
        WriteState::Failed(e) => Some((format!("Test not sent: {}", refusal(e)), false)),
    }
}

fn refusal(e: &crate::api::ApiError) -> String {
    super::workspace_chooser::error_sentence(e)
}

// ------------------------------------------------------------ form engine

/// One field of a `connection_schema` (the web's `triggerFormFields`).
#[derive(Clone, Debug, PartialEq)]
pub struct FormField {
    pub key: String,
    /// string | password | integer | number | boolean | enum | readonly
    pub kind: &'static str,
    pub label: String,
    pub description: String,
    pub required: bool,
    pub schema: Value,
}

const FORM_KEYWORDS: &[&str] = &[
    "type",
    "title",
    "description",
    "enum",
    "default",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "examples",
    "writeOnly",
    "enumNames",
];

/// A duration's units (the web's `TRG_DURATION_UNITS`): `(suffix, words)`.
pub const DURATION_UNITS: [(&str, &str); 4] = [
    ("s", "seconds"),
    ("m", "minutes"),
    ("h", "hours"),
    ("d", "days"),
];

/// A duration setting (`format: duration`, or the runtime's `^[1-9][0-9]*[smhd]$`): shown as
/// "Check every [15] [minutes]" (the web's control), never as raw "15m".
pub fn is_duration(f: &FormField) -> bool {
    f.kind == "string"
        && (st(&f.schema, "format").as_deref() == Some("duration")
            || st(&f.schema, "pattern").as_deref() == Some("^[1-9][0-9]*[smhd]$"))
}

/// "15m" → ("15", index of minutes); anything else → ("", minutes).
pub fn duration_parts(raw: &str) -> (String, usize) {
    let raw = raw.trim();
    let minutes = 1;
    let Some(last) = raw.chars().last() else {
        return (String::new(), minutes);
    };
    let num = &raw[..raw.len() - last.len_utf8()];
    match DURATION_UNITS.iter().position(|(u, _)| u.starts_with(last)) {
        Some(i)
            if !num.is_empty()
                && num.chars().all(|c| c.is_ascii_digit())
                && !num.starts_with('0') =>
        {
            (num.to_string(), i)
        }
        _ => (String::new(), minutes),
    }
}

/// ("15", minutes) → "15m"; an empty number → "" (not set).
pub fn duration_value(num: &str, unit: usize) -> String {
    let num = num.trim();
    if num.is_empty() {
        String::new()
    } else {
        format!(
            "{num}{}",
            DURATION_UNITS[unit.min(DURATION_UNITS.len() - 1)].0
        )
    }
}

/// Check every after a pick (the web's `trgPickEvery`): `Some((value, recommended))` while the
/// field is the picker's — no pick yet (`auto` is `None`: a value set before any pick is
/// overwritten) or it still holds what the last pick left (`auto`) — else `None` (the person
/// changed it after a pick: it stands). The value is the feed's `check_every`, or `default` (the
/// form's) when the feed has none.
pub fn every_after_pick(
    current: &str,
    auto: Option<&str>,
    check_every: Option<&str>,
    default: &str,
) -> Option<(String, bool)> {
    if auto.is_some_and(|a| a != current) {
        return None;
    }
    Some(match check_every {
        Some(c) => (c.to_string(), true),
        None => (default.to_string(), false),
    })
}

/// An enum's shown names: the schema's `enumNames` (same length), else `None`.
pub fn enum_names(f: &FormField) -> Option<Vec<String>> {
    let vals = f.schema.get("enum").and_then(Value::as_array)?;
    let names = f.schema.get("enumNames").and_then(Value::as_array)?;
    (names.len() == vals.len()).then(|| {
        names
            .iter()
            .map(|n| {
                n.as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| n.to_string())
            })
            .collect()
    })
}

/// A description that only restates what the control shows (a duration's "s, m, h or d", a
/// choice's raw values) is not shown (the web's `trgControlSaysIt`).
pub fn description_shown(f: &FormField) -> bool {
    !f.description.is_empty()
        && !is_duration(f)
        && !(f.kind == "enum" && (enum_names(f).is_some() || f.key == "initial_items"))
}

/// The label of a setting whose schema has no `title` (the web's `TRG_FIELD_LABELS`, then
/// the key in words).
pub fn field_label(key: &str) -> String {
    match key {
        "subject_path" => "Subject (path in the JSON body)".into(),
        "tolerance_s" => "Accepted clock skew (seconds)".into(),
        "url" => "Feed address".into(),
        "every" => "Check every".into(),
        "max_items" => "Items per check (at most)".into(),
        "initial_items" => "When added".into(),
        other => sentence_case(&other.replace('_', " ")),
    }
}

fn sentence_case(s: &str) -> String {
    let t = s.trim();
    let mut c = t.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// The trigger in words (the same words as Flow): never `source_id@version`.
pub fn trigger_phrase(r: &ConnRow, sources: &Value) -> String {
    match r.source_id.as_str() {
        "feed.item" => format!("When {} has a new item", r.label),
        "webhook.received" => format!("When {} is called", r.label),
        _ => source_label(r, sources),
    }
}

/// One mailbox row (`GET /me/email/accounts` item; configured ones only).
#[derive(Clone, Debug, PartialEq)]
pub struct MailboxRow {
    pub id: String,
    pub primary: bool,
    /// The mailbox's name (another mailbox is named by its address).
    pub label: Option<String>,
    pub address: Option<String>,
    pub can_send: bool,
    /// "read" (Read only) | "write" (Read and write): `access`, else from `can_write`.
    pub access: String,
    pub enabled: bool,
    pub state: String,
    pub last_poll: Option<String>,
    pub used_by: Option<i64>,
}

pub fn mailboxes_from(v: &Value) -> Result<Vec<MailboxRow>, String> {
    let items = v
        .get("items")
        .and_then(Value::as_array)
        .ok_or("the gateway's answer has no items list.")?;
    Ok(items
        .iter()
        .filter(|m| m.get("configured").and_then(Value::as_bool) == Some(true))
        .map(|m| MailboxRow {
            id: st(m, "account_id").unwrap_or_default(),
            primary: m.get("primary").and_then(Value::as_bool) == Some(true),
            label: st(m, "name").filter(|a| !a.is_empty()),
            address: st(m, "address").filter(|a| !a.is_empty()),
            can_send: m.get("can_send").and_then(Value::as_bool) == Some(true),
            access: crate::store::email::mailbox_access(m).to_string(),
            enabled: m.get("enabled").and_then(Value::as_bool) == Some(true),
            state: m
                .get("mailbox")
                .and_then(|b| st(b, "state"))
                .unwrap_or_default(),
            last_poll: m.get("watcher").and_then(|w| st(w, "last_poll")),
            used_by: used_by_of(m),
        })
        .collect())
}

impl MailboxRow {
    /// Named like My email: the mailbox's name, else its address (never its derived id).
    pub fn name(&self) -> String {
        self.label
            .clone()
            .or_else(|| self.address.clone())
            .unwrap_or_else(|| self.id.clone())
    }
    /// "When mail arrives in <address> · Read only" / "· Read and write" (the web's words).
    pub fn phrase(&self) -> String {
        format!(
            "When mail arrives in {} · {}",
            self.address.as_deref().unwrap_or(&self.id),
            crate::store::email::access_words(&self.access)
        )
    }
    /// The web's status words (`TRG_MAILBOX_STATE`).
    pub fn status(&self) -> (String, &'static str) {
        if !self.enabled {
            return ("Off".into(), "muted");
        }
        match self.state.as_str() {
            "connected" => ("Active".into(), "ok"),
            // Read and write, but no outgoing server: it can't send.
            "receive_only" => ("Can't send".into(), "warn"),
            "paused" => ("Paused".into(), "muted"),
            "needs_reconnect" => ("Needs reconnecting".into(), "warn"),
            "unavailable" => ("Unavailable".into(), "muted"),
            "not_connected" => ("Not connected".into(), "muted"),
            "" => ("unknown".into(), "warn"),
            other => (other.to_string(), "warn"),
        }
    }
}

/// The signed call the setup panel offers (the web's `webhookCurlExample`), with the real values.
pub fn webhook_curl_example(ingress_url: &str, secret: &str) -> String {
    [
        format!("SECRET='{secret}'"),
        format!("URL='{ingress_url}'"),
        "KEY=$(printf %s \"${SECRET#whsec_}\" | base64 -d | xxd -p -c 256)".to_string(),
        "ID=msg_$(date +%s); TS=$(date +%s); BODY='{\"hello\": \"world\"}'".to_string(),
        "SIG=$(printf %s \"$ID.$TS.$BODY\" | openssl dgst -sha256 -mac HMAC -macopt hexkey:$KEY -binary | base64)".to_string(),
        "curl -sS -X POST \"$URL\" -H \"webhook-id: $ID\" -H \"webhook-timestamp: $TS\" \\".to_string(),
        "  -H \"webhook-signature: v1,$SIG\" -H \"Content-Type: application/json\" --data \"$BODY\"".to_string(),
    ]
    .join("\n")
}

pub fn form_fields(schema: &Value) -> Vec<FormField> {
    let required: Vec<String> = schema
        .get("required")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    let Some(props) = schema.get("properties").and_then(Value::as_object) else {
        return Vec::new();
    };
    let mut fields = props
        .iter()
        // A secret the gateway generates (`x-secret`) is never typed: it is shown once after Create.
        .filter(|(_, p)| p.get("x-secret").and_then(Value::as_bool) != Some(true))
        .map(|(key, p)| {
            let known = p.as_object().is_some_and(|o| {
                o.keys()
                    .all(|k| FORM_KEYWORDS.contains(&k.as_str()) || k.starts_with("x-"))
            });
            let ty = p.get("type").and_then(Value::as_str).unwrap_or("");
            let kind = if !known {
                "readonly"
            } else if p.get("enum").is_some_and(Value::is_array) {
                "enum"
            } else if ty == "string" {
                if st(p, "format").as_deref() == Some("password")
                    || p.get("writeOnly").and_then(Value::as_bool) == Some(true)
                {
                    "password"
                } else {
                    "string"
                }
            } else if ty == "integer" {
                "integer"
            } else if ty == "number" {
                "number"
            } else if ty == "boolean" {
                "boolean"
            } else {
                "readonly"
            };
            FormField {
                key: key.clone(),
                kind,
                label: st(p, "title")
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| field_label(key)),
                description: sentence_case(&st(p, "description").unwrap_or_default()),
                required: required.iter().any(|r| r == key),
                schema: p.clone(),
            }
        })
        .collect::<Vec<_>>();
    // Required settings first (the web shows the schema's order; serde_json sorts keys, so the
    // required Feed address would otherwise come last).
    fields.sort_by_key(|f| !f.required);
    fields
}

/// The options of a choice field: `(raw value, shown label)`, "(not set)" first.
pub fn choice_options(f: &FormField) -> Vec<(String, String)> {
    let mut out = vec![(String::new(), "(not set)".to_string())];
    if f.kind == "boolean" {
        out.push(("true".into(), "Yes".into()));
        out.push(("false".into(), "No".into()));
    } else if let Some(vals) = f.schema.get("enum").and_then(Value::as_array) {
        let names = enum_names(f);
        for (i, v) in vals.iter().enumerate() {
            let shown = match (f.key.as_str(), v.as_str()) {
                _ if names.is_some() => names.as_ref().map(|n| n[i].clone()).unwrap_or_default(),
                ("initial_items", Some("skip")) => "Ignore what is already there".to_string(),
                ("initial_items", Some("deliver")) => "Also deliver the latest items".to_string(),
                _ => v
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| v.to_string()),
            };
            out.push((v.to_string(), shown));
        }
    }
    out
}

/// A field's initial raw value (its schema default; never for a secret).
pub fn initial_value(f: &FormField) -> String {
    match (f.kind, f.schema.get("default")) {
        ("password", _) | (_, None) => String::new(),
        ("enum", Some(d)) => d.to_string(),
        ("boolean", Some(Value::Bool(b))) => b.to_string(),
        (_, Some(Value::String(s))) => s.clone(),
        (_, Some(d)) => d.to_string(),
    }
}

/// Raw values → the config, and the web's problem sentences.
pub fn config_from(fields: &[FormField], values: &[(String, String)]) -> (Value, Vec<String>) {
    let mut config = serde_json::Map::new();
    let mut problems = Vec::new();
    for f in fields {
        if f.kind == "readonly" {
            continue;
        }
        let raw = values
            .iter()
            .find(|(k, _)| *k == f.key)
            .map(|(_, v)| v.trim().to_string())
            .unwrap_or_default();
        if raw.is_empty() {
            if f.required {
                problems.push(format!("{} is required.", f.label));
            }
            continue;
        }
        match f.kind {
            "integer" | "number" => match raw.parse::<f64>() {
                Ok(n) if n.is_finite() && (f.kind == "number" || n.fract() == 0.0) => {
                    config.insert(
                        f.key.clone(),
                        if f.kind == "integer" {
                            json!(n as i64)
                        } else {
                            json!(n)
                        },
                    );
                }
                _ => problems.push(format!(
                    "{} must be {}.",
                    f.label,
                    if f.kind == "integer" {
                        "a whole number"
                    } else {
                        "a number"
                    }
                )),
            },
            "boolean" => {
                config.insert(f.key.clone(), json!(raw == "true"));
            }
            "enum" => {
                config.insert(
                    f.key.clone(),
                    serde_json::from_str(&raw).unwrap_or(Value::String(raw)),
                );
            }
            _ => {
                if let Some(p) = f.schema.get("pattern").and_then(Value::as_str) {
                    if regex_lite_match(p, &raw) == Some(false) {
                        problems.push(format!("{} does not have the expected form.", f.label));
                        continue;
                    }
                }
                config.insert(f.key.clone(), Value::String(raw));
            }
        }
    }
    (Value::Object(config), problems)
}

/// The patterns sources use in practice: a literal prefix anchor
/// (`^https?://`, `^[a-z]`...) is checked here; anything richer is left to
/// the gateway, which validates the whole config again (None = unchecked).
fn regex_lite_match(pattern: &str, value: &str) -> Option<bool> {
    let body = pattern.strip_prefix('^')?;
    if body == "https?://" {
        return Some(value.starts_with("http://") || value.starts_with("https://"));
    }
    if body
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == ':' || c == '/' || c == '.' || c == '-')
    {
        return Some(value.starts_with(body));
    }
    None
}

/// The create body (the web's `createTriggerConnection`).
pub fn create_body(s: &SourceDesc, label: &str, config: Value) -> Value {
    json!({"source_id": s.id, "source_version": s.version, "label": label, "config": config})
}

// ----------------------------------------------------- the feed catalogue

/// One well-known feed (`GET /triggers/feeds/catalog`, `feeds[]`).
#[derive(Clone, Debug, PartialEq)]
pub struct CatalogFeed {
    pub id: String,
    pub name: String,
    pub url: String,
    pub site: String,
    pub category: String,
    pub description: String,
    /// A template's one value (`template: true`): the address holds `{key}`.
    pub placeholder: Option<Placeholder>,
    pub name_template: Option<String>,
    /// The recommended interval (`check_every`, e.g. "15m"; a pick prefills Check every with
    /// it). `None` when absent or not a duration (a custom entry may omit it).
    pub check_every: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Placeholder {
    pub key: String,
    pub label: String,
    pub example: String,
    pub pattern: Option<String>,
}

/// The catalogue's feeds; an entry without id, name or url is skipped.
pub fn catalog_from(v: &Value) -> Result<Vec<CatalogFeed>, String> {
    let items = v
        .get("feeds")
        .and_then(Value::as_array)
        .ok_or_else(|| "the gateway's answer has no feeds list.".to_string())?;
    Ok(items
        .iter()
        .filter_map(|f| {
            let placeholder = if f.get("template").and_then(Value::as_bool) == Some(true) {
                let p = f.get("placeholder")?;
                Some(Placeholder {
                    key: st(p, "key")?,
                    label: st(p, "label").unwrap_or_else(|| "Value".into()),
                    example: st(p, "example").unwrap_or_default(),
                    pattern: st(p, "pattern"),
                })
            } else {
                None
            };
            Some(CatalogFeed {
                id: st(f, "id")?,
                name: st(f, "name")?,
                url: st(f, "url")?,
                site: st(f, "site").unwrap_or_default(),
                category: st(f, "category").unwrap_or_else(|| "Other".into()),
                description: st(f, "description").unwrap_or_default(),
                placeholder,
                name_template: st(f, "name_template"),
                check_every: st(f, "check_every").filter(|c| !duration_parts(c).0.is_empty()),
            })
        })
        .collect())
}

/// One row of the picker: a category heading (not choosable), a feed
/// (its index in the catalogue), or Another address (the free field).
#[derive(Clone, Debug, PartialEq)]
pub enum CatalogRow {
    Heading(String),
    Feed(usize),
    Another,
}

/// The picker's rows: grouped by category in the catalogue's order, then
/// Another address (the web's optgroups + its last option).
pub fn catalog_rows(feeds: &[CatalogFeed]) -> Vec<CatalogRow> {
    let mut cats: Vec<&str> = Vec::new();
    for f in feeds {
        if !cats.contains(&f.category.as_str()) {
            cats.push(&f.category);
        }
    }
    let mut rows = Vec::new();
    for c in cats {
        rows.push(CatalogRow::Heading(c.to_string()));
        for (i, f) in feeds.iter().enumerate() {
            if f.category == c {
                rows.push(CatalogRow::Feed(i));
            }
        }
    }
    rows.push(CatalogRow::Another);
    rows
}

/// A row's option: the feed's name with its site as the hint (a template
/// ends with "…": it asks for one value).
pub fn catalog_option(row: &CatalogRow, feeds: &[CatalogFeed]) -> SelectOption {
    match row {
        CatalogRow::Heading(c) => {
            SelectOption::keyed(format!("#{c}"), format!("— {c} —")).disabled(true)
        }
        CatalogRow::Feed(i) => {
            let f = &feeds[*i];
            if f.placeholder.is_some() {
                SelectOption::keyed(f.id.clone(), format!("{}…", f.name))
            } else {
                SelectOption::keyed(f.id.clone(), f.name.clone())
                    .hint(site_name(if f.site.is_empty() { &f.url } else { &f.site }))
            }
        }
        CatalogRow::Another => SelectOption::keyed("__another", ANOTHER_ADDRESS),
    }
}

/// `https://www.bbc.co.uk/news` → `bbc.co.uk` (the web's `trgSiteName`).
pub fn site_name(url: &str) -> String {
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    host.strip_prefix("www.").unwrap_or(host).to_string()
}

/// The address of `f` (a template's `{key}` replaced by `value`).
pub fn feed_address(f: &CatalogFeed, value: &str) -> String {
    match &f.placeholder {
        Some(p) => f.url.replace(&format!("{{{}}}", p.key), value),
        None => f.url.clone(),
    }
}

/// The name a pick fills in (a template's `name_template` once its value is typed).
pub fn feed_name(f: &CatalogFeed, value: &str) -> String {
    match (&f.placeholder, &f.name_template) {
        (Some(p), Some(nt)) if !value.is_empty() => nt.replace(&format!("{{{}}}", p.key), value),
        _ => f.name.clone(),
    }
}

/// Why a template's value is refused (the web's `trgFeedParamProblem`).
pub fn feed_param_problem(f: &CatalogFeed, value: &str) -> Option<String> {
    let p = f.placeholder.as_ref()?;
    if value.is_empty() {
        return Some(format!("{} is required.", p.label));
    }
    let ok = p
        .pattern
        .as_deref()
        .and_then(|pat| pattern_match(pat, value))
        .unwrap_or(true);
    (!ok).then(|| format!("{}: not the expected form (e.g. {}).", p.label, p.example))
}

/// An anchored pattern of the catalogue's subset — literals, `[...]`
/// classes (ranges), `{n}` / `{m,n}` / `+` / `*` / `?` — matched whole.
/// None when the pattern uses anything else (left unchecked).
pub fn pattern_match(pattern: &str, value: &str) -> Option<bool> {
    #[derive(Debug)]
    enum Atom {
        Lit(char),
        Class(Vec<(char, char)>),
    }
    let body = pattern.strip_prefix('^')?.strip_suffix('$')?;
    let chars: Vec<char> = body.chars().collect();
    let mut atoms: Vec<(Atom, usize, usize)> = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let atom = match chars[i] {
            '[' => {
                let mut set = Vec::new();
                i += 1;
                while i < chars.len() && chars[i] != ']' {
                    let mut c = chars[i];
                    if c == '\\' {
                        i += 1;
                        c = *chars.get(i)?;
                    }
                    if chars.get(i + 1) == Some(&'-') && chars.get(i + 2).is_some_and(|n| *n != ']')
                    {
                        set.push((c, chars[i + 2]));
                        i += 3;
                    } else {
                        set.push((c, c));
                        i += 1;
                    }
                }
                if i >= chars.len() || set.is_empty() {
                    return None;
                }
                i += 1;
                Atom::Class(set)
            }
            '\\' => {
                let c = *chars.get(i + 1)?;
                if c.is_ascii_alphanumeric() {
                    return None; // \d, \w ...: not in the subset
                }
                i += 2;
                Atom::Lit(c)
            }
            c if "(){}|*+?.$^".contains(c) => return None,
            c => {
                i += 1;
                Atom::Lit(c)
            }
        };
        let (lo, hi) = match chars.get(i) {
            Some('+') => {
                i += 1;
                (1, usize::MAX)
            }
            Some('*') => {
                i += 1;
                (0, usize::MAX)
            }
            Some('?') => {
                i += 1;
                (0, 1)
            }
            Some('{') => {
                let end = chars[i..].iter().position(|c| *c == '}')? + i;
                let inner: String = chars[i + 1..end].iter().collect();
                i = end + 1;
                match inner.split_once(',') {
                    Some((a, b)) => (
                        a.trim().parse().ok()?,
                        if b.trim().is_empty() {
                            usize::MAX
                        } else {
                            b.trim().parse().ok()?
                        },
                    ),
                    None => {
                        let n = inner.trim().parse().ok()?;
                        (n, n)
                    }
                }
            }
            _ => (1, 1),
        };
        atoms.push((atom, lo, hi));
    }
    fn hit(a: &Atom, c: char) -> bool {
        match a {
            Atom::Lit(l) => *l == c,
            Atom::Class(set) => set.iter().any(|(lo, hi)| *lo <= c && c <= *hi),
        }
    }
    fn go(atoms: &[(Atom, usize, usize)], v: &[char]) -> bool {
        let Some(((a, lo, hi), rest)) = atoms.split_first() else {
            return v.is_empty();
        };
        let most = v.iter().take_while(|c| hit(a, **c)).count().min(*hi);
        (*lo..=most).rev().any(|n| n >= *lo && go(rest, &v[n..]))
    }
    let v: Vec<char> = value.chars().collect();
    Some(go(&atoms, &v))
}

// ------------------------------------------------------------------- page

#[derive(Clone, Copy)]
struct Tu {
    sel: Signal<usize>,
    all: Signal<bool>,
    /// (connection id, sentence, ok) — the Test result under its row.
    fire: Signal<Option<(String, String, bool)>>,
    msg: Signal<Option<(String, bool)>>,
    pending: Signal<Option<(&'static str, String)>>,
}

fn is_admin(ctx: &Ctx) -> bool {
    ctx.store.conn.with_untracked(ConnPhase::is_admin)
}

fn now_s() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

thread_local! {
    /// The All accounts view survives a re-read (`r`) of the page.
    static ALL_VIEW: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Read the sources and the list (the web's `loadTriggers`).
pub fn refresh(ctx: &Ctx) {
    let all = ALL_VIEW.with(|a| a.get()) && is_admin(ctx);
    if ctx.store.json.get_untracked(K_LIST).ready().is_none() {
        ctx.store.json.set(K_LIST, Loadable::Loading);
    }
    ctx.send(Cmd::Json(JsonCmd::get(K_SOURCES, SOURCES_PATH)));
    ctx.send(Cmd::Json(JsonCmd::get(K_LIST, list_path(all))));
    if !all {
        ctx.send(Cmd::Json(JsonCmd::get(K_MAILBOXES, MAILBOXES_PATH)));
        ctx.send(Cmd::Json(JsonCmd::get(K_AUTOS, AUTOS_PATH)));
        ctx.send(Cmd::Json(JsonCmd::get(K_FLOWS, FLOWS_PATH)));
    }
}

/// The footer verbs.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let _ = ctx;
    vec![
        ("↑↓", "rows"),
        ("Tab", "actions"),
        ("f", USE_IN_FLOW),
        ("Enter/e", "Events"),
        ("t", "Test"),
        ("d", "Delete"),
        ("l", "Listening"),
        ("k", "Keep fetching even when nothing uses it"),
        ("n", NEW),
        ("a", ALL),
        ("r", "refresh"),
    ]
}

fn rows_now(ctx: &Ctx) -> Vec<ConnRow> {
    match ctx.store.json.get_untracked(K_LIST) {
        Loadable::Ready(v) => rows_from(&v).unwrap_or_default(),
        _ => Vec::new(),
    }
}

fn selected(ctx: &Ctx, tu: Tu) -> Option<ConnRow> {
    let rows = rows_now(ctx);
    let i = tu.sel.get_untracked().min(rows.len().saturating_sub(1));
    rows.get(i).cloned()
}

/// The page.
pub fn screen(ctx: &Ctx, cx: Scope) -> View {
    let tu = Tu {
        sel: cx.signal(0usize),
        all: cx.signal(ALL_VIEW.with(|a| a.get())),
        fire: cx.signal(None),
        msg: cx.signal(None),
        pending: cx.signal(None),
    };
    if ctx.store.json.get_untracked(K_LIST).ready().is_none()
        && ctx.store.conn.with_untracked(ConnPhase::is_connected)
    {
        refresh(ctx);
    }
    install_effects(cx, ctx, tu);
    let keys_ctx = ctx.clone();
    let head_ctx = ctx.clone();
    let body_ctx = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                if handle_key(cx, &keys_ctx, tu, k.key) {
                    ectx.stop_propagation();
                }
            }
        })
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |hcx| {
                let t = use_theme(hcx).get().tokens;
                let w = (crate::ui::page_viewport(hcx).get().w - 2).max(20);
                let mut buttons: Vec<(View, i32)> = Vec::new();
                for a in panel_actions() {
                    let c = head_ctx.clone();
                    let id = a.id;
                    let wd = a.width() + 2;
                    buttons.push((
                        button(hcx, &t, &a, On::Page, true, move || {
                            panel_action(cx, &c, tu, id)
                        }),
                        wd,
                    ));
                }
                super::workflows::page_head(&t, TITLE, SUBTITLE, w, buttons)
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).grow(1.0).min_h(3),
            move |gcx| body(gcx, cx, &body_ctx, tu),
        ))
        .build()
}

fn install_effects(cx: Scope, ctx: &Ctx, tu: Tu) {
    let store = ctx.store;
    let c = ctx.clone();
    cx.effect(move || {
        let Some((kind, id)) = tu.pending.get() else {
            return;
        };
        let key = if kind == "test" { K_TEST } else { K_DELETE };
        let Some(out) = store.json.write(key) else {
            return;
        };
        if out.is_pending() {
            return;
        }
        if kind == "test" {
            if let Some((text, ok)) = test_result(&out) {
                tu.fire.set(Some((id.clone(), text, ok)));
            }
        } else {
            match &out {
                WriteState::Done(_) => {
                    let label = rows_now(&c)
                        .into_iter()
                        .find(|r| r.id == id)
                        .map(|r| r.label)
                        .unwrap_or(id.clone());
                    super::w::toast(&c, cx, format!("Deleted {label}."));
                    tu.msg.set(None);
                }
                WriteState::Failed(e) => tu
                    .msg
                    .set(Some((format!("Not deleted: {}", refusal(e)), false))),
                WriteState::Pending => {}
            }
        }
        store.json.set_write(key, None);
        tu.pending.set(None);
    });
}

fn handle_key(cx: Scope, ctx: &Ctx, tu: Tu, key: Key) -> bool {
    match key {
        Key::Char('n') => panel_action(cx, ctx, tu, "new"),
        Key::Char('a') if is_admin(ctx) => set_all(ctx, tu, !tu.all.get_untracked()),
        Key::Char('f') if !tu.all.get_untracked() => {
            open_use_flow(cx, ctx, selected_target(ctx, tu))
        }
        Key::Char('e') => row_action(cx, ctx, tu, "events"),
        Key::Char('t') => row_action(cx, ctx, tu, "test"),
        Key::Char('d') => row_action(cx, ctx, tu, "delete"),
        Key::Char('l') => toggle_listening(ctx, tu),
        Key::Char('k') => toggle_always_listen(ctx, tu),
        _ => return false,
    }
    true
}

fn set_all(ctx: &Ctx, tu: Tu, on: bool) {
    ALL_VIEW.with(|a| a.set(on));
    tu.all.set(on);
    ctx.store.json.set(K_LIST, Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get(K_LIST, list_path(on))));
    if !on {
        ctx.send(Cmd::Json(JsonCmd::get(K_MAILBOXES, MAILBOXES_PATH)));
    }
}

fn panel_action(cx: Scope, ctx: &Ctx, tu: Tu, id: &str) {
    match id {
        "refresh" => refresh(ctx),
        "new" => open_create(cx, ctx, tu),
        _ => {}
    }
}

/// The trigger the selected row names (connections first, then your mailboxes, as listed);
/// Err when a mailbox's source is not served by this gateway.
fn selected_target(ctx: &Ctx, tu: Tu) -> Option<Result<UseTarget, String>> {
    let rows = rows_now(ctx);
    let i = tu.sel.get_untracked();
    if i < rows.len() {
        return Some(Ok(conn_target(&rows[i])));
    }
    let boxes = match ctx.store.json.get_untracked(K_MAILBOXES) {
        Loadable::Ready(v) => mailboxes_from(&v).unwrap_or_default(),
        _ => Vec::new(),
    };
    let m = boxes
        .get(i - rows.len())
        .or(if rows.is_empty() { boxes.first() } else { None });
    match m {
        Some(m) => Some(mailbox_target(ctx, m)),
        None => rows.last().map(|r| Ok(conn_target(r))),
    }
}

fn conn_target(r: &ConnRow) -> UseTarget {
    UseTarget {
        mailbox: false,
        primary: false,
        id: r.id.clone(),
        label: r.label.clone(),
        source_id: r.source_id.clone(),
        source_version: r.source_version,
    }
}

/// A mailbox's trigger: `email.received` at the version this gateway serves.
fn mailbox_target(ctx: &Ctx, m: &MailboxRow) -> Result<UseTarget, String> {
    let sources = ctx
        .store
        .json
        .get_untracked(K_SOURCES)
        .ready()
        .cloned()
        .unwrap_or(Value::Null);
    let version = sources
        .get("items")
        .and_then(Value::as_array)
        .and_then(|l| {
            l.iter().find(|s| {
                s.get("id").and_then(Value::as_str) == Some("email.received")
                    && s.get("available").and_then(Value::as_bool) == Some(true)
            })
        })
        .and_then(|s| s.get("version").and_then(Value::as_i64))
        .ok_or("this gateway has no email trigger (email.received). Update the gateway.")?;
    Ok(UseTarget {
        mailbox: true,
        primary: m.primary,
        id: m.id.clone(),
        label: m.name(),
        source_id: "email.received".into(),
        source_version: version,
    })
}

fn row_action(cx: Scope, ctx: &Ctx, tu: Tu, id: &str) {
    let Some(r) = selected(ctx, tu) else { return };
    let all = tu.all.get_untracked();
    match id {
        "flow" if !all => open_use_flow(cx, ctx, Some(Ok(conn_target(&r)))),
        "events" => open_events(cx, ctx, r),
        "test" => {
            if tu.pending.with_untracked(Option::is_some) {
                return;
            }
            tu.fire
                .set(Some((r.id.clone(), "Sending a test event...".into(), true)));
            tu.pending.set(Some(("test", r.id.clone())));
            ctx.store.json.set_write(K_TEST, Some(WriteState::Pending));
            ctx.send(Cmd::Json(JsonCmd::Send {
                key: K_TEST.into(),
                method: "POST".into(),
                path: test_path(&r.id),
                body: json!({}),
                slow: false,
                label: format!("Test {}", r.label),
                reload: vec![(K_LIST.into(), list_path(all))],
                journal: false,
            }));
        }
        "delete" => {
            let c = ctx.clone();
            super::w::Confirm::danger(delete_question(&r.label), "Delete", "Cancel").open(
                cx,
                ctx.ui,
                move || {
                    tu.pending.set(Some(("delete", r.id.clone())));
                    c.store.json.set_write(K_DELETE, Some(WriteState::Pending));
                    c.send(Cmd::Json(JsonCmd::Send {
                        key: K_DELETE.into(),
                        method: "DELETE".into(),
                        path: conn_path(&r.id),
                        body: json!({}),
                        slow: false,
                        label: format!("Delete {}", r.label),
                        reload: vec![(K_LIST.into(), list_path(all))],
                        journal: true,
                    }));
                },
            );
        }
        _ => {}
    }
}

fn body(cx: Scope, pcx: Scope, ctx: &Ctx, tu: Tu) -> View {
    let t = use_theme(cx).get().tokens;
    let vp = crate::ui::page_viewport(cx).get();
    let width = (vp.w - 2).max(20);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    col = col.child(sentence(&t, PURPOSE, width, t.text_muted));
    // Tracked: the toggle appears once the sign-in says admin.
    if ctx.store.conn.with(ConnPhase::is_admin) {
        let c = ctx.clone();
        let toggle = Toggle::bound(tu.all)
            .label(ALL)
            .tip("Admin view: every account's connections, with who owns each; off shows only yours  (a)")
            .on_change(move |v| set_all(&c, tu, v))
            .view(cx, &t);
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().h(1).shrink(0.0))
                .child(toggle)
                .build(),
        );
    }
    col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
        let t = abstracttui::app::current_theme().tokens;
        match tu.msg.get() {
            Some((m, ok)) => sentence(&t, &m, width, if ok { t.ok } else { t.error }),
            None => Element::new().style(LayoutStyle::default().h(0)).build(),
        }
    }));
    let c = ctx.clone();
    col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).grow(1.0),
        move |gcx| {
            let t = use_theme(gcx).get().tokens;
            table(gcx, pcx, &c, tu, &t, width, vp.h)
        },
    ))
    .child(sentence(&t, LISTEN_HELP, width, t.text_muted))
    .build()
}

/// The Listening switch (`l`): pause or resume the selected connection
/// (`PUT /triggers/connections/{id}` `{paused}`), then read the list again.
fn toggle_listening(ctx: &Ctx, tu: Tu) {
    let Some(r) = selected(ctx, tu) else { return };
    let Some(paused) = r.paused else { return };
    let all = tu.all.get_untracked();
    ctx.store.json.set_write(K_PAUSE, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: K_PAUSE.into(),
        method: "PUT".into(),
        path: conn_path(&r.id),
        body: json!({ "paused": !paused }),
        slow: false,
        label: format!("{} {}", if paused { "Resume" } else { "Pause" }, r.label),
        reload: vec![(K_LIST.into(), list_path(all))],
        journal: true,
    }));
}

/// A mailbox row's switch: the mailbox's **Active** (My email → Mailboxes; `PUT /me/email/accounts/{id}/enabled`).
fn set_mailbox_in_use(ctx: &Ctx, id: &str, enabled: bool) {
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: K_PAUSE.into(),
        method: "PUT".into(),
        path: format!("/me/email/accounts/{}/enabled", crate::api::urlencode(id)),
        body: json!({ "enabled": enabled }),
        slow: false,
        label: format!("Mailbox active: {id}"),
        reload: vec![(K_MAILBOXES.into(), MAILBOXES_PATH.into())],
        journal: true,
    }));
}

/// Advanced (`k`): keep fetching even when nothing uses the selected connection.
fn toggle_always_listen(ctx: &Ctx, tu: Tu) {
    let Some(r) = selected(ctx, tu) else { return };
    let Some(on) = r.always_listen else { return };
    let all = tu.all.get_untracked();
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: K_PAUSE.into(),
        method: "PUT".into(),
        path: conn_path(&r.id),
        body: json!({ "always_listen": !on }),
        slow: false,
        label: format!("Keep fetching even when nothing uses it: {}", r.label),
        reload: vec![(K_LIST.into(), list_path(all))],
        journal: true,
    }));
}

fn table(cx: Scope, pcx: Scope, ctx: &Ctx, tu: Tu, t: &TokenSet, width: i32, page_h: i32) -> View {
    let all = tu.all.get();
    let fire = tu.fire.get();
    let anchor = |v: View| -> View {
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .focusable()
            .autofocus()
            .child(v)
            .build()
    };
    let rows = match ctx.store.json.get(K_LIST) {
        Loadable::NotAsked | Loadable::Loading => {
            return anchor(sentence(
                t,
                if all { LOADING } else { LOADING_OWN },
                width,
                t.text_muted,
            ))
        }
        Loadable::Failed(e) => {
            return anchor(sentence(
                t,
                &format!("Could not list the connections: {}", refusal(&e)),
                width,
                t.error,
            ))
        }
        Loadable::Ready(v) => match rows_from(&v) {
            Ok(r) => r,
            Err(e) => {
                return anchor(sentence(
                    t,
                    &format!("Could not list the connections: {e}"),
                    width,
                    t.error,
                ))
            }
        },
    };
    // Own view: the mailboxes are rows too, so the list waits for them (loading, never empty).
    let mailbox_error = if all {
        None
    } else {
        match ctx.store.json.get(K_MAILBOXES) {
            Loadable::NotAsked | Loadable::Loading => {
                return anchor(super::w::form::sentence(
                    t,
                    LOADING_OWN,
                    width,
                    t.text_muted,
                ))
            }
            Loadable::Failed(e) => Some(format!("Could not read your mailboxes: {}", refusal(&e))),
            Loadable::Ready(_) => None,
        }
    };
    let sources = match ctx.store.json.get(K_SOURCES) {
        Loadable::Ready(v) => v,
        _ => Value::Null,
    };
    // Your active automations (own view): the names of what uses each connection.
    let autos = match (all, ctx.store.json.get(K_AUTOS)) {
        (false, Loadable::Ready(v)) => v,
        _ => Value::Null,
    };
    let now = now_s();
    let narrow = width < 100;
    let mut cols = vec![Col::new("Name", ColW::Flex { weight: 1, min: 16 })];
    if all {
        cols.push(Col::new("Account", ColW::Fit { min: 8, max: 18 }));
    }
    cols.push(Col::new("Status", ColW::Fit { min: 6, max: 30 }));
    cols.push(Col::new("Listening", ColW::Fit { min: 4, max: 12 }));
    // Handled by (own view): the flows that handle each trigger, one per line.
    if !all {
        cols.push(Col::new(
            HANDLED_BY,
            ColW::Fit {
                min: 10,
                max: if narrow { 16 } else { 26 },
            },
        ));
    }
    if !narrow {
        cols.push(Col::new("Last event", ColW::Fit { min: 10, max: 12 }));
    }
    // Below 160 columns the own view's actions fold onto two lines: the Name keeps its room.
    cols.push(Col::new(
        "Actions",
        ColW::Fit {
            min: if narrow { 13 } else { 6 },
            max: if !all && width < 160 { 24 } else { 50 },
        },
    ));
    let handled_cell = |hs: Option<Vec<Handler>>| -> Cell {
        Cell::Lines(
            handled_by(hs.as_deref())
                .into_iter()
                .map(|(line, muted)| {
                    vec![Ink::new(line, if muted { t.text_muted } else { t.text })]
                })
                .collect(),
        )
    };
    let table_rows: Vec<WRow> = rows
        .iter()
        .map(|r| {
            let (word, tone) = status_word(&r.status);
            let ink = match tone {
                "ok" => t.ok,
                "error" => t.error,
                "warn" => t.warn,
                _ => t.text_muted,
            };
            let mut name_lines = vec![
                vec![Ink::new(r.label.clone(), t.text).bold()],
                vec![Ink::new(trigger_phrase(r, &sources), t.text_muted)],
            ];
            // A failing connection that is on says its problem (red) as its status line; a paused
            // one keeps it under the name.
            let problem = error_text(r).filter(|_| r.paused == Some(false));
            if let (Some(e), Some(true)) = (error_text(r), r.paused) {
                name_lines.push(vec![Ink::new(e, t.error)]);
            }
            let mut cells = vec![Cell::Lines(name_lines)];
            if all {
                cells.push(Cell::text(owner_text(r.principal.as_deref()), t.text));
            }
            match doing_text_with(r, &connection_users(r, &autos)) {
                Some(_) if problem.is_some() => cells.push(Cell::Lines(
                    if r.status.trim().eq_ignore_ascii_case("active") {
                        vec![vec![Ink::new(problem.clone().unwrap_or_default(), t.error)]]
                    } else {
                        vec![
                            vec![Ink::new(word, ink)],
                            vec![Ink::new(problem.clone().unwrap_or_default(), t.error)],
                        ]
                    },
                )),
                Some(d) => cells.push(Cell::Lines(
                    if r.status.trim().eq_ignore_ascii_case("active") {
                        vec![vec![Ink::new(d, t.text)]]
                    } else {
                        vec![vec![Ink::new(word, ink)], vec![Ink::new(d, t.text)]]
                    },
                )),
                None => cells.push(Cell::Lines(vec![
                    vec![Ink::new(word, ink)],
                    vec![Ink::new(
                        "listening state not reported (update the gateway)",
                        t.text_muted,
                    )],
                ])),
            }
            cells.push(match r.paused {
                Some(p) => Cell::Toggle {
                    id: "listen",
                    on: !p,
                    refused: None,
                    tip: Some(format!(
                        "{} {}  (l)",
                        if p { "Resume" } else { "Pause" },
                        r.label
                    )),
                },
                None => Cell::text("—", t.text_muted),
            });
            if !all {
                cells.push(handled_cell(handlers(&autos, |tr| {
                    connection_match(&r.id, tr)
                })));
            }
            if !narrow {
                cells.push(Cell::text(last_event_text(r, now), t.text));
            }
            // All accounts: other people's connections, never bound to your flows here.
            cells.push(Cell::Actions(
                row_actions(r)
                    .into_iter()
                    .filter(|a| !(all && a.id == "flow"))
                    .collect(),
            ));
            let mut note: Option<(String, Rgba)> = None;
            if narrow {
                note = Some((
                    format!("Last event: {}", last_event_text(r, now)),
                    t.text_muted,
                ));
            }
            if let Some((id, text, ok)) = &fire {
                if *id == r.id {
                    note = Some((text.clone(), if *ok { t.ok } else { t.error }));
                }
            }
            WRow::new(r.id.clone(), cells).note(note)
        })
        .collect();
    let mut table_rows = table_rows;
    let mut keys: Vec<String> = rows.iter().map(|r| r.id.clone()).collect();
    // The person's mailboxes (own view): rows of "When mail arrives in …", managed under My email.
    if !all {
        if let Loadable::Ready(v) = ctx.store.json.get(K_MAILBOXES) {
            for m in mailboxes_from(&v).unwrap_or_default() {
                let (word, tone) = m.status();
                let ink = match tone {
                    "ok" => t.ok,
                    "warn" => t.warn,
                    _ => t.text_muted,
                };
                let last = match &m.last_poll {
                    Some(p) => format!("checked {}", ago(p, now).unwrap_or_else(|| p.clone())),
                    None => "Never".into(),
                };
                let mut cells = vec![
                    Cell::Lines(vec![
                        {
                            let mut l = vec![Ink::new(m.name(), t.text).bold()];
                            if m.primary {
                                l.push(Ink::new(format!("  {SENDS_AS_YOU}"), t.info));
                            }
                            l
                        },
                        vec![Ink::new(m.phrase(), t.text_muted)],
                    ]),
                    {
                        let users = mailbox_users(&m, &autos);
                        let used = autos.is_object().then_some(users.len() as i64);
                        let words = listening_words(!m.enabled, used, false, &users);
                        if tone == "warn" {
                            Cell::Lines(vec![
                                vec![Ink::new(word, ink)],
                                vec![Ink::new(words, t.text)],
                            ])
                        } else {
                            Cell::text(words, t.text)
                        }
                    },
                    Cell::Toggle {
                        id: "use",
                        on: m.enabled,
                        refused: None,
                        tip: Some(format!("Listening: {}", m.name())),
                    },
                    handled_cell(handlers(&autos, |tr| mailbox_match(&m, tr))),
                ];
                if !narrow {
                    cells.push(Cell::text(last.clone(), t.text));
                }
                cells.push(Cell::Actions(mailbox_actions()));
                let key = format!("mailbox:{}", m.id);
                let note = narrow.then(|| (last.clone(), t.text_muted));
                table_rows.push(WRow::new(key.clone(), cells).note(note));
                keys.push(key);
            }
        }
    }
    let i = tu.sel.get_untracked().min(keys.len().saturating_sub(1));
    let sel = cx.signal(keys.get(i).cloned());
    {
        let keys = keys.clone();
        cx.effect(move || {
            if let Some(k) = sel.get() {
                if let Some(p) = keys.iter().position(|x| *x == k) {
                    if tu.sel.get_untracked() != p {
                        tu.sel.set(p);
                    }
                }
            }
        });
    }
    let (ca, ce) = (ctx.clone(), ctx.clone());
    let (ka, ke, kt) = (keys.clone(), keys.clone(), keys);
    let ct = ctx.clone();
    let table = DataTable::new(cols, table_rows, sel)
        .width(width)
        .max_rows((page_h - 9).max(4))
        .empty(if all { EMPTY_ALL } else { EMPTY })
        .autofocus()
        .on_action(move |key, id| {
            if let Some(mid) = key.strip_prefix("mailbox:") {
                if id == "manage" {
                    super::my_email::open(pcx, &ca);
                } else if id == "flow" {
                    let boxes = match ca.store.json.get_untracked(K_MAILBOXES) {
                        Loadable::Ready(v) => mailboxes_from(&v).unwrap_or_default(),
                        _ => Vec::new(),
                    };
                    if let Some(m) = boxes.iter().find(|m| m.id == mid) {
                        open_use_flow(pcx, &ca, Some(mailbox_target(&ca, m)));
                    }
                }
                return;
            }
            if let Some(p) = ka.iter().position(|x| x == key) {
                tu.sel.set(p);
            }
            row_action(pcx, &ca, tu, id);
        })
        .on_toggle(move |key, _id, want| {
            if let Some(id) = key.strip_prefix("mailbox:") {
                set_mailbox_in_use(&ct, id, want);
                return;
            }
            if let Some(p) = kt.iter().position(|x| x == key) {
                tu.sel.set(p);
                toggle_listening(&ct, tu);
            }
        })
        .on_activate(move |key| {
            if key.starts_with("mailbox:") {
                super::my_email::open(pcx, &ce);
                return;
            }
            if let Some(p) = ke.iter().position(|x| x == key) {
                tu.sel.set(p);
            }
            row_action(pcx, &ce, tu, "events");
        })
        .view(cx, t);
    match mailbox_error {
        Some(e) => Element::new()
            .style(LayoutStyle::column().gap(0).grow(1.0))
            .child(sentence(t, &e, width, t.error))
            .child(table)
            .build(),
        None => table,
    }
}

// ---------------------------------------------------------------- modals

fn close_button(cx: Scope, t: &TokenSet, label: &str, close: super::CloserFn) -> View {
    super::w::form::button_row(vec![button(
        cx,
        t,
        &Action::label("close", label.to_string()),
        On::Raised,
        true,
        move || close(),
    )])
}

fn copy_row(cx: Scope, ctx: &Ctx, t: &TokenSet, label: &str, value: &str, width: i32) -> View {
    let v = value.to_string();
    let store = ctx.store;
    let what = label.to_lowercase();
    let copy = Action::label("copy", "Copy").tooltip(format!("Copy the {what}"));
    Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(super::w::form::section(t, label))
        .child(sentence(t, value, width, t.accent))
        .child(
            Element::new()
                .style(LayoutStyle::row().h(1).shrink(0.0))
                .child(button(cx, t, &copy, On::Raised, false, move || {
                    copy_to_clipboard(v.clone());
                    store.notice.set(Some(format!("copied the {what}")));
                }))
                .build(),
        )
        .build()
}

/// Events: the connection's 20 most recent events, newest first, each
/// with its whole JSON (the web's expandable list), plus a webhook's address.
fn open_events(cx: Scope, ctx: &Ctx, r: ConnRow) {
    ctx.store.json.set(K_EVENTS, Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get(K_EVENTS, events_path(&r.id))));
    let c = ctx.clone();
    super::w::FormModal::new(format!("Recent events — {}", r.label))
        .size(110, 36)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let t = use_theme(mcx).get().tokens;
            let c2 = c.clone();
            let r2 = r.clone();
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    abstracttui::widgets::Scroll::new(dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                        let t = use_theme(gcx).get().tokens;
                        let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                        if let Some(url) = &r2.ingress_url {
                            col = col.child(copy_row(gcx, &c2, &t, "Webhook address", url, w));
                            col = col.child(sentence(&t, WEBHOOK_HELP, w, t.text_muted));
                        }
                        col = col.child(super::w::form::section(&t, "Recent events"));
                        match c2.store.json.get(K_EVENTS) {
                            Loadable::Ready(v) => match v.get("events").and_then(Value::as_array) {
                                Some(list) if list.is_empty() => col = col.child(sentence(&t, EVENTS_EMPTY, w, t.text_muted)),
                                Some(list) => {
                                    for (head, json) in event_lines(list, now_s()) {
                                        col = col.child(sentence(&t, &head, w, t.text));
                                        for l in json.lines() {
                                            col = col.child(super::kit::sentence_indent(&t, l, w, 4, t.text_muted));
                                        }
                                    }
                                }
                                None => col = col.child(sentence(&t, "Could not read the events: the gateway's answer has no events list.", w, t.error)),
                            },
                            Loadable::Failed(e) => col = col.child(sentence(&t, &format!("Could not read the events: {}", refusal(&e)), w, t.error)),
                            _ => col = col.child(sentence(&t, EVENTS_LOADING, w, t.text_muted)),
                        }
                        col.build()
                    }))
                    .axes(false, true)
                    .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                    .scrollbar_auto_hide(true)
                    .view(mcx),
                )
                .child(close_button(mcx, &t, "Close", close))
                .build()
        });
}

/// Use in a flow (the web's menu): "New flow for this trigger…" prints Flow's link for this
/// trigger (a terminal cannot open Flow), then your published triggered flows, "✓ <flow>" when it
/// already handles the trigger (and every handler whose flow is not published). An unchecked pick
/// creates the automation (`POST /automations`, the web's body), turned on; a checked one opens
/// that flow's automations on this trigger, each with Pause (Resume) and Remove (asked first,
/// inline). Handled by is read again after each.
fn open_use_flow(cx: Scope, ctx: &Ctx, target: Option<Result<UseTarget, String>>) {
    let Some(target) = target else { return };
    if ctx.store.json.get_untracked(K_FLOWS).ready().is_none() {
        ctx.send(Cmd::Json(JsonCmd::get(K_FLOWS, FLOWS_PATH)));
    }
    ctx.store.json.set_write(K_USE, None);
    ctx.store.json.set_write(K_CMD, None);
    let title = match &target {
        Ok(t) => format!("{USE_IN_FLOW} — {}", t.label),
        Err(_) => USE_IN_FLOW.to_string(),
    };
    let c = ctx.clone();
    super::w::FormModal::new(title)
        .size(90, 28)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let show_link = mcx.signal(false);
            let picked: Signal<Option<String>> = mcx.signal(None);
            // The checked item opened (its name), the automation asked about Remove, the last command.
            let opened: Signal<Option<String>> = mcx.signal(None);
            let asking: Signal<Option<String>> = mcx.signal(None);
            let acted: Signal<Option<(HandlerCmd, String)>> = mcx.signal(None);
            let c2 = c.clone();
            let t0 = use_theme(mcx).get().tokens;
            let body = dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                let t = use_theme(gcx).get().tokens;
                let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                let target = match &target {
                    Ok(x) => x.clone(),
                    Err(e) => return sentence(&t, &format!("Not available: {e}"), w, t.error),
                };
                let key = target.key();
                let link = new_flow_link(&c2.ui.conn_url.get_untracked(), &key);
                let new_flow = Action::label("new_flow", NEW_FLOW)
                    .tooltip("Create a flow that starts from this trigger");
                col = col.child(menu_item(super::w::action::button_focused(
                    gcx,
                    &t,
                    &new_flow,
                    On::Raised,
                    move || show_link.set(true),
                )));
                if show_link.get() {
                    col = col.child(sentence(&t, NEW_FLOW_HELP, w, t.text_muted));
                    col = col.child(copy_row(gcx, &c2, &t, "Flow link", &link, w));
                }
                let autos = c2
                    .store
                    .json
                    .get(K_AUTOS)
                    .ready()
                    .cloned()
                    .unwrap_or(Value::Null);
                let hs = handlers(&autos, |tr| target.matches(tr)).unwrap_or_default();
                // The published flows (none while unread or refused: the handlers stay reachable).
                let flows = match c2.store.json.get(K_FLOWS) {
                    Loadable::Ready(v) => match triggered_flows(&v) {
                        Ok(f) => Some(f),
                        Err(e) => {
                            col = col.child(sentence(
                                &t,
                                &format!("Could not read your flows: {e}"),
                                w,
                                t.error,
                            ));
                            None
                        }
                    },
                    Loadable::Failed(e) => {
                        col = col.child(sentence(
                            &t,
                            &format!("Could not read your flows: {}", refusal(&e)),
                            w,
                            t.error,
                        ));
                        None
                    }
                    _ => {
                        col = col.child(sentence(&t, "Reading your flows...", w, t.text_muted));
                        None
                    }
                };
                let items = menu_items(flows.as_deref().unwrap_or(&[]), &hs);
                if items.is_empty() {
                    if flows.is_some() {
                        col = col.child(sentence(&t, NO_FLOWS, w, t.text_muted));
                    }
                } else {
                    col = col.child(super::w::form::section(&t, FLOWS_HEAD));
                }
                let busy = |c: &Ctx| {
                    [K_USE, K_CMD].iter().any(|k| {
                        c.store
                            .json
                            .write_untracked(k)
                            .is_some_and(|w| w.is_pending())
                    })
                };
                for item in items {
                    if item.checked() {
                        let name = item.name.clone();
                        let a = Action::label("handled", item.label()).tooltip(format!(
                            "{} already handles {}: pause or remove it",
                            item.name, target.label
                        ));
                        col = col.child(menu_item(button(
                            gcx,
                            &t,
                            &a,
                            On::Raised,
                            true,
                            move || {
                                asking.set(None);
                                opened.update(|o| {
                                    *o = if o.as_deref() == Some(name.as_str()) {
                                        None
                                    } else {
                                        Some(name.clone())
                                    }
                                });
                            },
                        )));
                        if opened.get().as_deref() != Some(item.name.as_str()) {
                            continue;
                        }
                        // The flow's automations on this trigger, each with its actions.
                        for h in &item.handlers {
                            let text = if h.paused {
                                format!("{} (paused)", h.title)
                            } else {
                                h.title.clone()
                            };
                            col = col.child(super::kit::sentence_indent(
                                &t,
                                &text,
                                w,
                                4,
                                if h.paused { t.text_muted } else { t.text },
                            ));
                            let ask = asking.get().as_deref() == Some(h.id.as_str());
                            if ask {
                                col = col.child(super::kit::sentence_indent(
                                    &t,
                                    &remove_question(&h.title, &target.label),
                                    w,
                                    4,
                                    t.warn,
                                ));
                            }
                            let acts = if ask {
                                remove_actions()
                            } else {
                                handler_actions(h)
                            };
                            let mut views = Vec::new();
                            for a in acts {
                                let (c3, h2, label) = (c2.clone(), h.clone(), target.label.clone());
                                let id = a.id;
                                views.push(button(gcx, &t, &a, On::Raised, true, move || {
                                    let cmd = match id {
                                        "remove" => {
                                            asking.set(Some(h2.id.clone()));
                                            return;
                                        }
                                        "remove_no" => {
                                            asking.set(None);
                                            return;
                                        }
                                        "pause" => HandlerCmd::Pause,
                                        "resume" => HandlerCmd::Resume,
                                        _ => HandlerCmd::Remove,
                                    };
                                    if busy(&c3) {
                                        return;
                                    }
                                    asking.set(None);
                                    c3.store.json.set_write(K_USE, None);
                                    acted.set(Some((cmd, h2.title.clone())));
                                    send_command(&c3, &h2, cmd, &label);
                                }));
                            }
                            col = col.child(indented_row(views, 4));
                        }
                        continue;
                    }
                    let Some(f) = item.flow else { continue };
                    let a = Action::label("pick", f.name.clone())
                        .tooltip(format!("Turn {} on for {}", f.name, target.label));
                    let (c3, tg) = (c2.clone(), target.clone());
                    col = col.child(menu_item(button(
                        gcx,
                        &t,
                        &a,
                        On::Raised,
                        true,
                        move || {
                            if busy(&c3) {
                                return;
                            }
                            picked.set(Some(f.name.clone()));
                            c3.store.json.set_write(K_CMD, None);
                            let rid = format!("console-tui-trigger-{}", unique());
                            c3.store.json.set_write(K_USE, Some(WriteState::Pending));
                            c3.send(Cmd::Json(JsonCmd::Send {
                                key: K_USE.into(),
                                method: "POST".into(),
                                path: "/automations".into(),
                                body: use_flow_body(&f, &tg, &rid),
                                slow: false,
                                label: format!("Turn on {} for {}", f.name, tg.label),
                                reload: vec![(K_AUTOS.into(), AUTOS_PATH.into())],
                                journal: true,
                            }));
                        },
                    )));
                }
                let name = picked.get().unwrap_or_default();
                match c2.store.json.write(K_USE) {
                    Some(WriteState::Pending) => {
                        col = col.child(sentence(
                            &t,
                            &format!("Turning on {name}..."),
                            w,
                            t.text_muted,
                        ))
                    }
                    Some(WriteState::Done(_)) => {
                        col = col.child(sentence(
                            &t,
                            &format!("{name} now handles {}. It is on.", target.label),
                            w,
                            t.ok,
                        ))
                    }
                    Some(WriteState::Failed(e)) => {
                        col = col.child(sentence(
                            &t,
                            &use_refusal(&e, &name, &target.label),
                            w,
                            t.error,
                        ))
                    }
                    None => {}
                }
                if let Some((cmd, title)) = acted.get() {
                    match c2.store.json.write(K_CMD) {
                        Some(WriteState::Done(_)) => {
                            col = col.child(sentence(
                                &t,
                                &command_said(cmd, &title, &target.label),
                                w,
                                t.ok,
                            ))
                        }
                        Some(WriteState::Failed(e)) => {
                            col = col.child(sentence(
                                &t,
                                &command_refusal(&e, cmd, &title),
                                w,
                                t.error,
                            ))
                        }
                        _ => {}
                    }
                }
                col.build()
            });
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    abstracttui::widgets::Scroll::new(body)
                        .axes(false, true)
                        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                        .scrollbar_auto_hide(true)
                        .view(mcx),
                )
                .child(close_button(mcx, &t0, "Close", close))
                .build()
        });
}

/// A per-call unique suffix (request and command ids).
fn unique() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

/// Pause / Resume / Remove a handler: `POST /automations/{id}/commands`. The receipt only means
/// queued (the runner applies it shortly after), so the automations are read again with the
/// receipt and once more a moment later.
fn send_command(ctx: &Ctx, h: &Handler, cmd: HandlerCmd, label: &str) {
    let verb = match cmd {
        HandlerCmd::Pause => "Pause",
        HandlerCmd::Resume => "Resume",
        HandlerCmd::Remove => "Remove",
    };
    ctx.store.json.set_write(K_CMD, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: K_CMD.into(),
        method: "POST".into(),
        path: command_path(&h.id),
        body: command_body(cmd, &format!("console-trigger-{}", unique())),
        slow: false,
        label: format!("{verb} {} on {label}", h.title),
        reload: vec![(K_AUTOS.into(), AUTOS_PATH.into())],
        journal: true,
    }));
    let c = ctx.clone();
    abstracttui::reactive::after(REREAD_AFTER, move || {
        c.send(Cmd::Json(JsonCmd::get(K_AUTOS, AUTOS_PATH)));
    });
}

/// A left-aligned row of buttons, indented.
fn indented_row(views: Vec<View>, indent: i32) -> View {
    let mut row = Element::new()
        .style(
            LayoutStyle::row()
                .height(Dimension::Cells(1))
                .shrink(0.0)
                .gap(1),
        )
        .child(
            Element::new()
                .style(LayoutStyle::default().w(indent - 1).shrink(0.0))
                .build(),
        );
    for v in views {
        row = row.child(v);
    }
    row.build()
}

/// One left-aligned menu line (a button).
fn menu_item(v: View) -> View {
    Element::new()
        .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
        .child(v)
        .build()
}

/// The create form's field readers: (key, raw value now).
type FieldValues = Rc<RefCell<Vec<(String, Box<dyn Fn() -> String>)>>>;

/// New connection: source, name, the source's settings; then (webhooks)
/// the address and the one-time secret.
/// The feed the picker's row `k` names (None: a heading, Another address, none yet).
fn picked_feed(ctx: &Ctx, k: usize) -> Option<CatalogFeed> {
    let got = ctx.store.json.get_untracked(K_CATALOG);
    let feeds = catalog_from(got.ready()?).ok()?;
    match catalog_rows(&feeds).get(k)? {
        CatalogRow::Feed(i) => feeds.get(*i).cloned(),
        _ => None,
    }
}

fn open_create(cx: Scope, ctx: &Ctx, tu: Tu) {
    let _ = tu;
    if ctx.store.json.get_untracked(K_SOURCES).ready().is_none() {
        ctx.send(Cmd::Json(JsonCmd::get(K_SOURCES, SOURCES_PATH)));
    }
    if ctx.store.json.get_untracked(K_CATALOG).ready().is_none() {
        ctx.send(Cmd::Json(JsonCmd::get(K_CATALOG, CATALOG_PATH)));
    }
    ctx.store.json.set_write(K_CREATE, None);
    let c = ctx.clone();
    super::w::FormModal::new(NEW)
        .size(96, 32)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let src_idx = mcx.signal(0usize);
            let name = mcx.signal(String::new());
            let note = mcx.signal(String::new());
            let values: FieldValues = Rc::new(RefCell::new(Vec::new()));
            // The well-known feed picked (an index into `catalog_rows`; out of
            // range = none yet), a template's value, the feed address (shared
            // with the address field), and what the picker last filled in.
            let feed_pick = mcx.signal(usize::MAX);
            let feed_param = mcx.signal(String::new());
            let url_sig = mcx.signal(String::new());
            let auto: Rc<RefCell<(String, String)>> = Rc::new(RefCell::new(Default::default()));
            // A feed's Check every, shared by the picker and the field (the web's
            // `trgPickEvery`): number, unit, (what the last pick left there, None = no pick
            // yet; seeded with the form default), and whether it is the feed's recommendation.
            let every_n = mcx.signal(String::new());
            let every_u = mcx.signal(1usize);
            let every_state: Rc<RefCell<(Option<String>, bool)>> = Rc::new(RefCell::new((None, false)));
            let every_rec = mcx.signal(false);
            {
                // Typing a template's value builds the address (and the name,
                // while it is the picker's) — the web's `trgFeedParamTyped`.
                let (c, auto) = (c.clone(), auto.clone());
                mcx.effect(move || {
                    let v = feed_param.get().trim().to_string();
                    let Some(f) = picked_feed(&c, feed_pick.get_untracked()) else {
                        return;
                    };
                    if f.placeholder.is_none() {
                        return;
                    }
                    let mut a = auto.borrow_mut();
                    let url_now = url_sig.get_untracked();
                    if url_now.trim().is_empty() || url_now == a.1 {
                        a.1 = if v.is_empty() { String::new() } else { feed_address(&f, &v) };
                        url_sig.set(a.1.clone());
                    }
                    let name_now = name.get_untracked();
                    if name_now.trim().is_empty() || name_now == a.0 {
                        a.0 = feed_name(&f, &v);
                        name.set(a.0.clone());
                    }
                });
            }
            let c2 = c.clone();
            let close2 = close.clone();
            let close_cancel = close.clone();
            dyn_view_scoped(LayoutStyle::column().grow(1.0), move |gcx| {
                let t = use_theme(gcx).get().tokens;
                // Created: the address and the secret, once.
                if let Some(WriteState::Done(created)) = c2.store.json.write(K_CREATE) {
                    let mut col = Element::new().style(LayoutStyle::column().grow(1.0));
                    let label = st(&created, "label").unwrap_or_default();
                    col = col.child(super::w::form::section(
                        &t,
                        &format!("Connection created — {label}"),
                    ));
                    let url = st(&created, "ingress_url");
                    let secret = st(&created, "secret");
                    if let Some(u) = &url {
                        col = col.child(copy_row(gcx, &c2, &t, "Webhook address", u, w));
                    }
                    if let Some(s) = &secret {
                        col = col.child(sentence(&t, SECRET_ONCE, w, t.warn));
                        col = col.child(copy_row(gcx, &c2, &t, "Signing secret", s, w));
                    }
                    if let (Some(u), Some(s)) = (&url, &secret) {
                        col = col.child(super::w::form::section(&t, "Set up the sender"));
                        // UX #15: a local address cannot be reached by GitHub.
                        col = col.child(if local_address(u) {
                            sentence(&t, LOCAL_WEBHOOK, w, t.warn)
                        } else {
                            sentence(&t, WEBHOOK_RECIPE, w, t.text_muted)
                        });
                        col = col.child(sentence(&t, WEBHOOK_SHELL, w, t.text_muted));
                        col = col.child(copy_row(
                            gcx,
                            &c2,
                            &t,
                            "Signed call",
                            &webhook_curl_example(u, s),
                            w,
                        ));
                    }
                    if url.is_none() && secret.is_none() {
                        col = col.child(sentence(&t, CREATED_HELP, w, t.text_muted));
                    }
                    // UX #17: the next step.
                    col = col.child(sentence(&t, &next_step(&label), w, t.accent));
                    let (store, close3) = (c2.store, close2.clone());
                    col = col.child(
                        Element::new()
                            .style(LayoutStyle::default().grow(1.0))
                            .build(),
                    );
                    col = col.child(super::w::form::button_row(vec![button(
                        gcx,
                        &t,
                        &Action::label("done", "Done"),
                        On::Raised,
                        true,
                        move || {
                            store.json.set_write(K_CREATE, None);
                            close3();
                        },
                    )]));
                    return col.build();
                }
                let sources = match c2.store.json.get(K_SOURCES) {
                    Loadable::Ready(v) => connectable_sources(&v),
                    Loadable::Failed(e) => Err(format!(
                        "Could not read the trigger sources: {}",
                        refusal(&e)
                    )),
                    _ => return sentence(&t, "Reading the trigger sources...", w, t.text_muted),
                };
                let sources = match sources {
                    Ok(s) => s,
                    Err(e) => return sentence(&t, &e, w, t.error),
                };
                if sources.is_empty() {
                    return sentence(&t, NO_SOURCE, w, t.text_muted);
                }
                // The label column fits the longest label (the web's form shows them whole).
                let label_w = sources
                    .iter()
                    .flat_map(|s| form_fields(&s.connection_schema))
                    .map(|f| abstracttui::text::width(&f.label) + 4)
                    .chain([18])
                    .max()
                    .unwrap_or(18)
                    .min((w / 2).max(18));
                let field_w = (w - label_w).clamp(16, 70);
                let caret = c2.ui.caret;
                let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
                let src_select = Select::new(
                    sources
                        .iter()
                        .map(|s| SelectOption::new(s.label.clone()))
                        .collect(),
                )
                .value(src_idx)
                .element(gcx, &t)
                .autofocus()
                .build();
                col = col.child(super::w::field_row(
                    &t,
                    "What to listen to",
                    label_w,
                    src_select,
                ));
                // UX #18: one line on what the chosen option listens to.
                {
                    let hint_sources = sources.clone();
                    col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                        let t = abstracttui::app::current_theme().tokens;
                        let i = src_idx.get().min(hint_sources.len() - 1);
                        let hint = source_hint(&hint_sources[i].id);
                        super::kit::sentence_indent(&t, hint, w, label_w as usize, t.text_faint)
                    }));
                }
                // The well-known feeds (the web's "Choose a well-known feed").
                {
                    let (pick_sources, c4, auto, every_state) = (sources.clone(), c2.clone(), auto.clone(), every_state.clone());
                    col = col.child(dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |pcx| {
                        let t = use_theme(pcx).get().tokens;
                        let i = src_idx.get().min(pick_sources.len() - 1);
                        if pick_sources[i].id != FEED_SOURCE {
                            return Element::new().style(LayoutStyle::default().h(0)).build();
                        }
                        let feeds = match c4.store.json.get(K_CATALOG) {
                            Loadable::Ready(v) => catalog_from(&v),
                            Loadable::Failed(e) => Err(refusal(&e)),
                            _ => return super::kit::sentence_indent(&t, CATALOG_LOADING, w, label_w as usize, t.text_faint),
                        };
                        let feeds = match feeds {
                            Ok(f) if !f.is_empty() => f,
                            Ok(_) => return Element::new().style(LayoutStyle::default().h(0)).build(),
                            Err(e) => {
                                let text = format!("Could not read the well-known feeds ({e}). Type the feed address below.");
                                return super::kit::sentence_indent(&t, &text, w, label_w as usize, t.text_faint);
                            }
                        };
                        let rows = catalog_rows(&feeds);
                        let options = rows.iter().map(|r| catalog_option(r, &feeds)).collect();
                        let (rows2, feeds2, auto2) = (rows.clone(), feeds.clone(), auto.clone());
                        let every_default = form_fields(&pick_sources[i].connection_schema)
                            .iter()
                            .find(|f| f.key == "every" && is_duration(f))
                            .map(initial_value)
                            .unwrap_or_default();
                        let every_state2 = every_state.clone();
                        let select = Select::new(options)
                            .value(feed_pick)
                            .placeholder("Choose…")
                            .max_visible(12)
                            .layout(LayoutStyle::default().w(field_w).h(1))
                            .on_change(move |k| {
                                // Check every: the feed's recommendation unless the person
                                // changed it since the last pick (state released before the sets).
                                let recommended = match rows2.get(k) {
                                    Some(CatalogRow::Feed(fi)) => feeds2[*fi].check_every.clone(),
                                    _ => None,
                                };
                                let now = duration_value(&every_n.get_untracked(), every_u.get_untracked());
                                let after = every_after_pick(&now, every_state2.borrow().0.as_deref(), recommended.as_deref(), &every_default);
                                every_rec.set(false);
                                if let Some((v, rec)) = after {
                                    every_state2.borrow_mut().0 = Some(v.clone());
                                    let (n, u) = duration_parts(&v);
                                    every_n.set(n);
                                    every_u.set(u);
                                    every_rec.set(rec);
                                }
                                let mut a = auto2.borrow_mut();
                                let typed = name.get_untracked();
                                let name_ours = typed.trim().is_empty() || typed == a.0;
                                let url_now = url_sig.get_untracked();
                                let url_ours = url_now.trim().is_empty() || url_now == a.1;
                                feed_param.set(String::new());
                                note.set(String::new());
                                match rows2.get(k) {
                                    Some(CatalogRow::Feed(fi)) => {
                                        let f = &feeds2[*fi];
                                        if name_ours {
                                            a.0 = feed_name(f, "");
                                            name.set(a.0.clone());
                                        }
                                        if f.placeholder.is_none() || url_ours {
                                            a.1 = if f.placeholder.is_some() { String::new() } else { f.url.clone() };
                                            url_sig.set(a.1.clone());
                                        }
                                    }
                                    _ => {
                                        if name_ours {
                                            a.0.clear();
                                            name.set(String::new());
                                        }
                                        if url_ours {
                                            a.1.clear();
                                            url_sig.set(String::new());
                                        }
                                    }
                                }
                            })
                            .element(pcx, &t)
                            .build();
                        let mut pc = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                        pc = pc.child(super::w::field_row(&t, CATALOG_LABEL, label_w, select));
                        let (rows3, feeds3) = (rows.clone(), feeds.clone());
                        pc = pc.child(dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |hcx| {
                            let t = use_theme(hcx).get().tokens;
                            let mut hc = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                            match rows3.get(feed_pick.get()) {
                                Some(CatalogRow::Feed(fi)) => {
                                    let f = &feeds3[*fi];
                                    let about = if f.site.is_empty() {
                                        f.description.clone()
                                    } else {
                                        format!("{} · {}", f.description, site_name(&f.site))
                                    };
                                    hc = hc.child(super::kit::sentence_indent(&t, &about, w, label_w as usize, t.text_faint));
                                    if let Some(p) = &f.placeholder {
                                        hc = hc.child(super::w::field_row(
                                            &t,
                                            &format!("{} *", p.label),
                                            label_w,
                                            super::w::caret_tracked(
                                                hcx,
                                                caret,
                                                TextInput::new()
                                                    .value(feed_param)
                                                    .placeholder(&p.example)
                                                    .layout(LayoutStyle::default().w(field_w).h(1))
                                                    .element(hcx, &t),
                                            )
                                            .build(),
                                        ));
                                    }
                                }
                                Some(CatalogRow::Another) => {}
                                _ => {
                                    hc = hc.child(super::kit::sentence_indent(&t, CATALOG_HELP, w, label_w as usize, t.text_faint));
                                }
                            }
                            hc.build()
                        }));
                        pc.build()
                    }));
                }
                col = col.child(super::w::field_row(
                    &t,
                    "Name *",
                    label_w,
                    super::w::caret_tracked(
                        gcx,
                        caret,
                        TextInput::new()
                            .value(name)
                            .placeholder(NAME_PLACEHOLDER)
                            .layout(LayoutStyle::default().w(field_w).h(1))
                            .element(gcx, &t),
                    )
                    .build(),
                ));
                col = col.child(super::kit::sentence_indent(
                    &t,
                    NAME_HELP,
                    w,
                    label_w as usize,
                    t.text_faint,
                ));
                let vals = values.clone();
                let field_sources = sources.clone();
                let every_state = every_state.clone();
                col = col.child(dyn_view_scoped(LayoutStyle::column().gap(0), move |fcx| {
                    let t = use_theme(fcx).get().tokens;
                    let sources = &field_sources;
                    let i = src_idx.get().min(sources.len() - 1);
                    vals.borrow_mut().clear();
                    let mut fc = Element::new().style(LayoutStyle::column().gap(0));
                    for f in form_fields(&sources[i].connection_schema) {
                        let label = format!("{}{}", f.label, if f.required { " *" } else { "" });
                        // A feed's Check every is the picker's too (it prefills it).
                        let feed_every = f.key == "every" && sources[i].id == FEED_SOURCE && is_duration(&f);
                        let control = match f.kind {
                            "enum" | "boolean" => {
                                let opts = choice_options(&f);
                                let init = initial_value(&f);
                                let at = fcx
                                    .signal(opts.iter().position(|(v, _)| *v == init).unwrap_or(0));
                                let raw: Vec<String> =
                                    opts.iter().map(|(v, _)| v.clone()).collect();
                                vals.borrow_mut().push((
                                    f.key.clone(),
                                    Box::new(move || {
                                        raw.get(at.get_untracked()).cloned().unwrap_or_default()
                                    }),
                                ));
                                Select::new(
                                    opts.iter()
                                        .map(|(_, l)| SelectOption::new(l.clone()))
                                        .collect(),
                                )
                                .value(at)
                                .element(fcx, &t)
                                .build()
                            }
                            "string" if is_duration(&f) => {
                                // Check every [15] [minutes] (the web's control).
                                let (n0, u0) = duration_parts(&initial_value(&f));
                                let (n, u) = if feed_every {
                                    // Seeded once with the form default (released before the sets).
                                    let seed = !std::mem::replace(&mut every_state.borrow_mut().1, true);
                                    if seed {
                                        every_n.set_if_changed(n0);
                                        every_u.set_if_changed(u0);
                                    }
                                    (every_n, every_u)
                                } else {
                                    (fcx.signal(n0), fcx.signal(u0))
                                };
                                vals.borrow_mut().push((
                                    f.key.clone(),
                                    Box::new(move || duration_value(&n.get_untracked(), u.get_untracked())),
                                ));
                                Element::new()
                                    .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                                    .child(
                                        super::w::caret_tracked(
                                            fcx,
                                            caret,
                                            TextInput::new()
                                                .value(n)
                                                .layout(
                                                    LayoutStyle::default().w(6).h(1).shrink(0.0),
                                                )
                                                .element(fcx, &t),
                                        )
                                        .build(),
                                    )
                                    .child(
                                        Select::new(
                                            DURATION_UNITS
                                                .iter()
                                                .map(|(_, w)| SelectOption::new(w.to_string()))
                                                .collect(),
                                        )
                                        .value(u)
                                        .layout(LayoutStyle::default().w(14).h(1).shrink(0.0))
                                        .element(fcx, &t)
                                        .build(),
                                    )
                                    .build()
                            }
                            "readonly" => sentence(
                                &t,
                                &format!("Not editable here: {}", f.schema),
                                field_w,
                                t.text_faint,
                            ),
                            _ => {
                                // A feed's address is the picker's too (it fills it in).
                                let sig = if f.key == "url" && sources[i].id == FEED_SOURCE {
                                    url_sig
                                } else {
                                    fcx.signal(initial_value(&f))
                                };
                                vals.borrow_mut()
                                    .push((f.key.clone(), Box::new(move || sig.get_untracked())));
                                let ph = f
                                    .schema
                                    .get("examples")
                                    .and_then(Value::as_array)
                                    .and_then(|a| a.first())
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_string();
                                super::w::caret_tracked(
                                    fcx,
                                    caret,
                                    TextInput::new()
                                        .value(sig)
                                        .placeholder(&ph)
                                        .masked(f.kind == "password")
                                        .layout(LayoutStyle::default().w(field_w).h(1))
                                        .element(fcx, &t),
                                )
                                .build()
                            }
                        };
                        fc = fc.child(super::w::field_row(&t, &label, label_w, control));
                        if feed_every {
                            // "Recommended for this feed" while it holds the pick's interval.
                            let es = every_state.clone();
                            fc = fc.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                                let t = abstracttui::app::current_theme().tokens;
                                let now = duration_value(&every_n.get(), every_u.get());
                                if every_rec.get() && es.borrow().0.as_deref() == Some(now.as_str()) {
                                    super::kit::sentence_indent(&t, EVERY_RECOMMENDED, w, label_w as usize, t.text_faint)
                                } else {
                                    Element::new().style(LayoutStyle::default().h(0)).build()
                                }
                            }));
                        }
                        if description_shown(&f) {
                            fc = fc.child(super::kit::sentence_indent(
                                &t,
                                &f.description,
                                w,
                                label_w as usize,
                                t.text_faint,
                            ));
                        }
                    }
                    fc.build()
                }));
                col = col.child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    let t = abstracttui::app::current_theme().tokens;
                    let n = note.get();
                    if n.is_empty() {
                        Element::new().style(LayoutStyle::default().h(0)).build()
                    } else {
                        sentence(&t, &n, w, t.error)
                    }
                }));
                col = col.child(
                    Element::new()
                        .style(LayoutStyle::default().grow(1.0))
                        .build(),
                );
                let (c3, vals2, srcs) = (c2.clone(), values.clone(), sources.clone());
                let all = ALL_VIEW.with(|a| a.get()) && is_admin(&c2);
                col = col.child(super::w::form::button_row(vec![
                    button(
                        gcx,
                        &t,
                        &Action::label("cancel", "Cancel"),
                        On::Raised,
                        false,
                        {
                            let close = close_cancel.clone();
                            move || close()
                        },
                    ),
                    button(
                        gcx,
                        &t,
                        &Action::label("create", "Create"),
                        On::Raised,
                        true,
                        move || {
                            if c3
                                .store
                                .json
                                .write_untracked(K_CREATE)
                                .is_some_and(|w| w.is_pending())
                            {
                                return;
                            }
                            let s = &srcs[src_idx.get_untracked().min(srcs.len() - 1)];
                            let label = name.get_untracked().trim().to_string();
                            let raw: Vec<(String, String)> = vals2
                                .borrow()
                                .iter()
                                .map(|(k, f)| (k.clone(), f()))
                                .collect();
                            let (config, mut problems) =
                                config_from(&form_fields(&s.connection_schema), &raw);
                            if label.is_empty() {
                                problems.insert(0, "Name is required.".into());
                            }
                            // A template's value: required and of its form while the
                            // address is still the one it built (the web's trgCheckFeedParam).
                            if s.id == FEED_SOURCE {
                                if let Some(f) = picked_feed(&c3, feed_pick.get_untracked()) {
                                    let v = feed_param.get_untracked().trim().to_string();
                                    let url = config.get("url").and_then(Value::as_str).unwrap_or("").to_string();
                                    if f.placeholder.is_some() && (url.is_empty() || url == feed_address(&f, &v)) {
                                        if let Some(p) = feed_param_problem(&f, &v) {
                                            problems.retain(|x| !x.starts_with("Feed address is required"));
                                            problems.push(p);
                                        }
                                    }
                                }
                            }
                            if !problems.is_empty() {
                                note.set(problems.join(" "));
                                return;
                            }
                            note.set(String::new());
                            c3.store.json.set_write(K_CREATE, Some(WriteState::Pending));
                            c3.send(Cmd::Json(JsonCmd::Send {
                                key: K_CREATE.into(),
                                method: "POST".into(),
                                path: list_path(false),
                                body: create_body(s, &label, config),
                                slow: false,
                                label: format!("Create {label}"),
                                reload: vec![(K_LIST.into(), list_path(all))],
                                journal: true,
                            }));
                        },
                    ),
                ]));
                if let Some(WriteState::Failed(e)) = c2.store.json.write(K_CREATE) {
                    note.set(format!("Not created: {}", refusal(&e)));
                }
                col.build()
            })
        });
}
