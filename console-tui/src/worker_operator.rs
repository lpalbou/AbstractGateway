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
use crate::store::email::{email_error_text, notifications_body, MyEmail, MyNotifications};
use crate::store::operator::{
    my_policy_body, seed_report_text, start_again_hint, tray_note, HostRunner, HostUpdate,
    MyPolicy, StartAtLogin,
};
use crate::store::{JournalEntry, Loadable, NetworkData, RuntimeConfigData, Store};

/// A failed `/host/runner` poll (UI thread). It never erases a held
/// answer (the web's silent catch: the banner keeps its last truth), and a
/// TRANSPORT failure is handed to the health authority (the write-failure
/// trigger channel): the gateway died while the console sat on a screen
/// that loads nothing (Connection) must not keep saying "● connected".
pub fn runner_poll_failed(s: Store, e: crate::api::ApiError) {
    if matches!(e.kind, crate::api::ApiErrorKind::Unreachable) {
        s.net_fail_seq.update(|n| *n += 1);
    }
    if s.op.runner.with_untracked(|r| r.ready().is_none()) {
        s.op.runner.set(Loadable::Failed(e));
    }
}

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
    LoadHost {
        admin: bool,
    },
    /// ONE silent `/host/runner` read for the paused banner, then
    /// reschedule — generation-gated on `store.op.runner_poll_gen`.
    PollRunner {
        gen: u64,
    },
    /// `pause: true` → POST /host/pause, false → /host/resume.
    SetPaused {
        pause: bool,
    },
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
        /// Quit only: how to start this gateway again (its login service
        /// facts, read before it went away — `start_again_hint`).
        start_again: String,
    },
    UpdateCheck,
    UpdateStart {
        installer_sha256: Option<String>,
    },
    PollUpdate,
    /// Upload one LOCAL `.flow` file (the web console's Import).
    ImportWorkflow {
        path: String,
        include_drafts: bool,
        form_id: Option<u64>,
    },
    /// POST /bundles/reload, then re-list.
    ReloadWorkflows {
        include_drafts: bool,
    },
    /// POST /admin/skills/reseed, then re-read the knobs.
    ReseedSkills,
    /// GET /network?lookup_public=1.
    LookupPublic,
    /// GET /host/start-at-login (admin).
    LoadStartAtLogin,
    /// PUT /host/start-at-login, verified by a GET.
    SetStartAtLogin {
        enabled: bool,
        replace_other: bool,
    },
    /// POST /network/restart, then the restart watcher (reconnecting to
    /// the address the new exposure serves on).
    RestartNetwork,
    LoadMyPolicy,
    /// PUT /workspace/policy/self (`clear` = `{}` back to inherited).
    SaveMyPolicy {
        body: Body,
        clear: bool,
        form_id: Option<u64>,
    },
    /// GET /me/email + GET /me/notifications (framework backlog 0992).
    LoadMyEmail,
    /// One "My email" write, verified by a GET (the worker's write law).
    Email {
        action: EmailAction,
        form_id: Option<u64>,
    },
}

/// The "My email" writes (web parity: the Users tab's My email section,
/// and the admin's per-user switch on the Users table).
#[derive(Clone, Debug)]
pub enum EmailAction {
    /// PUT /me/email — test, then store.
    Connect(Body),
    /// POST /me/email/test.
    Test,
    /// DELETE /me/email.
    Disconnect,
    /// PUT /me/email/policy.
    Policy(Body),
    /// PUT /me/email/limits.
    Limits(Body),
    /// PUT /me/email/enabled — the user's own switch.
    Enabled(bool),
    /// PUT /me/email/agent-tools — the agents' email tools (default off).
    AgentTools(bool),
    /// PUT /me/notifications.
    Notifications(Body),
    /// POST /me/notifications/test.
    TestNotification,
    /// POST /me/email/oauth/start, then poll the flow.
    OAuthStart(Body),
    /// One poll of the awaited OAuth2 sign-in (rescheduled while pending).
    OAuthPoll(String),
    /// POST /me/email/oauth/cancel.
    OAuthCancel(String),
    /// PUT /admin/users/{id}/email (admin) — on/off only.
    AdminSetEnabled {
        user_id: String,
        tenant_id: String,
        enabled: bool,
    },
}

impl OpCmd {
    /// The form awaiting this command (released on a worker panic).
    pub fn form_id(&self) -> Option<u64> {
        match self {
            OpCmd::ImportWorkflow { form_id, .. }
            | OpCmd::SaveMyPolicy { form_id, .. }
            | OpCmd::Email { form_id, .. } => *form_id,
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

/// Start the restart/quit watcher: the banner line, then the first probe.
fn start_watch(
    wake: &WakeHandle,
    store: &Store,
    tx: &Sender<Cmd>,
    restart: bool,
    url: String,
    token: Option<String>,
    start_again: String,
) {
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
            start_again,
        }),
    );
}

/// `url` with its port replaced by `port` (the port a network restart
/// binds): scheme and host stay, a path is dropped (base URLs have none).
pub fn url_with_port(url: &str, port: u64) -> String {
    let (scheme, rest) = match url.split_once("://") {
        Some((s, r)) => (s, r),
        None => ("http", url),
    };
    let authority = rest.split('/').next().unwrap_or(rest);
    // An IPv6 literal keeps its brackets; the port follows the last ']'.
    let host = if let Some(end) = authority.rfind(']') {
        &authority[..=end]
    } else {
        authority
            .rsplit_once(':')
            .map(|(h, _)| h)
            .unwrap_or(authority)
    };
    format!("{scheme}://{host}:{port}")
}

fn journal(wake: &WakeHandle, store: &Store, action: String, outcome: Result<String, String>) {
    let s = *store;
    let entry = JournalEntry {
        attention: None,
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
                require_client(client)?
                    .host_runner()
                    .map(|v| HostRunner::from_value(&v))
            });
            reload(store, wake, "reading the desktop tray", op.tray, || {
                require_client(client)?.host_tray().map(|v| tray_note(&v))
            });
            if admin {
                reload(store, wake, "reading the update state", op.update, || {
                    require_client(client)?
                        .host_update()
                        .map(|v| HostUpdate::from_value(&v))
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
                    Err(e) => runner_poll_failed(s, e),
                }
                later(
                    &tx2,
                    RUNNER_POLL_INTERVAL,
                    Cmd::Operator(OpCmd::PollRunner { gen }),
                );
            });
        }

        OpCmd::SetPaused { pause } => {
            let (verb, label) = if pause {
                ("PAUSE", "pausing workflows")
            } else {
                ("RESUME", "resuming workflows")
            };
            let (write, verify) = with_busy(store, wake, label, || {
                let write = require_client(client).and_then(|c| {
                    if pause {
                        c.host_pause()
                    } else {
                        c.host_resume()
                    }
                });
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
            finish_write(
                store,
                wake,
                format!("{verb} workflows (gateway host)"),
                write,
                verified,
                None,
                on_done,
            );
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
            // Quit: read how this gateway starts again BEFORE it goes away
            // (its login service, `GET /host/state`); after, nothing answers.
            let start_again = if restart {
                String::new()
            } else {
                let svc = require_client(client)
                    .and_then(|c| c.host_state())
                    .ok()
                    .map(|v| crate::api::firstrun::WelcomeSummary::from_host_state(&v));
                start_again_hint(
                    svc.as_ref().and_then(|w| w.service_installed),
                    svc.as_ref().and_then(|w| w.service_mechanism.as_deref()),
                )
            };
            let write = with_busy(store, wake, label, || {
                require_client(client).and_then(|c| {
                    if restart {
                        c.host_restart()
                    } else {
                        c.host_shutdown()
                    }
                })
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
            finish_write(
                store,
                wake,
                verb.to_string(),
                write,
                verified,
                None,
                on_done,
            );
            if accepted {
                if let Ok(c) = require_client(client) {
                    let (url, token) = c.credentials();
                    start_watch(wake, store, tx, restart, url, token, start_again);
                }
            }
        }

        OpCmd::RestartNetwork => {
            let res = with_busy(store, wake, "restarting the gateway", || {
                require_client(client).and_then(|c| c.restart_network())
            });
            match res {
                Ok(v) => {
                    journal(
                        wake,
                        store,
                        "RESTART gateway (apply the network exposure)".into(),
                        Ok("accepted — watching it go down and come back".into()),
                    );
                    if let Ok(c) = require_client(client) {
                        let (url, token) = c.credentials();
                        let port = v
                            .get("next")
                            .and_then(|n| n.get("port"))
                            .and_then(Value::as_u64);
                        let url = match port {
                            Some(p) => url_with_port(&url, p),
                            None => url,
                        };
                        start_watch(wake, store, tx, true, url, token, String::new());
                    }
                }
                Err(e) => {
                    let note = format!("✗ restart refused: {}", super::refusal_text(&e));
                    let s = *store;
                    wake.post(move || s.notice.set(Some(note.clone())));
                }
            }
        }

        OpCmd::LoadStartAtLogin => load(
            store,
            wake,
            "reading start at login",
            op.start_at_login,
            || {
                require_client(client)?
                    .start_at_login()
                    .map(|v| StartAtLogin::from_value(&v))
            },
        ),

        OpCmd::SetStartAtLogin {
            enabled,
            replace_other,
        } => {
            let label = if enabled {
                "turning start at login on"
            } else {
                "turning start at login off"
            };
            let (write, verify) = with_busy(store, wake, label, || {
                let write = require_client(client)
                    .and_then(|c| c.set_start_at_login(enabled, replace_other));
                let verify = require_client(client).and_then(|c| c.start_at_login());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let st = StartAtLogin::from_value(v);
                if st.enabled == enabled {
                    Ok(format!("GET /host/start-at-login: {}", st.text()))
                } else {
                    Err(format!("GET /host/start-at-login still says {}", st.text()))
                }
            });
            finish_write(
                store,
                wake,
                format!("START AT LOGIN {}", if enabled { "on" } else { "off" }),
                write,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                publish_ready(wake, op.start_at_login, StartAtLogin::from_value(&v));
            }
        }

        OpCmd::WatchLifecycle {
            restart,
            started_ms,
            saw_down,
            url,
            token,
            start_again,
        } => {
            let elapsed = now_ms().saturating_sub(started_ms);
            let secs = elapsed / 1000;
            // A throwaway short-deadline client: never the pooled one.
            let probe =
                GatewayClient::new(&url, Some(&token.0)).with_read_timeout(Duration::from_secs(3));
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
                    start_again: start_again.clone(),
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
                        saw_down
                            && matches!(
                                e.kind,
                                ApiErrorKind::Unauthorized | ApiErrorKind::Forbidden
                            )
                    }
                };
                if back {
                    post_lifecycle(
                        wake,
                        store,
                        format!(
                            "✓ the gateway restarted and answers again ({secs}s) — reconnected"
                        ),
                    );
                    journal(
                        wake,
                        store,
                        "RESTART gateway".into(),
                        Ok(format!("down then back after {secs}s; re-probed")),
                    );
                    let _ = tx.send(reconnect);
                } else if elapsed >= RESTART_DEADLINE_MS {
                    let why = if saw_down {
                        "went down and has not come back — check its log, then probe on Connection"
                    } else {
                        "still answering as the OLD process — the restart did not happen"
                    };
                    post_lifecycle(wake, store, format!("✗ the gateway {why} ({secs}s)"));
                    journal(
                        wake,
                        store,
                        "RESTART gateway".into(),
                        Err(format!("{why} ({secs}s)")),
                    );
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
                    format!("✓ the gateway stopped ({secs}s) — {start_again}"),
                );
                journal(
                    wake,
                    store,
                    "QUIT gateway".into(),
                    Ok(format!("unreachable after {secs}s")),
                );
                // The probe settles the honest header (unreachable).
                let _ = tx.send(reconnect);
            } else if elapsed >= SHUTDOWN_DEADLINE_MS {
                post_lifecycle(
                    wake,
                    store,
                    format!("✗ the gateway still answers {secs}s after quitting was accepted"),
                );
                journal(
                    wake,
                    store,
                    "QUIT gateway".into(),
                    Err(format!("still answering after {secs}s")),
                );
                let _ = tx.send(reconnect);
            } else {
                post_lifecycle(
                    wake,
                    store,
                    format!("⟳ quitting — the gateway is finishing its work ({secs}s)…"),
                );
                later(tx, LIFECYCLE_PROBE_INTERVAL, next(false));
            }
        }

        OpCmd::UpdateCheck => {
            let (write, verify) = with_busy(store, wake, "checking for a gateway update", || {
                let write = require_client(client).and_then(|c| c.host_update_check());
                let verify = require_client(client).and_then(|c| c.host_update());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                Ok(format!(
                    "GET /host/update: {}",
                    HostUpdate::from_value(v).version_text()
                ))
            });
            finish_write(
                store,
                wake,
                "CHECK for a gateway update".into(),
                write,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                publish_ready(wake, op.update, HostUpdate::from_value(&v));
            }
        }

        OpCmd::UpdateStart { installer_sha256 } => {
            let (write, verify) = with_busy(store, wake, "starting the gateway update", || {
                let write = require_client(client)
                    .and_then(|c| c.host_update_start(installer_sha256.as_deref()));
                let verify = require_client(client).and_then(|c| c.host_update());
                (write, verify)
            });
            let running = verify
                .as_ref()
                .ok()
                .map(|v| HostUpdate::from_value(v).job_state == "running")
                .unwrap_or(false);
            let verified = verify.as_ref().ok().map(|v| {
                Ok(format!(
                    "GET /host/update: {}",
                    HostUpdate::from_value(v).version_text()
                ))
            });
            finish_write(
                store,
                wake,
                "START the gateway update".into(),
                write,
                verified,
                None,
                on_done,
            );
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
                    let ver = w
                        .get("bundle_version")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let listed = data.rows.iter().any(|r| {
                        r.bundle_id == bid && r.versions.iter().any(|(x, _, _, _)| x == ver)
                    });
                    let skipped = data
                        .skipped
                        .iter()
                        .find(|s| s.bundle_id == bid && s.bundle_version == ver)
                        .map(|s| s.reason.clone());
                    Some(match (listed, skipped) {
                        (true, _) => Ok(format!("GET lists {bid}@{ver}")),
                        (false, Some(why)) => {
                            Err(format!("GET lists {bid}@{ver} as NOT runnable: {why}"))
                        }
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
                    publish_ready(
                        wake,
                        store.workflows,
                        crate::store::workflows_from_payload(&v),
                    );
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
            finish_write(
                store,
                wake,
                "RELOAD workflows".into(),
                write,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.workflows,
                    crate::store::workflows_from_payload(&v),
                );
            }
        }

        OpCmd::ReseedSkills => {
            let (write, verify) =
                with_busy(store, wake, "refreshing the curated skills shelf", || {
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
            finish_write(
                store,
                wake,
                "REFRESH curated skills shelf".into(),
                write,
                verified,
                None,
                on_done,
            );
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.runtime_config,
                    RuntimeConfigData::from_value(&v),
                );
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
                            d.public_note
                                .clone()
                                .unwrap_or_else(|| "the gateway gave no reason".into())
                        ),
                    };
                    s.network.set(Loadable::Ready(d));
                    s.notice.set(Some(note));
                }
                Err(e) => s
                    .notice
                    .set(Some(format!("public address lookup failed: {e}"))),
            });
        }

        OpCmd::LoadMyPolicy => load(
            store,
            wake,
            "reading my workspace policy",
            op.my_policy,
            || {
                require_client(client)?
                    .my_workspace_policy()
                    .map(|v| MyPolicy::from_value(&v))
            },
        ),

        OpCmd::SaveMyPolicy {
            body,
            clear,
            form_id,
        } => {
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
                let now = my_policy_body(
                    &got.mode,
                    &got.trust,
                    &got.allowed.join("\n"),
                    &got.blocked.join("\n"),
                );
                if now == body.0 {
                    Ok(format!(
                        "GET /workspace/policy/self — {}",
                        got.effective_text()
                    ))
                } else {
                    Err(format!(
                        "GET /workspace/policy/self holds {now}, not {}",
                        body.0
                    ))
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

        OpCmd::LoadMyEmail => {
            load(store, wake, "reading my email", op.my_email, || {
                require_client(client)?
                    .my_email()
                    .map(|v| MyEmail::from_value(&v))
            });
            load(
                store,
                wake,
                "reading my notifications",
                op.my_notifications,
                || {
                    require_client(client)?
                        .my_notifications()
                        .map(|v| MyNotifications::from_value(&v))
                },
            );
        }

        OpCmd::Email { action, form_id } => {
            email_write(client, store, wake, tx, action, form_id, on_done)
        }
    }
}

/// A typed refusal keeps its words (`cause Fix: fix`), not a JSON dump.
fn email_err(e: ApiError) -> ApiError {
    let message = email_error_text(&e);
    ApiError { message, ..e }
}

/// `{ok:false, ...}` answers (a failed test leg, an unsent test notice) as
/// a body-level failure carrying the cause and the fix.
fn email_outcome(v: Value) -> Value {
    if v.get("ok").and_then(Value::as_bool) != Some(false) {
        return v;
    }
    let from_leg = ["imap", "smtp"]
        .iter()
        .filter_map(|k| v.get(*k))
        .find(|leg| leg.get("ok").and_then(Value::as_bool) == Some(false))
        .cloned();
    let err = from_leg
        .or_else(|| v.get("error").cloned())
        .unwrap_or(Value::Null);
    let cause = err.get("cause").and_then(Value::as_str).unwrap_or("");
    let fix = err.get("fix").and_then(Value::as_str).unwrap_or("");
    let text = match (cause.is_empty(), fix.is_empty()) {
        (true, _) => v
            .get("state")
            .and_then(Value::as_str)
            .map(|s| format!("not sent ({s})"))
            .unwrap_or_else(|| "failed".into()),
        (false, true) => cause.to_string(),
        (false, false) => format!("{cause} Fix: {fix}"),
    };
    serde_json::json!({"ok": false, "error": text})
}

/// The OAuth2 poll gap (the gateway answers at once while pending).
const OAUTH_POLL_INTERVAL: Duration = Duration::from_secs(3);

#[allow(clippy::too_many_arguments)]
fn email_write(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    action: EmailAction,
    form_id: Option<u64>,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    let op = store.op;
    match action {
        EmailAction::OAuthStart(body) => {
            let started = with_busy(store, wake, "starting the OAuth2 sign-in", || {
                require_client(client).and_then(|c| c.my_email_oauth_start(&body))
            })
            .map_err(email_err);
            match started {
                Ok(v) => {
                    let flow_id = v
                        .get("flow_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    let prompt = if v.get("flow").and_then(Value::as_str) == Some("device") {
                        format!(
                            "Open {} in any browser and enter the code {}. Waiting for the approval…",
                            v.get("verification_uri").and_then(Value::as_str).unwrap_or("?"),
                            v.get("user_code").and_then(Value::as_str).unwrap_or("?"),
                        )
                    } else {
                        format!(
                            "Open this link in a browser on the gateway's computer: {} — waiting for the sign-in…",
                            v.get("authorization_url").and_then(Value::as_str).unwrap_or("?"),
                        )
                    };
                    let (fid, p) = (flow_id.clone(), prompt);
                    wake.post(move || op.email_oauth.set(Some((fid.clone(), p.clone()))));
                    later(
                        tx,
                        OAUTH_POLL_INTERVAL,
                        Cmd::Operator(OpCmd::Email {
                            action: EmailAction::OAuthPoll(flow_id),
                            form_id: None,
                        }),
                    );
                    finish_write(
                        store,
                        wake,
                        "START OAuth2 sign-in".into(),
                        Ok(v),
                        Some(Ok("sign-in started — follow the prompt".into())),
                        form_id,
                        on_done,
                    );
                }
                Err(e) => {
                    finish_write(
                        store,
                        wake,
                        "START OAuth2 sign-in".into(),
                        Err(e),
                        None,
                        form_id,
                        on_done,
                    );
                }
            }
        }

        EmailAction::OAuthPoll(flow_id) => {
            // Only the flow still awaited is polled (a cancel or a new start ends this chain).
            let awaited = flow_id.clone();
            let result = require_client(client)
                .and_then(|c| c.my_email_oauth_finish(&awaited, 0.0))
                .map_err(email_err);
            let s = *store;
            let tx2 = tx.clone();
            wake.post(move || {
                let still =
                    s.op.email_oauth
                        .with_untracked(|f| f.as_ref().map(|(id, _)| id.clone()));
                if still.as_deref() != Some(flow_id.as_str()) {
                    return;
                }
                match &result {
                    Ok(v) if v.get("pending").and_then(Value::as_bool) == Some(true) => {
                        later(
                            &tx2,
                            OAUTH_POLL_INTERVAL,
                            Cmd::Operator(OpCmd::Email {
                                action: EmailAction::OAuthPoll(flow_id.clone()),
                                form_id: None,
                            }),
                        );
                    }
                    Ok(v) => {
                        s.op.email_oauth.set(None);
                        let e = MyEmail::from_value(v);
                        s.notice.set(Some(format!(
                            "signed in: {} (connection test passed)",
                            e.address
                        )));
                        s.op.my_email.set(Loadable::Ready(e));
                    }
                    Err(e) => {
                        s.op.email_oauth.set(None);
                        s.notice
                            .set(Some(format!("OAuth2 sign-in failed: {}", e.message)));
                    }
                }
            });
        }

        EmailAction::OAuthCancel(flow_id) => {
            let _ = require_client(client).and_then(|c| c.my_email_oauth_cancel(&flow_id));
            let s = *store;
            wake.post(move || {
                s.op.email_oauth.set(None);
                s.notice.set(Some("OAuth2 sign-in cancelled".into()));
            });
        }

        EmailAction::Notifications(body) => {
            let (write, verify) = with_busy(store, wake, "saving my notifications", || {
                let write = require_client(client)
                    .and_then(|c| c.set_my_notifications(&body))
                    .map_err(email_err);
                let verify = require_client(client).and_then(|c| c.my_notifications());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let got = notifications_body(&MyNotifications::from_value(v).events);
                if got == body.0 {
                    Ok("GET /me/notifications holds the saved choices".to_string())
                } else {
                    Err(format!("GET /me/notifications holds {got}, not {}", body.0))
                }
            });
            let wrote = write.is_ok();
            finish_write(
                store,
                wake,
                "PUT my notifications".into(),
                write,
                verified,
                form_id,
                on_done,
            );
            if let (true, Ok(v)) = (wrote, verify) {
                publish_ready(wake, op.my_notifications, MyNotifications::from_value(&v));
            }
        }

        EmailAction::TestNotification => {
            let write = with_busy(store, wake, "sending a test notification", || {
                require_client(client).and_then(|c| c.test_my_notifications())
            })
            .map_err(email_err)
            .map(email_outcome);
            let verified = write
                .as_ref()
                .ok()
                .filter(|v| v.get("ok").and_then(Value::as_bool) == Some(true))
                .map(|_| Ok("the gateway sent it (state: sent)".to_string()));
            finish_write(
                store,
                wake,
                "SEND test notification".into(),
                write,
                verified,
                form_id,
                on_done,
            );
            if let Ok(v) = require_client(client).and_then(|c| c.my_notifications()) {
                publish_ready(wake, op.my_notifications, MyNotifications::from_value(&v));
            }
        }

        EmailAction::AdminSetEnabled {
            user_id,
            tenant_id,
            enabled,
        } => {
            let (write, verify) = with_busy(store, wake, "switching email for the user", || {
                let write = require_client(client)
                    .and_then(|c| c.set_user_email_enabled(&user_id, &tenant_id, enabled))
                    .map_err(email_err);
                let verify =
                    require_client(client).and_then(|c| c.user_email_status(&user_id, &tenant_id));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let got = v.get("admin_enabled").and_then(Value::as_bool);
                if got == Some(enabled) {
                    Ok(format!(
                        "GET /admin/users/{user_id}/email — {}",
                        v.get("state").and_then(Value::as_str).unwrap_or("?")
                    ))
                } else {
                    Err(format!(
                        "GET /admin/users/{user_id}/email still says admin_enabled={got:?}"
                    ))
                }
            });
            let verb = if enabled { "TURN ON" } else { "TURN OFF" };
            finish_write(
                store,
                wake,
                format!("{verb} email for {user_id}"),
                write,
                verified,
                form_id,
                on_done,
            );
            let _ = tx.send(Cmd::LoadUsers);
        }

        other => {
            let (label, action_text) = match &other {
                EmailAction::Connect(_) => {
                    ("connecting my email (test, then store)", "CONNECT my email")
                }
                EmailAction::Test => ("testing my email", "TEST my email"),
                EmailAction::Disconnect => ("disconnecting my email", "DISCONNECT my email"),
                EmailAction::Policy(_) => ("saving my recipient policy", "PUT my recipient policy"),
                EmailAction::Limits(_) => ("saving my send limits", "PUT my send limits"),
                EmailAction::Enabled(true) => ("turning my email on", "TURN ON my email"),
                EmailAction::Enabled(false) => ("turning my email off", "TURN OFF my email"),
                EmailAction::AgentTools(true) => (
                    "turning my agents' email tools on",
                    "TURN ON agent email tools",
                ),
                EmailAction::AgentTools(false) => (
                    "turning my agents' email tools off",
                    "TURN OFF agent email tools",
                ),
                _ => ("writing my email settings", "WRITE my email"),
            };
            let (write, verify) = with_busy(store, wake, label, || {
                let write = require_client(client)
                    .and_then(|c| match &other {
                        EmailAction::Connect(body) => c.connect_my_email(body),
                        EmailAction::Test => c.test_my_email(),
                        EmailAction::Disconnect => c.disconnect_my_email(),
                        EmailAction::Policy(body) => c.set_my_email_policy(body),
                        EmailAction::Limits(body) => c.set_my_email_limits(body),
                        EmailAction::Enabled(on) => c.set_my_email_enabled(*on),
                        EmailAction::AgentTools(on) => c.set_my_email_agent_tools(*on),
                        _ => unreachable!("handled above"),
                    })
                    .map_err(email_err)
                    .map(email_outcome);
                let verify = require_client(client).and_then(|c| c.my_email());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let got = MyEmail::from_value(v);
                let ok = match &other {
                    EmailAction::Connect(body) => {
                        got.configured
                            && body.get("address").and_then(Value::as_str)
                                == Some(got.address.as_str())
                    }
                    EmailAction::Test => got.last_error.is_none(),
                    EmailAction::Disconnect => !got.configured,
                    EmailAction::Policy(body) => {
                        body.get("mode").and_then(Value::as_str) == Some(got.policy_mode.as_str())
                            && body
                                .get("entries")
                                .and_then(Value::as_array)
                                .map(|a| {
                                    a.iter()
                                        .filter_map(Value::as_str)
                                        .map(str::to_string)
                                        .collect::<Vec<_>>()
                                })
                                .unwrap_or_default()
                                == got.policy_entries
                    }
                    EmailAction::Limits(body) => {
                        let want = |k: &str, have: Option<i64>| {
                            body.get(k)
                                .and_then(Value::as_i64)
                                .is_none_or(|w| Some(w) == have)
                        };
                        want("per_hour", got.per_hour) && want("per_day", got.per_day)
                    }
                    EmailAction::Enabled(on) => got.enabled == *on,
                    EmailAction::AgentTools(on) => got.agent_tools_enabled == *on,
                    _ => true,
                };
                if ok {
                    Ok(format!("GET /me/email — {}", got.state_label()))
                } else {
                    Err(format!("GET /me/email says {}", got.state_label()))
                }
            });
            let wrote = write
                .as_ref()
                .map(|v| v.get("ok").and_then(Value::as_bool) != Some(false))
                .unwrap_or(false);
            finish_write(
                store,
                wake,
                action_text.into(),
                write,
                verified,
                form_id,
                on_done,
            );
            if let Ok(v) = verify {
                let _ = wrote;
                publish_ready(wake, op.my_email, MyEmail::from_value(&v));
            }
            if let Ok(v) = require_client(client).and_then(|c| c.my_notifications()) {
                publish_ready(wake, op.my_notifications, MyNotifications::from_value(&v));
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
    fn email_outcomes_keep_the_cause_and_the_fix() {
        let leg = email_outcome(json!({
            "ok": false,
            "imap": {"ok": true},
            "smtp": {"ok": false, "code": "email_auth_failed", "cause": "rejected", "fix": "use an app password"}
        }));
        assert_eq!(
            leg,
            json!({"ok": false, "error": "rejected Fix: use an app password"})
        );
        let notice = email_outcome(json!({
            "ok": false, "state": "failed",
            "error": {"code": "email_disabled", "cause": "turned off", "fix": "ask an admin"}
        }));
        assert_eq!(notice["error"], json!("turned off Fix: ask an admin"));
        let fine = json!({"ok": true, "imap": {"ok": true}});
        assert_eq!(email_outcome(fine.clone()), fine);
    }

    #[test]
    fn email_bodies_never_print_the_password() {
        let body = Body(
            json!({"address": "me@example.test", "password": "pw-SENTINEL", "client_secret": "cs-SENTINEL"}),
        );
        let shown = format!("{body:?}");
        assert!(!shown.contains("SENTINEL"), "{shown}");
        let cmd = OpCmd::Email {
            action: EmailAction::Connect(body),
            form_id: None,
        };
        assert!(!format!("{cmd:?}").contains("SENTINEL"));
    }

    #[test]
    fn expand_home_only_touches_a_leading_tilde() {
        let home = std::env::var("HOME").expect("HOME in the test env");
        let home = home.trim_end_matches('/');
        assert_eq!(
            expand_home(" ~/flows/x.flow "),
            format!("{home}/flows/x.flow")
        );
        assert_eq!(expand_home("/abs/~/x.flow"), "/abs/~/x.flow");
    }
}
