//! Worker half of the operator controls (web-console parity): the gateway
//! host card verbs, the paused-banner poll, the restart/quit watcher,
//! workflow import/reload, the skills reseed, the WAN lookup and the
//! caller's own workspace policy.
//!
//! A child module of `worker` (one `#[path]` line there, one `Cmd`
//! variant, one dispatch arm) so it shares the worker's write law —
//! write → verify via GET → journal — through the same private helpers
//! (`with_busy`, `finish_write`, `load`, `publish_ready`).

use std::sync::mpsc::Sender;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use abstracttui::reactive::WakeHandle;
use serde_json::Value;

use super::{finish_write, load, publish_ready, require_client, with_busy, Body, Cmd, Secret};
use crate::api::{ApiError, ApiErrorKind, GatewayClient};
use crate::store::operator::{
    my_policy_body, seed_report_text, tray_note, HostRunner, HostUpdate, MyPolicy,
};
use crate::store::{JournalEntry, Loadable, NetworkData, RuntimeConfigData, Store};

/// The `/host/runner` poll behind the paused banner (web: 15 s).
pub const RUNNER_POLL_INTERVAL: Duration = Duration::from_secs(15);
/// Gap between restart/quit watcher probes.
const LIFECYCLE_PROBE_INTERVAL: Duration = Duration::from_millis(700);
/// How long a restart may take before the console says it did not come back.
const RESTART_DEADLINE_MS: u64 = 90_000;
/// How long a quit may drain before the console says it is still answering.
const SHUTDOWN_DEADLINE_MS: u64 = 45_000;
/// Update-job poll gap while an upgrade runs (web: 3 s).
const UPDATE_POLL_INTERVAL: Duration = Duration::from_secs(3);

/// Operator-control commands (one `Cmd::Operator` variant carries them).
#[derive(Clone, Debug)]
pub enum OpCmd {
    /// Runner + tray, plus the update overview when `admin`.
    LoadHost { admin: bool },
    /// ONE silent `/host/runner` read for the paused banner, then
    /// reschedule — generation-gated on `store.op.runner_poll_gen`.
    PollRunner { gen: u64 },
    /// `pause: true` → POST /host/pause, false → /host/resume.
    SetPaused { pause: bool },
    /// POST /host/restart, then watch it go down and come back.
    Restart,
    /// POST /host/shutdown, then watch it go down.
    Shutdown,
    /// One watcher probe (rescheduled until settled or the deadline).
    WatchLifecycle {
        restart: bool,
        started_ms: u64,
        saw_down: bool,
        url: String,
        token: Secret,
    },
    UpdateCheck,
    UpdateStart,
    PollUpdate,
    /// Upload one LOCAL `.flow` file (the web console's Import).
    ImportWorkflow {
        path: String,
        include_drafts: bool,
        form_id: Option<u64>,
    },
    /// POST /bundles/reload, then re-list.
    ReloadWorkflows { include_drafts: bool },
    /// POST /admin/skills/reseed, then re-read the knobs.
    ReseedSkills,
    /// GET /network?lookup_public=1.
    LookupPublic,
    LoadMyPolicy,
    /// PUT /workspace/policy/self (`clear` = `{}` back to inherited).
    SaveMyPolicy {
        body: Body,
        clear: bool,
        form_id: Option<u64>,
    },
}

impl OpCmd {
    /// The form awaiting this command (released on a worker panic).
    pub fn form_id(&self) -> Option<u64> {
        match self {
            OpCmd::ImportWorkflow { form_id, .. } | OpCmd::SaveMyPolicy { form_id, .. } => *form_id,
            _ => None,
        }
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Send `cmd` after `after` on a throwaway timer thread — the wait never
/// holds the serial worker lane (the download-poll pattern).
fn later(tx: &Sender<Cmd>, after: Duration, cmd: Cmd) {
    let tx = tx.clone();
    std::thread::Builder::new()
        .name("operator-timer".into())
        .spawn(move || {
            std::thread::sleep(after);
            let _ = tx.send(cmd);
        })
        .ok();
}

fn local_error(message: String) -> ApiError {
    ApiError {
        kind: ApiErrorKind::Protocol,
        message,
        body: None,
        timed_out: false,
    }
}

/// `~/x` → `$HOME/x` (the operator typed a path in THEIR shell's words).
pub fn expand_home(path: &str) -> String {
    let p = path.trim();
    if let Some(rest) = p.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return format!("{}/{rest}", home.trim_end_matches('/'));
        }
    }
    p.to_string()
}

/// The upload answer as a write outcome: `loaded:false` is a FAILURE the
/// operator must see ("installed but NOT running — why"), never "applied".
pub fn import_outcome(v: &Value) -> Value {
    if v.get("loaded").and_then(Value::as_bool) == Some(false) {
        let why = v
            .get("skipped")
            .and_then(|s| s.get("reason"))
            .and_then(Value::as_str)
            .unwrap_or("not served");
        return serde_json::json!({
            "ok": false,
            "error": format!(
                "installed {} but NOT running",
                v.get("bundle_ref").and_then(Value::as_str).unwrap_or("the bundle")
            ),
            "detail": why,
        });
    }
    v.clone()
}

/// `load` without the Loading flash: the UI sets Loading itself only
/// when nothing is held (the paused banner reads the same slot), and the
/// answer — or the failure — replaces what was shown.
fn reload<T: Clone + Send + 'static>(
    store: &Store,
    wake: &WakeHandle,
    label: &str,
    signal: abstracttui::reactive::Signal<Loadable<T>>,
    f: impl FnOnce() -> crate::api::ApiResult<T>,
) {
    let result = with_busy(store, wake, label, f);
    wake.post(move || {
        signal.set(match result {
            Ok(t) => Loadable::Ready(t),
            Err(e) => Loadable::Failed(e),
        })
    });
}

fn post_lifecycle(wake: &WakeHandle, store: &Store, line: String) {
    let s = *store;
    wake.post(move || s.op.lifecycle.set(Some(line.clone())));
}

fn journal(wake: &WakeHandle, store: &Store, action: String, outcome: Result<String, String>) {
    let s = *store;
    let entry = JournalEntry {
        when: crate::store::now_hms(),
        action: action.clone(),
        verified: Some(outcome.clone()),
        outcome,
    };
    wake.post(move || {
        let note = match &entry.outcome {
            Ok(v) => format!("{} — verified: {v}", entry.action),
            Err(e) => format!("{} — {e}", entry.action),
        };
        s.push_journal(entry);
        s.notice.set(Some(note));
    });
}

#[allow(clippy::too_many_arguments)]
pub(super) fn handle(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    cmd: OpCmd,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    let op = store.op;
    match cmd {
        OpCmd::LoadHost { admin } => {
            reload(store, wake, "reading the gateway host", op.runner, || {
                require_client(client)?.host_runner().map(|v| HostRunner::from_value(&v))
            });
            reload(store, wake, "reading the desktop tray", op.tray, || {
                require_client(client)?.host_tray().map(|v| tray_note(&v))
            });
            if admin {
                reload(store, wake, "reading the update state", op.update, || {
                    require_client(client)?.host_update().map(|v| HostUpdate::from_value(&v))
                });
            }
        }

        OpCmd::PollRunner { gen } => {
            let result = require_client(client).and_then(|c| c.host_runner());
            let s = *store;
            let tx2 = tx.clone();
            wake.post(move || {
                if s.op.runner_poll_gen.get_untracked() != gen {
                    return; // disconnected / reset: this chain is over
                }
                match result {
                    Ok(v) => s.op.runner.set(Loadable::Ready(HostRunner::from_value(&v))),
                    // A failed poll never erases a held answer (the web's
                    // silent catch): the banner keeps its last truth and
                    // the health authority owns transport failures.
                    Err(e) => {
                        if s.op.runner.with_untracked(|r| r.ready().is_none()) {
                            s.op.runner.set(Loadable::Failed(e));
                        }
                    }
                }
                later(&tx2, RUNNER_POLL_INTERVAL, Cmd::Operator(OpCmd::PollRunner { gen }));
            });
        }

        OpCmd::SetPaused { pause } => {
            let (verb, label) = if pause {
                ("PAUSE", "pausing workflows")
            } else {
                ("RESUME", "resuming workflows")
            };
            let (write, verify) = with_busy(store, wake, label, || {
                let write = require_client(client)
                    .and_then(|c| if pause { c.host_pause() } else { c.host_resume() });
                let verify = require_client(client).and_then(|c| c.host_runner());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let r = HostRunner::from_value(v);
                if r.paused == pause {
                    Ok(format!("GET /host/runner: {}", r.state_text()))
                } else {
                    Err(format!("GET /host/runner still says {}", r.state_text()))
                }
            });
            finish_write(store, wake, format!("{verb} workflows (gateway host)"), write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, op.runner, HostRunner::from_value(&v));
            }
        }

        OpCmd::Restart | OpCmd::Shutdown => {
            let restart = matches!(cmd, OpCmd::Restart);
            let (verb, label) = if restart {
                ("RESTART gateway", "asking the gateway to restart")
            } else {
                ("QUIT gateway", "asking the gateway to quit")
            };
            let write = with_busy(store, wake, label, || {
                require_client(client).and_then(|c| if restart { c.host_restart() } else { c.host_shutdown() })
            });
            let accepted = write.is_ok();
            // The real verification is the watcher's: the connection is
            // SUPPOSED to drop now, so a follow-up GET here proves nothing.
            let verified = accepted.then(|| {
                Ok(if restart {
                    "accepted — watching it go down and come back".to_string()
                } else {
                    "accepted — watching it go down".to_string()
                })
            });
            finish_write(store, wake, verb.to_string(), write, verified, None, on_done);
            if accepted {
                if let Ok(c) = require_client(client) {
                    let (url, token) = c.credentials();
                    post_lifecycle(
                        wake,
                        store,
                        if restart {
                            "⟳ restarting — waiting for the gateway to go down and come back…".into()
                        } else {
                            "⟳ quitting — waiting for the gateway to stop…".into()
                        },
                    );
                    later(
                        tx,
                        LIFECYCLE_PROBE_INTERVAL,
                        Cmd::Operator(OpCmd::WatchLifecycle {
                            restart,
                            started_ms: now_ms(),
                            saw_down: false,
                            url,
                            token: Secret(token.unwrap_or_default()),
                        }),
                    );
                }
            }
        }

        OpCmd::WatchLifecycle {
            restart,
            started_ms,
            saw_down,
            url,
            token,
        } => {
            let elapsed = now_ms().saturating_sub(started_ms);
            let secs = elapsed / 1000;
            // A throwaway short-deadline client: never the pooled one.
            let probe = GatewayClient::new(&url, Some(&token.0)).with_read_timeout(Duration::from_secs(3));
            let res = probe.host_runner();
            let down = matches!(&res, Err(e) if e.kind == ApiErrorKind::Unreachable);
            let reconnect = Cmd::Connect {
                url: url.clone(),
                token: token.clone(),
            };
            let next = |saw_down: bool| {
                Cmd::Operator(OpCmd::WatchLifecycle {
                    restart,
                    started_ms,
                    saw_down,
                    url: url.clone(),
                    token: token.clone(),
                })
            };
            if restart {
                // Back = answering after we saw it down, OR answering
                // with `restart_requested` cleared (a fresh process —
                // the old one reports true until it exits).
                let back = match &res {
                    Ok(v) => saw_down || !HostRunner::from_value(v).restart_requested,
                    // Back but refusing this token (auth changed): the
                    // connection probe says so in its own words.
                    Err(e) => {
                        saw_down && matches!(e.kind, ApiErrorKind::Unauthorized | ApiErrorKind::Forbidden)
                    }
                };
                if back {
                    post_lifecycle(wake, store, format!("✓ the gateway restarted and answers again ({secs}s) — reconnected"));
                    journal(wake, store, "RESTART gateway".into(), Ok(format!("down then back after {secs}s; re-probed")));
                    let _ = tx.send(reconnect);
                } else if elapsed >= RESTART_DEADLINE_MS {
                    let why = if saw_down {
                        "went down and has not come back — check its log, then probe on Connection"
                    } else {
                        "still answering as the OLD process — the restart did not happen"
                    };
                    post_lifecycle(wake, store, format!("✗ the gateway {why} ({secs}s)"));
                    journal(wake, store, "RESTART gateway".into(), Err(format!("{why} ({secs}s)")));
                    let _ = tx.send(reconnect);
                } else {
                    let saw = saw_down || down;
                    post_lifecycle(
                        wake,
                        store,
                        if saw {
                            format!("⟳ restarting — the gateway is down, waiting for it to come back ({secs}s)…")
                        } else {
                            format!("⟳ restarting — the gateway is finishing its work ({secs}s)…")
                        },
                    );
                    later(tx, LIFECYCLE_PROBE_INTERVAL, next(saw));
                }
            } else if down {
                post_lifecycle(
                    wake,
                    store,
                    format!("✓ the gateway stopped ({secs}s) — start it again with `abstractgateway serve`"),
                );
                journal(wake, store, "QUIT gateway".into(), Ok(format!("unreachable after {secs}s")));
                // The probe settles the honest header (unreachable).
                let _ = tx.send(reconnect);
            } else if elapsed >= SHUTDOWN_DEADLINE_MS {
                post_lifecycle(wake, store, format!("✗ the gateway still answers {secs}s after quitting was accepted"));
                journal(wake, store, "QUIT gateway".into(), Err(format!("still answering after {secs}s")));
                let _ = tx.send(reconnect);
            } else {
                post_lifecycle(wake, store, format!("⟳ quitting — the gateway is finishing its work ({secs}s)…"));
                later(tx, LIFECYCLE_PROBE_INTERVAL, next(false));
            }
        }

        OpCmd::UpdateCheck => {
            let (write, verify) = with_busy(store, wake, "checking for a gateway update", || {
                let write = require_client(client).and_then(|c| c.host_update_check());
                let verify = require_client(client).and_then(|c| c.host_update());
                (write, verify)
            });
            let verified = verify
                .as_ref()
                .ok()
                .map(|v| Ok(format!("GET /host/update: {}", HostUpdate::from_value(v).version_text())));
            finish_write(store, wake, "CHECK for a gateway update".into(), write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, op.update, HostUpdate::from_value(&v));
            }
        }

        OpCmd::UpdateStart => {
            let (write, verify) = with_busy(store, wake, "starting the gateway update", || {
                let write = require_client(client).and_then(|c| c.host_update_start());
                let verify = require_client(client).and_then(|c| c.host_update());
                (write, verify)
            });
            let running = verify
                .as_ref()
                .ok()
                .map(|v| HostUpdate::from_value(v).job_state == "running")
                .unwrap_or(false);
            let verified = verify
                .as_ref()
                .ok()
                .map(|v| Ok(format!("GET /host/update: {}", HostUpdate::from_value(v).version_text())));
            finish_write(store, wake, "START the gateway update".into(), write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, op.update, HostUpdate::from_value(&v));
            }
            if running {
                later(tx, UPDATE_POLL_INTERVAL, Cmd::Operator(OpCmd::PollUpdate));
            }
        }

        OpCmd::PollUpdate => {
            if let Ok(v) = require_client(client).and_then(|c| c.host_update()) {
                let u = HostUpdate::from_value(&v);
                let running = u.job_state == "running";
                publish_ready(wake, op.update, u);
                if running {
                    later(tx, UPDATE_POLL_INTERVAL, Cmd::Operator(OpCmd::PollUpdate));
                }
            }
        }

        OpCmd::ImportWorkflow {
            path,
            include_drafts,
            form_id,
        } => {
            let full = expand_home(&path);
            let filename = std::path::Path::new(&full)
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_else(|| "upload.flow".into());
            let action = format!("IMPORT workflow {full}");
            let (write, verify) = with_busy(store, wake, &format!("importing {filename}"), || {
                let bytes = match std::fs::read(&full) {
                    Ok(b) => b,
                    Err(e) => return (Err(local_error(format!("cannot read {full}: {e}"))), None),
                };
                // Web parity: overwrite=false, reload=true.
                let write = require_client(client)
                    .and_then(|c| c.upload_bundle(&filename, &bytes, false, true))
                    .map(|v| import_outcome(&v));
                let verify = write
                    .as_ref()
                    .ok()
                    .map(|_| require_client(client).and_then(|c| c.bundles(true)));
                (write, verify)
            });
            let verified = match (&write, &verify) {
                (Ok(w), Some(Ok(listing))) => {
                    let data = crate::store::workflows_from_payload(listing);
                    let bid = w.get("bundle_id").and_then(Value::as_str).unwrap_or("");
                    let ver = w.get("bundle_version").and_then(Value::as_str).unwrap_or("");
                    let listed = data
                        .rows
                        .iter()
                        .any(|r| r.bundle_id == bid && r.versions.iter().any(|(x, _, _, _)| x == ver));
                    let skipped = data
                        .skipped
                        .iter()
                        .find(|s| s.bundle_id == bid && s.bundle_version == ver)
                        .map(|s| s.reason.clone());
                    Some(match (listed, skipped) {
                        (true, _) => Ok(format!("GET lists {bid}@{ver}")),
                        (false, Some(why)) => Err(format!("GET lists {bid}@{ver} as NOT runnable: {why}")),
                        (false, None) => Err(format!("GET does not list {bid}@{ver}")),
                    })
                }
                _ => None,
            };
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Some(Ok(v)) = verify {
                let listing = if include_drafts {
                    Ok(v)
                } else {
                    require_client(client).and_then(|c| c.bundles(false))
                };
                if let Ok(v) = listing {
                    publish_ready(wake, store.workflows, crate::store::workflows_from_payload(&v));
                }
            }
        }

        OpCmd::ReloadWorkflows { include_drafts } => {
            let (write, verify) = with_busy(store, wake, "reloading workflows from disk", || {
                let write = require_client(client).and_then(|c| c.reload_bundles());
                let verify = require_client(client).and_then(|c| c.bundles(include_drafts));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let d = crate::store::workflows_from_payload(v);
                Ok(format!(
                    "GET lists {} workflow(s), {} version(s) not runnable",
                    d.rows.len(),
                    d.skipped.len()
                ))
            });
            finish_write(store, wake, "RELOAD workflows".into(), write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.workflows, crate::store::workflows_from_payload(&v));
            }
        }

        OpCmd::ReseedSkills => {
            let (write, verify) = with_busy(store, wake, "refreshing the curated skills shelf", || {
                let write = require_client(client).and_then(|c| c.reseed_skills());
                let verify = require_client(client).and_then(|c| c.runtime_config());
                (write, verify)
            });
            let report = write.as_ref().ok().map(seed_report_text);
            let verified = verify.as_ref().ok().map(|v| {
                let d = RuntimeConfigData::from_value(v);
                let shelf = d
                    .skills_shelf
                    .map(|sh| format!("GET shows shelf {} ({})", sh.resolved, sh.source))
                    .unwrap_or_else(|| "GET reports no skills shelf".into());
                Ok(match &report {
                    Some(r) => format!("{r} {shelf}"),
                    None => shelf,
                })
            });
            finish_write(store, wake, "REFRESH curated skills shelf".into(), write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.runtime_config, RuntimeConfigData::from_value(&v));
            }
        }

        OpCmd::LookupPublic => {
            let result = with_busy(store, wake, "looking up the public address", || {
                require_client(client).and_then(|c| c.network_lookup_public())
            });
            let s = *store;
            wake.post(move || match result {
                Ok(v) => {
                    let d = NetworkData::from_value(&v);
                    let note = match d.addresses.iter().find(|a| a.kind == "public") {
                        Some(a) if !a.url.is_empty() => format!("public address: {}", a.url),
                        Some(a) => format!("public address lookup: {}", a.note),
                        None => format!(
                            "public address not looked up: {}",
                            d.public_note.clone().unwrap_or_else(|| "the gateway gave no reason".into())
                        ),
                    };
                    s.network.set(Loadable::Ready(d));
                    s.notice.set(Some(note));
                }
                Err(e) => s.notice.set(Some(format!("public address lookup failed: {e}"))),
            });
        }

        OpCmd::LoadMyPolicy => load(store, wake, "reading my workspace policy", op.my_policy, || {
            require_client(client)?
                .my_workspace_policy()
                .map(|v| MyPolicy::from_value(&v))
        }),

        OpCmd::SaveMyPolicy { body, clear, form_id } => {
            let action = if clear {
                "RESET my workspace policy to inherited".to_string()
            } else {
                "PUT my workspace policy".to_string()
            };
            let (write, verify) = with_busy(store, wake, "saving my workspace policy", || {
                let write = require_client(client).and_then(|c| c.save_my_workspace_policy(&body));
                let verify = require_client(client).and_then(|c| c.my_workspace_policy());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let got = MyPolicy::from_value(v);
                // Read back what the store holds and compare it to what
                // was asked, field by field (the body builder's shape).
                let now = my_policy_body(&got.mode, &got.trust, &got.allowed.join("\n"), &got.blocked.join("\n"));
                if now == body.0 {
                    Ok(format!("GET /workspace/policy/self — {}", got.effective_text()))
                } else {
                    Err(format!("GET /workspace/policy/self holds {now}, not {}", body.0))
                }
            });
            // A refused save keeps the form's edits: only a landed write
            // republishes (the form body rebuilds from the published value).
            let wrote = write.is_ok();
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let (true, Ok(v)) = (wrote, verify) {
                publish_ready(wake, op.my_policy, MyPolicy::from_value(&v));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn import_outcome_turns_not_loaded_into_a_failure() {
        let ok = json!({"ok": true, "loaded": true, "bundle_ref": "a@1"});
        assert_eq!(import_outcome(&ok), ok);
        let bad = import_outcome(&json!({
            "ok": false, "loaded": false, "bundle_ref": "a@1",
            "skipped": {"reason": "requires runtime >= 9"}
        }));
        assert_eq!(bad["ok"], json!(false));
        assert_eq!(bad["error"], json!("installed a@1 but NOT running"));
        assert_eq!(bad["detail"], json!("requires runtime >= 9"));
    }

    #[test]
    fn expand_home_only_touches_a_leading_tilde() {
        let home = std::env::var("HOME").expect("HOME in the test env");
        let home = home.trim_end_matches('/');
        assert_eq!(expand_home(" ~/flows/x.flow "), format!("{home}/flows/x.flow"));
        assert_eq!(expand_home("/abs/~/x.flow"), "/abs/~/x.flow");
    }
}
