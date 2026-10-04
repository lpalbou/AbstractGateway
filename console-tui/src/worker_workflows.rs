//! Worker half of the Workflows page (web console parity, R7.2): a child
//! module of `worker` (one `#[path]` line, one `Cmd::Workflows` variant,
//! one dispatch arm). Writes follow the worker's law — write → verify via
//! GET → journal — and post the web page's own sentences.

use std::path::PathBuf;

use abstracttui::reactive::WakeHandle;
use serde_json::{json, Value};

use super::skills::{expand_home, refusal_text};
use super::{finish_write, load, require_client, with_busy};
use crate::api::{ApiError, ApiErrorKind, GatewayClient};
use crate::store::skills::Tone;
use crate::store::workflows_page::{defaults_from_payload, workflows_from_payload};
use crate::store::{Loadable, Store};

/// The list's filters, carried by every command that re-reads it.
#[derive(Clone, Copy, Debug, Default)]
pub struct ListArgs {
    pub drafts: bool,
    pub archived: bool,
    /// Read "Default workflow per app" too (admins only: the web hides it
    /// and never fetches it for anyone else).
    pub defaults: bool,
}

#[derive(Clone, Debug)]
pub enum WfCmd {
    /// The list (`GET /bundles…`) and the defaults (`GET /admin/runtime-config`).
    Load(ListArgs),
    SetAvailability {
        bundle_id: String,
        name: String,
        available: bool,
        list: ListArgs,
    },
    /// `version` empty = every version.
    Archive {
        bundle_id: String,
        version: String,
        label: String,
        list: ListArgs,
    },
    Unarchive {
        bundle_id: String,
        version: String,
        label: String,
        list: ListArgs,
    },
    ArchiveBroken {
        bundle_id: String,
        versions: Vec<String>,
        list: ListArgs,
    },
    /// `.flow` files on THIS machine, each uploaded like the web's picker.
    Import {
        paths: Vec<String>,
        list: ListArgs,
    },
    Export {
        bundle_id: String,
        version: String,
        dir: PathBuf,
    },
    SaveDefault {
        iface: String,
        value: String,
    },
    SetStreaming {
        on: bool,
    },
    /// `PATCH /bundles/{id} {description}` (round 8, owner-editable).
    SetDescription {
        bundle_id: String,
        label: String,
        description: String,
        list: ListArgs,
    },
}

fn protocol(m: String) -> ApiError {
    ApiError::new(ApiErrorKind::Protocol, m)
}

fn post_msg(wake: &WakeHandle, store: &Store, text: String, tone: Tone) {
    let sig = store.wf.msg;
    wake.post(move || sig.set(Some((text, tone))));
}

fn post_def_msg(wake: &WakeHandle, store: &Store, text: String, tone: Tone) {
    let sig = store.wf.defaults_msg;
    wake.post(move || sig.set(Some((text, tone))));
}

fn reload(c: &GatewayClient, store: &Store, wake: &WakeHandle, list: ListArgs) {
    let sig = store.wf.data;
    let res = c
        .bundles_page(list.drafts, list.archived)
        .and_then(|v| workflows_from_payload(&v).map_err(protocol));
    wake.post(move || {
        sig.set(match res {
            Ok(d) => Loadable::Ready(d),
            Err(e) => Loadable::Failed(e),
        })
    });
}

fn reload_defaults(c: &GatewayClient, store: &Store, wake: &WakeHandle) {
    let sig = store.wf.defaults;
    let res = c
        .runtime_config_raw()
        .and_then(|v| defaults_from_payload(&v).map_err(protocol));
    wake.post(move || {
        sig.set(match res {
            Ok(d) => Loadable::Ready(d),
            Err(e) => Loadable::Failed(e),
        })
    });
}

pub(super) fn handle(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    cmd: WfCmd,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    let wf = store.wf;
    match cmd {
        WfCmd::Load(list) => {
            load(store, wake, "reading the workflows", wf.data, || {
                let v = require_client(client)?.bundles_page(list.drafts, list.archived)?;
                workflows_from_payload(&v).map_err(protocol)
            });
            if !list.defaults {
                return;
            }
            load(
                store,
                wake,
                "reading the default workflows",
                wf.defaults,
                || {
                    let v = require_client(client)?.runtime_config_raw()?;
                    defaults_from_payload(&v).map_err(protocol)
                },
            );
        }
        WfCmd::SetAvailability {
            bundle_id,
            name,
            available,
            list,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "saving Available to users", || {
                c.set_workflow_availability(&bundle_id, available)
            });
            let verified = match &res {
                Ok(v) => {
                    let text = if available {
                        format!("{name} is available to users again. Automations paused earlier stay paused until their owners resume them.")
                    } else {
                        let n = v
                            .get("paused_automations")
                            .and_then(Value::as_array)
                            .map(Vec::len)
                            .unwrap_or(0);
                        let tail = match n {
                            0 => String::new(),
                            1 => "; 1 of their automation was paused".into(),
                            n => format!("; {n} of their automations were paused"),
                        };
                        format!("{name} is hidden from users{tail}.")
                    };
                    post_msg(wake, store, text, Tone::Ok);
                    reload(&c, store, wake, list);
                    Some(Ok(format!("{bundle_id}: available to users = {available}")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        store,
                        format!("Not changed: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("PUT /admin/workflows/{bundle_id}/availability"),
                res,
                verified,
                None,
                on_done,
            );
        }
        WfCmd::SetDescription {
            bundle_id,
            label,
            description,
            list,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "saving the description", || {
                c.set_bundle_description(&bundle_id, &description)
            });
            let verified = match &res {
                Ok(_) => {
                    post_msg(
                        wake,
                        store,
                        format!("Saved the description of {label}."),
                        Tone::Ok,
                    );
                    let editing = store.wf.editing;
                    wake.post(move || editing.set(None));
                    reload(&c, store, wake, list);
                    Some(Ok(format!("{label} description saved")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        store,
                        format!("Not saved: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("PATCH workflow description {bundle_id}"),
                res,
                verified,
                None,
                on_done,
            );
        }
        WfCmd::Archive {
            bundle_id,
            version,
            label,
            list,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "archiving the workflow", || {
                c.archive_bundle(&bundle_id, &version)
            });
            let verified = match &res {
                Ok(_) => {
                    post_msg(
                        wake,
                        store,
                        format!(
                            "Archived {label}. Turn on “Show archived” to see it or unarchive it."
                        ),
                        Tone::Ok,
                    );
                    reload(&c, store, wake, list);
                    Some(Ok(format!("{label} archived")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        store,
                        format!("Not archived: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("POST /bundles/{bundle_id}/archive"),
                res,
                verified,
                None,
                on_done,
            );
        }
        WfCmd::Unarchive {
            bundle_id,
            version,
            label,
            list,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "unarchiving the workflow", || {
                c.unarchive_bundle(&bundle_id, &version)
            });
            let verified = match &res {
                Ok(_) => {
                    post_msg(
                        wake,
                        store,
                        format!("{label} is back in the lists and can start runs again."),
                        Tone::Ok,
                    );
                    reload(&c, store, wake, list);
                    Some(Ok(format!("{label} unarchived")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        store,
                        format!("Not unarchived: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("POST /bundles/{bundle_id}/unarchive"),
                res,
                verified,
                None,
                on_done,
            );
        }
        WfCmd::ArchiveBroken {
            bundle_id,
            versions,
            list,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let mut done = 0usize;
            let mut failed: Vec<String> = Vec::new();
            with_busy(store, wake, "archiving broken versions", || {
                for v in &versions {
                    match c.archive_bundle(&bundle_id, v) {
                        Ok(_) => done += 1,
                        Err(e) => failed.push(format!("{v}: {}", refusal_text(&e))),
                    }
                }
            });
            let text = if failed.is_empty() {
                format!(
                    "Archived {done} broken {} of {bundle_id}. The files stay on the gateway.",
                    if done == 1 { "version" } else { "versions" }
                )
            } else {
                format!("Archived {done}; not archived — {}.", failed.join("; "))
            };
            post_msg(
                wake,
                store,
                text,
                if failed.is_empty() {
                    Tone::Ok
                } else {
                    Tone::Error
                },
            );
            reload(&c, store, wake, list);
        }
        WfCmd::Import { paths, list } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let paths: Vec<String> = paths.into_iter().filter(|p| !p.trim().is_empty()).collect();
            if paths.is_empty() {
                post_msg(wake, store, "Nothing to import.".into(), Tone::Plain);
                return;
            }
            let mut installed: Vec<String> = Vec::new();
            let mut not_running: Vec<String> = Vec::new();
            let mut failed: Vec<String> = Vec::new();
            for p in &paths {
                let path = expand_home(p);
                let fname = path
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_else(|| p.clone());
                post_msg(wake, store, format!("Importing {fname}…"), Tone::Plain);
                let res = with_busy(store, wake, "importing a workflow", || {
                    let bytes = std::fs::read(&path)
                        .map_err(|e| protocol(format!("{}: {e}", path.display())))?;
                    c.upload_bundle(&fname, &bytes, false, true)
                });
                match &res {
                    Ok(v) => {
                        let r = v
                            .get("bundle_ref")
                            .and_then(Value::as_str)
                            .unwrap_or(&fname)
                            .to_string();
                        if v.get("loaded").and_then(Value::as_bool) == Some(false) {
                            let why = v
                                .get("skipped")
                                .and_then(|s| s.get("reason"))
                                .and_then(Value::as_str)
                                .unwrap_or("not served")
                                .to_string();
                            not_running.push(format!("{r}: {why}"));
                        } else {
                            installed.push(r);
                        }
                    }
                    Err(e) => failed.push(format!("{fname}: {}", refusal_text(e))),
                }
                finish_write(
                    store,
                    wake,
                    "POST /bundles/upload".into(),
                    res,
                    None,
                    None,
                    on_done,
                );
            }
            let mut parts: Vec<String> = Vec::new();
            if !installed.is_empty() {
                parts.push(format!("Installed {}.", installed.join(", ")));
            }
            if !not_running.is_empty() {
                parts.push(format!(
                    "Installed but NOT running — {}.",
                    not_running.join("; ")
                ));
            }
            if !failed.is_empty() {
                parts.push(format!("Failed — {}.", failed.join("; ")));
            }
            let tone = if !failed.is_empty() {
                Tone::Error
            } else if !not_running.is_empty() {
                Tone::Plain
            } else {
                Tone::Ok
            };
            post_msg(wake, store, parts.join(" "), tone);
            reload(&c, store, wake, list);
        }
        WfCmd::Export {
            bundle_id,
            version,
            dir,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "exporting the workflow", || {
                c.download_bundle(&bundle_id, &version)
            });
            match res {
                Ok(bytes) => {
                    let path = dir.join(format!("{bundle_id}@{version}.flow"));
                    if path.exists() {
                        post_msg(
                            wake,
                            store,
                            format!("Not exported: {} already exists (an existing file is never overwritten).", path.display()),
                            Tone::Error,
                        );
                        return;
                    }
                    match std::fs::create_dir_all(&dir).and_then(|_| std::fs::write(&path, &bytes))
                    {
                        Ok(()) => post_msg(
                            wake,
                            store,
                            format!(
                                "Exported {bundle_id}@{version} to {} ({} bytes).",
                                path.display(),
                                bytes.len()
                            ),
                            Tone::Ok,
                        ),
                        Err(e) => post_msg(
                            wake,
                            store,
                            format!("Not exported: {}: {e}", path.display()),
                            Tone::Error,
                        ),
                    }
                }
                Err(e) => post_msg(
                    wake,
                    store,
                    format!("Not exported: {}", refusal_text(&e)),
                    Tone::Error,
                ),
            }
        }
        WfCmd::SaveDefault { iface, value } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let body = json!({"agents": {"default_workflow": {iface.clone(): value}}});
            let res = with_busy(store, wake, "saving the default workflow", || {
                c.post_runtime_config(&body)
            });
            let verified = match &res {
                Ok(_) => {
                    post_def_msg(wake, store, "Saved".into(), Tone::Ok);
                    reload_defaults(&c, store, wake);
                    Some(Ok(format!("agents.default_workflow.{iface} saved")))
                }
                Err(e) => {
                    let m = refusal_text(e);
                    post_def_msg(
                        wake,
                        store,
                        if m.is_empty() { "Not saved.".into() } else { m },
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                "POST /admin/runtime-config".into(),
                res,
                verified,
                None,
                on_done,
            );
        }
        WfCmd::SetStreaming { on } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let body = json!({"agents": {"streaming_default": on}});
            let res = with_busy(store, wake, "saving Streamed replies", || {
                c.post_runtime_config(&body)
            });
            let verified = match &res {
                Ok(_) => {
                    let text = if on {
                        "Saved: New interactive runs stream their replies unless the client says otherwise."
                    } else {
                        "Saved: New interactive runs send their replies whole unless the client asks to stream."
                    };
                    post_def_msg(wake, store, text.into(), Tone::Ok);
                    reload_defaults(&c, store, wake);
                    Some(Ok(format!("agents.streaming_default = {on}")))
                }
                Err(e) => {
                    post_def_msg(wake, store, refusal_text(e), Tone::Error);
                    None
                }
            };
            finish_write(
                store,
                wake,
                "POST /admin/runtime-config".into(),
                res,
                verified,
                None,
                on_done,
            );
        }
    }
}
