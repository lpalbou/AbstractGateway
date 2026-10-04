//! Worker half of the Skills & MCP page (web console parity, R7.2). A
//! child module of `worker` (one `#[path]` line, one `Cmd::Skills`
//! variant, one dispatch arm) sharing its write law — write → verify via
//! GET → journal — and posting the web page's own sentences to the page's
//! message lines.

use std::path::{Path, PathBuf};

use abstracttui::reactive::WakeHandle;
use serde_json::Value;

use super::{finish_write, load, require_client, with_busy, Body};
use crate::api::skills::UploadFile;
use crate::api::{ApiError, GatewayClient};
use crate::store::skills::{
    mcp_from_payload, mcp_row_from, skill_detail_from_payload, skills_from_payload,
    test_result_from, Tone,
};
use crate::store::{Loadable, Store};

/// Skills & MCP commands (one `Cmd::Skills` variant carries them).
#[derive(Clone, Debug)]
pub enum SkCmd {
    LoadSkills {
        include_archived: bool,
    },
    LoadMcp,
    OpenSkill {
        name: String,
    },
    SaveSkill {
        name: String,
        body: Body,
        include_archived: bool,
    },
    DuplicateSkill {
        name: String,
        include_archived: bool,
    },
    SetSkillArchived {
        name: String,
        archive: bool,
        include_archived: bool,
        /// Reopen the skill overlay afterwards (its Unarchive button).
        reopen: bool,
    },
    ExportSkill {
        name: String,
        dir: PathBuf,
    },
    ImportSkill {
        path: String,
        include_archived: bool,
    },
    SaveMcp {
        editing: Option<String>,
        body: Body,
    },
    TestMcpForm {
        body: Body,
    },
    TestMcp {
        name: String,
    },
    SetMcpArchived {
        name: String,
        archive: bool,
    },
    SetMcpAgents {
        name: String,
        enabled: bool,
    },
}

/// The gateway's sentence for a refusal: `detail.message` (the web's
/// `api()` shows exactly that), else the transport text.
pub fn refusal_text(e: &ApiError) -> String {
    e.body
        .as_ref()
        .and_then(|b| b.get("message"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| e.message.clone())
}

fn post_msg(
    wake: &WakeHandle,
    sig: abstracttui::reactive::Signal<Option<(String, Tone)>>,
    text: String,
    tone: Tone,
) {
    wake.post(move || sig.set(Some((text, tone))));
}

fn reload_skills(client: &GatewayClient, store: &Store, wake: &WakeHandle, include_archived: bool) {
    let sig = store.skills.skills;
    let res = client.skills(include_archived).and_then(|v| {
        skills_from_payload(&v).map_err(|m| ApiError::new(crate::api::ApiErrorKind::Protocol, m))
    });
    wake.post(move || {
        sig.set(match res {
            Ok(d) => Loadable::Ready(d),
            Err(e) => Loadable::Failed(e),
        })
    });
}

fn reload_mcp(client: &GatewayClient, store: &Store, wake: &WakeHandle) {
    let sig = store.skills.mcp;
    let res = client.mcp_servers().and_then(|v| {
        mcp_from_payload(&v).map_err(|m| ApiError::new(crate::api::ApiErrorKind::Protocol, m))
    });
    wake.post(move || {
        sig.set(match res {
            Ok(d) => Loadable::Ready(d),
            Err(e) => Loadable::Failed(e),
        })
    });
}

/// The files of a skill folder for the web's folder upload: every
/// regular file under `dir`, its path relative to `dir`'s PARENT (so the
/// first segment is the folder's name, like `webkitRelativePath`).
pub fn folder_files(dir: &Path) -> std::io::Result<Vec<UploadFile>> {
    let base = dir.parent().unwrap_or(Path::new(""));
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let mut entries: Vec<_> = std::fs::read_dir(&d)?.filter_map(Result::ok).collect();
        entries.sort_by_key(|e| e.file_name());
        for e in entries {
            let p = e.path();
            let ft = e.file_type()?;
            if ft.is_dir() {
                stack.push(p);
            } else if ft.is_file() {
                let rel = p
                    .strip_prefix(base)
                    .unwrap_or(&p)
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push(UploadFile {
                    path: rel,
                    bytes: std::fs::read(&p)?,
                });
            }
        }
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

/// Expand a leading `~/` (the import/export paths are typed by hand).
pub fn expand_home(p: &str) -> PathBuf {
    let p = p.trim();
    if let Some(rest) = p.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(p)
}

pub(super) fn handle(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    cmd: SkCmd,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    let sk = store.skills;
    match cmd {
        SkCmd::LoadSkills { include_archived } => {
            load(store, wake, "reading the skills", sk.skills, || {
                let v = require_client(client)?.skills(include_archived)?;
                skills_from_payload(&v)
                    .map_err(|m| ApiError::new(crate::api::ApiErrorKind::Protocol, m))
            })
        }
        SkCmd::LoadMcp => load(store, wake, "reading the MCP servers", sk.mcp, || {
            let v = require_client(client)?.mcp_servers()?;
            mcp_from_payload(&v).map_err(|m| ApiError::new(crate::api::ApiErrorKind::Protocol, m))
        }),
        SkCmd::OpenSkill { name } => {
            wake.post(move || sk.detail_msg.set(None));
            load(store, wake, "reading the skill", sk.detail, || {
                let v = require_client(client)?.skill_detail(&name)?;
                skill_detail_from_payload(&v)
                    .map_err(|m| ApiError::new(crate::api::ApiErrorKind::Protocol, m))
            })
        }
        SkCmd::SaveSkill {
            name,
            body,
            include_archived,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            post_msg(wake, sk.detail_msg, "Saving...".into(), Tone::Plain);
            let res = with_busy(store, wake, "saving the skill", || {
                c.skill_update(&name, &body.0)
            });
            let verified = match &res {
                Ok(v) => {
                    if let Ok(d) = skill_detail_from_payload(v) {
                        wake.post(move || sk.detail.set(Loadable::Ready(d)));
                    }
                    post_msg(
                        wake,
                        sk.detail_msg,
                        "Saved. New runs read this version.".into(),
                        Tone::Ok,
                    );
                    reload_skills(&c, store, wake, include_archived);
                    Some(Ok(format!("skill {name} saved")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        sk.detail_msg,
                        format!("Not saved: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("PUT /admin/skills/{name}"),
                res,
                verified,
                None,
                on_done,
            );
        }
        SkCmd::DuplicateSkill {
            name,
            include_archived,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            post_msg(wake, sk.detail_msg, "Duplicating...".into(), Tone::Plain);
            let copy = format!("{name}-copy");
            let res = with_busy(store, wake, "duplicating the skill", || {
                c.skill_duplicate(&name, &copy)
            });
            let verified = match &res {
                Ok(v) => {
                    let copy_name = v
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or(&copy)
                        .to_string();
                    if let Ok(d) = skill_detail_from_payload(v) {
                        wake.post(move || sk.detail.set(Loadable::Ready(d)));
                    }
                    post_msg(
                        wake,
                        sk.detail_msg,
                        format!("Copied to {copy_name}: this copy is yours to edit (Unverified until reviewed)."),
                        Tone::Ok,
                    );
                    reload_skills(&c, store, wake, include_archived);
                    Some(Ok(format!("skill {copy_name} created")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        sk.detail_msg,
                        format!("Not duplicated: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("POST /admin/skills/{name}/duplicate"),
                res,
                verified,
                None,
                on_done,
            );
        }
        SkCmd::SetSkillArchived {
            name,
            archive,
            include_archived,
            reopen,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let verb = if archive { "archive" } else { "unarchive" };
            let res = with_busy(store, wake, "archiving the skill", || {
                c.skill_set_archived(&name, archive)
            });
            let verified = match &res {
                Ok(_) => {
                    let text = if archive {
                        format!("Archived {name}: runs no longer see it. Turn on Show archived to find it again.")
                    } else {
                        format!("Unarchived {name}: it is back on the shelf.")
                    };
                    post_msg(wake, sk.skills_msg, text, Tone::Ok);
                    reload_skills(&c, store, wake, include_archived);
                    if reopen {
                        if let Ok(v) = c.skill_detail(&name) {
                            if let Ok(d) = skill_detail_from_payload(&v) {
                                wake.post(move || sk.detail.set(Loadable::Ready(d)));
                            }
                        }
                    }
                    Some(Ok(format!("skill {name} {verb}d")))
                }
                Err(e) => {
                    let lead = if archive {
                        "Not archived"
                    } else {
                        "Not unarchived"
                    };
                    post_msg(
                        wake,
                        sk.skills_msg,
                        format!("{lead}: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("POST /admin/skills/{name}/{verb}"),
                res,
                verified,
                None,
                on_done,
            );
        }
        SkCmd::ExportSkill { name, dir } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "exporting the skill", || c.skill_export(&name));
            match res {
                Ok(bytes) => {
                    let path = dir.join(format!("{name}.zip"));
                    let written =
                        std::fs::create_dir_all(&dir).and_then(|_| std::fs::write(&path, &bytes));
                    match written {
                        Ok(()) => post_msg(
                            wake,
                            sk.skills_msg,
                            format!(
                                "Exported {name} to {} ({} bytes).",
                                path.display(),
                                bytes.len()
                            ),
                            Tone::Ok,
                        ),
                        Err(e) => post_msg(
                            wake,
                            sk.skills_msg,
                            format!("Not exported: {}: {e}", path.display()),
                            Tone::Error,
                        ),
                    }
                }
                Err(e) => post_msg(
                    wake,
                    sk.skills_msg,
                    format!("Not exported: {}", refusal_text(&e)),
                    Tone::Error,
                ),
            }
        }
        SkCmd::ImportSkill {
            path,
            include_archived,
        } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let p = expand_home(&path);
            let label = p
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_else(|| path.clone());
            if path.trim().is_empty() {
                post_msg(
                    wake,
                    sk.skills_msg,
                    "Not imported: Choose a .zip file or a skill folder to import.".into(),
                    Tone::Error,
                );
                return;
            }
            post_msg(
                wake,
                sk.skills_msg,
                format!("Importing {label}..."),
                Tone::Plain,
            );
            let res = with_busy(store, wake, "importing the skill", || {
                if p.is_dir() {
                    let files = folder_files(&p).map_err(|e| {
                        ApiError::new(
                            crate::api::ApiErrorKind::Protocol,
                            format!("{}: {e}", p.display()),
                        )
                    })?;
                    c.skill_import_folder(&files)
                } else {
                    let bytes = std::fs::read(&p).map_err(|e| {
                        ApiError::new(
                            crate::api::ApiErrorKind::Protocol,
                            format!("{}: {e}", p.display()),
                        )
                    })?;
                    c.skill_import_zip(&label, &bytes)
                }
            });
            let verified = match &res {
                Ok(v) => {
                    let name = v
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or(&label)
                        .to_string();
                    post_msg(
                        wake,
                        sk.skills_msg,
                        format!("Imported {name}. It is Unverified until a reviewer adds it to the shelf's validations; open it to read or edit it."),
                        Tone::Ok,
                    );
                    reload_skills(&c, store, wake, include_archived);
                    Some(Ok(format!("skill {name} imported")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        sk.skills_msg,
                        format!("Not imported: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                "POST /admin/skills/import".into(),
                res,
                verified,
                None,
                on_done,
            );
        }
        SkCmd::SaveMcp { editing, body } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            wake.post(move || sk.form_note.set(Some("Saving...".into())));
            let res = with_busy(store, wake, "saving the MCP server", || {
                c.mcp_save(editing.as_deref(), &body.0)
            });
            let verified = match &res {
                Ok(v) => {
                    let name = mcp_row_from(v).name;
                    let name = if name.is_empty() {
                        body.0
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string()
                    } else {
                        name
                    };
                    wake.post(move || {
                        sk.form_note.set(None);
                        sk.form_saved.update(|n| *n += 1);
                    });
                    post_msg(
                        wake,
                        sk.mcp_msg,
                        format!(
                            "Saved {name}. Test it to record whether the gateway can reach it."
                        ),
                        Tone::Ok,
                    );
                    reload_mcp(&c, store, wake);
                    Some(Ok(format!("MCP server {name} saved")))
                }
                Err(e) => {
                    let t = format!("Not saved: {}", refusal_text(e));
                    wake.post(move || sk.form_note.set(Some(t)));
                    None
                }
            };
            let action = match &editing {
                None => "POST /admin/mcp/servers".to_string(),
                Some(n) => format!("PUT /admin/mcp/servers/{n}"),
            };
            finish_write(store, wake, action, res, verified, None, on_done);
        }
        SkCmd::TestMcpForm { body } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            wake.post(move || sk.form_test.set(Some(None)));
            let res = with_busy(store, wake, "testing the MCP server", || {
                c.mcp_test_unsaved(&body.0)
            });
            let out = match res {
                Ok(v) => {
                    let t = test_result_from(&v);
                    if t.ok {
                        Ok(t)
                    } else {
                        Err(t.message)
                    }
                }
                Err(e) => Err(refusal_text(&e)),
            };
            wake.post(move || sk.form_test.set(Some(Some(out))));
        }
        SkCmd::TestMcp { name } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            post_msg(
                wake,
                sk.mcp_msg,
                format!("Testing {name} (up to 10 seconds)..."),
                Tone::Plain,
            );
            let res = with_busy(store, wake, "testing the MCP server", || {
                c.mcp_test_saved(&name)
            });
            match &res {
                Ok(v) => {
                    let t = test_result_from(v);
                    post_msg(
                        wake,
                        sk.mcp_msg,
                        format!("{name}: {}", t.message),
                        if t.ok { Tone::Ok } else { Tone::Error },
                    );
                }
                Err(e) => post_msg(
                    wake,
                    sk.mcp_msg,
                    format!("{name}: {}", refusal_text(e)),
                    Tone::Error,
                ),
            }
            reload_mcp(&c, store, wake);
            let verified = res
                .as_ref()
                .ok()
                .map(|_| Ok(format!("MCP server {name} tested")));
            finish_write(
                store,
                wake,
                format!("POST /admin/mcp/servers/{name}/test"),
                res,
                verified,
                None,
                on_done,
            );
        }
        SkCmd::SetMcpArchived { name, archive } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let verb = if archive { "archive" } else { "unarchive" };
            let res = with_busy(store, wake, "archiving the MCP server", || {
                c.mcp_set_archived(&name, archive)
            });
            let verified = match &res {
                Ok(_) => {
                    let text = if archive {
                        format!("Archived {name}. Turn on Show archived to find it again.")
                    } else {
                        format!("Unarchived {name}.")
                    };
                    post_msg(wake, sk.mcp_msg, text, Tone::Ok);
                    reload_mcp(&c, store, wake);
                    Some(Ok(format!("MCP server {name} {verb}d")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        sk.mcp_msg,
                        format!("{name}: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("POST /admin/mcp/servers/{name}/{verb}"),
                res,
                verified,
                None,
                on_done,
            );
        }
        SkCmd::SetMcpAgents { name, enabled } => {
            let Ok(c) = require_client(client) else {
                return;
            };
            let res = with_busy(store, wake, "saving Enabled for agents", || {
                c.mcp_set_agents(&name, enabled)
            });
            let verified = match &res {
                Ok(v) => {
                    let status = v
                        .get("agents_status")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                        .unwrap_or_else(|| {
                            if enabled {
                                "Offered to agents".into()
                            } else {
                                "Not offered to agents".into()
                            }
                        });
                    post_msg(wake, sk.mcp_msg, format!("{name}: {status}"), Tone::Ok);
                    reload_mcp(&c, store, wake);
                    Some(Ok(format!("MCP server {name}: {status}")))
                }
                Err(e) => {
                    post_msg(
                        wake,
                        sk.mcp_msg,
                        format!("{name}: {}", refusal_text(e)),
                        Tone::Error,
                    );
                    None
                }
            };
            finish_write(
                store,
                wake,
                format!("POST /admin/mcp/servers/{name}/agents"),
                res,
                verified,
                None,
                on_done,
            );
        }
    }
}
