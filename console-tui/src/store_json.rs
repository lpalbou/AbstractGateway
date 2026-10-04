//! The plain JSON lane's store (round 7, R7.2): one keyed slot per read
//! and one keyed outcome per write, for the parity pages that read the web
//! console's routes as they are (see `api_json.rs`, `worker_json.rs`).
//!
//! Keys are chosen by the page (`"openai"`, `"openai.logs"`,
//! `"catalog"`…): a page owns its keys, and a reconnect clears every key
//! (`Store::reset_domains`), so nothing read from one gateway renders
//! under another.

use std::collections::HashMap;

use abstracttui::prelude::*;
use serde_json::Value;

use super::Loadable;
use crate::api::ApiError;

/// A write's state, by the key the page gave it.
#[derive(Debug, Clone)]
pub enum WriteState {
    /// Sent; the answer has not arrived.
    Pending,
    /// The route's 2xx body.
    Done(Value),
    /// The route refused or failed: the error keeps the JSON body
    /// (`refused_reason`, `errors[]`…) for the page to word verbatim.
    Failed(ApiError),
}

impl WriteState {
    pub fn is_pending(&self) -> bool {
        matches!(self, WriteState::Pending)
    }
}

/// The JSON lane's slots (Copy: all signals).
#[derive(Clone, Copy)]
pub struct JsonStore {
    pub slots: Signal<HashMap<String, Loadable<Value>>>,
    pub writes: Signal<HashMap<String, WriteState>>,
}

impl JsonStore {
    pub fn create(cx: Scope) -> JsonStore {
        JsonStore {
            slots: cx.signal(HashMap::new()),
            writes: cx.signal(HashMap::new()),
        }
    }

    /// The read under `key` (tracked): NotAsked when never asked.
    pub fn get(&self, key: &str) -> Loadable<Value> {
        self.slots
            .with(|m| m.get(key).cloned().unwrap_or(Loadable::NotAsked))
    }

    /// The read under `key` without tracking.
    pub fn get_untracked(&self, key: &str) -> Loadable<Value> {
        self.slots
            .with_untracked(|m| m.get(key).cloned().unwrap_or(Loadable::NotAsked))
    }

    pub fn set(&self, key: &str, v: Loadable<Value>) {
        let key = key.to_string();
        self.slots.update(|m| {
            m.insert(key, v);
        });
    }

    /// The write under `key` (tracked).
    pub fn write(&self, key: &str) -> Option<WriteState> {
        self.writes.with(|m| m.get(key).cloned())
    }

    pub fn write_untracked(&self, key: &str) -> Option<WriteState> {
        self.writes.with_untracked(|m| m.get(key).cloned())
    }

    pub fn set_write(&self, key: &str, w: Option<WriteState>) {
        let key = key.to_string();
        self.writes.update(|m| match w {
            Some(w) => {
                m.insert(key, w);
            }
            None => {
                m.remove(&key);
            }
        });
    }

    /// Forget everything (a new gateway or principal).
    pub fn reset(&self) {
        self.slots.set(HashMap::new());
        self.writes.set(HashMap::new());
    }
}
