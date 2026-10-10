//! The Skills & MCP page (web console `console_skills_mcp.py`): state and
//! pure parsing/wording. Every sentence below is the web page's own,
//! verbatim, so the two consoles read the same.

use abstracttui::reactive::{Scope, Signal};
use serde_json::Value;

use super::Loadable;

/// The page's title and subtitle (`TAB_TITLES`).
pub const TITLE: &str = "Skills & MCP";
pub const SUBTITLE: &str = "Skills agents can load, and MCP tool servers";
pub const SKILLS_PURPOSE: &str = "Instructions agents load when a task needs them: curated ones ship with the gateway, imported ones are yours to edit.";
pub const MCP_PURPOSE: &str =
    "Tool servers over the Model Context Protocol: Test runs the real handshake and lists their tools.";
pub const SKILLS_LOADING: &str = "Reading the skills...";
pub const SKILLS_EMPTY: &str = "No skills on this shelf yet.";
pub const SKILLS_NO_MATCH: &str = "No skill matches this search.";
pub const MCP_LOADING: &str = "Reading the MCP servers...";
pub const MCP_EMPTY: &str = "No MCP server registered yet.";
pub const MCP_EMPTY_ADMIN_TAIL: &str = " Add one to check that the gateway can reach it.";
pub const AGENTS_LABEL: &str = "Enabled for agents";

/// A message line's tone (the web's `ok` / `error` / plain).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Plain,
    Ok,
    Error,
}

/// One row of `GET /skills`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SkillRow {
    pub name: String,
    pub description: String,
    pub version: Option<String>,
    pub origin: String,
    pub source_label: String,
    pub archived: bool,
    pub editable: bool,
    pub trust_level: String,
    pub blocked: bool,
    pub reasons: Vec<String>,
}

impl SkillRow {
    /// The Trust chip (`Archived` wins; `blocked` overrides the level).
    pub fn trust_text(&self) -> &'static str {
        if self.archived {
            return "Archived";
        }
        let level = if self.blocked {
            "blocked"
        } else {
            self.trust_level.as_str()
        };
        match level {
            "first_party" => "First party",
            "audited" => "Audited",
            "adopted" => "Adopted",
            "community" => "Community",
            "blocked" => "Blocked",
            _ => "Unverified",
        }
    }

    pub fn version_text(&self) -> String {
        self.version
            .clone()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "—".into())
    }

    pub fn source_text(&self) -> String {
        if !self.source_label.is_empty() {
            return self.source_label.clone();
        }
        if self.origin == "imported" {
            "Imported".into()
        } else {
            "Curated registry".into()
        }
    }

    /// The web row's buttons, in order, for this viewer.
    pub fn actions(&self, admin: bool) -> Vec<&'static str> {
        let mut out = vec!["View"];
        if !self.archived {
            out.push("Export");
        }
        if admin && self.origin == "imported" && !self.archived {
            out.push("Archive");
        }
        if admin && self.archived {
            out.push("Unarchive");
        }
        out
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct SkillsData {
    pub rows: Vec<SkillRow>,
    /// `warnings[]` minus the `#FALLBACK` ones (the web hides those).
    pub warnings: Vec<String>,
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn b(v: &Value, k: &str) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(false)
}

fn strings(v: &Value, k: &str) -> Vec<String> {
    v.get(k)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

pub fn skills_from_payload(v: &Value) -> Result<SkillsData, String> {
    let arr = v
        .get("skills")
        .and_then(Value::as_array)
        .ok_or("GET /skills answered without a skills list")?;
    let rows = arr
        .iter()
        .map(|r| SkillRow {
            name: s(r, "name"),
            description: s(r, "description"),
            version: r.get("version").and_then(Value::as_str).map(str::to_string),
            origin: s(r, "origin"),
            source_label: s(r, "source_label"),
            archived: b(r, "archived"),
            editable: b(r, "editable"),
            trust_level: s(r, "trust_level"),
            blocked: b(r, "blocked"),
            reasons: strings(r, "reasons"),
        })
        .collect();
    let warnings = strings(v, "warnings")
        .into_iter()
        .filter(|w| !w.starts_with("#FALLBACK"))
        .collect();
    Ok(SkillsData { rows, warnings })
}

/// The rows the search box keeps (case-insensitive over name + description).
pub fn filter_skills<'a>(rows: &'a [SkillRow], query: &str) -> Vec<&'a SkillRow> {
    let q = query.trim().to_lowercase();
    rows.iter()
        .filter(|r| {
            q.is_empty()
                || format!("{} {}", r.name, r.description)
                    .to_lowercase()
                    .contains(&q)
        })
        .collect()
}

/// `GET /skills/{name}` — the skill overlay.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SkillDetail {
    pub name: String,
    pub origin: String,
    pub archived: bool,
    pub editable: bool,
    pub read_only_reason: Option<String>,
    pub skill_md: String,
    pub description: String,
    pub version: String,
    pub license: String,
    pub files: Vec<(String, u64)>,
    pub problem: Option<String>,
}

impl SkillDetail {
    pub fn source_text(&self) -> &'static str {
        if self.archived {
            "Imported (archived)"
        } else if self.origin == "imported" {
            "Imported"
        } else {
            "Curated registry"
        }
    }

    /// The banner above the fields (None: editable by this admin).
    pub fn lead(&self, admin: bool) -> Option<String> {
        if !self.editable {
            return Some(
                self.read_only_reason
                    .clone()
                    .filter(|r| !r.is_empty())
                    .unwrap_or_else(|| "This skill is read-only.".into()),
            );
        }
        if !admin {
            return Some("Only an admin can edit skills.".into());
        }
        None
    }
}

pub fn skill_detail_from_payload(v: &Value) -> Result<SkillDetail, String> {
    if v.get("name").and_then(Value::as_str).is_none() {
        return Err("GET /skills/{name} answered without a name".into());
    }
    let fm = v.get("frontmatter").cloned().unwrap_or(Value::Null);
    let files = v
        .get("files")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|f| {
                    (
                        s(f, "path"),
                        f.get("size").and_then(Value::as_u64).unwrap_or(0),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    Ok(SkillDetail {
        name: s(v, "name"),
        origin: s(v, "origin"),
        archived: b(v, "archived"),
        editable: b(v, "editable"),
        read_only_reason: v
            .get("read_only_reason")
            .and_then(Value::as_str)
            .map(str::to_string),
        skill_md: s(v, "skill_md"),
        description: s(&fm, "description"),
        version: s(v, "version"),
        license: s(&fm, "license"),
        files,
        problem: v.get("problem").and_then(Value::as_str).map(str::to_string),
    })
}

/// A recorded connection test.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct McpTest {
    pub ok: bool,
    pub at: Option<String>,
    pub message: String,
    pub tools: Vec<(String, String)>,
}

fn test_from(v: &Value) -> McpTest {
    McpTest {
        ok: b(v, "ok"),
        at: v.get("at").and_then(Value::as_str).map(str::to_string),
        message: s(v, "message"),
        tools: v
            .get("tools")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .map(|t| (s(t, "name"), s(t, "description")))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

/// One row of `GET /mcp/servers`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct McpRow {
    pub name: String,
    pub transport: String,
    pub command: String,
    pub args: Vec<String>,
    pub cwd: Option<String>,
    pub url: String,
    /// Header name → stored fingerprint (values are never shown).
    pub headers: Vec<(String, String)>,
    pub description: String,
    pub archived: bool,
    pub enabled_for_agents: bool,
    pub last_test: Option<McpTest>,
    pub agents_status: String,
    /// THE sentence when the header values were sealed with the old keychain key and must be
    /// typed again (gateway 0.14 `needs_reconnect`); empty otherwise.
    pub needs_reconnect: String,
}

impl McpRow {
    pub fn is_stdio(&self) -> bool {
        self.transport == "stdio"
    }

    /// "Command" / "URL" — the transport label.
    pub fn transport_label(&self) -> &'static str {
        if self.is_stdio() {
            "Command"
        } else {
            "URL"
        }
    }

    /// The target in full (`command args…` or the URL).
    pub fn target(&self) -> String {
        if self.is_stdio() {
            let mut t = self.command.clone();
            for a in &self.args {
                t.push(' ');
                t.push_str(a);
            }
            t
        } else {
            self.url.clone()
        }
    }

    /// The Status cell (`now` = epoch seconds, for the "ago").
    pub fn status_text(&self, now: i64) -> String {
        match &self.last_test {
            None => "Not tested".into(),
            Some(t) if t.ok => {
                let n = t.tools.len();
                let ago =
                    t.at.as_deref()
                        .and_then(crate::localtime::parse_iso_epoch)
                        .map(|e| ago_text(now - e))
                        .unwrap_or_else(|| "just now".into());
                format!("OK · {n} {} · {ago}", plural(n, "tool", "tools"))
            }
            Some(t) => {
                if t.message.trim().is_empty() {
                    "Failed: no reason given".into()
                } else {
                    format!("Failed: {}", t.message)
                }
            }
        }
    }

    /// The Tools cell.
    pub fn tools_text(&self) -> String {
        match &self.last_test {
            Some(t) if t.ok => {
                let n = t.tools.len();
                if n == 0 {
                    "0".into()
                } else {
                    format!("{n} {}", plural(n, "tool", "tools"))
                }
            }
            _ => "—".into(),
        }
    }

    /// Why the "Enabled for agents" switch can't be switched ON now
    /// (`mcpAgentsBlockReason`; an ON switch is never blocked).
    pub fn agents_block_reason(&self) -> Option<&'static str> {
        if self.enabled_for_agents {
            return None;
        }
        if self.archived {
            return Some("Archived: unarchive it first.");
        }
        if !self.last_test.as_ref().is_some_and(|t| t.ok) {
            return Some(
                "Test the connection first: agents get the tools a successful test lists.",
            );
        }
        None
    }

    /// The inline confirm before turning "Enabled for agents" on.
    pub fn agents_confirm_sentence(&self) -> String {
        let k = self.last_test.as_ref().map(|t| t.tools.len()).unwrap_or(0);
        format!(
            "Offer its {k} {} to your agents? Each call asks for approval unless a run allows all tools.",
            plural(k, "tool", "tools")
        )
    }

    /// The web row's buttons (admins only).
    pub fn actions(&self, admin: bool) -> Vec<&'static str> {
        if !admin {
            return vec![];
        }
        if self.archived {
            vec!["Unarchive"]
        } else {
            vec!["Edit", "Test", "Archive"]
        }
    }
}

pub fn plural(n: usize, one: &'static str, many: &'static str) -> &'static str {
    if n == 1 {
        one
    } else {
        many
    }
}

/// "just now" / "N min ago" / "N h ago" / "N d ago" (the web's `ago`).
pub fn ago_text(secs: i64) -> String {
    let m = secs.max(0) / 60;
    if m < 1 {
        "just now".into()
    } else if m < 60 {
        format!("{m} min ago")
    } else if m < 60 * 24 {
        format!("{} h ago", m / 60)
    } else {
        format!("{} d ago", m / (60 * 24))
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct McpData {
    pub rows: Vec<McpRow>,
    pub agents_note: String,
    pub warnings: Vec<String>,
}

pub fn mcp_row_from(r: &Value) -> McpRow {
    let headers = r
        .get("headers")
        .and_then(Value::as_object)
        .map(|o| {
            o.iter()
                .map(|(k, v)| (k.clone(), s(v, "fingerprint")))
                .collect()
        })
        .unwrap_or_default();
    McpRow {
        name: s(r, "name"),
        transport: s(r, "transport"),
        command: s(r, "command"),
        args: strings(r, "args"),
        cwd: r.get("cwd").and_then(Value::as_str).map(str::to_string),
        url: s(r, "url"),
        headers,
        description: s(r, "description"),
        archived: b(r, "archived"),
        enabled_for_agents: b(r, "enabled_for_agents"),
        last_test: r.get("last_test").filter(|t| t.is_object()).map(test_from),
        agents_status: s(r, "agents_status"),
        needs_reconnect: s(r, "needs_reconnect"),
    }
}

pub fn mcp_from_payload(v: &Value) -> Result<McpData, String> {
    let arr = v
        .get("servers")
        .and_then(Value::as_array)
        .ok_or("GET /mcp/servers answered without a servers list")?;
    Ok(McpData {
        rows: arr.iter().map(mcp_row_from).collect(),
        agents_note: s(v, "agents_note"),
        warnings: strings(v, "warnings"),
    })
}

/// A connection test answer (`POST …/test`): `{ok, message, tools}`.
pub fn test_result_from(v: &Value) -> McpTest {
    test_from(v)
}

/// The Add/Edit form's values (the web's `mcpModalBody`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct McpForm {
    pub name: String,
    pub description: String,
    pub stdio: bool,
    pub command: String,
    pub cwd: String,
    /// One argument per line.
    pub args: String,
    pub url: String,
    /// (name, value, stored fingerprint): an empty value of a stored
    /// header keeps the stored value (sent as null).
    pub headers: Vec<(String, String, Option<String>)>,
}

impl McpForm {
    pub fn body(&self) -> Value {
        let mut o = serde_json::Map::new();
        o.insert(
            "transport".into(),
            Value::String(if self.stdio { "stdio" } else { "http" }.into()),
        );
        o.insert(
            "description".into(),
            Value::String(self.description.trim().into()),
        );
        o.insert("name".into(), Value::String(self.name.trim().into()));
        if self.stdio {
            o.insert("command".into(), Value::String(self.command.trim().into()));
            let args: Vec<Value> = self
                .args
                .lines()
                .map(str::trim)
                .filter(|l| !l.is_empty())
                .map(|l| Value::String(l.into()))
                .collect();
            o.insert("args".into(), Value::Array(args));
            o.insert("cwd".into(), Value::String(self.cwd.trim().into()));
        } else {
            o.insert("url".into(), Value::String(self.url.trim().into()));
            let mut h = serde_json::Map::new();
            for (k, v, fp) in &self.headers {
                let k = k.trim();
                if k.is_empty() {
                    continue;
                }
                if v.is_empty() && fp.is_some() {
                    h.insert(k.into(), Value::Null);
                } else {
                    h.insert(k.into(), Value::String(v.clone()));
                }
            }
            o.insert("headers".into(), Value::Object(h));
        }
        Value::Object(o)
    }

    pub fn from_row(r: &McpRow) -> McpForm {
        McpForm {
            name: r.name.clone(),
            description: r.description.clone(),
            stdio: r.is_stdio(),
            command: r.command.clone(),
            cwd: r.cwd.clone().unwrap_or_default(),
            args: r.args.join("\n"),
            url: r.url.clone(),
            headers: r
                .headers
                .iter()
                .map(|(k, fp)| (k.clone(), String::new(), Some(fp.clone())))
                .collect(),
        }
    }
}

/// The page's signals (ride `Store::skills`).
#[derive(Clone, Copy)]
pub struct SkillsStore {
    pub skills: Signal<Loadable<SkillsData>>,
    pub mcp: Signal<Loadable<McpData>>,
    pub skills_msg: Signal<Option<(String, Tone)>>,
    pub mcp_msg: Signal<Option<(String, Tone)>>,
    pub skills_archived: Signal<bool>,
    pub mcp_archived: Signal<bool>,
    /// 0 = Skills, 1 = MCP servers.
    pub tab: Signal<usize>,
    pub skill_sel: Signal<usize>,
    pub mcp_sel: Signal<usize>,
    pub skill_expanded: Signal<Option<usize>>,
    pub mcp_expanded: Signal<Option<usize>>,
    pub query: Signal<String>,
    pub detail: Signal<Loadable<SkillDetail>>,
    /// The skill overlay's own message line.
    pub detail_msg: Signal<Option<(String, Tone)>>,
    /// The MCP form's test block: None idle, Some(None) testing,
    /// Some(Some(result)).
    pub form_test: Signal<Option<Option<Result<McpTest, String>>>>,
    /// The MCP form's footer note ("Saving..." / "Not saved: …").
    pub form_note: Signal<Option<String>>,
    /// Bumped when an MCP save succeeded (the open form closes).
    pub form_saved: Signal<u64>,
    /// R8.1 shelf row: the folder field is being edited (in place).
    pub shelf_editing: Signal<bool>,
    /// The folder field's text while editing.
    pub shelf_draft: Signal<String>,
    /// The shelf row's own line ("Saved", "Refreshed. …", "Not saved: …").
    pub shelf_msg: Signal<Option<(String, Tone)>>,
    /// The folder save in flight (its form id), if any.
    pub shelf_form: Signal<Option<u64>>,
}

impl SkillsStore {
    pub fn create(cx: Scope) -> SkillsStore {
        SkillsStore {
            skills: cx.signal(Loadable::NotAsked),
            mcp: cx.signal(Loadable::NotAsked),
            skills_msg: cx.signal(None),
            mcp_msg: cx.signal(None),
            skills_archived: cx.signal(false),
            mcp_archived: cx.signal(false),
            tab: cx.signal(0),
            skill_sel: cx.signal(0),
            mcp_sel: cx.signal(0),
            skill_expanded: cx.signal(None),
            mcp_expanded: cx.signal(None),
            query: cx.signal(String::new()),
            detail: cx.signal(Loadable::NotAsked),
            detail_msg: cx.signal(None),
            form_test: cx.signal(None),
            form_note: cx.signal(None),
            form_saved: cx.signal(0),
            shelf_editing: cx.signal(false),
            shelf_draft: cx.signal(String::new()),
            shelf_msg: cx.signal(None),
            shelf_form: cx.signal(None),
        }
    }

    /// Forget everything read from the previous gateway (a reconnect).
    pub fn reset(&self) {
        self.skills.set(Loadable::NotAsked);
        self.mcp.set(Loadable::NotAsked);
        self.skills_msg.set(None);
        self.mcp_msg.set(None);
        self.detail.set(Loadable::NotAsked);
        self.detail_msg.set(None);
        self.form_test.set(None);
        self.form_note.set(None);
        self.shelf_editing.set(false);
        self.shelf_msg.set(None);
        self.shelf_form.set(None);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn mcp_row_carries_the_needs_reconnect_sentence() {
        let v: Value = serde_json::json!({"name": "docs", "transport": "http", "url": "http://x",
            "needs_reconnect": "The MCP header values were sealed with the old macOS keychain key; enter them again \u{2014} the new key lives in the data folder."});
        assert!(mcp_row_from(&v)
            .needs_reconnect
            .contains("old macOS keychain key"));
        let none: Value =
            serde_json::json!({"name": "docs", "transport": "http", "needs_reconnect": null});
        assert_eq!(mcp_row_from(&none).needs_reconnect, "");
    }

    use super::*;
    use serde_json::json;

    #[test]
    fn trust_chip_words_match_the_web() {
        let mut r = SkillRow {
            trust_level: "first_party".into(),
            ..SkillRow::default()
        };
        assert_eq!(r.trust_text(), "First party");
        r.blocked = true;
        assert_eq!(r.trust_text(), "Blocked");
        r.archived = true;
        assert_eq!(r.trust_text(), "Archived");
        let u = SkillRow::default();
        assert_eq!(u.trust_text(), "Unverified");
    }

    #[test]
    fn skill_actions_follow_origin_archive_and_role() {
        let imp = SkillRow {
            origin: "imported".into(),
            ..SkillRow::default()
        };
        assert_eq!(imp.actions(true), vec!["View", "Export", "Archive"]);
        assert_eq!(imp.actions(false), vec!["View", "Export"]);
        let cur = SkillRow {
            origin: "curated".into(),
            ..SkillRow::default()
        };
        assert_eq!(cur.actions(true), vec!["View", "Export"]);
        let arch = SkillRow {
            archived: true,
            ..SkillRow::default()
        };
        assert_eq!(arch.actions(true), vec!["View", "Unarchive"]);
    }

    #[test]
    fn mcp_status_tools_and_block_reasons() {
        let mut r = mcp_row_from(
            &json!({"name":"calc","transport":"stdio","command":"python3","args":["-u","s.py"]}),
        );
        assert_eq!(r.status_text(0), "Not tested");
        assert_eq!(r.tools_text(), "—");
        assert_eq!(r.target(), "python3 -u s.py");
        assert_eq!(
            r.agents_block_reason(),
            Some("Test the connection first: agents get the tools a successful test lists.")
        );
        r.last_test = Some(McpTest {
            ok: true,
            at: Some("2026-10-04T10:00:00Z".into()),
            message: "Connected.".into(),
            tools: vec![("add".into(), "".into()), ("echo".into(), "".into())],
        });
        let now = crate::localtime::parse_iso_epoch("2026-10-04T12:30:00Z").unwrap();
        assert_eq!(r.status_text(now), "OK · 2 tools · 2 h ago");
        assert_eq!(r.tools_text(), "2 tools");
        assert_eq!(r.agents_block_reason(), None);
        assert_eq!(
            r.agents_confirm_sentence(),
            "Offer its 2 tools to your agents? Each call asks for approval unless a run allows all tools."
        );
        r.archived = true;
        assert_eq!(
            r.agents_block_reason(),
            Some("Archived: unarchive it first.")
        );
        r.enabled_for_agents = true;
        assert_eq!(r.agents_block_reason(), None);
    }

    #[test]
    fn form_body_is_the_web_modal_body() {
        let f = McpForm {
            name: "calc".into(),
            stdio: true,
            command: "npx".into(),
            args: "-y\n\n @mcp/x \n".into(),
            ..McpForm::default()
        };
        assert_eq!(
            f.body(),
            json!({"transport":"stdio","description":"","name":"calc","command":"npx","args":["-y","@mcp/x"],"cwd":""})
        );
        let h = McpForm {
            name: "docs".into(),
            url: "http://x/mcp".into(),
            headers: vec![
                ("Authorization".into(), "".into(), Some("abc".into())),
                ("X-New".into(), "v".into(), None),
            ],
            ..McpForm::default()
        };
        assert_eq!(
            h.body(),
            json!({"transport":"http","description":"","name":"docs","url":"http://x/mcp","headers":{"Authorization":null,"X-New":"v"}})
        );
    }
}
