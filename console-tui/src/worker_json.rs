//! Worker side of the plain JSON lane (round 7, R7.2): read a web-console
//! route into a keyed slot, or write one and re-read what it changed.
//!
//! Same laws as every write here: write → re-read the named slots (the
//! verify) → journal {action, outcome, verified} + a notice. A page that
//! wants its own sentence for the result reads `json.writes[key]` and
//! words it; the journal line is the audit trail.

use abstracttui::reactive::WakeHandle;
use serde_json::Value;

use super::{require_client, with_busy};
use crate::api::GatewayClient;
use crate::store::json::WriteState;
use crate::store::{JournalEntry, Loadable, Store};

/// One JSON lane command.
#[derive(Debug, Clone)]
pub enum JsonCmd {
    /// `GET path` into `json.slots[key]`. The UI decides whether the slot
    /// shows Loading first (a quiet poll keeps the old rows on screen).
    Get {
        key: String,
        path: String,
        slow: bool,
    },
    /// `method path body` → `json.writes[key]`, then re-read each
    /// `(slot key, path)` in `reload`.
    Send {
        key: String,
        method: String,
        path: String,
        body: Value,
        slow: bool,
        /// The busy strip's and the journal's words ("Delete qwen3:0.6b").
        label: String,
        reload: Vec<(String, String)>,
        /// Journal + notice the outcome (false for page-local writes whose
        /// sentence the page shows inline, e.g. a dry run).
        journal: bool,
    },
}

impl JsonCmd {
    pub fn get(key: &str, path: impl Into<String>) -> JsonCmd {
        JsonCmd::Get {
            key: key.to_string(),
            path: path.into(),
            slow: false,
        }
    }

    pub fn get_slow(key: &str, path: impl Into<String>) -> JsonCmd {
        JsonCmd::Get {
            key: key.to_string(),
            path: path.into(),
            slow: true,
        }
    }
}

fn post_slot(store: &Store, wake: &WakeHandle, key: String, v: Loadable<Value>) {
    let s = *store;
    wake.post(move || s.json.set(&key, v));
}

fn read(client: &GatewayClient, path: &str, slow: bool) -> Loadable<Value> {
    match client.json_get(path, slow) {
        Ok(v) => Loadable::Ready(v),
        Err(e) => Loadable::Failed(e),
    }
}

pub(super) fn handle(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    cmd: JsonCmd,
) {
    let client = match require_client(client) {
        Ok(c) => c,
        Err(e) => {
            match cmd {
                JsonCmd::Get { key, .. } => post_slot(store, wake, key, Loadable::Failed(e)),
                JsonCmd::Send { key, .. } => {
                    let s = *store;
                    wake.post(move || s.json.set_write(&key, Some(WriteState::Failed(e))));
                }
            }
            return;
        }
    };
    match cmd {
        JsonCmd::Get { key, path, slow } => {
            let v = read(&client, &path, slow);
            post_slot(store, wake, key, v);
        }
        JsonCmd::Send {
            key,
            method,
            path,
            body,
            slow,
            label,
            reload,
            journal,
        } => {
            let out = with_busy(store, wake, &label, || {
                client.json_send(&method, &path, &body, slow)
            });
            let mut verified: Option<Result<String, String>> = None;
            for (rkey, rpath) in reload {
                let v = read(&client, &rpath, slow);
                let ok = match &v {
                    Loadable::Failed(e) => Err(format!("GET {rpath}: {e}")),
                    _ => Ok(format!("GET {rpath} read back")),
                };
                verified = Some(match (verified.take(), ok) {
                    (Some(Err(a)), _) => Err(a),
                    (_, b) => b,
                });
                post_slot(store, wake, rkey, v);
            }
            let s = *store;
            wake.post(move || {
                let outcome = match &out {
                    Ok(_) => Ok(format!("{method} {path}")),
                    Err(e) => Err(super::refusal_text(e)),
                };
                if journal {
                    let note = match &outcome {
                        Ok(_) => format!("✓ {label}"),
                        Err(e) => format!("✗ {label}: {e}"),
                    };
                    s.push_journal(JournalEntry {
                        attention: None,
                        when: crate::store::now_hms(),
                        action: label.clone(),
                        outcome,
                        verified,
                    });
                    s.notice.set(Some(note));
                }
                s.json.set_write(
                    &key,
                    Some(match out {
                        Ok(v) => WriteState::Done(v),
                        Err(e) => WriteState::Failed(e),
                    }),
                );
            });
        }
    }
}
