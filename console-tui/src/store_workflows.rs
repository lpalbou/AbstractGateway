//! The Workflows page (web console `#workflows-section`,
//! `#workflows-skipped-section`, `#agent-defaults-section`): state and
//! pure parsing/wording, every sentence the web page's own.
//!
//! Rows are grouped exactly like the web's `workflowRows()`: one row per
//! `owner.kind:bundle_id` (a user's bundle and a shared bundle with the
//! same id stay two rows), versions newest first by a semver-like
//! compare, the latest = the first non-draft.

use std::cmp::Ordering;

use abstracttui::reactive::{Scope, Signal};
use serde_json::Value;

use super::skills::Tone;
use super::Loadable;

pub const TITLE: &str = "Workflows";
pub const SUBTITLE: &str = "Bundles, versions, import and export";
pub const PURPOSE: &str = "Workflows are the programs your apps and automations run. They come in bundles (.flow files): some ship with the gateway, others you import or publish from AbstractFlow.";
pub const EMPTY: &str = "No workflows registered.";
pub const NO_MATCH: &str = "No workflow matches this search.";
pub const GROUP_SHARED: &str = "Shared with everyone";
pub const GROUP_MINE: &str = "Mine";
pub const AVAILABLE_LABEL: &str = "Available to users";
pub const AVAILABLE_HELP: &str = "Off hides this workflow from users' lists and app pickers and pauses their automations on it; turning it back on doesn't resume them. Admins always see it, and an app's default workflow keeps running for everyone.";
pub const BROKEN_TITLE: &str = "Broken workflows";
pub const BROKEN_SENTENCE: &str = "These bundle files are on disk but the gateway cannot run them, so they do not appear in the list. Nothing was deleted — fix the cause and reload, or archive them.";
pub const DEFAULTS_TITLE: &str = "Default workflow per app";
pub const DEFAULTS_NOTE: &str =
    "When an app asks for “an agent” without naming a workflow, the gateway runs this one.";
pub const DEFAULTS_ADMIN_ONLY: &str = "Only an admin can change these.";
pub const DEFAULTS_LOADING: &str = "Reading the default workflows...";
pub const STREAMING_LABEL: &str = "Streamed replies";
pub const STREAMING_HELP: &str = "New interactive runs show the model's reply as it is written, unless the app asks for it whole; scheduled runs, bridges and entities always get whole replies.";

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}
fn b(v: &Value, k: &str) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(false)
}

/// The web's `compareVersions`: numeric segments compare as numbers, then
/// as text; a longer version wins a tie.
pub fn compare_versions(a: &str, b: &str) -> Ordering {
    let pa: Vec<&str> = a.split(['.', '-', '+']).collect();
    let pb: Vec<&str> = b.split(['.', '-', '+']).collect();
    for i in 0..pa.len().max(pb.len()) {
        let (x, y) = (pa.get(i), pb.get(i));
        let o = match (x, y) {
            (Some(x), Some(y)) => match (x.parse::<u64>(), y.parse::<u64>()) {
                (Ok(m), Ok(n)) => m.cmp(&n),
                _ => x.cmp(y),
            },
            (Some(_), None) => Ordering::Greater,
            (None, Some(_)) => Ordering::Less,
            (None, None) => Ordering::Equal,
        };
        if o != Ordering::Equal {
            return o;
        }
    }
    Ordering::Equal
}

/// "unversioned" for 0.0.0 (the web's `versionLabel`).
pub fn version_label(v: &str) -> String {
    if v == "0.0.0" {
        "unversioned".into()
    } else {
        v.to_string()
    }
}

pub fn source_label(source: &str) -> &'static str {
    match source {
        "shipped" => "Shipped",
        "imported" => "Imported",
        "published" => "From AbstractFlow",
        _ => "Unknown source",
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Entry {
    pub flow_id: String,
    pub name: String,
    pub description: String,
    pub interfaces: Vec<String>,
    pub deprecated: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Version {
    pub version: String,
    pub is_draft: bool,
    pub channel: String,
    pub created_at: String,
    pub archived: bool,
    pub available: bool,
    pub source: String,
    pub description: String,
    pub default_entrypoint: String,
    pub entrypoints: Vec<Entry>,
    pub can_archive: bool,
    pub can_set_availability: bool,
}

impl Version {
    /// `<channel or draft/published> · <date>`.
    pub fn meta(&self) -> String {
        let ch = if !self.channel.is_empty() {
            self.channel.clone()
        } else if self.is_draft {
            "draft".into()
        } else {
            "published".into()
        };
        let date: String = self.created_at.chars().take(10).collect();
        if date.is_empty() {
            ch
        } else {
            format!("{ch} · {date}")
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct WfRow {
    /// `owner.kind` ("gateway" | "user").
    pub owner: String,
    pub bundle_id: String,
    pub name: String,
    pub description: String,
    pub source: String,
    pub available: bool,
    pub archived: bool,
    pub deprecated: bool,
    pub interfaces: Vec<String>,
    /// Newest first.
    pub versions: Vec<Version>,
    /// Index of the latest (first non-draft) version.
    pub latest: usize,
}

impl WfRow {
    pub fn latest(&self) -> &Version {
        &self.versions[self.latest]
    }

    /// "1.2.0 +2 older" / "No version".
    pub fn version_text(&self) -> String {
        let v = self.latest().version.as_str();
        let base = if v.is_empty() {
            "No version".to_string()
        } else {
            version_label(v)
        };
        let older = self.versions.len().saturating_sub(1);
        if older > 0 {
            format!("{base} +{older} older")
        } else {
            base
        }
    }

    pub fn description_text(&self) -> String {
        if self.description.trim().is_empty() {
            "No description.".into()
        } else {
            self.description.clone()
        }
    }

    pub fn can_archive(&self) -> bool {
        self.latest().can_archive
    }

    pub fn can_set_availability(&self) -> bool {
        self.latest().can_set_availability
    }

    /// The "Used by" cell: plain names when the defaults table names them.
    pub fn used_by(&self, labels: &dyn Fn(&str) -> Option<String>) -> String {
        if self.interfaces.is_empty() {
            return "No app".into();
        }
        self.interfaces
            .iter()
            .map(|i| labels(i).unwrap_or_else(|| i.clone()))
            .collect::<Vec<_>>()
            .join(", ")
    }

    fn matches(&self, q: &str) -> bool {
        if q.is_empty() {
            return true;
        }
        let hit = |t: &str| t.to_lowercase().contains(q);
        hit(&self.bundle_id)
            || hit(&self.name)
            || hit(&self.description)
            || self
                .versions
                .iter()
                .flat_map(|v| v.entrypoints.iter())
                .any(|e| hit(&e.name) || hit(&e.description))
    }
}

/// Broken versions grouped by (bundle, reason).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Broken {
    pub bundle_id: String,
    pub reason: String,
    pub versions: Vec<String>,
    pub paths: Vec<String>,
    pub can_archive: bool,
}

impl Broken {
    pub fn affected(&self) -> String {
        let n = self.versions.len();
        if n == 1 {
            "1 version".into()
        } else {
            format!("{n} versions")
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct WorkflowsData {
    pub rows: Vec<WfRow>,
    pub broken: Vec<Broken>,
}

impl WorkflowsData {
    /// "{W} workflow, {N} version the gateway could not load."
    pub fn broken_count_line(&self) -> String {
        let w = self
            .broken
            .iter()
            .map(|b| &b.bundle_id)
            .collect::<std::collections::BTreeSet<_>>()
            .len();
        let n: usize = self.broken.iter().map(|b| b.versions.len()).sum();
        format!(
            "{w} {}, {n} {} the gateway could not load.",
            if w == 1 { "workflow" } else { "workflows" },
            if n == 1 { "version" } else { "versions" }
        )
    }

    /// The shown groups: (title, rows) for Shared then Mine, empty groups
    /// dropped, search applied, rows sorted by name.
    pub fn groups(&self, query: &str) -> Vec<(&'static str, Vec<&WfRow>)> {
        let q = query.trim().to_lowercase();
        let mut out = Vec::new();
        for (title, kind) in [(GROUP_SHARED, "gateway"), (GROUP_MINE, "user")] {
            let rows: Vec<&WfRow> = self
                .rows
                .iter()
                .filter(|r| r.owner == kind && r.matches(&q))
                .collect();
            if !rows.is_empty() {
                out.push((title, rows));
            }
        }
        out
    }
}

fn entry_from(e: &Value) -> Entry {
    Entry {
        flow_id: s(e, "flow_id"),
        name: s(e, "name"),
        description: s(e, "description"),
        interfaces: e
            .get("interfaces")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default(),
        deprecated: b(e, "deprecated"),
    }
}

fn version_from(v: &Value) -> Version {
    let actions = v.get("actions").cloned().unwrap_or(Value::Null);
    Version {
        version: s(v, "bundle_version"),
        is_draft: b(v, "is_draft"),
        channel: s(v, "version_channel"),
        created_at: s(v, "created_at"),
        archived: b(v, "archived"),
        available: v.get("available").and_then(Value::as_bool).unwrap_or(true),
        source: s(v, "source"),
        description: s(v, "description"),
        default_entrypoint: s(v, "default_entrypoint"),
        entrypoints: v
            .get("entrypoints")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(entry_from).collect())
            .unwrap_or_default(),
        can_archive: b(&actions, "can_archive"),
        can_set_availability: b(&actions, "can_set_availability"),
    }
}

/// `GET /bundles?all_versions=true&…` → the page's rows and broken groups.
pub fn workflows_from_payload(v: &Value) -> Result<WorkflowsData, String> {
    let items = v
        .get("items")
        .and_then(Value::as_array)
        .ok_or("GET /bundles answered without an items list")?;
    let mut groups: Vec<(String, String, Vec<Version>)> = Vec::new();
    for it in items {
        let owner = it
            .get("owner")
            .and_then(|o| o.get("kind"))
            .and_then(Value::as_str)
            .unwrap_or("gateway")
            .to_string();
        let bid = s(it, "bundle_id");
        let ver = version_from(it);
        match groups.iter_mut().find(|(o, b, _)| *o == owner && *b == bid) {
            Some(g) => g.2.push(ver),
            None => groups.push((owner, bid, vec![ver])),
        }
    }
    let mut rows: Vec<WfRow> = groups
        .into_iter()
        .map(|(owner, bundle_id, mut versions)| {
            versions.sort_by(|a, b| compare_versions(&b.version, &a.version));
            let latest = versions.iter().position(|v| !v.is_draft).unwrap_or(0);
            let lv = &versions[latest];
            let entry = lv
                .entrypoints
                .iter()
                .find(|e| e.flow_id == lv.default_entrypoint)
                .or_else(|| lv.entrypoints.first());
            let name = entry
                .map(|e| e.name.clone())
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| bundle_id.clone());
            let mut interfaces: Vec<String> = Vec::new();
            for e in &lv.entrypoints {
                for i in &e.interfaces {
                    if !interfaces.contains(i) {
                        interfaces.push(i.clone());
                    }
                }
            }
            WfRow {
                name,
                description: lv.description.clone(),
                source: lv.source.clone(),
                available: lv.available,
                archived: versions.iter().all(|v| v.archived),
                deprecated: !lv.entrypoints.is_empty()
                    && lv.entrypoints.iter().all(|e| e.deprecated),
                interfaces,
                latest,
                owner,
                bundle_id,
                versions,
            }
        })
        .collect();
    rows.sort_by(|a, b| {
        a.name
            .to_lowercase()
            .cmp(&b.name.to_lowercase())
            .then(a.bundle_id.cmp(&b.bundle_id))
    });
    let mut broken: Vec<Broken> = Vec::new();
    for sk in v
        .get("skipped")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if b(sk, "archived") {
            continue;
        }
        let bid = s(sk, "bundle_id");
        let reason = s(sk, "reason");
        let i = match broken
            .iter()
            .position(|g| g.bundle_id == bid && g.reason == reason)
        {
            Some(i) => i,
            None => {
                broken.push(Broken {
                    bundle_id: bid,
                    reason,
                    ..Broken::default()
                });
                broken.len() - 1
            }
        };
        let g = &mut broken[i];
        g.versions.push(s(sk, "bundle_version"));
        g.paths.push(s(sk, "path"));
        g.can_archive = g.can_archive || b(sk, "can_archive");
    }
    Ok(WorkflowsData { rows, broken })
}

/// One option of a default-workflow picker.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Eligible {
    pub value: String,
    pub label: String,
}

/// One `agents.default_workflow.<iface>` row.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct DefaultRow {
    pub interface: String,
    pub label: String,
    pub help: String,
    pub group: String,
    pub state: String,
    pub reason: String,
    pub value: String,
    pub source: String,
    pub default: String,
    pub eligible: Vec<Eligible>,
    pub resolved: Option<String>,
}

impl DefaultRow {
    /// The picker's options, in the web's order: the gateway default
    /// first (value ""), a stored value no longer installed, then every
    /// eligible entrypoint.
    pub fn options(&self) -> Vec<(String, String)> {
        let mut out = Vec::new();
        let first = if self.default.is_empty() {
            "No workflow available".to_string()
        } else {
            let label = self
                .eligible
                .iter()
                .find(|e| e.value == self.default)
                .map(|e| e.label.clone())
                .unwrap_or_else(|| self.default.split(':').next().unwrap_or("").to_string());
            format!("Gateway default: {label}")
        };
        out.push((String::new(), first));
        if self.source == "stored"
            && !self.value.is_empty()
            && !self.eligible.iter().any(|e| e.value == self.value)
        {
            out.push((
                self.value.clone(),
                format!("{} (not installed)", self.value),
            ));
        }
        for e in &self.eligible {
            out.push((e.value.clone(), e.label.clone()));
        }
        out
    }

    /// The selected option's value ("" = the gateway default).
    pub fn selected(&self) -> String {
        if self.source == "stored" {
            self.value.clone()
        } else {
            String::new()
        }
    }

    pub fn selected_label(&self) -> String {
        let sel = self.selected();
        self.options()
            .into_iter()
            .find(|(v, _)| *v == sel)
            .map(|(_, l)| l)
            .unwrap_or(sel)
    }

    /// The line under the select (broken → reason; set → "Runs …").
    pub fn state_line(&self) -> Option<String> {
        match self.state.as_str() {
            "broken" => Some(self.reason.clone()),
            "set" => self.resolved.clone().map(|r| format!("Runs {r}")),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct DefaultsData {
    pub writable: bool,
    pub rows: Vec<DefaultRow>,
    /// `agents.error` (the block shows it as a failed read).
    pub error: Option<String>,
    /// `agents.streaming_default.value` (None: not served).
    pub streaming: Option<bool>,
}

impl DefaultsData {
    /// The plain name of an interface (for "Used by").
    pub fn label_of(&self, iface: &str) -> Option<String> {
        self.rows
            .iter()
            .find(|r| r.interface == iface && !r.label.is_empty())
            .map(|r| r.label.clone())
    }
}

/// The web's known-interfaces order: the two app agents first, then the
/// rest sorted.
const FIRST: [&str; 2] = ["abstractcode.agent.v1", "abstractassistant.agent.v1"];

pub fn defaults_from_payload(v: &Value) -> Result<DefaultsData, String> {
    let agents = v.get("agents").cloned().unwrap_or(Value::Null);
    let error = agents
        .get("error")
        .and_then(Value::as_str)
        .map(str::to_string);
    let map = agents.get("default_workflow").and_then(Value::as_object);
    let mut rows: Vec<DefaultRow> = Vec::new();
    if let Some(map) = map {
        for (iface, r) in map {
            for k in ["label", "help", "group", "state"] {
                if r.get(k).is_none() {
                    return Err(format!(
                        "agents.default_workflow.{iface} has no {k} (the gateway's defaults table is incomplete)"
                    ));
                }
            }
            let eligible = r
                .get("eligible")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|e| {
                            let mut label = format!(
                                "{} {}",
                                {
                                    let n = s(e, "name");
                                    if n.is_empty() {
                                        s(e, "flow_id")
                                    } else {
                                        n
                                    }
                                },
                                s(e, "bundle_version")
                            )
                            .trim()
                            .to_string();
                            if b(e, "show_scope") {
                                label.push_str(if s(e, "registry_scope") == "private" {
                                    " (this gateway)"
                                } else {
                                    " (catalog)"
                                });
                            }
                            Eligible {
                                value: s(e, "value"),
                                label,
                            }
                        })
                        .collect()
                })
                .unwrap_or_default();
            let resolved = r.get("resolved").filter(|x| x.is_object()).map(|x| {
                let n = s(x, "name");
                let n = if n.is_empty() { s(x, "flow_id") } else { n };
                format!("{n} {}", s(x, "bundle_version")).trim().to_string()
            });
            rows.push(DefaultRow {
                interface: iface.clone(),
                label: s(r, "label"),
                help: s(r, "help"),
                group: s(r, "group"),
                state: s(r, "state"),
                reason: s(r, "reason"),
                value: s(r, "value"),
                source: s(r, "source"),
                default: s(r, "default"),
                eligible,
                resolved,
            });
        }
    }
    rows.sort_by(|a, b| {
        let ka = FIRST
            .iter()
            .position(|f| *f == a.interface)
            .unwrap_or(FIRST.len());
        let kb = FIRST
            .iter()
            .position(|f| *f == b.interface)
            .unwrap_or(FIRST.len());
        ka.cmp(&kb).then(a.interface.cmp(&b.interface))
    });
    Ok(DefaultsData {
        writable: b(v, "writable"),
        rows,
        error,
        streaming: agents
            .get("streaming_default")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_bool),
    })
}

/// The page's signals (ride `Store::wf`).
#[derive(Clone, Copy)]
pub struct WorkflowsStore {
    pub data: Signal<Loadable<WorkflowsData>>,
    pub defaults: Signal<Loadable<DefaultsData>>,
    pub msg: Signal<Option<(String, Tone)>>,
    pub defaults_msg: Signal<Option<(String, Tone)>>,
    pub drafts: Signal<bool>,
    pub older: Signal<bool>,
    pub archived: Signal<bool>,
    pub query: Signal<String>,
    /// 0 = Workflows, 1 = Default workflow per app, 2 = Broken workflows.
    pub tab: Signal<usize>,
    pub sel: Signal<usize>,
    pub expanded: Signal<Option<usize>>,
    pub def_sel: Signal<usize>,
    pub def_expanded: Signal<Option<usize>>,
    pub broken_sel: Signal<usize>,
    /// "Other workflow types" unfolded.
    pub other_open: Signal<bool>,
}

impl WorkflowsStore {
    pub fn create(cx: Scope) -> WorkflowsStore {
        WorkflowsStore {
            data: cx.signal(Loadable::NotAsked),
            defaults: cx.signal(Loadable::NotAsked),
            msg: cx.signal(None),
            defaults_msg: cx.signal(None),
            drafts: cx.signal(false),
            older: cx.signal(false),
            archived: cx.signal(false),
            query: cx.signal(String::new()),
            tab: cx.signal(0),
            sel: cx.signal(0),
            expanded: cx.signal(None),
            def_sel: cx.signal(0),
            def_expanded: cx.signal(None),
            broken_sel: cx.signal(0),
            other_open: cx.signal(false),
        }
    }

    pub fn reset(&self) {
        self.data.set(Loadable::NotAsked);
        self.defaults.set(Loadable::NotAsked);
        self.msg.set(None);
        self.defaults_msg.set(None);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn item(owner: &str, id: &str, v: &str, draft: bool, archived: bool) -> Value {
        json!({"bundle_id": id, "bundle_version": v, "owner": {"kind": owner}, "is_draft": draft,
               "archived": archived, "available": true, "source": "imported", "description": format!("{id} does things"),
               "default_entrypoint": "f1", "created_at": "2026-10-01T10:00:00Z",
               "entrypoints": [{"flow_id": "f1", "name": format!("{id} name"), "interfaces": ["abstractcode.agent.v1"]}],
               "actions": {"can_archive": owner == "user", "can_set_availability": owner == "gateway"}})
    }

    #[test]
    fn rows_group_by_owner_and_id_with_semver_latest() {
        let d = workflows_from_payload(&json!({"items": [
            item("gateway", "basic", "0.10.0", false, false),
            item("gateway", "basic", "0.9.0", false, false),
            item("gateway", "basic", "0.11.0", true, false),
            item("user", "basic", "1.0.0", false, false),
        ], "skipped": []}))
        .unwrap();
        assert_eq!(d.rows.len(), 2, "same id, two owners = two rows");
        let shared = d.rows.iter().find(|r| r.owner == "gateway").unwrap();
        assert_eq!(shared.versions[0].version, "0.11.0");
        assert_eq!(shared.latest().version, "0.10.0", "first non-draft");
        assert_eq!(shared.version_text(), "0.10.0 +2 older");
        let g = d.groups("");
        assert_eq!(g[0].0, GROUP_SHARED);
        assert_eq!(g[1].0, GROUP_MINE);
    }

    #[test]
    fn words_match_the_web() {
        assert_eq!(source_label("published"), "From AbstractFlow");
        assert_eq!(source_label("weird"), "Unknown source");
        assert_eq!(version_label("0.0.0"), "unversioned");
        let d = workflows_from_payload(&json!({"items": [], "skipped": [
            {"bundle_id": "x", "bundle_version": "1", "reason": "needs y", "path": "/a", "can_archive": true},
            {"bundle_id": "x", "bundle_version": "2", "reason": "needs y", "path": "/b", "can_archive": true}
        ]}))
        .unwrap();
        assert_eq!(
            d.broken_count_line(),
            "1 workflow, 2 versions the gateway could not load."
        );
        assert_eq!(d.broken[0].affected(), "2 versions");
    }

    #[test]
    fn default_options_follow_the_web_select() {
        let d = defaults_from_payload(&json!({"writable": true, "agents": {"default_workflow": {
            "abstractcode.agent.v1": {"label": "AbstractCode — chat agent", "help": "h", "group": "apps", "state": "set",
                "value": "gone:f9", "source": "stored", "default": "basic@0.0.5:f1",
                "resolved": {"name": "Basic agent", "bundle_version": "0.0.5"},
                "eligible": [{"value": "basic@0.0.5:f1", "name": "Basic agent", "bundle_version": "0.0.5", "registry_scope": "private", "show_scope": true}]}
        }, "streaming_default": {"value": true}}}))
        .unwrap();
        let r = &d.rows[0];
        assert_eq!(
            r.options(),
            vec![
                (
                    "".into(),
                    "Gateway default: Basic agent 0.0.5 (this gateway)".into()
                ),
                ("gone:f9".into(), "gone:f9 (not installed)".into()),
                (
                    "basic@0.0.5:f1".into(),
                    "Basic agent 0.0.5 (this gateway)".into()
                ),
            ]
        );
        assert_eq!(r.state_line().as_deref(), Some("Runs Basic agent 0.0.5"));
        assert_eq!(d.streaming, Some(true));
    }
}
