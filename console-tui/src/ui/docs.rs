//! The DOCS ASSISTANT (F2) — the terminal twin of the web console's
//! top-bar ✦ drawer (console.py "Docs assistant drawer", `assistantAsk`):
//!
//! 1. `GET /docs/corpus` once per gateway (the gateway's own llms.txt).
//!    A failure degrades honestly: the question still runs with an empty
//!    corpus and the note says `#FALLBACK … answers are ungrounded`, once.
//! 2. `POST /runs/start` with the docs-qa catalog bundle
//!    (`tenant_catalog / docs-qa 0.1.1 / docsqa001`, actor `gateway`),
//!    this conversation's `session_id` and
//!    `input_data {prompt, docs, app, use_session_history: true}`.
//!    History (ADR-0026, operator ruling 2026-09-28): no client-side copy,
//!    no turn cap — the gateway replays the session's earlier turns through
//!    the runtime's history window (newest whole turns up to 50,000 tokens)
//!    and records the receipt (`session_history` on `GET /runs/{id}`), shown
//!    when earlier messages were not replayed. New conversation = new session.
//! 3. `GET /runs/{id}` every 2 s, up to 90 polls (~3 minutes): completed
//!    → `output.response` (else the output JSON); failed/cancelled → the
//!    run's error; still running → the web's "check the Runtimes tab".
//!
//! Never entity chat (a visit is billable and forms memories — the kit
//! contract the web comment cites). Keep-alive like the web drawer: the
//! conversation lives in the Store, so closing the modal mid-answer
//! loses nothing and reopening shows the answer when it lands.

use std::sync::mpsc::Sender;
use std::sync::Mutex;

use serde_json::{json, Value};

use super::util::{line, span, span_bold, wrap_text};
use super::w::action::{button, On};
use super::w::Action;
use super::Ctx;
use crate::api::{ApiError, ApiResult, GatewayClient};
use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;
use abstracttui::app::drawer::DrawerHandle;
use abstracttui::prelude::*;
use abstracttui::reactive::WakeHandle;
use abstracttui::ui::{Phase, UiEvent};
use abstracttui::widgets::TextInput;

/// The web's `ASSISTANT_BUNDLE` + `actor_id`.
pub const BUNDLE_SCOPE: &str = "tenant_catalog";
pub const BUNDLE_ID: &str = "docs-qa";
pub const BUNDLE_VERSION: &str = "0.1.1";
pub const FLOW_ID: &str = "docsqa001";
pub const POLL_EVERY: std::time::Duration = std::time::Duration::from_secs(2);
pub const MAX_POLLS: u32 = 90;
/// The kit's docs session prefix (`<app>-docs-assistant:`, app = gateway):
/// the web drawer and this one list each other's past conversations.
pub const SESSION_PREFIX: &str = "gateway-docs-assistant";
pub const DEFAULT_NOTE: &str =
    "Answers are grounded on the gateway's own documentation (llms.txt) via the docs-qa workflow.";

#[derive(Clone, Debug, PartialEq)]
pub enum Role {
    User,
    Assistant,
    Pending,
    /// A warning-level line (the kit's Stopped outcome).
    Notice,
    Error,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Turn {
    pub role: Role,
    pub text: String,
}

#[derive(Clone, Copy)]
pub struct DocsState {
    pub turns: Signal<Vec<Turn>>,
    pub busy: Signal<bool>,
    pub note: Signal<String>,
    pub draft: Signal<String>,
    /// This conversation's gateway session (the server replays its turns).
    pub session_id: Signal<String>,
    /// "Earlier messages not replayed: N …" from the latest answer, or "".
    pub replay: Signal<String>,
    /// The Past conversations list is shown instead of the thread.
    pub history_open: Signal<bool>,
    /// A past conversation being read back (its questions and answers).
    pub resuming: Signal<Option<HistItem>>,
    /// The op of the answer on its way, and the ops the person stopped
    /// (their late answers are not shown — the kit's abort).
    pub op: Signal<u64>,
    pub stopped: Signal<Vec<u64>>,
}

/// A fresh conversation's session id: nothing from another conversation is replayed.
pub fn new_session_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    format!(
        "{SESSION_PREFIX}:{nanos:x}-{:x}-{seq:x}",
        std::process::id()
    )
}

impl DocsState {
    pub fn create(cx: Scope) -> DocsState {
        DocsState {
            turns: cx.signal(Vec::new()),
            busy: cx.signal(false),
            note: cx.signal(DEFAULT_NOTE.to_string()),
            draft: cx.signal(String::new()),
            session_id: cx.signal(new_session_id()),
            replay: cx.signal(String::new()),
            history_open: cx.signal(false),
            resuming: cx.signal(None),
            op: cx.signal(0),
            stopped: cx.signal(Vec::new()),
        }
    }

    /// New gateway: the conversation belonged to the old one.
    pub fn reset(&self) {
        self.turns.set(Vec::new());
        self.busy.set(false);
        self.note.set(DEFAULT_NOTE.to_string());
        self.new_conversation();
    }

    /// The web's assistantClear: a new session, an empty thread (an answer
    /// still on its way keeps its pending row), no replay note.
    pub fn new_conversation(&self) {
        self.session_id.set(new_session_id());
        self.turns.update(|v| v.retain(|x| x.role == Role::Pending));
        self.replay.set(String::new());
    }
}

// ---------------------------------------------------------------------
// Pure request/response logic (unit-tested)
// ---------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub struct Corpus {
    pub app: String,
    pub text: String,
}

impl Corpus {
    /// `{app: data.app || "AbstractGateway", text: data.text || ""}`.
    pub fn from_value(v: &Value) -> Corpus {
        Corpus {
            app: v
                .get("app")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("AbstractGateway")
                .to_string(),
            text: v
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        }
    }
}

/// The `POST /runs/start` body the web sends.
pub fn start_body(question: &str, session_id: &str, corpus: &Corpus) -> Value {
    json!({
        "registry_scope": BUNDLE_SCOPE,
        "bundle_id": BUNDLE_ID,
        "bundle_version": BUNDLE_VERSION,
        "flow_id": FLOW_ID,
        "actor_id": "gateway",
        "session_id": session_id,
        "input_data": {
            "prompt": question,
            "docs": corpus.text,
            "app": corpus.app,
            "use_session_history": true,
        },
    })
}

/// The web's assistantReplayNote: what the gateway's history window did not
/// replay (`session_history` on `GET /runs/{id}`), or "" when it replayed all.
pub fn replay_note(run: &Value) -> String {
    let h = run.get("session_history").cloned().unwrap_or(Value::Null);
    let n = |k: &str| {
        h.get(k)
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite() && *v > 0.0)
            .map(|v| v as u64)
            .unwrap_or(0)
    };
    let dropped = n("dropped_messages");
    if dropped == 0 {
        return String::new();
    }
    let replayed = n("replayed_messages");
    let tokens = n("dropped_tokens");
    let budget = n("max_tokens");
    let mut out = format!("Earlier messages not replayed: {}", group(dropped));
    if tokens > 0 {
        out.push_str(&format!(" (~{} tokens)", group(tokens)));
    }
    out.push_str(&format!(
        ". The model read the newest {} message{}",
        group(replayed),
        if replayed == 1 { "" } else { "s" }
    ));
    if budget > 0 {
        out.push_str(&format!(
            " (history window: the most recent {} tokens of whole messages).",
            group(budget)
        ));
    } else {
        out.push('.');
    }
    out
}

/// 61234 -> "61,234" (the web's toLocaleString("en-US")).
fn group(v: u64) -> String {
    let digits = v.to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// One poll's verdict.
#[derive(Clone, Debug, PartialEq)]
pub enum PollVerdict {
    Answer(String),
    Failed(String),
    Running,
}

fn js_slice(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Read a `GET /runs/{id}` summary the way `assistantAsk` does.
pub fn poll_verdict(run: &Value) -> PollVerdict {
    let status = run.get("status").and_then(Value::as_str).unwrap_or("");
    match status {
        "completed" => {
            let out = run.get("output").cloned().unwrap_or(Value::Null);
            let out = if out.is_object() { out } else { json!({}) };
            match out.get("response").and_then(Value::as_str) {
                Some(r) if !r.trim().is_empty() => PollVerdict::Answer(r.to_string()),
                _ => PollVerdict::Answer(out.to_string()),
            }
        }
        "failed" | "cancelled" => {
            let detail = run
                .get("error")
                .filter(|e| !e.is_null() && e.as_str() != Some(""))
                .or_else(|| run.get("output").filter(|o| !o.is_null()))
                .cloned()
                .unwrap_or_else(|| json!({}));
            PollVerdict::Failed(format!(
                "docs-qa run {status}: {}",
                js_slice(&detail.to_string(), 300)
            ))
        }
        _ => PollVerdict::Running,
    }
}

// ---------------------------------------------------------------------
// Worker side
// ---------------------------------------------------------------------

/// Corpus cache per gateway base URL (the web caches it for the page's
/// life; a FAILED fetch is cached as empty too, so the warning shows once).
static CORPUS: Mutex<Option<(String, Corpus)>> = Mutex::new(None);

/// Fetch (or reuse) the corpus; `Some(note)` when it degraded.
fn ensure_corpus(c: &GatewayClient) -> (Corpus, Option<String>) {
    if let Ok(g) = CORPUS.lock() {
        if let Some((url, corpus)) = g.as_ref() {
            if url == c.base_url() {
                return (corpus.clone(), None);
            }
        }
    }
    let (corpus, note) = match c.docs_corpus() {
        Ok(v) => (Corpus::from_value(&v), None),
        Err(e) => (
            Corpus {
                app: "AbstractGateway".into(),
                text: String::new(),
            },
            Some(format!(
                "#FALLBACK no documentation corpus available ({}) — answers are ungrounded.",
                e.message
            )),
        ),
    };
    if let Ok(mut g) = CORPUS.lock() {
        *g = Some((c.base_url().to_string(), corpus.clone()));
    }
    (corpus, note)
}

/// Start the docs-qa run: `(run_id, fallback note)`.
pub fn start_ask(
    c: &GatewayClient,
    question: &str,
    session_id: &str,
) -> ApiResult<(String, Option<String>)> {
    let (corpus, note) = ensure_corpus(c);
    let started = c.start_run(&start_body(question, session_id, &corpus))?;
    let run_id = started
        .get("run_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError {
            kind: crate::api::ApiErrorKind::Protocol,
            message: format!("POST /runs/start answered without a run_id: {started}"),
            body: Some(started.clone()),
            timed_out: false,
        })?
        .to_string();
    Ok((run_id, note))
}

fn schedule(tx: &Sender<Cmd>, run_id: String, attempt: u32, op: u64) {
    let tx = tx.clone();
    std::thread::Builder::new()
        .name("docs-poll-timer".into())
        .spawn(move || {
            std::thread::sleep(POLL_EVERY);
            let _ = tx.send(Cmd::DocsPoll {
                run_id,
                attempt,
                op,
            });
        })
        .ok();
}

/// Settle the pending turn (UI thread); `replay` = the answer's replay note.
fn settle(store: &Store, op: u64, turn: Turn, replay: Option<String>) {
    // Stopped by the person: nothing more is shown for that question.
    if store.docs.stopped.with_untracked(|s| s.contains(&op)) {
        return;
    }
    if let Some(r) = replay {
        store.docs.replay.set(r);
    }
    store.docs.turns.update(|t| {
        if let Some(last) = t.iter_mut().rev().find(|x| x.role == Role::Pending) {
            *last = turn;
        } else {
            t.push(turn);
        }
    });
    store.docs.busy.set(false);
    store.end_busy(op);
}

/// After the start call (worker thread).
pub fn after_start(
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    op: u64,
    r: ApiResult<(String, Option<String>)>,
) {
    let s = *store;
    match r {
        Ok((run_id, note)) => {
            if let Some(n) = note {
                wake.post(move || s.docs.note.set(n));
            }
            schedule(tx, run_id, 0, op);
        }
        Err(e) => {
            let msg = format!("Failed: {}", e.message);
            wake.post(move || {
                settle(
                    &s,
                    op,
                    Turn {
                        role: Role::Error,
                        text: msg,
                    },
                    None,
                )
            });
        }
    }
}

/// After one poll (worker thread).
pub fn after_poll(
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    run_id: String,
    attempt: u32,
    op: u64,
    r: ApiResult<Value>,
) {
    let s = *store;
    let replay = match &r {
        Ok(v) if matches!(poll_verdict(v), PollVerdict::Answer(_)) => Some(replay_note(v)),
        _ => None,
    };
    let turn = match r.map(|v| poll_verdict(&v)) {
        Ok(PollVerdict::Answer(a)) => Turn { role: Role::Assistant, text: a },
        Ok(PollVerdict::Failed(m)) => Turn { role: Role::Error, text: format!("Failed: {m}") },
        Ok(PollVerdict::Running) if attempt + 1 < MAX_POLLS => {
            schedule(tx, run_id, attempt + 1, op);
            return;
        }
        Ok(PollVerdict::Running) => Turn {
            role: Role::Error,
            text: format!(
                "Failed: docs-qa run {run_id} still running after 3 minutes — check the Runtimes tab"
            ),
        },
        Err(e) => Turn { role: Role::Error, text: format!("Failed: {}", e.message) },
    };
    wake.post(move || settle(&s, op, turn, replay));
}

// ---------------------------------------------------------------------
// UI — the kit's DocsAssistantDrawer in the terminal (R15, DESIGN-TUI
// §3.16): a right-edge drawer (48% wide from 120 columns, the full width
// below), the head's [Past conversations] [New conversation], the empty
// state with the console's three suggestions, the thread, the composer
// with [Send] / [Stop], and the grounding footer. Words: the kit's.
// ---------------------------------------------------------------------

/// The drawer's title (the kit's `label`).
pub const TITLE: &str = "Docs assistant";
/// The console's placeholder and suggestions (console.py docsAssistantProps).
pub const PLACEHOLDER: &str = "Ask about the gateway…";
pub const SUGGESTIONS: [&str; 3] = [
    "How do I give a user a mailbox?",
    "How do apps reach my models through the OpenAI API?",
    "Where do workflows come from?",
];
/// The kit's empty-state sentence and footer (source name AbstractGateway).
pub const EMPTY: &str = "Ask anything about AbstractGateway.";
pub const FOOTER: &str = "Grounded on AbstractGateway’s documentation (llms.txt) · docs-qa";
pub const FOOTER_TIP: &str =
    "Answers come from AbstractGateway's documentation (llms.txt) through the gateway's docs-qa workflow.";
/// The kit's Stop outcome.
pub const STOPPED: &str = "Stopped. Nothing more will be shown for this question.";
/// The kit's history sentences.
pub const HISTORY_LOADING: &str = "Loading past conversations…";
pub const HISTORY_EMPTY: &str = "No past conversations yet.";
pub const HISTORY_ERROR: &str = "The history could not be read.";
pub const ARCHIVE_QUESTION: &str =
    "Archive this conversation? It stays in the gateway; it leaves this list.";
pub const UNTITLED: &str = "Untitled question";

/// JSON-lane keys of the history (`GET /runs?…kind=docs`, each run's
/// input and summary, the archive write).
pub const K_HISTORY: &str = "docs.history";
pub const K_INPUT: &str = "docs.hist.input:";
pub const K_RUN: &str = "docs.hist.run:";
pub const W_ARCHIVE: &str = "docs.archive";
pub const HISTORY_PATH: &str = "/runs?root_only=true&kind=docs&limit=500&include_ledger_len=false";

/// The head's buttons (one source for the buttons, keys and tests): the
/// kit's aria labels as faces, its data-af-tip sentences as tooltips.
pub fn head_actions() -> Vec<Action> {
    vec![
        Action::label("history", "Past conversations")
            .tooltip("Past conversations")
            .key('h'),
        Action::label("new", "New conversation")
            .tooltip("Start a new conversation")
            .key('n'),
    ]
}

/// The composer's buttons: Send (Answering… while busy) and Stop.
pub fn composer_actions(busy: bool) -> Vec<Action> {
    let mut out = vec![if busy {
        Action::label("send", "Answering…").refused(Some(
            "an answer is on its way — one question at a time".into(),
        ))
    } else {
        Action::label("send", "Send")
    }];
    if busy {
        out.push(Action::label("stop", "Stop").tooltip("Stop the answer"));
    }
    out
}

/// One past conversation (the kit's history item).
#[derive(Clone, Debug, PartialEq)]
pub struct HistItem {
    pub session_id: String,
    pub run_ids: Vec<String>,
    pub updated_at: String,
}

/// The kit's history grouping (`Fw`): root docs runs whose session starts
/// with this app's docs prefix, grouped by session, newest first, 20 at
/// most; each session's runs oldest first.
pub fn history_items(v: &Value) -> Vec<HistItem> {
    let prefix = format!("{SESSION_PREFIX}:");
    // (session, [(run, created)], updated)
    type Group = (String, Vec<(String, String)>, String);
    let mut map: Vec<Group> = Vec::new();
    for it in v
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let sid = it.get("session_id").and_then(Value::as_str).unwrap_or("");
        let rid = it.get("run_id").and_then(Value::as_str).unwrap_or("");
        if !sid.starts_with(&prefix) || rid.is_empty() {
            continue;
        }
        let created = it
            .get("created_at")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let updated = it
            .get("updated_at")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| created.clone());
        match map.iter_mut().find(|(s, _, _)| s == sid) {
            Some((_, runs, up)) => {
                runs.push((rid.to_string(), created));
                if updated > *up {
                    *up = updated;
                }
            }
            None => map.push((sid.to_string(), vec![(rid.to_string(), created)], updated)),
        }
    }
    map.sort_by(|a, b| b.2.cmp(&a.2));
    map.truncate(20);
    map.into_iter()
        .map(|(session_id, mut runs, updated_at)| {
            runs.sort_by(|a, b| a.1.cmp(&b.1));
            HistItem {
                session_id,
                run_ids: runs.into_iter().map(|(r, _)| r).collect(),
                updated_at,
            }
        })
        .collect()
}

/// The kit's relative time (`qc`) for a past moment.
pub fn ago(ts: &str, now_epoch: i64) -> String {
    let Some(t) = crate::localtime::parse_iso_epoch(ts) else {
        return String::new();
    };
    let r = now_epoch - t;
    if r < 60 {
        return "just now".into();
    }
    if r < 3600 {
        return format!("{} min ago", r / 60);
    }
    if r < 86_400 {
        return format!("{} h ago", r / 3600);
    }
    let d = r / 86_400;
    if d == 1 {
        "yesterday".into()
    } else if d < 7 {
        format!("{d} days ago")
    } else {
        crate::localtime::local_parts(ts)
            .map(|(day, _)| day)
            .unwrap_or_default()
    }
}

/// A past conversation's title: its first question (the kit's).
fn title_of(store: &Store, item: &HistItem) -> String {
    let Some(first) = item.run_ids.first() else {
        return UNTITLED.into();
    };
    match store.json.get(&format!("{K_INPUT}{first}")) {
        Loadable::Ready(v) => {
            let p = v
                .get("input_data")
                .and_then(|i| i.get("prompt"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            if p.is_empty() {
                UNTITLED.into()
            } else {
                p
            }
        }
        _ => UNTITLED.into(),
    }
}

/// A history row's actions: open (the title) and Archive.
pub fn history_actions(title: &str) -> Vec<Action> {
    vec![
        Action::link("open", title.to_string()).tooltip(title.to_string()),
        Action::glyph("archive", "Archive")
            .tooltip(format!("Archive \"{title}\" (kept, hidden)"))
            .danger(),
    ]
}

fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Ask `question` (a suggestion) or the draft (Enter / Send). Refusals
/// name their reason.
fn ask_text(ctx: &Ctx, question: Option<String>) {
    let store = ctx.store;
    let d = store.docs;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        store.notice.set(Some(
            "connect to the gateway first — the docs assistant runs on the gateway".into(),
        ));
        return;
    }
    if d.busy.get_untracked() {
        store.notice.set(Some(
            "an answer is on its way — one question at a time".into(),
        ));
        return;
    }
    let question = question
        .unwrap_or_else(|| d.draft.get_untracked())
        .trim()
        .to_string();
    if question.is_empty() {
        store
            .notice
            .set(Some("type a question about the gateway first".into()));
        return;
    }
    let session_id = d.session_id.get_untracked();
    d.draft.set(String::new());
    d.busy.set(true);
    d.history_open.set(false);
    d.turns.update(|t| {
        t.push(Turn {
            role: Role::User,
            text: question.clone(),
        });
        t.push(Turn {
            role: Role::Pending,
            text: "Thinking…".into(),
        });
    });
    let op = crate::worker::next_op();
    d.op.set(op);
    store.begin_busy(op, "docs assistant: answering");
    ctx.send(Cmd::DocsAsk {
        question,
        session_id,
        op,
    });
}

/// Stop the answer on its way (the kit's abort): the pending turn says
/// so, nothing more is shown for it, a new question may go.
fn stop(store: &Store) {
    let d = store.docs;
    if !d.busy.get_untracked() {
        return;
    }
    let op = d.op.get_untracked();
    d.stopped.update(|s| s.push(op));
    d.turns.update(|t| {
        if let Some(last) = t.iter_mut().rev().find(|x| x.role == Role::Pending) {
            *last = Turn {
                role: Role::Notice,
                text: STOPPED.into(),
            };
        }
    });
    d.busy.set(false);
    store.end_busy(op);
}

fn load_history(ctx: &Ctx) {
    ctx.store.json.set(K_HISTORY, Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get(K_HISTORY, HISTORY_PATH)));
}

/// Read what a history row needs that is not here yet.
fn want(ctx: &Ctx, key: String, path: String) {
    if matches!(ctx.store.json.get_untracked(&key), Loadable::NotAsked) {
        ctx.store.json.set(&key, Loadable::Loading);
        ctx.send(Cmd::Json(JsonCmd::get(&key, path)));
    }
}

/// Open a past conversation: read each question and answer, then show
/// the thread and continue in that session.
fn resume(ctx: &Ctx, item: HistItem) {
    for r in &item.run_ids {
        want(
            ctx,
            format!("{K_INPUT}{r}"),
            format!("/runs/{r}/input_data"),
        );
        want(ctx, format!("{K_RUN}{r}"), format!("/runs/{r}"));
    }
    ctx.store.docs.resuming.set(Some(item));
}

/// The thread of a past conversation once every read landed (None while
/// one is still on its way).
fn resumed_turns(store: &Store, item: &HistItem) -> Option<Vec<Turn>> {
    let mut out = Vec::new();
    for r in &item.run_ids {
        let input = store.json.get(&format!("{K_INPUT}{r}"));
        let run = store.json.get(&format!("{K_RUN}{r}"));
        let (input, run) = match (input, run) {
            (Loadable::Ready(i), Loadable::Ready(r)) => (i, Ok(r)),
            (Loadable::Failed(e), _) | (_, Loadable::Failed(e)) => {
                (Value::Null, Err(e.to_string()))
            }
            _ => return None,
        };
        let q = input
            .get("input_data")
            .and_then(|i| i.get("prompt"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if !q.is_empty() {
            out.push(Turn {
                role: Role::User,
                text: q,
            });
        }
        out.push(match run {
            Ok(v) => match poll_verdict(&v) {
                PollVerdict::Answer(a) => Turn {
                    role: Role::Assistant,
                    text: a,
                },
                PollVerdict::Failed(m) => Turn {
                    role: Role::Error,
                    text: format!("Failed: {m}"),
                },
                PollVerdict::Running => Turn {
                    role: Role::Notice,
                    text: "Still answering — open it again in a moment.".into(),
                },
            },
            Err(e) => Turn {
                role: Role::Error,
                text: format!("A past answer could not be read: {e}"),
            },
        });
    }
    Some(out)
}

fn archive(ctx: &Ctx, cx: Scope, item: HistItem) {
    let c = ctx.clone();
    super::w::Confirm::danger(ARCHIVE_QUESTION, "Archive", "Cancel").open(cx, ctx.ui, move || {
        let sid = item.session_id.clone();
        c.send(Cmd::Json(JsonCmd::Send {
            key: W_ARCHIVE.into(),
            method: "POST".into(),
            path: format!("/sessions/{}/archive", crate::api::urlencode(&sid)),
            body: json!({}),
            slow: false,
            label: "Archive the docs conversation".into(),
            reload: vec![(K_HISTORY.into(), HISTORY_PATH.into())],
            journal: false,
        }));
        if c.store.docs.session_id.get_untracked() == sid {
            c.store.docs.new_conversation();
        }
    });
}

thread_local! {
    /// The installed drawers: (from 120 columns: 48% wide, below: full width).
    static DRAWERS: std::cell::RefCell<Option<(DrawerHandle, DrawerHandle)>> =
        const { std::cell::RefCell::new(None) };
}

/// Install the docs drawer (ONCE, at the root, like the entity inspector):
/// its state lives in the Store, so closing it mid-answer loses nothing.
pub fn install(cx: Scope, ctx: &Ctx) {
    use abstracttui::app::drawer::{Drawer, DrawerEdge, DrawerFocus, DrawerSize};
    ROOT_SCOPE.with(|s| s.set(Some(cx)));
    let build = |size: f32| {
        let c = ctx.clone();
        Drawer::new(DrawerEdge::Right)
            .size(DrawerSize::Percent(size))
            .focus(DrawerFocus::Modal)
            .title(TITLE)
            .motion(std::time::Duration::ZERO)
            .overlays(&ctx.overlays)
            .install(cx, move |dcx| drawer_view(dcx, &c))
    };
    let wide = build(0.48);
    let full = build(1.0);
    DRAWERS.with(|d| *d.borrow_mut() = Some((wide, full)));
}

/// The drawer's hint pairs (the status bar while it is open).
pub fn hints() -> Vec<(&'static str, &'static str)> {
    vec![
        ("Enter", "Send"),
        ("Tab", "buttons"),
        ("h", "Past conversations"),
        ("n", "New conversation"),
        ("Esc", "close"),
    ]
}

/// Is the docs drawer open (tests, the shell)?
pub fn is_open() -> bool {
    DRAWERS.with(|d| {
        d.borrow()
            .as_ref()
            .is_some_and(|(a, b)| a.is_open() || b.is_open())
    })
}

/// Open the docs assistant (F2 / the header's ✦ Docs). Signed-in only,
/// like the web's session-only ✦ button.
pub fn open(ctx: &Ctx, cx: Scope) {
    let store = ctx.store;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        store.notice.set(Some(
            "the docs assistant needs a gateway connection — probe on the Connection screen".into(),
        ));
        return;
    }
    let _ = cx;
    let w = abstracttui::app::current_viewport().w;
    let handle = DRAWERS.with(|d| {
        d.borrow()
            .as_ref()
            .map(|(wide, full)| if w >= 120 { wide.clone() } else { full.clone() })
    });
    match handle {
        Some(h) => {
            super::w::tip::hide_all();
            h.open();
        }
        // The root installs it (ui::root → docs::install): reaching here
        // without it is a console defect, said — never a silent no-op.
        None => store.notice.set(Some(
            "the docs assistant drawer is not installed (console defect: ui::root must call docs::install)".into(),
        )),
    }
}

/// The drawer's width (cells) on this terminal: 48% from 120 columns,
/// the full width below (the two installed drawers).
fn drawer_width() -> i32 {
    let w = abstracttui::app::current_viewport().w;
    if w >= 120 {
        ((w as f32) * 0.48).round() as i32
    } else {
        w
    }
}

/// Close the drawer (Esc / ✕ are the drawer's own).
pub fn close() {
    DRAWERS.with(|d| {
        if let Some((a, b)) = d.borrow().as_ref() {
            a.close();
            b.close();
        }
    });
}

/// The drawer's content (rebuilt per open; the state is the Store's).
fn drawer_view(dcx: Scope, ctx: &Ctx) -> View {
    let store = ctx.store;
    let d = store.docs;
    // A past conversation whose reads all landed becomes the thread.
    {
        cx_effect_resume(dcx, store);
    }
    let theme = use_theme(dcx);
    let ctx_head = ctx.clone();
    let ctx_body = ctx.clone();
    let ctx_comp = ctx.clone();
    let keys = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().grow(1.0).padding(Edges::hv(1, 0)))
        .on(Phase::Bubble, move |ectx, ev| {
            // The head's keys when no text field took the key (h, n).
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                match k.key {
                    Key::Char('h') => toggle_history(&keys),
                    Key::Char('n') => new_conversation(&keys),
                    _ => return,
                }
                ectx.stop_propagation();
            }
        })
        // Head: [Past conversations] [New conversation], right-aligned.
        .child(dyn_view_scoped(
            LayoutStyle::line(1).shrink(0.0),
            move |hcx| {
                let t = theme.get().tokens;
                let open = d.history_open.get();
                let mut row = Element::new()
                    .style(LayoutStyle::row().h(1).gap(1).shrink(0.0))
                    .child(
                        Element::new()
                            .style(LayoutStyle::default().grow(1.0))
                            .build(),
                    );
                let _ = open;
                for a in head_actions() {
                    let c = ctx_head.clone();
                    let id = a.id;
                    row = row.child(button(hcx, &t, &a, On::Raised, true, move || match id {
                        "history" => toggle_history(&c),
                        _ => new_conversation(&c),
                    }));
                }
                row.build()
            },
        ))
        .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
            let t = theme.get().tokens;
            let note = d.note.get();
            if note == DEFAULT_NOTE {
                return Element::new().build();
            }
            let ink = if note.starts_with("#FALLBACK") {
                t.warn
            } else {
                t.text_faint
            };
            line(vec![span(note, ink)])
        }))
        .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
            let t = theme.get().tokens;
            let replay = d.replay.get();
            if replay.is_empty() {
                Element::new().build()
            } else {
                line(vec![span(replay, t.info)])
            }
        }))
        // Body: the history list, or the thread / the empty state.
        .child(dyn_view_scoped(
            LayoutStyle::column().grow(1.0).min_h(3),
            move |scx| body_view(scx, &ctx_body),
        ))
        // Composer: the question field, [Send] / [Stop].
        .child(composer_view(dcx, &ctx_comp))
        .child(
            super::w::tip::with_tip(
                dcx,
                Element::new()
                    .style(LayoutStyle::line(1).shrink(0.0))
                    .child(super::w::paint::fill_line(
                        LayoutStyle::fill(),
                        vec![super::w::Ink::new(
                            FOOTER,
                            theme.get_untracked().tokens.text_faint,
                        )],
                        None,
                    )),
                FOOTER_TIP.to_string(),
            )
            .build(),
        )
        .build()
}

fn cx_effect_resume(cx: Scope, store: Store) {
    cx.effect(move || {
        let Some(item) = store.docs.resuming.get() else {
            return;
        };
        if let Some(turns) = resumed_turns(&store, &item) {
            let d = store.docs;
            d.resuming.set(None);
            if d.busy.get_untracked() {
                stop(&store);
            }
            d.session_id.set(item.session_id.clone());
            d.turns.set(turns);
            d.replay.set(String::new());
            d.history_open.set(false);
        }
    });
}

fn toggle_history(ctx: &Ctx) {
    let d = ctx.store.docs;
    let open = !d.history_open.get_untracked();
    d.history_open.set(open);
    if open {
        load_history(ctx);
    }
}

fn new_conversation(ctx: &Ctx) {
    let d = ctx.store.docs;
    if d.busy.get_untracked() {
        stop(&ctx.store);
    }
    d.new_conversation();
    d.turns.set(Vec::new());
    d.history_open.set(false);
}

fn body_view(scx: Scope, ctx: &Ctx) -> View {
    let store = ctx.store;
    let d = store.docs;
    let t = use_theme(scx).get().tokens;
    let w = (drawer_width() as usize).saturating_sub(4).max(20);
    if d.history_open.get() {
        return history_view(scx, ctx, &t, w);
    }
    let turns = d.turns.get();
    if turns.is_empty() {
        let mut col = Element::new()
            .style(LayoutStyle::column().grow(1.0))
            .child(line(vec![span(EMPTY, t.text_muted)]));
        for q in SUGGESTIONS {
            let c = ctx.clone();
            let a = Action::label("suggest", q).tooltip(format!("Ask: {q}"));
            col = col.child(
                Element::new()
                    .style(LayoutStyle::row().h(1).shrink(0.0))
                    .child(button(scx, &t, &a, On::Raised, true, move || {
                        ask_text(&c, Some(q.to_string()))
                    }))
                    .build(),
            );
        }
        return col.build();
    }
    let mut rows: Vec<View> = Vec::new();
    for turn in turns {
        match turn.role {
            Role::User => {
                rows.push(line(vec![span_bold("You", t.accent)]));
                for l in wrap_text(&turn.text, w) {
                    rows.push(line(vec![span(l, t.text)]));
                }
            }
            Role::Assistant => {
                rows.push(line(vec![span_bold("Assistant", t.ok)]));
                rows.push(MarkdownView::new(turn.text.clone()).view(scx));
            }
            Role::Pending => {
                rows.push(line(vec![span(
                    "⟳ Answering… (docs-qa run in progress)",
                    t.info,
                )]));
            }
            Role::Notice => {
                for l in wrap_text(&turn.text, w) {
                    rows.push(line(vec![span(l, t.warn)]));
                }
            }
            Role::Error => {
                for l in wrap_text(&turn.text, w) {
                    rows.push(line(vec![span(l, t.error)]));
                }
            }
        }
        rows.push(line(vec![]));
    }
    Scroll::new(
        Element::new()
            .style(LayoutStyle::column())
            .children(rows)
            .build(),
    )
    .scrollbar_auto_hide(true)
    .view(scx)
}

fn history_view(scx: Scope, ctx: &Ctx, t: &TokenSet, w: usize) -> View {
    let store = ctx.store;
    let col = Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .child(line(vec![span_bold("Past conversations", t.text)]));
    match store.json.get(K_HISTORY) {
        Loadable::NotAsked | Loadable::Loading => col
            .child(line(vec![span(HISTORY_LOADING, t.text_muted)]))
            .build(),
        Loadable::Failed(e) => {
            let mut col = col;
            for l in wrap_text(&format!("{HISTORY_ERROR} {e}"), w) {
                col = col.child(line(vec![span(l, t.error)]));
            }
            col.build()
        }
        Loadable::Ready(v) => {
            let items = history_items(&v);
            if items.is_empty() {
                return col
                    .child(line(vec![span(HISTORY_EMPTY, t.text_muted)]))
                    .build();
            }
            let now = now_epoch();
            let active = store.docs.session_id.get();
            let resuming = store.docs.resuming.get().map(|i| i.session_id);
            let mut col = col;
            for item in items {
                if let Some(first) = item.run_ids.first() {
                    want(
                        ctx,
                        format!("{K_INPUT}{first}"),
                        format!("/runs/{first}/input_data"),
                    );
                }
                let title = title_of(&store, &item);
                let mut meta = ago(&item.updated_at, now);
                if item.run_ids.len() > 1 {
                    meta.push_str(&format!(" · {} questions", item.run_ids.len()));
                }
                if resuming.as_deref() == Some(item.session_id.as_str()) {
                    meta.push_str(" · opening…");
                }
                let acts = history_actions(&title);
                let mut row = Element::new().style(LayoutStyle::row().h(1).gap(1).shrink(0.0));
                let mark = if item.session_id == active {
                    "▌"
                } else {
                    " "
                };
                row = row.child(super::w::paint::fill_line(
                    LayoutStyle::default()
                        .width(Dimension::Cells(1))
                        .height(Dimension::Cells(1))
                        .shrink(0.0),
                    vec![super::w::Ink::new(mark, t.accent)],
                    None,
                ));
                let budget = (w as i32 - 4 - abstracttui::text::width(&meta) - 3).max(8);
                for a in acts {
                    let c = ctx.clone();
                    let it = item.clone();
                    let id = a.id;
                    let mut a = a;
                    if id == "open" {
                        a.label = super::w::paint::fit(&a.label, budget);
                    }
                    row = row.child(button(scx, t, &a, On::Raised, true, move || match id {
                        "open" => resume(&c, it.clone()),
                        _ => archive(&c, scx_root(&c), it.clone()),
                    }));
                    if id == "open" {
                        row = row.child(super::w::paint::fill_line(
                            LayoutStyle::default()
                                .width(Dimension::Cells(abstracttui::text::width(&meta)))
                                .height(Dimension::Cells(1))
                                .shrink(0.0),
                            vec![super::w::Ink::new(meta.clone(), t.text_faint)],
                            None,
                        ));
                    }
                }
                col = col.child(row.build());
            }
            col.build()
        }
    }
}

thread_local! {
    /// The drawer's installing scope (long-lived: the root's) — the
    /// Archive confirmation opens on it, never on a region a reload
    /// re-renders.
    static ROOT_SCOPE: std::cell::Cell<Option<Scope>> = const { std::cell::Cell::new(None) };
}

fn scx_root(_ctx: &Ctx) -> Scope {
    ROOT_SCOPE
        .with(|s| s.get())
        .expect("docs::install records its scope")
}

fn composer_view(dcx: Scope, ctx: &Ctx) -> View {
    let d = ctx.store.docs;
    let t = use_theme(dcx).get().tokens;
    let ctx_submit = ctx.clone();
    let ctx_btn = ctx.clone();
    let input = TextInput::new()
        .value(d.draft)
        .placeholder(PLACEHOLDER)
        .on_submit(move |_| ask_text(&ctx_submit, None))
        .layout(LayoutStyle::default().grow(1.0).h(1).shrink(1.0))
        .element(dcx, &t)
        .autofocus()
        .build();
    Element::new()
        .style(LayoutStyle::row().h(1).gap(1).shrink(0.0))
        .child(input)
        .child(dyn_view_scoped(
            LayoutStyle::row().h(1).gap(1).shrink(0.0),
            move |bcx| {
                let t = use_theme(bcx).get().tokens;
                let mut row = Element::new().style(LayoutStyle::row().h(1).gap(1).shrink(0.0));
                for a in composer_actions(d.busy.get()) {
                    let c = ctx_btn.clone();
                    let id = a.id;
                    row = row.child(button(bcx, &t, &a, On::Raised, true, move || match id {
                        "stop" => stop(&c.store),
                        _ => ask_text(&c, None),
                    }));
                }
                row.build()
            },
        ))
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn start_body_is_the_web_payload() {
        let c = Corpus {
            app: "AbstractGateway".into(),
            text: "# llms".into(),
        };
        let b = start_body("how do I add a provider?", "gateway-docs-assistant:s1", &c);
        assert_eq!(
            b,
            json!({
                "registry_scope": "tenant_catalog", "bundle_id": "docs-qa",
                "bundle_version": "0.1.1", "flow_id": "docsqa001", "actor_id": "gateway",
                "session_id": "gateway-docs-assistant:s1",
                "input_data": {"prompt": "how do I add a provider?", "docs": "# llms",
                               "app": "AbstractGateway", "use_session_history": true}
            })
        );
    }

    #[test]
    fn corpus_parses_the_route_shape() {
        // gateway_docs_corpus: {app, source, chars, text}.
        let v = json!({"app": "AbstractGateway", "source": "repo:llms.txt", "chars": 5, "text": "hello"});
        assert_eq!(
            Corpus::from_value(&v),
            Corpus {
                app: "AbstractGateway".into(),
                text: "hello".into()
            }
        );
        assert_eq!(Corpus::from_value(&json!({})).app, "AbstractGateway");
    }

    #[test]
    fn poll_verdicts_follow_assistant_ask() {
        let done = json!({"run_id": "r", "status": "completed", "output": {"response": "Use Providers."}, "error": null});
        assert_eq!(
            poll_verdict(&done),
            PollVerdict::Answer("Use Providers.".into())
        );
        let blank = json!({"status": "completed", "output": {"response": "  ", "x": 1}});
        match poll_verdict(&blank) {
            PollVerdict::Answer(a) => {
                assert!(a.contains("\"x\":1"), "falls back to the output JSON: {a}")
            }
            other => panic!("{other:?}"),
        }
        let failed =
            json!({"status": "failed", "error": "bundle docs-qa not found", "output": null});
        assert_eq!(
            poll_verdict(&failed),
            PollVerdict::Failed("docs-qa run failed: \"bundle docs-qa not found\"".into())
        );
        let cancelled = json!({"status": "cancelled", "error": null, "output": null});
        assert_eq!(
            poll_verdict(&cancelled),
            PollVerdict::Failed("docs-qa run cancelled: {}".into())
        );
        for s in ["running", "waiting", "queued"] {
            assert_eq!(poll_verdict(&json!({"status": s})), PollVerdict::Running);
        }
    }

    #[test]
    fn each_conversation_has_its_own_session() {
        let a = new_session_id();
        let b = new_session_id();
        assert!(a.starts_with("gateway-docs-assistant:"), "{a}");
        assert_ne!(a, b);
    }

    #[test]
    fn replay_note_follows_the_gateway_receipt() {
        let run = json!({"status": "completed", "session_history": {
            "replayed_messages": 12, "dropped_messages": 49, "dropped_tokens": 61234, "max_tokens": 50000}});
        assert_eq!(
            replay_note(&run),
            "Earlier messages not replayed: 49 (~61,234 tokens). The model read the newest 12 messages \
             (history window: the most recent 50,000 tokens of whole messages)."
        );
        assert_eq!(
            replay_note(
                &json!({"session_history": {"replayed_messages": 4, "dropped_messages": 0}})
            ),
            ""
        );
        assert_eq!(replay_note(&json!({"session_history": null})), "");
    }
}
