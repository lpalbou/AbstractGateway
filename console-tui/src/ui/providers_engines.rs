//! Providers → "Local providers": one row per local engine, the web
//! console's engine cards (console_ui.py `engineCardMarkup`, `engineState`,
//! `engineAction`, `engineLocationChoice`; console.py `localProviderExtras`)
//! in the terminal. Round 7 (R7.2): the old Engines tab is merged here.
//!
//! Contract `gateway_engines_v2` (routes/engines.py): `GET /engines?probe=1`,
//! `POST /engines/{id}/install {dry_run, location}` (a dry run plans the two
//! app locations, runs nothing), `POST /engines/{id}/start|stop`,
//! `GET /engines/jobs[/{job}]`, `POST /engines/jobs/{job}/cancel|continue`.
//! Every call rides the plain JSON lane (`Cmd::Json`, `store.json`) — the
//! web's routes and bodies exactly. The gateway decides which actions exist
//! (`row.actions[]` with `enabled` + `reason`); this file renders them and
//! never guesses.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use abstracttui::prelude::*;
use serde_json::{json, Value};

use super::super::kit::{Row, WrapTable};
use super::super::util::{line, span, span_bold, wrap_text};
use super::super::widths::ColRule;
use super::super::Ctx;
use crate::store::json::WriteState;
use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

/// `GET /engines?probe=1` lands here.
pub const SLOT_ENGINES: &str = "engines";
pub const PATH_ENGINES: &str = "/engines?probe=1";
/// `GET /engines/jobs` (the newest job per engine survives a reload).
pub const SLOT_JOBS: &str = "engines.jobs";
pub const PATH_JOBS: &str = "/engines/jobs";

/// The JSON-lane key of an engine's tracked job.
pub fn job_slot(engine: &str) -> String {
    format!("engines.job.{engine}")
}

/// The JSON-lane key of an engine's action write.
pub fn action_key(engine: &str) -> String {
    format!("engine.{engine}")
}

/// The JSON-lane key of one install-location dry run.
pub fn plan_key(engine: &str, location: &str) -> String {
    format!("engine.plan.{engine}.{location}")
}

/// The two desktop engines' own downloads (console_ui.py
/// `ENGINE_DOWNLOAD_LINKS`): offered when the gateway cannot list engines.
pub const ENGINE_DOWNLOAD_LINKS: [(&str, &str, &str); 2] = [
    ("ollama", "Ollama", "https://ollama.com/download"),
    ("lmstudio", "LM Studio", "https://lmstudio.ai/download"),
];

/// Engine id → the catalog provider its builds use (console_catalog.py
/// `MC_ENGINE_PROVIDER`): "Browse models" filters the Models page by it.
pub fn catalog_provider(engine: &str) -> Option<&'static str> {
    match engine {
        "mlx" => Some("mlx"),
        "ollama" => Some("ollama"),
        "lmstudio" => Some("lmstudio"),
        "huggingface" | "llamacpp" => Some("huggingface"),
        _ => None,
    }
}

/// A local provider's server connection (console.py
/// `LOCAL_PROVIDER_CONNECTIONS`): (family, fixed profile id, name, description).
pub fn local_connection(
    engine: &str,
) -> Option<(
    &'static str,
    Option<&'static str>,
    &'static str,
    &'static str,
)> {
    match engine {
        "ollama" => Some(("ollama", None, "", "")),
        "lmstudio" => Some(("lmstudio", None, "", "")),
        "vllm" => Some((
            "openai-compatible",
            Some("vllm"),
            "vLLM server",
            "vLLM OpenAI-compatible server.",
        )),
        _ => None,
    }
}

/// The profile ids reserved for local connections (vLLM's fixed id): the
/// Remote presets never count them.
pub const LOCAL_CONNECTION_PROFILE_IDS: [&str; 1] = ["vllm"];

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

/// `engineJobActive`.
pub fn job_active(job: Option<&Value>) -> bool {
    job.is_some_and(|j| {
        matches!(
            s(j, "state"),
            "queued" | "downloading" | "installing" | "needs_admin" | "needs_tools"
        )
    })
}

/// `engineAction_`: the gateway's action row `id` for this engine.
pub fn action<'a>(e: &'a Value, id: &str) -> Option<&'a Value> {
    e.get("actions")
        .and_then(Value::as_array)?
        .iter()
        .find(|a| s(a, "id") == id)
}

fn enabled(a: &Value) -> bool {
    a.get("enabled").and_then(Value::as_bool).unwrap_or(false)
}

/// `engineState`: (key, pill text).
pub fn engine_state(e: &Value, job: Option<&Value>) -> (&'static str, &'static str) {
    let st = job.map(|j| s(j, "state")).unwrap_or("");
    if st == "needs_admin" {
        return ("waiting", "Needs your approval");
    }
    if st == "needs_tools" {
        return ("waiting", "Needs Apple tools");
    }
    if job_active(job) {
        return ("installing", "Installing");
    }
    if e.get("supported").and_then(Value::as_bool) == Some(false) {
        return ("unsupported", "Not for this computer");
    }
    if e.get("installed").and_then(Value::as_bool) != Some(true) {
        return ("absent", "Not installed");
    }
    match e.get("running").and_then(Value::as_bool) {
        None => ("ready", "Ready"),
        Some(true) if e.get("reachable").and_then(Value::as_bool) != Some(false) => {
            ("running", "Running")
        }
        Some(true) => ("starting", "Not answering"),
        Some(false) => ("stopped", "Installed, stopped"),
    }
}

/// The engines in the web's order: installed first, then not installed,
/// then the ones this computer cannot run (`rank`), stable otherwise.
pub fn ordered_engines(data: &Value) -> Vec<Value> {
    let mut v: Vec<Value> = data
        .get("engines")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|e| e.is_object())
        .collect();
    let rank = |e: &Value| {
        if e.get("supported").and_then(Value::as_bool) == Some(false) {
            3
        } else if e.get("installed").and_then(Value::as_bool) == Some(true) {
            0
        } else {
            1
        }
    };
    v.sort_by_key(rank);
    v
}

/// The summary toolbar: "4 of 6 installed, 2 running · checked 02:25:51".
pub fn summary_line(data: &Value) -> String {
    let engines: Vec<&Value> = data
        .get("engines")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|e| e.is_object()).collect())
        .unwrap_or_default();
    let installed = engines
        .iter()
        .filter(|e| e.get("installed").and_then(Value::as_bool) == Some(true))
        .count();
    let running = engines
        .iter()
        .filter(|e| e.get("running").and_then(Value::as_bool) == Some(true))
        .count();
    let mut out = format!("{installed} of {} installed", engines.len());
    if running > 0 {
        out.push_str(&format!(", {running} running"));
    }
    let when = s(data, "generated_at");
    if let Some(t) = when.split('T').nth(1) {
        let hms: String = t.chars().take(8).collect();
        if !hms.is_empty() {
            out.push_str(&format!(" · checked {hms} UTC"));
        }
    }
    out
}

/// The tracked job of an engine: this console's (`engines.job.<id>`), else
/// the row's `active_job`, else the newest from `GET /engines/jobs`.
pub fn job_of(store: &Store, e: &Value) -> Option<Value> {
    let id = s(e, "id");
    if let Loadable::Ready(j) = store.json.get_untracked(&job_slot(id)) {
        if j.is_object() {
            return Some(j);
        }
    }
    if let Some(j) = e.get("active_job").filter(|j| j.is_object()) {
        return Some(j.clone());
    }
    if let Loadable::Ready(list) = store.json.get_untracked(SLOT_JOBS) {
        if let Some(j) = list
            .get("jobs")
            .and_then(Value::as_array)
            .and_then(|a| a.iter().find(|j| s(j, "engine") == id))
        {
            return Some(j.clone());
        }
    }
    None
}

/// Any engine install running (not merely waiting on the person): the
/// console is its only live view, so `q` asks first (the old Engines tab's
/// rule, kept on Providers).
pub fn engine_job_running(store: &Store) -> bool {
    let Loadable::Ready(data) = store.json.get_untracked(SLOT_ENGINES) else {
        return false;
    };
    ordered_engines(&data).iter().any(|e| {
        let j = job_of(store, e);
        job_active(j.as_ref())
            && !matches!(
                j.as_ref().map(|j| s(j, "state")),
                Some("needs_admin") | Some("needs_tools")
            )
    })
}

/// `uiProgressMarkup` as one line: label, percent or phase, bytes, message.
pub fn progress_line(job: &Value, label: &str) -> String {
    let num = |k: &str| job.get(k).and_then(Value::as_f64);
    let done = num("bytes_done").or_else(|| num("downloaded_bytes"));
    let total = num("bytes_total")
        .filter(|t| *t > 0.0)
        .or_else(|| num("total_bytes").filter(|t| *t > 0.0));
    let pct = num("percent")
        .map(|p| p.clamp(0.0, 100.0))
        .or_else(|| match (done, total) {
            (Some(d), Some(t)) => Some((d / t * 100.0).clamp(0.0, 100.0)),
            _ => None,
        });
    let phase = match s(job, "state") {
        "queued" => "Waiting to start",
        "downloading" => "Downloading",
        "installing" => "Installing",
        "done" => "Done",
        "failed" => "Failed",
        "cancelled" => "Cancelled",
        _ => "Working",
    };
    let mut out = format!(
        "{label} — {}",
        match pct {
            Some(p) if p < 10.0 => format!("{p:.1}%"),
            Some(p) => format!("{p:.0}%"),
            None => phase.to_string(),
        }
    );
    if let (Some(d), Some(t)) = (done, total) {
        out.push_str(&format!(" · {} of {}", bytes(d), bytes(t)));
    }
    let msg = s(job, "message").trim();
    if !msg.is_empty() {
        out.push_str(&format!(" · {msg}"));
    }
    out
}

fn bytes(n: f64) -> String {
    let units = ["B", "KB", "MB", "GB", "TB"];
    let mut v = n;
    let mut i = 0;
    while v >= 1024.0 && i < units.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{v:.0} {}", units[i])
    } else {
        format!("{v:.1} {}", units[i])
    }
}

/// A card's result line (`engineStore.notices`).
#[derive(Clone, Debug, PartialEq)]
pub struct Notice {
    /// ok | info | warn | err
    pub tone: &'static str,
    pub text: String,
    pub hint: String,
    pub details: String,
}

/// The page-local state of the Local providers section (Copy: signals).
#[derive(Clone, Copy)]
pub struct EnginesUi {
    pub sel: Signal<usize>,
    pub expanded: Signal<Option<usize>>,
    /// The engine whose install confirmation is open.
    pub confirm: Signal<Option<String>>,
    /// engine id → (action, its "Starting..." label): what the page sent.
    pub sent: Signal<HashMap<String, (String, String)>>,
    pub notices: Signal<HashMap<String, Notice>>,
}

impl EnginesUi {
    /// `sel` = the shared screens store's `engine_sel` (durable across tab
    /// switches; the old Engines tab's selection, kept).
    pub fn new(cx: Scope, sel: Signal<usize>) -> EnginesUi {
        EnginesUi {
            sel,
            expanded: cx.signal(None),
            confirm: cx.signal(None),
            sent: cx.signal(HashMap::new()),
            notices: cx.signal(HashMap::new()),
        }
    }
}

/// `ENGINE_PENDING`: the clicked button's words until the gateway answers.
pub fn pending_label(action: &str) -> &'static str {
    match action {
        "install-go" => "Starting the install...",
        "start" => "Starting...",
        "stop" => "Stopping...",
        "cancel" => "Cancelling...",
        "continue:approve_admin" => "Waiting for the password...",
        "continue:install_tools" => "Opening Apple's installer...",
        "continue:recheck" => "Checking...",
        _ => "Working...",
    }
}

/// Read the engines (and, once, the job list) — the web's `engineRefresh`.
pub fn refresh(ctx: &Ctx) {
    let store = ctx.store;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        return;
    }
    if store.json.get_untracked(SLOT_ENGINES).ready().is_none() {
        store.json.set(SLOT_ENGINES, Loadable::Loading);
    }
    ctx.send(Cmd::Json(JsonCmd::get_slow(SLOT_ENGINES, PATH_ENGINES)));
    if matches!(store.json.get_untracked(SLOT_JOBS), Loadable::NotAsked) {
        store.json.set(SLOT_JOBS, Loadable::Loading);
        ctx.send(Cmd::Json(JsonCmd::get(SLOT_JOBS, PATH_JOBS)));
    }
}

fn engine_by_id(store: &Store, id: &str) -> Option<Value> {
    store
        .json
        .get_untracked(SLOT_ENGINES)
        .ready()
        .and_then(|d| ordered_engines(d).into_iter().find(|e| s(e, "id") == id))
}

fn name_of(e: &Value) -> String {
    let n = s(e, "name");
    if n.is_empty() {
        s(e, "id").to_string()
    } else {
        n.to_string()
    }
}

/// POST an engine action through the JSON lane (`enginePost`).
fn post(ctx: &Ctx, ui: EnginesUi, id: &str, action: &str, path: String, body: Value) {
    let name = engine_by_id(&ctx.store, id)
        .map(|e| name_of(&e))
        .unwrap_or_else(|| id.to_string());
    ui.notices.update(|m| {
        m.remove(id);
    });
    ui.sent.update(|m| {
        m.insert(
            id.to_string(),
            (action.to_string(), pending_label(action).to_string()),
        );
    });
    ctx.store
        .json
        .set_write(&action_key(id), Some(WriteState::Pending));
    let label = match action {
        "install-go" => format!("Install {name}"),
        "start" => format!("Start {name}"),
        "stop" => format!("Stop {name}"),
        "cancel" => format!("Cancel the {name} install"),
        a => format!("{name}: {}", a.trim_start_matches("continue:")),
    };
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: action_key(id),
        method: "POST".into(),
        path,
        body,
        slow: true,
        label,
        reload: Vec::new(),
        journal: true,
    }));
}

fn busy(store: &Store, id: &str) -> bool {
    store
        .json
        .write_untracked(&action_key(id))
        .is_some_and(|w| w.is_pending())
}

/// Run one engine action (the web's `engineAction`), by its action id:
/// install · install-go:<location> · install-cancel · start · stop ·
/// cancel · continue:<what> · models · refresh.
pub fn run_action(ctx: &Ctx, ui: EnginesUi, id: &str, act: &str) {
    let store = ctx.store;
    if act == "refresh" {
        ui.notices.set(HashMap::new());
        refresh(ctx);
        return;
    }
    if act == "models" {
        let e = engine_by_id(&store, id);
        let provider = e
            .as_ref()
            .map(|e| {
                let p = s(e, "provider");
                if p.is_empty() { s(e, "id") } else { p }.to_string()
            })
            .unwrap_or_else(|| id.to_string());
        // The Models page reads the shared engine filter (Browse models =
        // the catalog filtered to this engine's builds).
        ctx.screens
            .store
            .engine_filter
            .set(catalog_provider(&provider).map(str::to_string));
        ctx.ui.screen.set(super::super::SCREEN_CATALOG);
        return;
    }
    if busy(&store, id) {
        store.notice.set(Some(format!(
            "{} — waiting for the gateway's answer to the last action",
            id
        )));
        return;
    }
    let e = engine_by_id(&store, id).unwrap_or_else(|| json!({"id": id, "name": id}));
    let enc = crate::api::urlencode(id);
    if act == "install" {
        ui.confirm.set(Some(id.to_string()));
        ui.notices.update(|m| {
            m.remove(id);
        });
        let app = e
            .get("install")
            .map(|i| s(i, "method") == "app")
            .unwrap_or(false);
        if app {
            // engineLoadPlans: the two real plans (dry runs run nothing).
            for loc in ["user", "system"] {
                let k = plan_key(id, loc);
                if store
                    .json
                    .write_untracked(&k)
                    .is_some_and(|w| matches!(w, WriteState::Done(_) | WriteState::Pending))
                {
                    continue;
                }
                store.json.set_write(&k, Some(WriteState::Pending));
                ctx.send(Cmd::Json(JsonCmd::Send {
                    key: k,
                    method: "POST".into(),
                    path: format!("/engines/{enc}/install"),
                    body: json!({"dry_run": true, "location": loc}),
                    slow: true,
                    label: format!("Check where {} can go", name_of(&e)),
                    reload: Vec::new(),
                    journal: false,
                }));
            }
        }
        return;
    }
    if act == "install-cancel" {
        ui.confirm.set(None);
        return;
    }
    if let Some(loc) = act.strip_prefix("install-go:") {
        ui.confirm.set(None);
        post(
            ctx,
            ui,
            id,
            "install-go",
            format!("/engines/{enc}/install"),
            json!({"dry_run": false, "location": loc}),
        );
        return;
    }
    if act == "start" || act == "stop" {
        post(ctx, ui, id, act, format!("/engines/{enc}/{act}"), json!({}));
        return;
    }
    let Some(job) = job_of(&store, &e) else {
        store.notice.set(Some(format!(
            "{} has no install job to act on",
            name_of(&e)
        )));
        return;
    };
    let jid = crate::api::urlencode(s(&job, "job_id"));
    if act == "cancel" {
        post(
            ctx,
            ui,
            id,
            act,
            format!("/engines/jobs/{jid}/cancel"),
            json!({}),
        );
    } else if let Some(what) = act.strip_prefix("continue:") {
        post(
            ctx,
            ui,
            id,
            act,
            format!("/engines/jobs/{jid}/continue"),
            json!({"action": what}),
        );
    }
}

fn body_details(body: Option<&Value>) -> String {
    let Some(data) = body else {
        return String::new();
    };
    let d = data.get("detail").filter(|d| d.is_object()).unwrap_or(data);
    let mut parts: Vec<String> = Vec::new();
    for k in ["details", "hint", "fix", "log", "log_tail"] {
        match d.get(k) {
            Some(Value::String(v)) if !v.trim().is_empty() => parts.push(v.trim().to_string()),
            Some(Value::Array(a)) if !a.is_empty() => parts.push(
                a.iter()
                    .map(|x| {
                        x.as_str()
                            .map(str::to_string)
                            .unwrap_or_else(|| x.to_string())
                    })
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            _ => {}
        }
    }
    parts.push(serde_json::to_string_pretty(data).unwrap_or_default());
    parts.join("\n\n")
}

/// Turn finished writes into card notices and tracked jobs; turn finished
/// jobs into "is installed." notices (the web's `enginePoll`); poll active
/// jobs once a second while this page is mounted.
pub fn install_effects(cx: Scope, ctx: &Ctx, ui: EnginesUi) {
    let store = ctx.store;
    // Writes → notices / jobs.
    {
        let ctx = ctx.clone();
        cx.effect(move || {
            let writes = store.json.writes.get();
            let sent = ui.sent.get_untracked();
            for (id, (act, _)) in sent {
                let Some(w) = writes.get(&action_key(&id)) else {
                    continue;
                };
                let e = engine_by_id(&store, &id).unwrap_or_else(|| json!({"id": id}));
                let name = name_of(&e);
                let notice = match w {
                    WriteState::Pending => continue,
                    WriteState::Failed(err) => {
                        let what = match act.as_str() {
                            "install-go" => format!("Could not start installing {name}"),
                            "start" | "stop" => format!("Could not {act} {name}"),
                            "cancel" => "Could not cancel".to_string(),
                            _ => "Could not continue".to_string(),
                        };
                        Some(Notice {
                            tone: "err",
                            text: format!("{what}: {}", err.message),
                            hint: String::new(),
                            details: body_details(err.body.as_ref()),
                        })
                    }
                    WriteState::Done(res) => {
                        if act == "start" || act == "stop" {
                            let running = res.get("running").and_then(Value::as_bool);
                            let ok = if act == "start" {
                                running != Some(false)
                            } else {
                                running != Some(true)
                            };
                            let text = match (act.as_str(), ok) {
                                ("start", true) => format!("{name} is running."),
                                ("start", false) => {
                                    format!("{name} was started but is not answering yet.")
                                }
                                (_, true) => format!("{name} is stopped."),
                                (_, false) => format!("{name} is still running."),
                            };
                            ctx.send(Cmd::Json(JsonCmd::get_slow(SLOT_ENGINES, PATH_ENGINES)));
                            Some(Notice {
                                tone: if ok { "ok" } else { "warn" },
                                text,
                                hint: s(res, "message").to_string(),
                                details: String::new(),
                            })
                        } else {
                            let job = if res.get("job_id").is_some() {
                                Some(res.clone())
                            } else {
                                res.get("job").filter(|j| j.is_object()).cloned()
                            };
                            if let Some(j) = job {
                                store.json.set(&job_slot(&id), Loadable::Ready(j));
                            }
                            None
                        }
                    }
                };
                ui.sent.update(|m| {
                    m.remove(&id);
                });
                if let Some(n) = notice {
                    ui.notices.update(|m| {
                        m.insert(id.clone(), n);
                    });
                }
            }
        });
    }
    // Jobs that finished → notice + re-read the engines.
    {
        let ctx = ctx.clone();
        let was_active: Rc<RefCell<HashSet<String>>> = Rc::new(RefCell::new(HashSet::new()));
        cx.effect(move || {
            let _ = store.json.slots.get();
            let Loadable::Ready(data) = store.json.get_untracked(SLOT_ENGINES) else {
                return;
            };
            let mut finished = false;
            for e in ordered_engines(&data) {
                let id = s(&e, "id").to_string();
                let job = job_of(&store, &e);
                let active = job_active(job.as_ref());
                let was = was_active.borrow().contains(&id);
                if active {
                    was_active.borrow_mut().insert(id);
                } else if was {
                    was_active.borrow_mut().remove(&id);
                    let name = name_of(&e);
                    if let Some(j) = &job {
                        let msg = s(j, "message");
                        let text = match s(j, "state") {
                            "done" => Some((
                                "ok",
                                if msg.is_empty() {
                                    format!("{name} is installed.")
                                } else {
                                    msg.to_string()
                                },
                            )),
                            "cancelled" => Some((
                                "info",
                                if msg.is_empty() {
                                    format!("The {name} install was cancelled.")
                                } else {
                                    msg.to_string()
                                },
                            )),
                            _ => None,
                        };
                        if let Some((tone, text)) = text {
                            ui.notices.update(|m| {
                                m.insert(
                                    id.clone(),
                                    Notice {
                                        tone,
                                        text,
                                        hint: String::new(),
                                        details: String::new(),
                                    },
                                );
                            });
                        }
                    }
                    finished = true;
                }
            }
            if finished {
                ctx.send(Cmd::Json(JsonCmd::get_slow(SLOT_ENGINES, PATH_ENGINES)));
            }
        });
    }
    // The poll: one GET per active job per second (3 s while every active
    // job waits on the person — the web's cadence, rounded to one tick).
    {
        let ctx = ctx.clone();
        let tick = Rc::new(std::cell::Cell::new(0u32));
        let handle =
            abstracttui::reactive::interval(cx, std::time::Duration::from_secs(1), move || {
                let Loadable::Ready(data) = store.json.get_untracked(SLOT_ENGINES) else {
                    return;
                };
                let n = tick.get().wrapping_add(1);
                tick.set(n);
                let jobs: Vec<(String, Value)> = ordered_engines(&data)
                    .iter()
                    .filter_map(|e| job_of(&store, e).map(|j| (s(e, "id").to_string(), j)))
                    .filter(|(_, j)| job_active(Some(j)))
                    .collect();
                if jobs.is_empty() {
                    return;
                }
                let waiting = jobs
                    .iter()
                    .all(|(_, j)| matches!(s(j, "state"), "needs_admin" | "needs_tools"));
                if waiting && !n.is_multiple_of(3) {
                    return;
                }
                for (id, j) in jobs {
                    let jid = s(&j, "job_id");
                    if jid.is_empty() {
                        continue;
                    }
                    ctx.send(Cmd::Json(JsonCmd::get(
                        &job_slot(&id),
                        format!("/engines/jobs/{}", crate::api::urlencode(jid)),
                    )));
                }
            });
        // The page scope owns it: it dies with the page.
        std::mem::forget(handle);
    }
}

/// What one engine row offers, as (action id, label) — the web's buttons,
/// admin-gated the same way (a non-admin's card does not render Install /
/// Start / Stop; Browse models and Learn more stay).
pub fn row_actions(
    e: &Value,
    job: Option<&Value>,
    admin: bool,
    confirm_open: bool,
) -> Vec<(String, String)> {
    let (key, _) = engine_state(e, job);
    let install = e.get("install").cloned().unwrap_or(json!({}));
    let needs_admin = install.get("needs_admin").and_then(Value::as_bool) == Some(true);
    let mut out: Vec<(String, String)> = Vec::new();
    let st = job.map(|j| s(j, "state")).unwrap_or("");
    let cont: Vec<&str> = job
        .and_then(|j| j.get("continue_actions"))
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let failed = matches!(st, "failed" | "cancelled")
        && e.get("installed").and_then(Value::as_bool) != Some(true);
    if st == "needs_admin" {
        let p = job
            .and_then(|j| j.get("admin_prompt"))
            .cloned()
            .unwrap_or(json!({}));
        if admin && cont.contains(&"approve_admin") {
            let b = s(&p, "button");
            out.push((
                "continue:approve_admin".into(),
                if b.is_empty() {
                    "Continue with administrator password"
                } else {
                    b
                }
                .into(),
            ));
        }
        if admin && cont.contains(&"recheck") {
            out.push(("continue:recheck".into(), "Re-check".into()));
        }
    } else if st == "needs_tools" {
        let a = job
            .and_then(|j| j.get("tools_prompt"))
            .and_then(|p| p.get("action"))
            .cloned()
            .unwrap_or(json!({}));
        if admin
            && cont.contains(&"install_tools")
            && a.get("available").and_then(Value::as_bool) != Some(false)
        {
            let b = s(&a, "button");
            out.push((
                "continue:install_tools".into(),
                if b.is_empty() { "Install tools" } else { b }.into(),
            ));
        }
        if admin && cont.contains(&"recheck") {
            out.push(("continue:recheck".into(), "Re-check".into()));
        }
    } else if key == "installing" || key == "unsupported" || confirm_open {
        // The progress bar / the reason / the confirmation speaks.
    } else {
        let inst = if admin { action(e, "install") } else { None };
        if let Some(i) = inst {
            if enabled(i) {
                out.push((
                    "install".into(),
                    if failed {
                        "Try again".into()
                    } else if needs_admin {
                        "Install (administrator)".into()
                    } else {
                        "Install".into()
                    },
                ));
            }
        }
        let start = if admin { action(e, "start") } else { None };
        if let Some(a) = start {
            if key != "running" && enabled(a) {
                let l = s(a, "label");
                out.push((
                    "start".into(),
                    if l.is_empty() { "Start" } else { l }.into(),
                ));
            }
        }
        let browse_extra = e.get("supported").and_then(Value::as_bool) != Some(false)
            && catalog_provider({
                let p = s(e, "provider");
                if p.is_empty() {
                    s(e, "id")
                } else {
                    p
                }
            })
            .is_some();
        let installed = e.get("installed").and_then(Value::as_bool) == Some(true);
        if (matches!(key, "running" | "ready" | "starting") && installed) || browse_extra {
            out.push(("models".into(), "Browse models".into()));
        }
        let stop = if admin { action(e, "stop") } else { None };
        if let Some(a) = stop {
            if enabled(a) {
                let l = s(a, "label");
                out.push(("stop".into(), if l.is_empty() { "Stop" } else { l }.into()));
            }
        }
        let page = action(e, "open_page");
        let inst_enabled = inst.is_some_and(enabled);
        if page.is_some() && action(e, "recheck").is_some() && !inst_enabled {
            out.push(("refresh".into(), "I installed it, check again".into()));
        }
    }
    let can_cancel = job
        .and_then(|j| j.get("can_cancel"))
        .and_then(Value::as_bool)
        == Some(true);
    if can_cancel && job_active(job) && admin {
        out.push(("cancel".into(), "Cancel".into()));
    }
    out
}

/// The key of each action (the row's key column and the page's shortcuts).
pub fn action_char(act: &str) -> char {
    match act {
        "install" => 'i',
        "start" => 's',
        "stop" => 'x',
        "models" => 'b',
        "cancel" => 'c',
        "continue:approve_admin" => 'A',
        "continue:install_tools" => 'T',
        "continue:recheck" => 'k',
        "refresh" => 'k',
        _ => '?',
    }
}

/// Everything an engine card's body says (the detail lines Enter shows):
/// blurb, facts, the notice, the state's sentence, the connection, the log,
/// the links.
pub fn detail_lines(
    e: &Value,
    job: Option<&Value>,
    admin: bool,
    notice: Option<&Notice>,
    connections: &[String],
) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let desc = s(e, "description");
    if !desc.is_empty() {
        out.push(desc.to_string());
    }
    let base = s(e, "base_url");
    if !base.is_empty() {
        out.push(format!("Address {base}"));
    }
    let loc = s(e, "install_location");
    if !loc.is_empty() {
        out.push(format!("Location {loc}"));
    }
    if let Some(n) = notice {
        out.push(n.text.clone());
        if !n.hint.is_empty() {
            out.push(n.hint.clone());
        }
        if !n.details.is_empty() {
            out.push(format!("Show details: {}", n.details));
        }
    }
    out.extend(state_sentences(e, job, admin));
    if local_connection(s(e, "id")).is_some() {
        if connections.is_empty() {
            out.push("Connection: none (a sets one up)".into());
        } else {
            out.push("Connection".into());
            out.extend(connections.iter().map(|c| format!("  {c}")));
        }
    }
    if let Some(j) = job {
        let log = match j.get("details").and_then(Value::as_str) {
            Some(d) if !d.is_empty() => d.to_string(),
            _ => j
                .get("log_tail")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<_>>()
                        .join("\n")
                })
                .unwrap_or_default(),
        };
        if !log.is_empty() {
            out.push(format!("Show details: {log}"));
        }
    }
    if let Some(page) = action(e, "open_page") {
        let inst_enabled = admin && action(e, "install").is_some_and(enabled);
        let url = s(page, "url");
        if !url.is_empty() && !inst_enabled {
            out.push(format!("Download page {url}"));
        }
    }
    if let Some(d) = action(e, "docs") {
        let url = s(d, "url");
        if !url.is_empty() {
            out.push(format!("Learn more {url}"));
        }
    }
    out
}

/// The sentence(s) of the card's state (web `body`), without the confirm.
pub fn state_sentences(e: &Value, job: Option<&Value>, admin: bool) -> Vec<String> {
    let (key, _) = engine_state(e, job);
    let mut out = Vec::new();
    let st = job.map(|j| s(j, "state")).unwrap_or("");
    let name = name_of(e);
    if st == "needs_admin" {
        let j = job.unwrap();
        let p = j.get("admin_prompt").cloned().unwrap_or(json!({}));
        let msg = [s(j, "message"), s(&p, "reason")]
            .into_iter()
            .find(|m| !m.is_empty())
            .unwrap_or("This step needs an administrator.");
        out.push(msg.to_string());
        if !s(&p, "command").is_empty() {
            out.push("It will run, as administrator:".into());
            out.push(s(&p, "command").to_string());
        }
        if !s(&p, "where").is_empty() {
            out.push(s(&p, "where").to_string());
        }
    } else if st == "needs_tools" {
        let j = job.unwrap();
        let p = j.get("tools_prompt").cloned().unwrap_or(json!({}));
        let msg = [s(j, "message"), s(&p, "reason")]
            .into_iter()
            .find(|m| !m.is_empty())
            .unwrap_or("This install needs extra tools.");
        out.push(msg.to_string());
        if p.get("started").and_then(Value::as_bool) == Some(true) {
            out.push("Apple's installer is open on this computer's screen; this continues by itself when it finishes.".into());
        }
    } else if key == "installing" {
        out.push(progress_line(job.unwrap(), &format!("Installing {name}")));
    } else if key == "unsupported" {
        let r = s(e, "support_reason");
        out.push(
            if r.is_empty() {
                "This engine does not run on this computer."
            } else {
                r
            }
            .to_string(),
        );
    } else {
        let failed = matches!(st, "failed" | "cancelled")
            && e.get("installed").and_then(Value::as_bool) != Some(true);
        if failed {
            let j = job.unwrap();
            let m = j
                .get("error")
                .and_then(|x| x.get("message"))
                .and_then(Value::as_str)
                .filter(|m| !m.is_empty())
                .or_else(|| Some(s(j, "message")).filter(|m| !m.is_empty()))
                .unwrap_or("The install did not finish.");
            out.push(m.to_string());
        }
        if admin {
            if let Some(i) = action(e, "install") {
                if !enabled(i) && !s(i, "reason").is_empty() {
                    out.push(s(i, "reason").to_string());
                }
            }
        }
        let start = if admin { action(e, "start") } else { None };
        if key == "stopped" && start.is_none() {
            out.push("Installed but not running. Start it from its app.".into());
        }
        if key == "ready" {
            out.push("Built in: models run inside the gateway when a workflow needs them.".into());
        }
    }
    out
}

/// The install confirmation's lines (web `engineStore.confirm` block +
/// `engineLocationChoice`), then its key line. `host` names the gateway
/// host; `plans` = the two dry runs (user, system).
pub fn confirm_lines(
    e: &Value,
    host: &str,
    plans: (Option<&WriteState>, Option<&WriteState>),
) -> Vec<String> {
    let name = name_of(e);
    let install = e.get("install").cloned().unwrap_or(json!({}));
    let app = s(&install, "method") == "app";
    let needs_admin = install.get("needs_admin").and_then(Value::as_bool) == Some(true);
    let mut out = vec![format!("Install {name} on {host}?")];
    if !app {
        if !s(&install, "notes").is_empty() {
            out.push(s(&install, "notes").to_string());
        }
        let steps: Vec<&str> = install
            .get("steps")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        if !steps.is_empty() {
            out.push(steps.join(" · "));
        }
        if needs_admin {
            let r = s(&install, "admin_reason");
            out.push(
                if r.is_empty() {
                    "One step needs an administrator password; you will be asked first."
                } else {
                    r
                }
                .to_string(),
            );
        }
        let preview: Vec<&str> = install
            .get("command_preview")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        out.extend(preview.iter().map(|c| format!("  {c}")));
        out.push(format!(
            "y {} · n Not now",
            if needs_admin {
                "Install (administrator)"
            } else {
                "Install now"
            }
        ));
        return out;
    }
    let plan = |w: Option<&WriteState>| match w {
        Some(WriteState::Done(v)) => v.get("plan").cloned().filter(|p| p.is_object()).map(Ok),
        Some(WriteState::Failed(e)) => Some(Err(e.message.clone())),
        _ => None,
    };
    match (plan(plans.0), plan(plans.1)) {
        (Some(Err(err)), _) | (_, Some(Err(err))) => {
            out.push("Could not check the install locations.".into());
            out.push(err);
            out.push("Install still works: the gateway picks /Applications when your account can write it, else your own Applications folder.".into());
            out.push("y Install · n Not now".into());
        }
        (Some(Ok(user)), Some(Ok(sys))) => {
            let sys_admin = sys.get("needs_admin").and_then(Value::as_bool) == Some(true);
            let all = if sys_admin {
                "Install for all users (administrator)"
            } else {
                "Install for all users"
            };
            out.push("Install puts it in your own Applications folder: only your account sees it, no password needed.".into());
            if !s(&user, "target").is_empty() {
                out.push(format!("  {}", s(&user, "target")));
            }
            let mut l =
                format!("{all} puts it in /Applications for every account on this computer.");
            if sys_admin {
                let r = s(&sys, "admin_reason");
                l.push(' ');
                l.push_str(if r.is_empty() {
                    "Your account cannot write there, so an administrator password is asked first."
                } else {
                    r
                });
            }
            out.push(l);
            if !s(&sys, "target").is_empty() {
                out.push(format!("  {}", s(&sys, "target")));
            }
            out.push(format!("y Install · u {all} · n Not now"));
        }
        _ => {
            out.push(format!("Checking where {name} can go on this computer..."));
            out.push("n Not now".into());
        }
    }
    out
}

/// The connection rows of a local provider (console.py
/// `localProviderExtras`): "<name> <address> enabled · key …" per profile.
pub fn connection_rows(engine: &str, profiles: &[crate::store::Profile]) -> Vec<String> {
    let Some((family, fixed, _, _)) = local_connection(engine) else {
        return Vec::new();
    };
    profiles
        .iter()
        .filter(|p| match fixed {
            Some(id) => p.id == id,
            None => p.family == family,
        })
        .map(|p| {
            let where_ = if p.base_url.is_empty() {
                "provider default address".to_string()
            } else {
                p.base_url.clone()
            };
            let key = if p.api_key_set {
                format!(
                    "key {}",
                    p.api_key_fingerprint
                        .as_deref()
                        .unwrap_or("")
                        .chars()
                        .take(8)
                        .collect::<String>()
                )
            } else {
                "no key".into()
            };
            let name = if p.display_name.is_empty() {
                &p.id
            } else {
                &p.display_name
            };
            format!(
                "{name} {where_} — {} · {key} ({})",
                if p.enabled { "enabled" } else { "disabled" },
                if p.synthetic { "e Override" } else { "e Edit" }
            )
        })
        .collect()
}

/// The Local providers section.
pub fn section(
    cx: Scope,
    ctx: &Ctx,
    ui: EnginesUi,
    t: &TokenSet,
    keeper: super::super::util::FocusKeeper,
) -> View {
    let store = ctx.store;
    let tt = *t;
    let host = ctx.screens_transport.host_label();
    let vp = abstracttui::app::use_viewport(cx);
    dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |gcx| {
        let t = tt;
        let width = vp.get().w - super::super::widths::BLOCK_CHROME - 2;
        let conn = store.conn.get();
        if !conn.is_connected() {
            return line(vec![span(
                "not connected — probe the gateway on 1 Connection first",
                t.text_faint,
            )]);
        }
        let admin = conn.is_admin();
        let data = store.json.get(SLOT_ENGINES);
        let _ = store.json.writes.get();
        let notices = ui.notices.get();
        let sent = ui.sent.get();
        let confirm = ui.confirm.get();
        let profiles: Vec<crate::store::Profile> = store
            .profiles
            .get()
            .ready()
            .map(|d| d.profiles.clone())
            .unwrap_or_default();
        let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
        let data = match data {
            Loadable::Ready(d) => d,
            Loadable::Failed(e) => {
                col = col.child(line(vec![span_bold(
                    "This gateway cannot list its engines right now.",
                    t.warn,
                )]));
                for l in wrap_text(&e.message, width.max(20) as usize) {
                    col = col.child(line(vec![span(l, t.text_muted)]));
                }
                for (_, name, url) in ENGINE_DOWNLOAD_LINKS {
                    col = col.child(line(vec![span(format!("Download {name}  {url}"), t.text)]));
                }
                return keeper.anchor(col.build());
            }
            _ => {
                col = col.child(line(vec![span(
                    "Looking at this computer's engines...",
                    t.info,
                )]));
                return keeper.anchor(col.build());
            }
        };
        let engines = ordered_engines(&data);
        col = col.child(line(vec![
            span(summary_line(&data), t.text_muted),
            span("  · k Check again", t.text_faint),
        ]));
        if data.get("install_allowed").and_then(Value::as_bool) == Some(false) {
            col = col.child(line(vec![span_bold(
                "Installing engines is turned off on this gateway.",
                t.info,
            )]));
            for l in wrap_text("An admin can allow it in the gateway settings (allow engine install). You can still install an engine yourself and check again.", width.max(20) as usize) {
                col = col.child(line(vec![span(l, t.text_muted)]));
            }
        }
        if engines.is_empty() {
            col = col.child(line(vec![span(
                "No engines reported by this gateway.",
                t.text_faint,
            )]));
            return keeper.anchor(col.build());
        }
        let rows: Vec<Row> = engines
            .iter()
            .map(|e| {
                let id = s(e, "id");
                let job = job_of(&store, e);
                let (_, pill) = engine_state(e, job.as_ref());
                let mut facts: Vec<String> = Vec::new();
                if !s(e, "version").is_empty() {
                    facts.push(format!("Version {}", s(e, "version")));
                }
                if let Some(n) = e.get("models_count").and_then(Value::as_u64) {
                    facts.push(format!("{n} model{}", if n == 1 { "" } else { "s" }));
                }
                let pend = sent.get(id).map(|(_, l)| l.clone());
                let acts = row_actions(e, job.as_ref(), admin, confirm.as_deref() == Some(id));
                let keys = match pend {
                    Some(l) => l,
                    None => acts
                        .iter()
                        .map(|(a, l)| format!("{} {l}", action_char(a)))
                        .collect::<Vec<_>>()
                        .join(" · "),
                };
                let mut status = pill.to_string();
                if job_active(job.as_ref())
                    && s(job.as_ref().unwrap(), "state") != "needs_admin"
                    && s(job.as_ref().unwrap(), "state") != "needs_tools"
                {
                    status = progress_line(job.as_ref().unwrap(), "Installing");
                }
                let n = notices.get(id);
                if let Some(n) = n {
                    status = format!("{status} · {}", n.text);
                }
                let conns = connection_rows(id, &profiles);
                Row::new(vec![name_of(e), status, facts.join(" · "), keys])
                    .detail(detail_lines(e, job.as_ref(), admin, n, &conns))
                    .dim(e.get("supported").and_then(Value::as_bool) == Some(false))
            })
            .collect();
        let rules = vec![
            ColRule::head("engine", 10),
            ColRule::head("status", 12),
            ColRule::head("facts", 10),
            ColRule::head("actions", 14),
        ];
        col = col.child(
            keeper.wire(
                WrapTable::new(rules, rows, ui.sel)
                    .expanded(ui.expanded)
                    .empty("No engines reported by this gateway.")
                    .element(gcx, &t),
            ),
        );
        if let Some(cid) = confirm {
            if let Some(e) = engines.iter().find(|e| s(e, "id") == cid) {
                let writes = store.json.writes.get_untracked();
                let lines = confirm_lines(
                    e,
                    &host,
                    (
                        writes.get(&plan_key(&cid, "user")),
                        writes.get(&plan_key(&cid, "system")),
                    ),
                );
                let last = lines.len().saturating_sub(1);
                for (i, l) in lines.into_iter().enumerate() {
                    let ink = if i == 0 {
                        t.warn
                    } else if i == last {
                        t.accent
                    } else {
                        t.text_muted
                    };
                    for w in wrap_text(&l, width.max(20) as usize) {
                        col = col.child(line(vec![if i == 0 {
                            span_bold(w, ink)
                        } else {
                            span(w, ink)
                        }]));
                    }
                }
            }
        }
        col.build()
    })
}

/// The selected engine row (by the section's selection).
pub fn selected(store: &Store, ui: EnginesUi) -> Option<Value> {
    let d = store.json.get_untracked(SLOT_ENGINES);
    let d = d.ready()?;
    ordered_engines(d).into_iter().nth(ui.sel.get_untracked())
}

/// A key pressed in the section: the selected engine's matching action
/// (refused with the reason when the row does not offer it).
pub fn key(ctx: &Ctx, ui: EnginesUi, ch: char) {
    let store = ctx.store;
    // An open install confirmation takes y / u / n first.
    if let Some(cid) = ui.confirm.get_untracked() {
        let e = engine_by_id(&store, &cid);
        let app = e
            .as_ref()
            .and_then(|e| e.get("install"))
            .map(|i| s(i, "method") == "app")
            .unwrap_or(false);
        match ch {
            'y' => {
                let plans_failed = [plan_key(&cid, "user"), plan_key(&cid, "system")]
                    .iter()
                    .any(|k| matches!(store.json.write_untracked(k), Some(WriteState::Failed(_))));
                let ready = [plan_key(&cid, "user"), plan_key(&cid, "system")]
                    .iter()
                    .all(|k| matches!(store.json.write_untracked(k), Some(WriteState::Done(_))));
                if app && !plans_failed && !ready {
                    store.notice.set(Some(
                        "still checking the install locations — one moment".into(),
                    ));
                    return;
                }
                let loc = if !app || plans_failed { "auto" } else { "user" };
                run_action(ctx, ui, &cid, &format!("install-go:{loc}"));
            }
            'u' if app => run_action(ctx, ui, &cid, "install-go:system"),
            'n' => run_action(ctx, ui, &cid, "install-cancel"),
            _ => store.notice.set(Some(
                "answer the install question first — y installs, n keeps".into(),
            )),
        }
        return;
    }
    if ch == 'k' {
        // "Check again" (and the row's "I installed it, check again").
        run_action(ctx, ui, "", "refresh");
        return;
    }
    let Some(e) = selected(&store, ui) else {
        store.notice.set(Some("no engine selected".into()));
        return;
    };
    let admin = store.conn.with_untracked(ConnPhase::is_admin);
    let job = job_of(&store, &e);
    let acts = row_actions(&e, job.as_ref(), admin, false);
    match acts.iter().find(|(a, _)| action_char(a) == ch) {
        Some((a, _)) => run_action(ctx, ui, s(&e, "id"), a),
        None => {
            let name = name_of(&e);
            let what = match ch {
                'i' => "Install",
                's' => "Start",
                'x' => "Stop",
                'b' => "Browse models",
                'c' => "Cancel",
                'A' => "Continue with administrator password",
                'T' => "Install tools",
                _ => "that action",
            };
            let why = match ch {
                'i' | 's' | 'x' if !admin => " (an admin's action)".to_string(),
                'i' => action(&e, "install")
                    .map(|a| s(a, "reason").to_string())
                    .filter(|r| !r.is_empty())
                    .map(|r| format!(": {r}"))
                    .unwrap_or_default(),
                _ => String::new(),
            };
            store
                .notice
                .set(Some(format!("{name} does not offer {what} now{why}")));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ollama() -> Value {
        json!({"id": "ollama", "name": "Ollama", "supported": true, "installed": true,
               "running": true, "reachable": true, "version": "0.20.2", "models_count": 2,
               "base_url": "http://127.0.0.1:11434", "provider": "ollama",
               "install": {"method": "app"},
               "actions": [{"id": "stop", "label": "Stop", "enabled": true},
                           {"id": "docs", "url": "https://docs.ollama.com", "enabled": true}]})
    }

    #[test]
    fn state_follows_the_web_engine_state() {
        assert_eq!(engine_state(&ollama(), None), ("running", "Running"));
        let mut e = ollama();
        e["running"] = json!(false);
        assert_eq!(engine_state(&e, None).1, "Installed, stopped");
        e["installed"] = json!(false);
        assert_eq!(engine_state(&e, None).1, "Not installed");
        e["supported"] = json!(false);
        assert_eq!(engine_state(&e, None).1, "Not for this computer");
        let job = json!({"state": "needs_admin"});
        assert_eq!(engine_state(&e, Some(&job)).1, "Needs your approval");
        let job = json!({"state": "downloading"});
        assert_eq!(engine_state(&e, Some(&job)).1, "Installing");
        let mlx = json!({"id": "mlx", "installed": true, "running": null});
        assert_eq!(engine_state(&mlx, None).1, "Ready");
    }

    #[test]
    fn actions_are_admin_gated_and_browse_stays() {
        let acts = row_actions(&ollama(), None, true, false);
        let ids: Vec<&str> = acts.iter().map(|(a, _)| a.as_str()).collect();
        assert_eq!(ids, ["models", "stop"]);
        let acts = row_actions(&ollama(), None, false, false);
        let ids: Vec<&str> = acts.iter().map(|(a, _)| a.as_str()).collect();
        assert_eq!(ids, ["models"], "a non-admin sees no Stop");
    }

    #[test]
    fn summary_counts_installed_and_running() {
        let d = json!({"engines": [ollama(), {"id": "vllm", "supported": false}],
                       "generated_at": "2026-10-04T02:25:51Z"});
        assert_eq!(
            summary_line(&d),
            "1 of 2 installed, 1 running · checked 02:25:51 UTC"
        );
    }

    #[test]
    fn app_confirm_offers_both_locations() {
        let user =
            WriteState::Done(json!({"plan": {"target": "/Users/me/Applications/Ollama.app"}}));
        let sys = WriteState::Done(
            json!({"plan": {"target": "/Applications/Ollama.app", "needs_admin": true}}),
        );
        let l = confirm_lines(&ollama(), "forge", (Some(&user), Some(&sys)));
        assert_eq!(l[0], "Install Ollama on forge?");
        assert!(l.iter().any(
            |x| x.starts_with("Install for all users (administrator) puts it in /Applications")
        ));
        assert_eq!(
            l.last().unwrap(),
            "y Install · u Install for all users (administrator) · n Not now"
        );
    }
}
