//! The DOCS ASSISTANT (F2) — the terminal twin of the web console's
//! top-bar ✦ drawer (console.py "Docs assistant drawer", `assistantAsk`):
//!
//! 1. `GET /docs/corpus` once per gateway (the gateway's own llms.txt).
//!    A failure degrades honestly: the question still runs with an empty
//!    corpus and the note says `#FALLBACK … answers are ungrounded`, once.
//! 2. `POST /runs/start` with the docs-qa catalog bundle
//!    (`tenant_catalog / docs-qa 0.1.0 / docsqa001`, actor `gateway`) and
//!    `input_data {question, history (last 12), docs, app}`.
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
use super::{open_form, Ctx};
use crate::api::{ApiError, ApiResult, GatewayClient};
use crate::store::{ConnPhase, Store};
use crate::worker::Cmd;
use abstracttui::prelude::*;
use abstracttui::reactive::WakeHandle;

/// The web's `ASSISTANT_BUNDLE` + `actor_id`.
pub const BUNDLE_SCOPE: &str = "tenant_catalog";
pub const BUNDLE_ID: &str = "docs-qa";
pub const BUNDLE_VERSION: &str = "0.1.0";
pub const FLOW_ID: &str = "docsqa001";
pub const POLL_EVERY: std::time::Duration = std::time::Duration::from_secs(2);
pub const MAX_POLLS: u32 = 90;
pub const HISTORY_TURNS: usize = 12;
pub const DEFAULT_NOTE: &str =
    "Answers are grounded on the gateway's own documentation (llms.txt) via the docs-qa workflow.";

#[derive(Clone, Debug, PartialEq)]
pub enum Role {
    User,
    Assistant,
    Pending,
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
}

impl DocsState {
    pub fn create(cx: Scope) -> DocsState {
        DocsState {
            turns: cx.signal(Vec::new()),
            busy: cx.signal(false),
            note: cx.signal(DEFAULT_NOTE.to_string()),
            draft: cx.signal(String::new()),
        }
    }

    /// New gateway: the conversation belonged to the old one.
    pub fn reset(&self) {
        self.turns.set(Vec::new());
        self.busy.set(false);
        self.note.set(DEFAULT_NOTE.to_string());
    }

    /// The web's `assistantState.history`: successful (user, assistant)
    /// pairs only, as role/content messages, last 12.
    pub fn history(turns: &[Turn]) -> Vec<Value> {
        let mut out = Vec::new();
        let mut i = 0;
        while i + 1 < turns.len() {
            if turns[i].role == Role::User && turns[i + 1].role == Role::Assistant {
                out.push(json!({"role": "user", "content": turns[i].text}));
                out.push(json!({"role": "assistant", "content": turns[i + 1].text}));
                i += 2;
            } else {
                i += 1;
            }
        }
        let skip = out.len().saturating_sub(HISTORY_TURNS);
        out.split_off(skip)
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
pub fn start_body(question: &str, history: &[Value], corpus: &Corpus) -> Value {
    json!({
        "registry_scope": BUNDLE_SCOPE,
        "bundle_id": BUNDLE_ID,
        "bundle_version": BUNDLE_VERSION,
        "flow_id": FLOW_ID,
        "actor_id": "gateway",
        "input_data": {
            "question": question,
            "history": history,
            "docs": corpus.text,
            "app": corpus.app,
        },
    })
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
    history: &[Value],
) -> ApiResult<(String, Option<String>)> {
    let (corpus, note) = ensure_corpus(c);
    let started = c.start_run(&start_body(question, history, &corpus))?;
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

/// Settle the pending turn (UI thread).
fn settle(store: &Store, op: u64, turn: Turn) {
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
    wake.post(move || settle(&s, op, turn));
}

// ---------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------

/// Ask (Enter / the Ask button). Refusals name their reason.
fn ask(ctx: &Ctx) {
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
    let question = d.draft.get_untracked().trim().to_string();
    if question.is_empty() {
        store
            .notice
            .set(Some("type a question about the gateway first".into()));
        return;
    }
    let history = DocsState::history(&d.turns.get_untracked());
    d.draft.set(String::new());
    d.busy.set(true);
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
    store.begin_busy(op, "docs assistant: answering");
    ctx.send(Cmd::DocsAsk {
        question,
        history,
        op,
    });
}

/// Open the docs assistant (F2). Signed-in only, like the web's
/// session-only ✦ button.
pub fn open(ctx: &Ctx, cx: Scope) {
    let store = ctx.store;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        store.notice.set(Some(
            "the docs assistant needs a gateway connection — probe on the Connection screen".into(),
        ));
        return;
    }
    let d = store.docs;
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    let size = Size::new(vp.w.clamp(1, 100), (vp.h - 2).clamp(1, 40));
    let ctx_ask = ctx.clone();
    let ctx_btn = ctx.clone();
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let close_btn = close.clone();
        let wrap_w = (size.w as usize).saturating_sub(8).max(20);
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Docs assistant", t0.accent)]))
            .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
                let t = theme.get().tokens;
                let note = d.note.get();
                let ink = if note.starts_with("#FALLBACK") {
                    t.warn
                } else {
                    t.text_faint
                };
                line(vec![span(note, ink)])
            }))
            .child(dyn_view_scoped(
                LayoutStyle::default().grow(1.0).min_h(3),
                move |scx| {
                    let t = theme.get().tokens;
                    let turns = d.turns.get();
                    if turns.is_empty() {
                        // Wrapped in a growing column so the composer
                        // sits at the bottom from the first frame.
                        return Element::new()
                            .style(LayoutStyle::column().grow(1.0))
                            .child(line(vec![span(
                                "ask anything about this gateway — setup, routes, providers, apps",
                                t.text_muted,
                            )]))
                            .build();
                    }
                    let mut rows: Vec<View> = Vec::new();
                    for turn in turns {
                        match turn.role {
                            Role::User => {
                                rows.push(line(vec![span_bold("You", t.accent)]));
                                for l in wrap_text(&turn.text, wrap_w) {
                                    rows.push(line(vec![span(l, t.text)]));
                                }
                            }
                            Role::Assistant => {
                                rows.push(line(vec![span_bold("Assistant", t.ok)]));
                                rows.push(MarkdownView::new(turn.text.clone()).view(scx));
                            }
                            Role::Pending => {
                                rows.push(line(vec![span(
                                    "⟳ Thinking… (docs-qa run in progress)",
                                    t.info,
                                )]));
                            }
                            Role::Error => {
                                for l in wrap_text(&turn.text, wrap_w) {
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
                },
            ))
            .child(
                TextInput::new()
                    .value(d.draft)
                    .placeholder("Ask about the gateway… (Enter asks)")
                    .on_submit({
                        let c = ctx_ask.clone();
                        move |_| ask(&c)
                    })
                    .layout(LayoutStyle::default().grow(1.0).h(1).shrink(0.0))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            )
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let busy = d.busy.get();
                    let c = ctx_btn.clone();
                    let close2 = close_btn.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1))
                        .child(
                            Button::new("Ask")
                                .disabled(busy)
                                .on_click(move || ask(&c))
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Clear")
                                .on_click(move || {
                                    // The web's assistantClear: history + messages.
                                    d.turns.update(|v| v.retain(|x| x.role == Role::Pending));
                                })
                                .element(bcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Close (Esc)")
                                .on_click(move || close2())
                                .element(bcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
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
        let h = vec![json!({"role": "user", "content": "q0"})];
        let b = start_body("how do I add a provider?", &h, &c);
        assert_eq!(
            b,
            json!({
                "registry_scope": "tenant_catalog", "bundle_id": "docs-qa",
                "bundle_version": "0.1.0", "flow_id": "docsqa001", "actor_id": "gateway",
                "input_data": {"question": "how do I add a provider?", "history": h,
                               "docs": "# llms", "app": "AbstractGateway"}
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
    fn history_keeps_answered_pairs_only_last_twelve() {
        let mut turns = Vec::new();
        for i in 0..8 {
            turns.push(Turn {
                role: Role::User,
                text: format!("q{i}"),
            });
            turns.push(Turn {
                role: Role::Assistant,
                text: format!("a{i}"),
            });
        }
        // A failed question never becomes context.
        turns.push(Turn {
            role: Role::User,
            text: "bad".into(),
        });
        turns.push(Turn {
            role: Role::Error,
            text: "Failed: x".into(),
        });
        let h = DocsState::history(&turns);
        assert_eq!(h.len(), 12);
        assert_eq!(h[0], json!({"role": "user", "content": "q2"}));
        assert_eq!(h[11], json!({"role": "assistant", "content": "a7"}));
        assert!(!h.iter().any(|m| m["content"] == "bad"));
    }
}
