//! Worker half of the Workspaces page (R8.2). A child module of `worker`
//! (one `#[path]` line, one `Cmd::Workspaces` variant, one dispatch arm)
//! sharing its write law — write → verify via GET → journal — and saying
//! the outcome on the page's own line ("Saved", or the gateway's sentence).
//!
//! Each change applies at once (the web applies rows on blur; there is no
//! Save per section). A typed folder goes through `POST
//! /workspace/path-check` first: refused → nothing is written and the
//! sentence is shown; accepted → the list is written with the gateway's
//! normalized path.

use abstracttui::reactive::WakeHandle;
use serde_json::{json, Value};

use super::{finish_write, require_client, with_busy};
use crate::api::{ApiError, GatewayClient};
use crate::store::operator::MyPolicy;
use crate::store::skills::Tone;
use crate::store::workspaces::{Edit, ListKind, PathCheck, Policy, Scope};
use crate::store::{Loadable, RuntimeConfigData, Store};

/// Workspaces commands (one `Cmd::Workspaces` variant carries them).
#[derive(Clone, Debug)]
pub enum WsCmd {
    /// Write `policy` for `scope` (the gateway: only the keys that differ
    /// from `before`; an account / own: the full entry, `None` = follow
    /// the gateway policy).
    Save {
        scope: Scope,
        before: Policy,
        policy: Option<Policy>,
        /// What changed, for the journal ("access mode", "a folder", …).
        what: String,
    },
    /// Check `path`, then write it into `kind` (a new row or row `index`).
    PutFolder {
        scope: Scope,
        before: Policy,
        edit: Edit,
        path: String,
    },
    /// A non-admin's view of the gateway policy (`GET /workspace/policy`).
    LoadPublic,
}

/// The gateway-wide body: only the keys that changed (lists as arrays).
pub fn gateway_body(before: &Policy, after: &Policy) -> Value {
    let mut m = serde_json::Map::new();
    if before.mode != after.mode {
        m.insert(
            "workspace_default_mode".into(),
            json!(after.mode.clone().unwrap_or_else(|| "whitelist".into())),
        );
    }
    if before.trust != after.trust {
        m.insert(
            "trust_client_launch_folder".into(),
            after.trust.map(Value::Bool).unwrap_or(Value::Null),
        );
    }
    for kind in [ListKind::Allowed, ListKind::Refused] {
        if before.list(kind) != after.list(kind) {
            m.insert(kind.gateway_key().into(), json!(after.list(kind)));
        }
    }
    Value::Object(m)
}

fn say(wake: &WakeHandle, store: &Store, text: String, tone: Tone) {
    let ws = store.ws;
    wake.post(move || ws.msg.set(Some((text.clone(), tone))));
}

fn refusal(e: &ApiError) -> String {
    crate::worker::skills::refusal_text(e)
}

/// Write one scope; true when the gateway took it.
fn write(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    scope: &Scope,
    before: &Policy,
    policy: Option<&Policy>,
    what: &str,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) -> Result<(), String> {
    match scope {
        Scope::Gateway => {
            let after = policy.cloned().unwrap_or_default();
            let body = gateway_body(before, &after);
            if body.as_object().is_some_and(|m| m.is_empty()) {
                return Ok(());
            }
            let (w, verify) = with_busy(store, wake, "saving the gateway workspace policy", || {
                let w = require_client(client).and_then(|c| c.save_runtime_config(&body));
                let v = require_client(client).and_then(|c| c.runtime_config());
                (w, v)
            });
            let ok = w.as_ref().map(|_| ()).map_err(refusal);
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok("GET reloaded the workspace policy".to_string()));
            finish_write(
                store,
                wake,
                format!("SAVE gateway workspace policy ({what})"),
                w,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                let d = RuntimeConfigData::from_value(&v);
                let rc = store.runtime_config;
                wake.post(move || rc.set(Loadable::Ready(d.clone())));
            }
            ok
        }
        Scope::Account { tenant_id, user_id } => {
            let body = json!({ "policy": policy.filter(|p| p.is_custom()).map(Policy::entry) });
            let (w, verify) =
                with_busy(store, wake, "saving the account's workspace policy", || {
                    let w = require_client(client)
                        .and_then(|c| c.save_user_workspace_policy(tenant_id, user_id, &body));
                    let v = require_client(client).and_then(|c| c.runtime_config());
                    (w, v)
                });
            let ok = w.as_ref().map(|_| ()).map_err(refusal);
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok("GET reloaded the workspace policies".to_string()));
            finish_write(
                store,
                wake,
                format!("SAVE workspace policy {tenant_id}:{user_id} ({what})"),
                w,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                let d = RuntimeConfigData::from_value(&v);
                let rc = store.runtime_config;
                wake.post(move || rc.set(Loadable::Ready(d.clone())));
            }
            ok
        }
        Scope::Own => {
            let body = json!({ "policy": policy.filter(|p| p.is_custom()).map(|p| {
                // Self-service refuses the admin grant: never send it.
                let mut e = p.entry();
                if let Some(m) = e.as_object_mut() {
                    m.remove("client_workspace_scope_overrides");
                }
                e
            }) });
            let (w, verify) = with_busy(store, wake, "saving your workspace policy", || {
                let w = require_client(client).and_then(|c| c.save_my_workspace_policy(&body));
                let v = require_client(client).and_then(|c| c.my_workspace_policy());
                (w, v)
            });
            let ok = w.as_ref().map(|_| ()).map_err(refusal);
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok("GET reloaded your workspace policy".to_string()));
            finish_write(
                store,
                wake,
                format!("SAVE my workspace policy ({what})"),
                w,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                let p = MyPolicy::from_value(&v);
                let mine = store.op.my_policy;
                wake.post(move || mine.set(Loadable::Ready(p.clone())));
            }
            ok
        }
    }
}

pub(super) fn handle(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    cmd: WsCmd,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    let ws = store.ws;
    match cmd {
        WsCmd::LoadPublic => {
            let res = with_busy(store, wake, "reading the gateway workspace policy", || {
                require_client(client).and_then(|c| c.server_workspace_policy())
            });
            let out = match res {
                Ok(v) => Loadable::Ready(v),
                Err(e) => Loadable::Failed(e),
            };
            wake.post(move || ws.public.set(out.clone()));
        }
        WsCmd::Save {
            scope,
            before,
            policy,
            what,
        } => {
            wake.post(move || ws.busy.set(true));
            let r = write(
                client,
                store,
                wake,
                &scope,
                &before,
                policy.as_ref(),
                &what,
                on_done,
            );
            match r {
                Ok(()) => say(wake, store, "Saved".into(), Tone::Ok),
                Err(e) => say(wake, store, format!("Not saved: {e}"), Tone::Error),
            }
            wake.post(move || ws.busy.set(false));
        }
        WsCmd::PutFolder {
            scope,
            before,
            edit,
            path,
        } => {
            wake.post(move || ws.busy.set(true));
            let check = with_busy(store, wake, "checking the folder", || {
                require_client(client).and_then(|c| c.workspace_path_check(&path))
            });
            let check = match check {
                Ok(v) => PathCheck::from_value(&v, &path),
                Err(e) => {
                    say(
                        wake,
                        store,
                        format!("Not saved: {}", refusal(&e)),
                        Tone::Error,
                    );
                    wake.post(move || ws.busy.set(false));
                    return;
                }
            };
            if !check.valid {
                let s = if check.sentence.is_empty() {
                    "This folder can't be used.".to_string()
                } else {
                    check.sentence.clone()
                };
                say(wake, store, format!("Not saved: {s}"), Tone::Error);
                wake.post(move || ws.busy.set(false));
                return;
            }
            let mut after = before.clone();
            let what = match &edit {
                Edit::Add(k) => {
                    let list = after.list_mut(*k);
                    if !list.contains(&check.normalized) {
                        list.push(check.normalized.clone());
                    }
                    "a folder added"
                }
                Edit::Row(k, i) => {
                    let list = after.list_mut(*k);
                    if let Some(slot) = list.get_mut(*i) {
                        *slot = check.normalized.clone();
                    }
                    "a folder changed"
                }
            };
            match write(
                client,
                store,
                wake,
                &scope,
                &before,
                Some(&after),
                what,
                on_done,
            ) {
                Ok(()) => {
                    // Saved — and the check's plain sentence when it has one
                    // to say (e.g. the folder does not exist yet).
                    let text = if check.exists || check.sentence.is_empty() {
                        "Saved".to_string()
                    } else {
                        format!("Saved. {}", check.sentence)
                    };
                    let tone = if check.exists { Tone::Ok } else { Tone::Plain };
                    say(wake, store, text, tone);
                    wake.post(move || ws.editing.set(None));
                }
                Err(e) => say(wake, store, format!("Not saved: {e}"), Tone::Error),
            }
            wake.post(move || ws.busy.set(false));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_gateway_body_names_only_what_changed() {
        let before = Policy {
            mode: Some("whitelist".into()),
            trust: Some(true),
            allowed: vec!["/a".into()],
            refused: vec![],
            keep: Default::default(),
        };
        let mut after = before.clone();
        after.allowed.push("/b".into());
        assert_eq!(
            gateway_body(&before, &after),
            json!({"workspace_allowed_paths": ["/a", "/b"]})
        );
        let mut m = before.clone();
        m.mode = Some("blacklist".into());
        m.trust = Some(false);
        assert_eq!(
            gateway_body(&before, &m),
            json!({"workspace_default_mode": "blacklist", "trust_client_launch_folder": false})
        );
        assert_eq!(gateway_body(&before, &before), json!({}));
    }
}
