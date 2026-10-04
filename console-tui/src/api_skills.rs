//! The Skills & MCP page's routes — the SAME ones the web console calls
//! (`console_skills_mcp.py`; `routes/gateway.py` "skills" / "mcp"):
//!
//! - `GET /skills[?include_archived=1]`, `GET /skills/{name}`,
//!   `GET /skills/{name}/export` (zip bytes),
//!   `POST /admin/skills/import` (multipart `file`, or `files` + `paths`),
//!   `PUT /admin/skills/{name}`, `POST /admin/skills/{name}/duplicate`,
//!   `POST /admin/skills/{name}/archive|unarchive`;
//! - `GET /mcp/servers`, `POST /admin/mcp/servers`,
//!   `PUT /admin/mcp/servers/{name}`,
//!   `POST /admin/mcp/servers/{name}/archive|unarchive|agents|test`,
//!   `POST /admin/mcp/test`.

use serde_json::{json, Value};

use super::{err_from_ureq, urlencode, ApiResult, GatewayClient};

/// One uploaded file of a folder import: its path inside the upload
/// (`<skill>/SKILL.md`) and its bytes.
pub struct UploadFile {
    pub path: String,
    pub bytes: Vec<u8>,
}

fn boundary() -> String {
    format!(
        "----abstractgateway-console-{:x}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    )
}

fn safe_name(s: &str) -> String {
    s.chars()
        .map(|c| {
            if matches!(c, '"' | '\r' | '\n') {
                '_'
            } else {
                c
            }
        })
        .collect()
}

/// The web console's folder upload as a multipart body: one `files` part
/// per file (filename = its relative path) and a parallel `paths` text
/// part per file (FormData order: all files, then all paths).
pub fn folder_multipart(boundary: &str, files: &[UploadFile]) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::new();
    for f in files {
        out.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        out.extend_from_slice(
            format!(
                "Content-Disposition: form-data; name=\"files\"; filename=\"{}\"\r\n",
                safe_name(&f.path)
            )
            .as_bytes(),
        );
        out.extend_from_slice(b"Content-Type: application/octet-stream\r\n\r\n");
        out.extend_from_slice(&f.bytes);
        out.extend_from_slice(b"\r\n");
    }
    for f in files {
        out.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        out.extend_from_slice(
            format!(
                "Content-Disposition: form-data; name=\"paths\"\r\n\r\n{}\r\n",
                safe_name(&f.path)
            )
            .as_bytes(),
        );
    }
    out.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    out
}

impl GatewayClient {
    // ---- skills -----------------------------------------------------------

    /// `GET /skills` (any signed-in user); `include_archived` adds the
    /// archived rows (the page's "Show archived").
    pub fn skills(&self, include_archived: bool) -> ApiResult<Value> {
        let q = if include_archived {
            "?include_archived=1"
        } else {
            ""
        };
        self.get(&format!("/skills{q}"), false)
    }

    /// `GET /skills/{name}` — the skill modal's detail.
    pub fn skill_detail(&self, name: &str) -> ApiResult<Value> {
        self.get(&format!("/skills/{}", urlencode(name)), false)
    }

    /// `GET /skills/{name}/export` — `<name>.zip` bytes.
    pub fn skill_export(&self, name: &str) -> ApiResult<Vec<u8>> {
        self.get_bytes(&format!("/skills/{}/export", urlencode(name)))
    }

    /// `POST /admin/skills/import` with ONE `file` part (a `.zip`).
    pub fn skill_import_zip(&self, filename: &str, bytes: &[u8]) -> ApiResult<Value> {
        let b = boundary();
        let body = super::operator::multipart_body(&b, filename, bytes, &[]);
        self.post_multipart("/admin/skills/import", &b, &body)
    }

    /// `POST /admin/skills/import` with a folder (`files` + `paths`).
    pub fn skill_import_folder(&self, files: &[UploadFile]) -> ApiResult<Value> {
        let b = boundary();
        let body = folder_multipart(&b, files);
        self.post_multipart("/admin/skills/import", &b, &body)
    }

    fn post_multipart(&self, path: &str, boundary: &str, body: &[u8]) -> ApiResult<Value> {
        let req = self.with_auth(
            self.slow_agent
                .post(&self.url(path))
                .set("Accept", "application/json"),
        );
        let resp = req
            .set(
                "Content-Type",
                &format!("multipart/form-data; boundary={boundary}"),
            )
            .send_bytes(body)
            .map_err(|e| err_from_ureq(path, e))?;
        Self::read_json(path, resp)
    }

    /// `PUT /admin/skills/{name}` — only the changed keys among
    /// `skill_md`, `description`, `version`, `license`.
    pub fn skill_update(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send(
            "PUT",
            &format!("/admin/skills/{}", urlencode(name)),
            body,
            false,
        )
    }

    /// `POST /admin/skills/{name}/duplicate` with `{name: <copy>}`.
    pub fn skill_duplicate(&self, name: &str, copy: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/admin/skills/{}/duplicate", urlencode(name)),
            &json!({ "name": copy }),
            false,
        )
    }

    /// `POST /admin/skills/{name}/archive` (`archive`) or `/unarchive`.
    pub fn skill_set_archived(&self, name: &str, archive: bool) -> ApiResult<Value> {
        let verb = if archive { "archive" } else { "unarchive" };
        self.send(
            "POST",
            &format!("/admin/skills/{}/{verb}", urlencode(name)),
            &json!({}),
            false,
        )
    }

    // ---- MCP servers --------------------------------------------------------

    /// `GET /mcp/servers` (admin; a non-admin gets 403 `admin_required`).
    pub fn mcp_servers(&self) -> ApiResult<Value> {
        self.get("/mcp/servers", false)
    }

    /// `POST /admin/mcp/servers` (add) or `PUT /admin/mcp/servers/{name}`
    /// (edit) — the web modal's body (`mcpModalBody`).
    pub fn mcp_save(&self, editing: Option<&str>, body: &Value) -> ApiResult<Value> {
        match editing {
            None => self.send("POST", "/admin/mcp/servers", body, false),
            Some(name) => self.send(
                "PUT",
                &format!("/admin/mcp/servers/{}", urlencode(name)),
                body,
                false,
            ),
        }
    }

    /// `POST /admin/mcp/servers/{name}/archive` or `/unarchive`.
    pub fn mcp_set_archived(&self, name: &str, archive: bool) -> ApiResult<Value> {
        let verb = if archive { "archive" } else { "unarchive" };
        self.send(
            "POST",
            &format!("/admin/mcp/servers/{}/{verb}", urlencode(name)),
            &json!({}),
            false,
        )
    }

    /// `POST /admin/mcp/servers/{name}/agents` with `{enabled}`.
    pub fn mcp_set_agents(&self, name: &str, enabled: bool) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/admin/mcp/servers/{}/agents", urlencode(name)),
            &json!({ "enabled": enabled }),
            false,
        )
    }

    /// `POST /admin/mcp/servers/{name}/test` — the real handshake, stored
    /// as the row's `last_test` (up to 10 s on the gateway).
    pub fn mcp_test_saved(&self, name: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/admin/mcp/servers/{}/test", urlencode(name)),
            &json!({}),
            true,
        )
    }

    /// `POST /admin/mcp/test` — test the form's values (stores nothing).
    pub fn mcp_test_unsaved(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/admin/mcp/test", body, true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folder_upload_sends_files_then_their_paths() {
        let body = folder_multipart(
            "B",
            &[
                UploadFile {
                    path: "notes/SKILL.md".into(),
                    bytes: b"---\nname: notes\n---\n".to_vec(),
                },
                UploadFile {
                    path: "notes/ref/a.md".into(),
                    bytes: b"a".to_vec(),
                },
            ],
        );
        let text = String::from_utf8_lossy(&body);
        let f1 = text
            .find("name=\"files\"; filename=\"notes/SKILL.md\"")
            .unwrap();
        let f2 = text
            .find("name=\"files\"; filename=\"notes/ref/a.md\"")
            .unwrap();
        let p1 = text
            .find("name=\"paths\"\r\n\r\nnotes/SKILL.md\r\n")
            .unwrap();
        assert!(f1 < f2 && f2 < p1, "{text}");
        assert!(text.ends_with("--B--\r\n"));
    }
}
