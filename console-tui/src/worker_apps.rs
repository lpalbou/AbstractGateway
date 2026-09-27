//! Worker side of the Apps screen (`/api/gateway/apps*`).
//!
//! Declared as a child of `worker` (`#[path]` in worker.rs) so it shares
//! the busy bracket and the client guard. Same laws as every write here:
//! write → verify with a follow-up read → post {journal entry + note +
//! refreshed domain}. Jobs are watched like the host-state poll: ONE
//! short GET per job per hop, the wait on a throwaway timer thread, the
//! chain generation-gated on the UI thread (a gateway reset kills it).

use std::sync::mpsc::Sender;

use abstracttui::reactive::WakeHandle;
use serde_json::Value;

use super::{require_client, with_busy, Cmd};
use crate::api::{ApiErrorKind, ApiResult, GatewayClient};
use crate::store::apps::{
    app_error_note, app_key, job_result_note, tui_key, AppJob, AppLog, AppNote, AppOpenLink,
    AppVerb, AppsOverview, NODE_KEY,
};
use crate::store::{JournalEntry, Loadable, Store};

/// Gap between job progress reads (the web console polls every second).
const APP_JOB_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(1);

fn journal(
    store: &Store,
    wake: &WakeHandle,
    action: String,
    outcome: Result<String, String>,
    verified: Option<Result<String, String>>,
) {
    let s = *store;
    wake.post(move || {
        let note = match (&outcome, &verified) {
            (Ok(o), Some(Ok(v))) => format!("{action} — {o} — verified: {v}"),
            (Ok(o), Some(Err(v))) => format!("{action} — {o} — VERIFY FAILED: {v}"),
            (Ok(o), None) => format!("{action} — {o}"),
            (Err(e), _) => format!("{action} — FAILED: {e}"),
        };
        s.push_journal(JournalEntry {
            attention: None,
            when: crate::store::now_hms(),
            action,
            outcome,
            verified,
        });
        s.notice.set(Some(note));
    });
}

/// Publish a fresh overview and adopt every job it reports as active
/// (a job started elsewhere — the web console, the CLI — shows here too),
/// then make sure a poll chain watches them.
fn publish_overview(store: &Store, wake: &WakeHandle, tx: &Sender<Cmd>, overview: AppsOverview) {
    let s = *store;
    let tx = tx.clone();
    wake.post(move || {
        for (key, job) in overview.active_jobs() {
            s.apps.set_job(&key, job);
        }
        s.apps.overview.set(Loadable::Ready(overview));
        ensure_poll(&s, &tx);
    });
}

/// UI thread: start a job poll chain when a tracked job is active and no
/// chain is live.
pub fn ensure_poll(store: &Store, tx: &Sender<Cmd>) {
    if store.apps.polling.get_untracked() {
        return;
    }
    let jobs = store.apps.active_job_ids();
    if jobs.is_empty() {
        return;
    }
    store.apps.polling.set(true);
    let gen = store.apps.poll_gen.get_untracked();
    let _ = tx.send(Cmd::PollAppJobs { gen, jobs });
}

/// The display name for a job key, from the overview on screen.
fn name_for(store: &Store, key: &str) -> String {
    if key == NODE_KEY {
        return "Node.js".into();
    }
    let id = key
        .split_once(':')
        .map(|(_, id)| id)
        .unwrap_or(key)
        .to_string();
    store
        .apps
        .overview
        .with_untracked(|o| {
            o.ready()
                .and_then(|d| d.apps.iter().find(|a| a.id == id).map(|a| a.name.clone()))
        })
        .unwrap_or(id)
}

pub(super) fn load(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    latest: bool,
) {
    // Keep the rows on screen while re-checking (the web's "Checking...");
    // only a first read shows the loading state.
    let s = *store;
    wake.post(move || {
        if s.apps.overview.with_untracked(|o| o.ready().is_none()) {
            s.apps.overview.set(Loadable::Loading);
        }
    });
    let label = if latest {
        "checking the apps (and newer versions)"
    } else {
        "reading the apps"
    };
    let res = with_busy(store, wake, label, || {
        require_client(client)?.apps_overview(latest)
    });
    match res {
        Ok(v) => publish_overview(store, wake, tx, AppsOverview::from_value(&v)),
        Err(e) => {
            let s = *store;
            wake.post(move || s.apps.overview.set(Loadable::Failed(e)));
        }
    }
}

/// The verify read after a write: the overview, as the web re-reads it.
fn verify_overview(client: &Option<GatewayClient>) -> ApiResult<AppsOverview> {
    require_client(client)?
        .apps_overview(true)
        .map(|v| AppsOverview::from_value(&v))
}

fn job_of(v: &Value) -> Option<AppJob> {
    v.get("job").and_then(AppJob::from_value)
}

#[allow(clippy::too_many_arguments)]
pub(super) fn act(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    app_id: &str,
    name: &str,
    verb: AppVerb,
    path: Option<String>,
    start_first: bool,
) {
    let key = match verb {
        AppVerb::OpenTerminal | AppVerb::InstallTerminal | AppVerb::CancelTerminal => {
            tui_key(app_id)
        }
        _ => app_key(app_id),
    };
    {
        let s = *store;
        let k = key.clone();
        wake.post(move || {
            s.apps.set_pending(&k, true);
            s.apps.set_note(&k, None);
        });
    }
    let done = |note: Option<AppNote>| {
        let s = *store;
        let k = key.clone();
        wake.post(move || {
            s.apps.set_pending(&k, false);
            if note.is_some() {
                s.apps.set_note(&k, note);
            }
        });
    };
    match verb {
        AppVerb::Install | AppVerb::Update | AppVerb::InstallTerminal => {
            let (route, what) = match verb {
                AppVerb::Install => ("install", format!("install {name}")),
                AppVerb::Update => ("update", format!("update {name}")),
                _ => ("install-tui", format!("install {name} for the terminal")),
            };
            let action = format!("POST /apps/{app_id}/{route}");
            let res = with_busy(store, wake, &format!("starting: {what}"), || {
                let c = require_client(client)?;
                match verb {
                    AppVerb::Install => c.apps_install(app_id),
                    AppVerb::Update => c.apps_update(app_id),
                    _ => c.apps_install_tui(app_id),
                }
            });
            match res {
                Ok(v) => {
                    let Some(job) = job_of(&v) else {
                        let e = "the gateway started no job".to_string();
                        journal(store, wake, action, Err(e.clone()), None);
                        done(Some(AppNote {
                            tone: Some(crate::store::apps::Tone::Err),
                            text: format!("Could not {what}: {e}"),
                            ..AppNote::default()
                        }));
                        return;
                    };
                    let created = v.get("created").and_then(Value::as_bool).unwrap_or(true);
                    // Verify: the job exists on the gateway, read back by id.
                    let verified = require_client(client)
                        .and_then(|c| c.apps_job(&job.id))
                        .map(|r| {
                            let st = job_of(&r).map(|j| j.state).unwrap_or_default();
                            format!("GET /apps/jobs/{} is {st}", job.id)
                        })
                        .map_err(|e| e.to_string());
                    let outcome = if created {
                        format!("job {} started", job.id)
                    } else {
                        format!("already in progress: showing job {}", job.id)
                    };
                    journal(store, wake, action, Ok(outcome), Some(verified));
                    let s = *store;
                    let tx2 = tx.clone();
                    let k = key.clone();
                    let nm = name.to_string();
                    wake.post(move || {
                        s.apps.set_job(&k, job);
                        if !created {
                            s.apps.set_note(
                                &k,
                                Some(AppNote::info(format!(
                                    "{nm}: already in progress; showing that job."
                                ))),
                            );
                        }
                        ensure_poll(&s, &tx2);
                    });
                    done(None);
                }
                Err(e) => {
                    journal(store, wake, action, Err(e.to_string()), None);
                    done(Some(app_error_note(&what, &e)));
                }
            }
        }
        AppVerb::Start | AppVerb::Stop | AppVerb::DesktopOpen => {
            let (route, what) = match verb {
                AppVerb::Start => ("launch", format!("start {name}")),
                AppVerb::Stop => ("stop", format!("stop {name}")),
                _ => ("launch", format!("open {name}")),
            };
            let action = format!("POST /apps/{app_id}/{route}");
            let (res, verify) = with_busy(store, wake, &what, || {
                let c = require_client(client);
                let res = c.clone().and_then(|c| {
                    if verb == AppVerb::Stop {
                        c.apps_stop(app_id)
                    } else {
                        c.apps_launch(app_id)
                    }
                });
                let verify = res.as_ref().ok().map(|_| verify_overview(client));
                (res, verify)
            });
            match res {
                Ok(v) => {
                    let row_after = verify
                        .as_ref()
                        .and_then(|r| r.as_ref().ok())
                        .and_then(|o| o.apps.iter().find(|a| a.id == app_id).cloned());
                    let verified = match (&verify, &row_after) {
                        (Some(Err(e)), _) => Some(Err(e.to_string())),
                        (_, None) => Some(Err(format!("GET /apps no longer lists {app_id}"))),
                        (_, Some(r)) => {
                            let ok = if verb == AppVerb::Stop {
                                !r.running
                            } else {
                                r.running || r.status == "starting"
                            };
                            let said =
                                format!("GET /apps: {} is {}", r.name, r.status.replace('_', " "));
                            Some(if ok || verb == AppVerb::DesktopOpen {
                                Ok(said)
                            } else {
                                Err(said)
                            })
                        }
                    };
                    let note = match verb {
                        AppVerb::Stop => AppNote::ok(format!("{name} is stopped.")),
                        AppVerb::DesktopOpen => AppNote::ok(
                            v.get("message")
                                .and_then(Value::as_str)
                                .map(str::to_string)
                                .unwrap_or_else(|| format!("{name} is starting.")),
                        ),
                        _ => {
                            let app = v.get("app").cloned().unwrap_or(Value::Null);
                            let running =
                                app.get("running").and_then(Value::as_bool).unwrap_or(false);
                            let status = app
                                .get("status")
                                .and_then(Value::as_str)
                                .unwrap_or("starting");
                            AppNote::ok(format!(
                                "{name} is {}.",
                                if running { "running" } else { status }
                            ))
                        }
                    };
                    journal(store, wake, action, Ok("applied".into()), verified);
                    if let Some(Ok(o)) = verify {
                        publish_overview(store, wake, tx, o);
                    }
                    done(Some(note));
                }
                Err(e) => {
                    journal(store, wake, action, Err(e.to_string()), None);
                    done(Some(app_error_note(&what, &e)));
                }
            }
        }
        AppVerb::Open => {
            // A stopped (or crashed) app is started first — Open is the one
            // action a plain user needs (POST /launch, then POST /open).
            let what = format!("open {name}");
            let base = require_client(client)
                .map(|c| c.base_url().to_string())
                .unwrap_or_default();
            let res: Result<(Value, Option<AppsOverview>), crate::api::ApiError> = with_busy(
                store,
                wake,
                &if start_first {
                    format!("starting {name}")
                } else {
                    format!("opening {name}")
                },
                || {
                    let c = require_client(client)?;
                    if start_first {
                        let st = c.apps_launch(app_id)?;
                        let app = st.get("app").cloned().unwrap_or(Value::Null);
                        if app.get("running").and_then(Value::as_bool) == Some(false) {
                            let status = app
                                .get("status")
                                .and_then(Value::as_str)
                                .unwrap_or("unknown")
                                .replace('_', " ");
                            let mut body = serde_json::json!({"message": format!("{name} did not start ({status}).")});
                            if let Some(le) = app.get("last_error").and_then(Value::as_str) {
                                body["hint"] = Value::String(le.to_string());
                            }
                            return Err(crate::api::ApiError {
                                kind: ApiErrorKind::Protocol,
                                message: format!("{name} did not start ({status})"),
                                body: Some(body),
                                timed_out: false,
                            });
                        }
                    }
                    let r = c.apps_open(app_id, path.as_deref())?;
                    let after = if start_first {
                        verify_overview(client).ok()
                    } else {
                        None
                    };
                    Ok((r, after))
                },
            );
            let action = if start_first {
                format!("POST /apps/{app_id}/launch + /open")
            } else {
                format!("POST /apps/{app_id}/open")
            };
            match res {
                Ok((v, after)) => {
                    match AppOpenLink::from_value(&base, app_id, name, start_first, &v) {
                        Some(link) => {
                            let exp = link
                                .expires_in_s
                                .map(|n| format!(" (works once, {n} s)"))
                                .unwrap_or_default();
                            let verified = after.as_ref().map(|o| {
                                match o.apps.iter().find(|a| a.id == app_id) {
                                    Some(r) if r.running => {
                                        Ok(format!("GET /apps: {} is running", r.name))
                                    }
                                    Some(r) => {
                                        Err(format!("GET /apps: {} is {}", r.name, r.status))
                                    }
                                    None => Err(format!("GET /apps no longer lists {app_id}")),
                                }
                            });
                            journal(
                                store,
                                wake,
                                action,
                                Ok(format!("one-time sign-in link minted{exp}")),
                                verified,
                            );
                            if let Some(o) = after {
                                publish_overview(store, wake, tx, o);
                            }
                            let s = *store;
                            wake.post(move || s.apps.open_link.set(Some(link)));
                            done(Some(AppNote::ok(format!("{name}: sign-in link ready."))));
                        }
                        None => {
                            let e = "the answer carried no open_url".to_string();
                            journal(store, wake, action, Err(e.clone()), None);
                            done(Some(AppNote {
                                tone: Some(crate::store::apps::Tone::Err),
                                text: format!("Could not {what}: {e}"),
                                ..AppNote::default()
                            }));
                        }
                    }
                }
                Err(e) => {
                    journal(store, wake, action, Err(e.to_string()), None);
                    done(Some(app_error_note(&what, &e)));
                    if start_first {
                        let _ = tx.send(Cmd::LoadApps { latest: true });
                    }
                }
            }
        }
        AppVerb::OpenTerminal => {
            let what = format!("open {name} for the terminal");
            let action = format!("POST /apps/{app_id}/launch-tui");
            let res = with_busy(
                store,
                wake,
                &format!("opening {name} in a terminal"),
                || require_client(client)?.apps_launch_tui(app_id),
            );
            match res {
                Ok(v) => {
                    let msg = v
                        .get("message")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                        .unwrap_or_else(|| format!("{name} is opening in a terminal window on the gateway machine's screen."));
                    // No read-back exists for a window on another screen:
                    // the journal says so instead of claiming a verify.
                    journal(
                        store,
                        wake,
                        action,
                        Ok("applied (a terminal window opens on the gateway machine)".into()),
                        None,
                    );
                    done(Some(AppNote::ok(msg)));
                }
                Err(e) => {
                    journal(store, wake, action, Err(e.to_string()), None);
                    done(Some(app_error_note(&what, &e)));
                }
            }
        }
        // Cancels and the log have their own commands (they need a job id
        // or a tail); reaching here is a wiring bug, said out loud.
        AppVerb::Cancel | AppVerb::CancelTerminal | AppVerb::Log => {
            done(Some(AppNote {
                tone: Some(crate::store::apps::Tone::Err),
                text: format!("internal: {verb:?} is not an AppAct verb"),
                ..AppNote::default()
            }));
        }
    }
}

pub(super) fn cancel(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    key: &str,
    name: &str,
    job_id: &str,
) {
    let action = format!("POST /apps/jobs/{job_id}/cancel");
    let (res, verify) = with_busy(store, wake, &format!("cancelling: {name}"), || {
        let res = require_client(client).and_then(|c| c.apps_job_cancel(job_id));
        let verify = res
            .as_ref()
            .ok()
            .map(|_| require_client(client).and_then(|c| c.apps_job(job_id)));
        (res, verify)
    });
    match res {
        Ok(v) => {
            // The job stops at its next checkpoint: the read-back usually
            // still says running; the poll chain reports the end.
            let latest = verify
                .as_ref()
                .and_then(|r| r.as_ref().ok())
                .and_then(job_of)
                .or_else(|| job_of(&v));
            let verified = verify.map(|r| {
                r.map(|j| {
                    format!(
                        "GET /apps/jobs/{job_id} is {} (it stops at its next step)",
                        job_of(&j).map(|x| x.state).unwrap_or_default()
                    )
                })
                .map_err(|e| e.to_string())
            });
            journal(store, wake, action, Ok("cancel requested".into()), verified);
            let s = *store;
            let tx2 = tx.clone();
            let k = key.to_string();
            wake.post(move || {
                if let Some(j) = latest {
                    s.apps.set_job(&k, j);
                }
                ensure_poll(&s, &tx2);
            });
        }
        Err(e) => {
            journal(store, wake, action, Err(e.to_string()), None);
            let s = *store;
            let k = key.to_string();
            let note = app_error_note("cancel", &e);
            wake.post(move || s.apps.set_note(&k, Some(note)));
        }
    }
}

pub(super) fn install_node(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
) {
    let action = "POST /apps/runtime/install".to_string();
    let s = *store;
    wake.post(move || {
        s.apps.set_pending(NODE_KEY, true);
        s.apps.set_note(NODE_KEY, None);
    });
    let res = with_busy(store, wake, "starting: install Node.js", || {
        require_client(client)?.apps_runtime_install()
    });
    let note = match res {
        Ok(v) => match job_of(&v) {
            Some(job) => {
                let verified = require_client(client)
                    .and_then(|c| c.apps_job(&job.id))
                    .map(|r| {
                        format!(
                            "GET /apps/jobs/{} is {}",
                            job.id,
                            job_of(&r).map(|j| j.state).unwrap_or_default()
                        )
                    })
                    .map_err(|e| e.to_string());
                journal(
                    store,
                    wake,
                    action,
                    Ok(format!("job {} started", job.id)),
                    Some(verified),
                );
                let s = *store;
                let tx2 = tx.clone();
                wake.post(move || {
                    s.apps.set_job(NODE_KEY, job);
                    ensure_poll(&s, &tx2);
                });
                None
            }
            None => {
                // Already there: the gateway says so (`job: null` + message).
                let msg = v
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("Node.js is already available.")
                    .to_string();
                journal(store, wake, action, Ok(msg.clone()), None);
                let _ = tx.send(Cmd::LoadApps { latest: false });
                Some(AppNote::ok(msg))
            }
        },
        Err(e) => {
            journal(store, wake, action, Err(e.to_string()), None);
            Some(app_error_note("install Node.js", &e))
        }
    };
    let s = *store;
    wake.post(move || {
        s.apps.set_pending(NODE_KEY, false);
        if note.is_some() {
            s.apps.set_note(NODE_KEY, note);
        }
    });
}

pub(super) fn load_log(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    app_id: &str,
    tail: u32,
) {
    let s = *store;
    wake.post(move || s.apps.log.set(Loadable::Loading));
    let res = with_busy(store, wake, &format!("reading the {app_id} log"), || {
        require_client(client)?.apps_logs(app_id, tail)
    });
    let id = app_id.to_string();
    let s = *store;
    wake.post(move || {
        s.apps.log.set(match res {
            Ok(v) => Loadable::Ready(AppLog::from_value(&id, tail, &v)),
            Err(e) => Loadable::Failed(e),
        })
    });
}

/// One hop of the job poll chain.
pub(super) fn poll(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    gen: u64,
    jobs: Vec<(String, String)>,
) {
    // (key, Ok(job) | Err(gone?, error))
    let mut results: Vec<(String, Result<AppJob, bool>)> = Vec::new();
    for (key, id) in jobs {
        match require_client(client).and_then(|c| c.apps_job(&id)) {
            Ok(v) => match job_of(&v) {
                Some(j) => results.push((key, Ok(j))),
                None => results.push((key, Err(false))),
            },
            // 404: the gateway restarted and forgot the job.
            Err(e) if e.status() == Some(404) => results.push((key, Err(true))),
            Err(_) => results.push((key, Err(false))),
        }
    }
    let s = *store;
    let tx = tx.clone();
    wake.post(move || {
        if s.apps.poll_gen.get_untracked() != gen {
            return; // reset: the chain dies with the world it was reading
        }
        let mut finished = false;
        let mut failed_read = false;
        for (key, r) in results {
            match r {
                Ok(job) => {
                    let was_active = s
                        .apps
                        .jobs
                        .with_untracked(|j| j.iter().any(|(k, x)| *k == key && x.is_active()));
                    if was_active && !job.is_active() {
                        finished = true;
                        let name = name_for(&s, &key);
                        if let Some(note) = job_result_note(&key, &name, &job) {
                            s.apps.set_note(&key, Some(note));
                        }
                        s.push_journal(JournalEntry {
                            attention: None,
                            when: crate::store::now_hms(),
                            action: format!(
                                "apps job {} ({})",
                                job.id,
                                if job.title.is_empty() {
                                    name.clone()
                                } else {
                                    job.title.clone()
                                }
                            ),
                            outcome: if job.state == "succeeded" {
                                Ok(job.state.clone())
                            } else {
                                Err(job.state.clone())
                            },
                            verified: Some(Ok(format!(
                                "GET /apps/jobs/{} is {}",
                                job.id, job.state
                            ))),
                        });
                    }
                    s.apps.set_job(&key, job);
                }
                Err(true) => {
                    finished = true;
                    let k = key.clone();
                    s.apps.jobs.update(move |j| j.retain(|(x, _)| *x != k));
                }
                Err(false) => failed_read = true,
            }
        }
        if finished {
            let _ = tx.send(Cmd::LoadApps { latest: true });
        }
        let still = s.apps.active_job_ids();
        if still.is_empty() || failed_read {
            // A read that failed stops the chain (no retry storm); `r`
            // re-reads the overview, which restarts it for active jobs.
            s.apps.polling.set(false);
            if failed_read {
                s.notice.set(Some(
                    "lost track of an apps job (the read failed) — r checks again".into(),
                ));
            }
            return;
        }
        let tx2 = tx.clone();
        std::thread::Builder::new()
            .name("apps-job-poll-timer".into())
            .spawn(move || {
                std::thread::sleep(APP_JOB_POLL_INTERVAL);
                let _ = tx2.send(Cmd::PollAppJobs { gen, jobs: still });
            })
            .ok();
    });
}
