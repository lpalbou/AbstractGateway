//! App state: one struct of signals owned by the UI thread, plus the
//! typed rows parsed out of gateway payloads.
//!
//! Every remote domain lives in a `Loadable<T>` so the four honest
//! states — not asked, loading, loaded (possibly empty), failed (with
//! the error kind) — render distinctly. Nothing here does I/O.

use std::collections::HashMap;
use std::time::Instant;

use abstracttui::prelude::*;
use serde_json::Value;

use crate::api::ApiError;

/// Remote data honesty: never render a guess.
#[derive(Clone, Debug, Default)]
pub enum Loadable<T> {
    #[default]
    NotAsked,
    Loading,
    Ready(T),
    Failed(ApiError),
}

impl<T> Loadable<T> {
    pub fn ready(&self) -> Option<&T> {
        match self {
            Loadable::Ready(t) => Some(t),
            _ => None,
        }
    }
    pub fn is_loading(&self) -> bool {
        matches!(self, Loadable::Loading)
    }
}

// ---------------------------------------------------------------------
// Typed rows (parsed from payloads; unknown fields ignored, absent
// fields honest `None` — the UI renders `—` with a reason, never a guess).
// ---------------------------------------------------------------------

fn s(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_string)
}
fn b(v: &Value, key: &str) -> Option<bool> {
    v.get(key).and_then(Value::as_bool)
}
/// The string-list fold (F12: was hand-rolled ×4).
fn str_list(v: &Value, key: &str) -> Vec<String> {
    v.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}
/// The rows fold: the FIRST present key's array, parsed per row (F12:
/// was hand-rolled ×10; `keys` is a slice because /runs alternates
/// items|runs).
fn rows_from<T>(v: &Value, keys: &[&str], f: impl Fn(&Value) -> Option<T>) -> Vec<T> {
    keys.iter()
        .find_map(|k| v.get(*k).and_then(Value::as_array))
        .map(|a| a.iter().filter_map(f).collect())
        .unwrap_or_default()
}

#[derive(Clone, Debug, PartialEq)]
pub struct Identity {
    pub user_id: String,
    pub tenant_id: String,
    pub admin: bool,
    pub roles: Vec<String>,
    /// "users" | "legacy-token"
    pub auth_mode: String,
    /// "per-principal" | "single-user"
    pub routing_mode: String,
}

impl Identity {
    pub fn from_me(v: &Value) -> Option<Identity> {
        let p = v.get("principal")?;
        Some(Identity {
            user_id: s(p, "user_id").unwrap_or_default(),
            tenant_id: s(p, "tenant_id").unwrap_or_default(),
            admin: b(p, "admin").unwrap_or(false),
            roles: str_list(p, "roles"),
            auth_mode: v
                .get("auth")
                .and_then(|a| s(a, "mode"))
                .unwrap_or_else(|| "unknown".into()),
            routing_mode: v
                .get("routing")
                .and_then(|r| s(r, "mode"))
                .unwrap_or_else(|| "unknown".into()),
        })
    }
}

/// Connection phase — probe-shaped, not stream-shaped (a config tool
/// probes on demand; there is no persistent transport to reconnect).
///
/// The taxonomy is deliberately wide: 401 (wrong token), 403 (token
/// without access), "an HTTP server answered but not like a gateway"
/// (a port squatter — a documented recurring event on 8080), and a
/// transport failure are four different operator actions.
#[derive(Clone, Debug, Default)]
pub enum ConnPhase {
    #[default]
    NotConnected,
    Probing,
    Connected(Identity),
    /// A request failed at the transport layer while we believed we
    /// were connected — the health authority is re-checking with one
    /// background ping. NOT connected (screen loads pause; the header
    /// keeps the identity so the operator sees WHO is being verified).
    Verifying(Identity),
    /// Reached the gateway; the token is wrong or missing (401).
    Unauthorized(String),
    /// Reached the gateway; this token lacks access (403).
    Forbidden(String),
    /// Something answered HTTP, but not like a gateway (status, detail).
    NotGateway(u16, String),
    /// Never reached it (refused / timeout / DNS).
    Unreachable(String),
}

impl ConnPhase {
    pub fn is_connected(&self) -> bool {
        matches!(self, ConnPhase::Connected(_))
    }
}

/// The ONE probe-error -> phase mapping (extracted from the worker's
/// Connect arm so the health authority and the Probe button can never
/// tell different stories about the same error).
pub fn phase_from_probe_error(e: &crate::api::ApiError) -> ConnPhase {
    use crate::api::ApiErrorKind;
    match e.kind {
        ApiErrorKind::Unauthorized => ConnPhase::Unauthorized(e.message.clone()),
        ApiErrorKind::Forbidden => ConnPhase::Forbidden(e.message.clone()),
        // An HTTP status from something that is not behaving like a
        // gateway (port squatters answer 404/500 to /ping) —
        // "unreachable" would be a lie: something IS there.
        ApiErrorKind::Http(code) => ConnPhase::NotGateway(code, e.message.clone()),
        ApiErrorKind::Protocol => ConnPhase::NotGateway(0, e.message.clone()),
        _ => ConnPhase::Unreachable(e.to_string()),
    }
}

#[derive(Clone, Debug)]
pub struct ProviderItem {
    pub name: String,
    pub display_name: String,
    pub local: bool,
    pub auth_required: bool,
    pub status: String,
    /// Inline models (may be empty — per-provider fallback loads more).
    pub models: Vec<String>,
    pub models_error: Option<String>,
}

#[derive(Clone, Debug, Default)]
pub struct ProvidersData {
    pub items: Vec<ProviderItem>,
    pub default_provider: Option<String>,
    pub default_model: Option<String>,
}

impl ProvidersData {
    pub fn from_value(v: &Value) -> ProvidersData {
        let items = rows_from(v, &["items"], |it| {
            let name = s(it, "name").or_else(|| s(it, "id"))?;
            Some(ProviderItem {
                display_name: s(it, "display_name").unwrap_or_else(|| name.clone()),
                local: b(it, "local_provider").unwrap_or(false),
                auth_required: b(it, "authentication_required").unwrap_or(false),
                status: s(it, "status").unwrap_or_default(),
                models: rows_from(it, &["models"], model_id),
                models_error: s(it, "models_error"),
                name,
            })
        });
        ProvidersData {
            items,
            default_provider: s(v, "default_provider"),
            default_model: s(v, "default_model"),
        }
    }
}

/// Model entries arrive as strings or as {id|name|model} objects.
fn model_id(v: &Value) -> Option<String> {
    v.as_str().map(str::to_string).or_else(|| {
        s(v, "id")
            .or_else(|| s(v, "name"))
            .or_else(|| s(v, "model"))
    })
}

pub fn models_from_payload(v: &Value) -> Vec<String> {
    v.get("models")
        .or_else(|| v.get("items"))
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(model_id).collect())
        .unwrap_or_default()
}

#[derive(Clone, Debug)]
pub struct Profile {
    pub id: String,
    pub display_name: String,
    pub description: String,
    pub family: String,
    pub base_url: String,
    pub default_base_url: Option<String>,
    pub api_key_set: bool,
    pub api_key_fingerprint: Option<String>,
    /// "gateway" | "user" | "environment" | "core"
    pub scope: String,
    pub enabled: bool,
    /// Synthetic rows mirror env/core config (or an auto-probed local
    /// server) — Override creates a managed copy; never edit/delete.
    pub synthetic: bool,
    pub allowed_models: Vec<String>,
    /// Direct provider id on synthetic rows (e.g. "anthropic") — the
    /// name Core routes WITHOUT an endpoint: prefix. Managed profiles
    /// leave this unset and answer to endpoint:<id>.
    pub provider_id: Option<String>,
    /// "environment" | "reachable-default" | "abstractcore.config" —
    /// synthetic rows say where they come from.
    pub source: Option<String>,
    /// Live model count the gateway already discovered for synthetic
    /// rows (reachable-default probes ship it).
    pub discovered_model_count: Option<u64>,
}

impl Profile {
    pub fn from_value(v: &Value) -> Option<Profile> {
        let id = s(v, "id")?;
        Some(Profile {
            display_name: s(v, "display_name").unwrap_or_else(|| id.clone()),
            description: s(v, "description").unwrap_or_default(),
            family: s(v, "provider_family").unwrap_or_default(),
            base_url: s(v, "base_url").unwrap_or_default(),
            default_base_url: s(v, "default_base_url"),
            api_key_set: b(v, "api_key_set").unwrap_or(false),
            api_key_fingerprint: s(v, "api_key_fingerprint").filter(|f| !f.is_empty()),
            scope: s(v, "scope").unwrap_or_default(),
            enabled: b(v, "enabled").unwrap_or(true),
            // The web checks `managed === false || synthetic === true`;
            // the live payload always sets the pair together on
            // synthetic rows and omits both on managed ones.
            synthetic: b(v, "synthetic").unwrap_or(false) || b(v, "managed") == Some(false),
            allowed_models: str_list(v, "allowed_models"),
            provider_id: s(v, "provider_id").filter(|p| !p.is_empty()),
            source: s(v, "source").filter(|p| !p.is_empty()),
            discovered_model_count: v.get("discovered_model_count").and_then(Value::as_u64),
            id,
        })
    }

    /// THE provider-name join law (web parity, live-verified): the name
    /// this row answers to in discovery/models/sandbox and in flow
    /// pins. Synthetic rows are DIRECT providers (bare `anthropic` —
    /// `endpoint:anthropic` is "Unknown provider" to the gateway);
    /// managed profiles answer to their virtual `endpoint:<id>`.
    pub fn provider_name(&self) -> String {
        match &self.provider_id {
            Some(p) => p.clone(),
            None => format!("endpoint:{}", self.id),
        }
    }
}

/// Discovery names with NO row in the unified providers list — the
/// registered-but-unconfigured backends (the web shows these only as
/// add-connection cards, never as list rows). Join key: the row's
/// provider name (bare for synthetic, endpoint:<id> for managed),
/// case-insensitive. Preserves discovery order.
pub fn unconfigured_provider_names(
    profiles: &ProfilesData,
    providers: &ProvidersData,
) -> Vec<String> {
    let taken: std::collections::BTreeSet<String> = profiles
        .profiles
        .iter()
        .map(|p| p.provider_name().to_ascii_lowercase())
        .collect();
    providers
        .items
        .iter()
        .map(|i| i.name.clone())
        .filter(|n| !taken.contains(&n.to_ascii_lowercase()))
        .collect()
}

#[derive(Clone, Debug, Default)]
pub struct ProfilesData {
    pub profiles: Vec<Profile>,
    pub can_create_gateway_scope: bool,
}

impl ProfilesData {
    pub fn from_value(v: &Value) -> ProfilesData {
        ProfilesData {
            profiles: rows_from(v, &["profiles"], Profile::from_value),
            can_create_gateway_scope: b(v, "can_create_gateway_scope").unwrap_or(false),
        }
    }
}

#[derive(Clone, Debug)]
pub struct RouteRow {
    pub key: String,
    pub kind: String,
    pub modality: String,
    pub task: Option<String>,
    pub label: String,
    pub configured: bool,
    pub source: String,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub base_url: Option<String>,
    pub options: Option<Value>,
    pub covered_by: Option<String>,
    pub read_only: bool,
    pub overrideable: bool,
    pub derived_from: Option<String>,
    pub package_hint: Option<String>,
    /// The default reasoning effort carried on the text-generation
    /// route. Core stores it on the route row and every entry point
    /// reads it from there — the gateway holds no copy.
    pub reasoning: Option<String>,
    /// THE ROUTE HIERARCHY, off the payload — never re-derived here.
    /// `output.image` is the PARENT of `output.image.*`: the one value
    /// that answers every image task without a row of its own (the
    /// simple path, and what the fresh-install seed writes). A task row
    /// carries `broad_key` = its parent; a parent carries `task_keys`.
    /// Core derives all three in `manager._decorate_route_hierarchy`, so
    /// the four grids that render this payload cannot disagree.
    pub broad_key: Option<String>,
    pub task_keys: Vec<String>,
    /// The parent is UNSET and every task row under it is configured, so
    /// nothing can reach it. Benign — not a missing setting. Deliberately
    /// separate from `covered_by`, which forces read-only: this row stays
    /// editable, because setting it is still the simple path.
    pub covered_by_tasks: bool,
    /// This TASK row is unset while its parent IS configured — the mirror
    /// of `covered_by_tasks`, and the shape a fresh install has (the seed
    /// writes `output.image` alone). The parent answers it, so it is not
    /// unconfigured in effect and must not be painted as a gap.
    pub inherits_broad: bool,
}

impl RouteRow {
    pub fn from_value(v: &Value) -> Option<RouteRow> {
        let key = s(v, "key")?;
        Some(RouteRow {
            kind: s(v, "kind").unwrap_or_default(),
            modality: s(v, "modality").unwrap_or_default(),
            task: {
                // The write path is /{kind}/{modality}[/{task}] where task
                // is the OPTIONAL third key segment — derive it from the
                // key, not from the descriptive `task` field (input.text
                // carries task="text_understanding" but writes to
                // /input/text).
                let segs: Vec<&str> = key.split('.').collect();
                if segs.len() >= 3 {
                    Some(segs[2..].join("."))
                } else {
                    None
                }
            },
            label: s(v, "label").unwrap_or_else(|| key.clone()),
            configured: b(v, "configured").unwrap_or(false),
            source: s(v, "source").unwrap_or_default(),
            provider: s(v, "provider").filter(|p| !p.is_empty()),
            model: s(v, "model").filter(|m| !m.is_empty()),
            base_url: s(v, "base_url").filter(|u| !u.is_empty()),
            options: v.get("options").filter(|o| !o.is_null()).cloned(),
            covered_by: s(v, "covered_by"),
            read_only: b(v, "read_only").unwrap_or(false),
            overrideable: b(v, "overrideable").unwrap_or(false),
            derived_from: s(v, "derived_from").filter(|d| !d.is_empty()),
            package_hint: s(v, "package_hint"),
            reasoning: s(v, "reasoning").filter(|r| !r.is_empty()),
            broad_key: s(v, "broad_key").filter(|k| !k.is_empty()),
            task_keys: v
                .get("task_keys")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|k| k.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default(),
            covered_by_tasks: b(v, "covered_by_tasks").unwrap_or(false),
            inherits_broad: b(v, "inherits_broad").unwrap_or(false),
            key,
        })
    }

    /// This row is a modality cell that has task rows overriding it.
    pub fn is_task_parent(&self) -> bool {
        !self.task_keys.is_empty()
    }

    /// The ROUTE column text: a task row shows only its task segment,
    /// under a tree marker, so the grid reads as the hierarchy it is.
    /// The full key stays available on the detail line and in every
    /// write path — and dropping the repeated `output.image.` prefix
    /// gives the column back ~13 cells rather than eating them.
    pub fn display_key(&self) -> String {
        match self.broad_key.as_deref() {
            Some(parent) if self.key.len() > parent.len() + 1 => {
                format!("  └ {}", &self.key[parent.len() + 1..])
            }
            _ => self.key.clone(),
        }
    }

    /// Can this row be edited at all? Derived rows (output.text) are
    /// read-only; covered rows are editable only when overrideable.
    /// THE SAME BODY as the AbstractCore console's `RouteRow::editable`
    /// — one editability law across both entry points.
    pub fn editable(&self) -> bool {
        if self.derived_from.is_some() {
            return false;
        }
        if self.covered_by.is_some() {
            return self.overrideable && !self.read_only;
        }
        !self.read_only
    }

    /// THE shared state vocabulary, one string per row state — the
    /// same four words the AbstractCore console's routes table prints,
    /// so an operator reads one grid whichever entry point they opened.
    pub fn state_label(&self) -> String {
        if let Some(from) = self.derived_from.as_deref() {
            return format!("derived ← {from}");
        }
        if let Some(by) = self.covered_by.as_deref() {
            return format!("covered by {by}");
        }
        if self.configured {
            return "configured".to_string();
        }
        // AN UNSET PARENT WHOSE TASK ROWS ARE ALL SET IS NOT A PROBLEM.
        // Core proves it (`capability_route_tasks_cover_broad`): the task
        // rows are exactly the keys the output route table can produce
        // for that modality, so nothing can reach the parent. Printing
        // "not configured" there sent an operator hunting for dead code
        // ("why do we have output.image AND t2i/i2i/upscale?").
        if self.covered_by_tasks {
            return "not needed".to_string();
        }
        // ...and the MIRROR: a task row with no value of its own whose
        // parent IS set is answered by that parent. A fresh install is
        // exactly this shape (the seed writes `output.image` alone), so
        // three red "not configured" rows used to sit under a working
        // parent and read as "image editing is not set up".
        if self.inherits_broad {
            return "inherited".to_string();
        }
        "not configured".to_string()
    }

    /// The reasoning effort is a property of TEXT GENERATION:
    /// `output.text` is the canonical route and `input.text` is where
    /// the store keeps it, so the control belongs on both cells and
    /// nowhere else (console.py `isTextGenerationDefault`).
    pub fn is_text_generation(&self) -> bool {
        self.key == "output.text" || self.key == "input.text"
    }

    /// "provider / model" as the store currently answers it, or the
    /// honest absence.
    pub fn pair_text(&self) -> String {
        match (self.provider.as_deref(), self.model.as_deref()) {
            (None, None) => "—".to_string(),
            (p, m) => format!("{} / {}", p.unwrap_or("—"), m.unwrap_or("—")),
        }
    }
}

/// The shared reasoning vocabulary: the effort levels the web console
/// offers, index 0 = "not set" (the placeholder that clears). Both
/// consoles offer exactly this list, in this order.
///
/// NOT the engine's `ReasoningSelect` (abstracttui 0.3.0), deliberately.
/// That control is the FOOTER/per-request picker: its ladder adds
/// `none`/`xhigh`/`auto`, and it is capability-driven — without a
/// `ReasoningFacts` block it renders LOCKED behind a "set anyway" gate.
/// Neither console has per-model capability facts for a route, and a
/// locked control on the screen whose entire job is setting this
/// default would be a dead one. The reference surface for a stored
/// DEFAULT is the web console's select, and this is that list verbatim.
pub const REASONING_LEVELS: [&str; 4] = ["minimal", "low", "medium", "high"];

/// Select index for a stored reasoning value — 0 ("not set") when the
/// row carries none, or one the list does not offer (never fabricate a
/// selection the store does not hold).
pub fn reasoning_index(stored: Option<&str>) -> usize {
    stored
        .map(str::trim)
        .filter(|r| !r.is_empty())
        .and_then(|r| {
            REASONING_LEVELS
                .iter()
                .position(|l| l.eq_ignore_ascii_case(r))
        })
        .map(|i| i + 1)
        .unwrap_or(0)
}

#[derive(Clone, Debug, Default)]
pub struct RoutesData {
    pub ok: bool,
    pub writable: bool,
    pub authority: String,
    pub rows: Vec<RouteRow>,
    pub errors: Vec<String>,
}

impl RoutesData {
    pub fn from_value(v: &Value) -> RoutesData {
        RoutesData {
            ok: b(v, "ok").unwrap_or(false),
            writable: b(v, "writable").unwrap_or(false),
            authority: s(v, "authority").unwrap_or_default(),
            rows: rows_from(v, &["routes"], RouteRow::from_value),
            errors: v
                .get("errors")
                .and_then(Value::as_array)
                .map(|a| a.iter().map(err_text).collect())
                .unwrap_or_default(),
        }
    }
}

// ---------------------------------------------------------------------
// WEIGHTS. A route can be perfectly configured and still unrunnable
// because the model is not on the execution host. `/models/availability`
// answers that in the SAME four words the AbstractCore console and the
// gateway web console print — installed / absent / unknown /
// not_applicable — and `unknown` is a real answer that must never be
// shown as either of its neighbours.
// ---------------------------------------------------------------------

/// One route's weight availability plus the artifact that would fetch
/// it. The ARTIFACT is not the route's model: a served id drops the
/// quantization suffix, so `input.text` stores `qwen/qwen3.5-9b` while
/// the download reference is `qwen/qwen3.5-9b@4bit`.
#[derive(Clone, Debug, Default)]
pub struct WeightsRow {
    pub status: String,
    pub provider: String,
    pub artifact: String,
    pub detail: String,
    pub instruction: String,
    pub downloadable: bool,
}

impl WeightsRow {
    pub fn label(&self) -> &str {
        match self.status.as_str() {
            "installed" => "installed",
            "absent" => "not downloaded",
            "not_applicable" => "remote",
            "unknown" => "unknown",
            _ => "",
        }
    }
}

/// `GET /api/gateway/models/availability`, folded for the routes screen.
#[derive(Clone, Debug, Default)]
pub struct AvailabilityData {
    pub by_route: HashMap<String, WeightsRow>,
    pub total: usize,
    pub installed: usize,
    pub absent: usize,
    pub unknown: usize,
    /// `(route, provider, artifact)` of every recommended model that is
    /// absent AND whose route has nothing else serving it — the gateway's
    /// `recommended.gaps`, never the raw `would_download`.
    ///
    /// THE STARTER KIT IS ADVICE FOR AN EMPTY ROUTE, NOT A STANDING DEBT.
    /// `would_download` answers "is the fresh-install model on this disk?",
    /// so a host whose operator routed `input.text` at their own model
    /// reported a missing model it would never install and could never
    /// clear. The gateway makes that judgement once (`core_config.
    /// _mark_recommended_route_gaps`) so this screen and the web console
    /// cannot disagree about which host is actually short of something.
    pub missing: Vec<(String, String, String)>,
}

impl AvailabilityData {
    pub fn from_value(v: &Value) -> AvailabilityData {
        let mut by_route = HashMap::new();
        for row in v
            .get("routes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(key) = s(row, "key").filter(|k| !k.is_empty()) else {
                continue;
            };
            let a = row.get("availability").cloned().unwrap_or(Value::Null);
            // An unconfigured route reports `unknown` with this evidence;
            // it is NOT a missing download and must not be offered as one.
            if s(&a, "evidence").as_deref() == Some("route not configured") {
                continue;
            }
            by_route.insert(
                key,
                WeightsRow {
                    status: s(&a, "status").unwrap_or_default(),
                    provider: s(row, "provider").unwrap_or_default(),
                    artifact: s(row, "download_artifact")
                        .or_else(|| s(&a, "artifact"))
                        .or_else(|| s(row, "model"))
                        .unwrap_or_default(),
                    detail: s(&a, "detail").unwrap_or_default(),
                    instruction: s(&a, "instruction").unwrap_or_default(),
                    downloadable: b(&a, "downloadable").unwrap_or(false),
                },
            );
        }
        let plan = v.get("recommended").cloned().unwrap_or(Value::Null);
        let n = |key: &str| plan.get(key).and_then(Value::as_u64).unwrap_or(0) as usize;
        // `gaps` when the gateway offers it; `would_download` only as the
        // fallback for a gateway that predates it. An EMPTY `gaps` array is an
        // answer ("nothing to do") and must not fall through to the raw list.
        let missing = plan
            .get("gaps")
            .or_else(|| plan.get("would_download"))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|item| {
                        Some((
                            s(item, "route").unwrap_or_default(),
                            s(item, "provider")?,
                            s(item, "artifact")?,
                        ))
                    })
                    .collect()
            })
            .unwrap_or_default();
        AvailabilityData {
            by_route,
            total: n("total"),
            installed: n("installed"),
            absent: n("absent"),
            unknown: n("unknown"),
            missing,
        }
    }
}

/// The LIVE status line of one download job, refreshed by the worker as
/// it polls `/models/download/{job}`.
#[derive(Clone, Debug, Default)]
pub struct DownloadStatus {
    pub job: String,
    pub provider: String,
    pub artifact: String,
    pub status: String,
    pub message: String,
    pub percent: Option<f64>,
    pub elapsed_s: f64,
}

impl DownloadStatus {
    pub fn from_job(v: &Value) -> DownloadStatus {
        DownloadStatus {
            job: s(v, "job").unwrap_or_default(),
            provider: s(v, "provider").unwrap_or_default(),
            artifact: s(v, "artifact").unwrap_or_default(),
            status: s(v, "status").unwrap_or_default(),
            message: s(v, "message").unwrap_or_default(),
            percent: v.get("percent").and_then(Value::as_f64),
            elapsed_s: v.get("elapsed_s").and_then(Value::as_f64).unwrap_or(0.0),
        }
    }

    pub fn running(&self) -> bool {
        self.status == "running"
    }

    /// Build one status row directly — the headless harness's way to
    /// stage a running download without a gateway on the other end.
    #[allow(clippy::too_many_arguments)]
    pub fn from_value_for_test(
        job: &str,
        provider: &str,
        artifact: &str,
        status: &str,
        message: &str,
        percent: Option<f64>,
        elapsed_s: f64,
    ) -> DownloadStatus {
        DownloadStatus {
            job: job.to_string(),
            provider: provider.to_string(),
            artifact: artifact.to_string(),
            status: status.to_string(),
            message: message.to_string(),
            percent,
            elapsed_s,
        }
    }

    pub fn line(&self) -> String {
        let pct = match self.percent {
            Some(p) => format!(" {p:.0}%"),
            None => String::new(),
        };
        format!(
            "{} {}{} — {} ({:.0}s)",
            self.provider, self.artifact, pct, self.message, self.elapsed_s
        )
    }
}

fn err_text(v: &Value) -> String {
    v.as_str()
        .map(str::to_string)
        .unwrap_or_else(|| v.to_string())
}

#[derive(Clone, Debug)]
pub struct UserRow {
    pub user_id: String,
    pub tenant_id: String,
    pub email: String,
    pub roles: Vec<String>,
    pub enabled: bool,
    pub runtime_id: String,
    pub created_at: String,
    /// First-class kind from the gateway (c5308 contract:
    /// `principal_kind: "human"|"entity"` on every /admin/users row).
    /// `None` = pre-contract gateway; the partition falls back to the
    /// documented roles convention.
    pub principal_kind: Option<String>,
}

impl UserRow {
    pub fn from_value(v: &Value) -> Option<UserRow> {
        Some(UserRow {
            user_id: s(v, "user_id")?,
            tenant_id: s(v, "tenant_id").unwrap_or_else(|| "default".into()),
            email: s(v, "email").unwrap_or_default(),
            roles: str_list(v, "roles"),
            enabled: b(v, "enabled").unwrap_or(true),
            runtime_id: s(v, "runtime_id").unwrap_or_default(),
            created_at: s(v, "created_at").unwrap_or_default(),
            principal_kind: s(v, "principal_kind"),
        })
    }

    /// The kind partition, contract-first: `principal_kind` is the ONE
    /// source when the gateway serves it (c5308 — clients never
    /// re-derive from roles); the roles convention remains the labeled
    /// #FALLBACK for pre-contract gateways only.
    pub fn is_entity_principal(&self) -> bool {
        match self.principal_kind.as_deref() {
            Some(k) => k.eq_ignore_ascii_case("entity"),
            None => self.roles.iter().any(|r| r == "entity"),
        }
    }
}

/// The users payload, PARTITIONED at the fold (operator complaint
/// 2026-07-25: "both lists look completely conflated"). The gateway
/// deliberately registers entities as authenticated principals
/// (roles=["entity"], GW-H design), so /admin/users returns BOTH kinds
/// — and every gateway presentation surface partitions on that role
/// (the web console filters them out of its users table with a count
/// note; this console was the one outlier). Fold-level, not
/// view-level: a view-only filter would desync table indices from the
/// selection, and rotate/delete would target a DIFFERENT row than the
/// highlighted one.
#[derive(Clone, Debug, Default)]
pub struct UsersData {
    /// Human principals — the only rows the users table renders and
    /// the only targets edit/rotate/delete can reach (entity
    /// principals are managed through the entities lane, never here).
    pub humans: Vec<UserRow>,
    /// Count of role=entity principals hidden from the table (they
    /// live in the Entities panel).
    pub entity_principals: usize,
}

pub fn users_from_payload(v: &Value) -> UsersData {
    let all = rows_from(v, &["users"], UserRow::from_value);
    let (entities, humans): (Vec<UserRow>, Vec<UserRow>) =
        all.into_iter().partition(UserRow::is_entity_principal);
    UsersData {
        humans,
        entity_principals: entities.len(),
    }
}

#[derive(Clone, Debug)]
pub struct EntityRow {
    pub name: String,
    /// "awake" | "asleep" | "paused" — the operator-state vocabulary.
    pub state: String,
    /// resting/dreaming/visiting when present — secondary text.
    pub mode: Option<String>,
    pub handle: Option<String>,
    pub open_questions: Option<u64>,
    pub open_problems: Option<u64>,
    pub open_interests: Option<u64>,
}

impl EntityRow {
    pub fn from_value(v: &Value) -> Option<EntityRow> {
        let name = s(v, "name")?;
        let state_obj = v.get("state");
        let drives = v.get("drives");
        let drive = |k: &str, open: &str| -> Option<u64> { drives?.get(k)?.get(open)?.as_u64() };
        Some(EntityRow {
            state: state_obj
                .and_then(|st| s(st, "state"))
                .unwrap_or_else(|| "unknown".into()),
            mode: state_obj.and_then(|st| s(st, "mode")),
            handle: s(v, "handle"),
            open_questions: drive("questions", "open"),
            open_problems: drive("problems", "open"),
            open_interests: drive("interests", "open"),
            name,
        })
    }
}

pub fn entities_from_payload(v: &Value) -> Vec<EntityRow> {
    rows_from(v, &["entities"], EntityRow::from_value)
}

/// One registered workflow. A row is a BUNDLE, not a version: "how many
/// versions" is the question this panel exists to answer, and a count has to
/// be a column.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct WorkflowRow {
    pub bundle_id: String,
    pub scope: String,
    pub published: usize,
    pub draft: usize,
    pub latest: String,
    pub entrypoints: usize,
    pub deprecated: bool,
    /// (version, channel, created_at, entrypoint count) newest first.
    pub versions: Vec<(String, String, String, usize)>,
    /// (flow_id, workflow_id, interfaces) of the newest version.
    pub flows: Vec<(String, String, String)>,
}

/// A bundle file on disk the gateway is NOT serving, and why. Kept visible:
/// the file still exists and still needs a decision.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct WorkflowSkipRow {
    pub bundle_id: String,
    pub bundle_version: String,
    pub reason: String,
    pub path: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct WorkflowsData {
    pub rows: Vec<WorkflowRow>,
    pub skipped: Vec<WorkflowSkipRow>,
    pub default_bundle_id: String,
    /// (interface, workflow_id) of the gateway default agent workflows
    /// (`default_agent_workflows`) — a different thing from
    /// `default_bundle_id`.
    pub agent_defaults: Vec<(String, String)>,
}

pub fn workflows_from_payload(v: &serde_json::Value) -> WorkflowsData {
    use std::collections::BTreeMap;
    let mut by_id: BTreeMap<String, WorkflowRow> = BTreeMap::new();
    for it in v
        .get("items")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default()
    {
        let bid = it
            .get("bundle_id")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
        if bid.is_empty() {
            continue;
        }
        let ver = it
            .get("bundle_version")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
        let is_draft = it
            .get("is_draft")
            .and_then(|x| x.as_bool())
            .unwrap_or(false);
        let channel = it
            .get("version_channel")
            .and_then(|x| x.as_str())
            .unwrap_or(if is_draft { "draft" } else { "published" })
            .to_string();
        let created = it
            .get("created_at")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
        let eps = it
            .get("entrypoints")
            .and_then(|x| x.as_array())
            .cloned()
            .unwrap_or_default();

        let row = by_id.entry(bid.clone()).or_insert_with(|| WorkflowRow {
            bundle_id: bid.clone(),
            scope: it
                .get("registry_scope")
                .and_then(|x| x.as_str())
                .unwrap_or("private")
                .to_string(),
            ..Default::default()
        });
        if is_draft {
            row.draft += 1;
        } else {
            row.published += 1;
        }
        row.entrypoints = row.entrypoints.max(eps.len());
        if row.latest.is_empty() {
            row.latest = it
                .get("latest_published_version")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string();
        }
        if eps.iter().any(|e| {
            e.get("deprecated")
                .and_then(|x| x.as_bool())
                .unwrap_or(false)
        }) {
            row.deprecated = true;
        }
        if row.flows.is_empty() {
            row.flows = eps
                .iter()
                .map(|e| {
                    (
                        e.get("flow_id")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_string(),
                        e.get("workflow_id")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_string(),
                        e.get("interfaces")
                            .and_then(|x| x.as_array())
                            .map(|a| {
                                a.iter()
                                    .filter_map(|i| i.as_str())
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            })
                            .unwrap_or_default(),
                    )
                })
                .collect();
        }
        row.versions.push((ver, channel, created, eps.len()));
    }
    let mut rows: Vec<WorkflowRow> = by_id.into_values().collect();
    for r in rows.iter_mut() {
        r.versions.sort_by(|a, b| b.0.cmp(&a.0));
    }
    rows.sort_by(|a, b| a.bundle_id.cmp(&b.bundle_id));

    let skipped = v
        .get("skipped")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|r| WorkflowSkipRow {
            bundle_id: r
                .get("bundle_id")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            bundle_version: r
                .get("bundle_version")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            reason: r
                .get("reason")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            path: r
                .get("path")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
        })
        .collect();

    WorkflowsData {
        rows,
        skipped,
        default_bundle_id: v
            .get("default_bundle_id")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string(),
        agent_defaults: v
            .get("default_agent_workflows")
            .and_then(|x| x.as_object())
            .map(|m| {
                m.iter()
                    .map(|(iface, row)| {
                        (
                            iface.clone(),
                            row.get("workflow_id")
                                .and_then(|x| x.as_str())
                                .unwrap_or("")
                                .to_string(),
                        )
                    })
                    .collect()
            })
            .unwrap_or_default(),
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeRow {
    pub kind: String,
    pub tenant_id: String,
    pub runtime_id: String,
    pub label: String,
    pub owners: Vec<String>,
    /// The plane's on-disk data dir (served by /admin/runtimes) — the
    /// Data tab's anchor fact and the key that attributes registered
    /// data-home stores to planes (longest path-boundary prefix).
    pub data_dir: String,
    pub size_bytes: Option<u64>,
    /// e.g. "#TRUNCATION size is a floor (walk capped)" — when present
    /// the size renders as "≥ …", never as an exact fact.
    pub size_note: Option<String>,
    pub state: Option<String>,
    pub liveness: Option<String>,
    pub note: Option<String>,
}

impl RuntimeRow {
    pub fn from_value(v: &Value) -> Option<RuntimeRow> {
        Some(RuntimeRow {
            kind: s(v, "kind")?,
            tenant_id: s(v, "tenant_id").unwrap_or_default(),
            runtime_id: s(v, "runtime_id").unwrap_or_default(),
            label: s(v, "label").unwrap_or_default(),
            owners: v
                .get("owners")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(|o| s(o, "user_id")).collect())
                .unwrap_or_default(),
            data_dir: s(v, "data_dir").unwrap_or_default(),
            size_bytes: v.get("size_bytes").and_then(Value::as_u64),
            size_note: s(v, "size_note").filter(|n| !n.is_empty()),
            state: s(v, "state"),
            liveness: s(v, "liveness"),
            note: s(v, "note"),
        })
    }
}

pub fn runtimes_from_payload(v: &Value) -> Vec<RuntimeRow> {
    rows_from(v, &["runtimes"], RuntimeRow::from_value)
}

/// Which plane owns a data-home row: the runtime whose `data_dir` is
/// the LONGEST path-boundary prefix of the home's path. Longest wins
/// because user/entity dirs NEST under the default plane's root — a
/// naive prefix match would attribute every per-user store to the
/// default plane. `None` = the home lives outside every plane (a
/// process-wide shared cache like ~/.abstractcore) — those surface
/// only on the default plane's Data tab, labeled shared.
pub fn home_plane_index(planes: &[RuntimeRow], home_path: &str) -> Option<usize> {
    let mut best: Option<(usize, usize)> = None; // (plane idx, dir len)
    for (i, p) in planes.iter().enumerate() {
        let d = p.data_dir.trim_end_matches('/');
        if d.is_empty() {
            continue;
        }
        // Path-BOUNDARY prefix: "/a/b" owns "/a/b" and "/a/b/c", never
        // "/a/bc" (a bare starts_with would).
        let owns = home_path == d
            || home_path
                .strip_prefix(d)
                .is_some_and(|rest| rest.starts_with('/'));
        if owns && best.is_none_or(|(_, l)| d.len() > l) {
            best = Some((i, d.len()));
        }
    }
    best.map(|(i, _)| i)
}

/// One probe's outcome, timestamped and numbered — the acknowledgment
/// record for the Probe action. A re-probe that lands on the SAME
/// connection state still mints a new report (seq + time change), so
/// pressing the button is never visually silent (operator incident
/// 2026-07-23: "nothing happens" while a millisecond re-probe resolved
/// back to connected).
#[derive(Clone, Debug)]
pub struct ProbeReport {
    pub seq: u64,
    /// UTC HH:MM:SSZ.
    pub at: String,
    pub ok: bool,
    /// One-line outcome ("connected as admin@default (users)" / the
    /// failure headline).
    pub outcome: String,
    pub took_ms: u64,
}

/// One mode row of `GET /network` (`gateway_network_v1.modes[]`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct NetworkMode {
    pub id: String,
    pub label: String,
    pub selected: bool,
    pub allowed: bool,
    /// Why the mode is refused today, and the exact fix (auth).
    pub reason: Option<String>,
    pub fix: Option<String>,
}

/// One address a client can use (`gateway_network_v1.addresses[]`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct NetworkAddress {
    /// loopback | lan | hostname | public
    pub kind: String,
    pub url: String,
    /// "Wi-Fi", "Ethernet", "VPN", else the interface name.
    pub label: String,
    /// true = the gateway listens there now; false = not yet (the
    /// mode/bind); None = unknowable from here (WAN).
    pub reachable: Option<bool>,
    pub note: String,
}

/// The network exposure surface (`GET /api/gateway/network`): who can
/// reach the gateway (configured vs running), the restart story, the
/// auth verdict and every address to copy.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct NetworkData {
    pub writable: bool,
    pub configured_mode: String,
    pub configured_label: String,
    pub configured_port: u64,
    pub configured_source: String,
    pub effective_mode: String,
    pub effective_label: String,
    pub effective_bind: String,
    pub effective_port: Option<u64>,
    pub overridden_by_cli: bool,
    pub restart_required: bool,
    pub restart_applies: bool,
    pub restart_available: bool,
    /// Why a restart cannot apply the setting / cannot be done here.
    pub restart_reason: Option<String>,
    pub restart_how: String,
    pub auth_ok: bool,
    pub auth_fix: Option<String>,
    pub modes: Vec<NetworkMode>,
    pub addresses: Vec<NetworkAddress>,
    pub copy_hint: String,
    pub warnings: Vec<String>,
    /// Reverse proxy (`reverse_proxy`, mission Z): the stored origins the
    /// edit line changes, where the winning value comes from
    /// (setting | env | default), whether the environment the gateway was
    /// started with overrides it, and what the middleware applies now.
    pub proxy: ReverseProxyView,
}

/// `gateway_network_v1.reverse_proxy` flattened for the panel.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReverseProxyView {
    /// The payload carried the block (an older gateway does not).
    pub present: bool,
    pub origins: Vec<String>,
    pub origins_source: String,
    pub origins_overridden: bool,
    pub origins_env_name: String,
    pub origins_env_value: Vec<String>,
    pub origins_effective: Vec<String>,
    pub origins_warnings: Vec<String>,
    pub trust_proxy: bool,
    pub trust_source: String,
    pub trust_overridden: bool,
    pub trust_env_name: String,
    pub trust_effective: bool,
}

impl ReverseProxyView {
    pub fn from_value(v: Option<&Value>) -> ReverseProxyView {
        let Some(rp) = v.filter(|x| x.is_object()) else {
            return ReverseProxyView::default();
        };
        let null = Value::Null;
        let o = rp.get("allowed_origins").unwrap_or(&null);
        let t = rp.get("trust_proxy").unwrap_or(&null);
        let strs = |x: &Value, k: &str| -> Vec<String> {
            x.get(k)
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
                .unwrap_or_default()
        };
        let st = |x: &Value, k: &str| x.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let bo = |x: &Value, k: &str| x.get(k).and_then(Value::as_bool).unwrap_or(false);
        ReverseProxyView {
            present: o.is_object() && t.is_object(),
            origins: strs(o, "value"),
            origins_source: st(o, "source"),
            origins_overridden: bo(o, "overridden_by_env"),
            origins_env_name: st(o, "env_name"),
            origins_env_value: strs(o, "env_value"),
            origins_effective: strs(o, "effective"),
            origins_warnings: strs(o, "warnings"),
            trust_proxy: bo(t, "value"),
            trust_source: st(t, "source"),
            trust_overridden: bo(t, "overridden_by_env"),
            trust_env_name: st(t, "env_name"),
            trust_effective: bo(t, "effective"),
        }
    }
}

impl NetworkData {
    pub fn from_value(v: &Value) -> NetworkData {
        let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let b = |v: &Value, k: &str| v.get(k).and_then(Value::as_bool).unwrap_or(false);
        let opt = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).map(str::to_string);
        let null = Value::Null;
        let c = v.get("configured").unwrap_or(&null);
        let e = v.get("effective").unwrap_or(&null);
        let r = v.get("restart").unwrap_or(&null);
        let a = v.get("auth").unwrap_or(&null);
        let modes = v
            .get("modes")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .map(|m| NetworkMode {
                        id: s(m, "id"),
                        label: s(m, "label"),
                        selected: b(m, "selected"),
                        allowed: b(m, "allowed"),
                        reason: opt(m, "reason"),
                        fix: opt(m, "fix"),
                    })
                    .collect()
            })
            .unwrap_or_default();
        let addresses = v
            .get("addresses")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter(|x| x.get("url").and_then(Value::as_str).is_some())
                    .map(|x| NetworkAddress {
                        kind: s(x, "kind"),
                        url: s(x, "url"),
                        label: opt(x, "interface_label")
                            .or_else(|| opt(x, "interface"))
                            .unwrap_or_default(),
                        reachable: x.get("reachable").and_then(Value::as_bool),
                        note: s(x, "note"),
                    })
                    .collect()
            })
            .unwrap_or_default();
        NetworkData {
            writable: b(v, "writable"),
            configured_mode: s(c, "mode"),
            configured_label: s(c, "label"),
            configured_port: c.get("port").and_then(Value::as_u64).unwrap_or(0),
            configured_source: s(c, "source"),
            effective_mode: s(e, "mode"),
            effective_label: s(e, "label"),
            effective_bind: s(e, "bind_host"),
            effective_port: e.get("port").and_then(Value::as_u64),
            overridden_by_cli: b(e, "overridden_by_cli"),
            restart_required: b(v, "restart_required"),
            restart_applies: b(r, "applies"),
            restart_available: b(r, "available"),
            restart_reason: opt(r, "reason").or_else(|| opt(r, "unavailable_reason")),
            restart_how: s(r, "how"),
            auth_ok: b(a, "ok_for_mode"),
            auth_fix: opt(a, "fix"),
            modes,
            addresses,
            copy_hint: s(v, "copy_hint"),
            proxy: ReverseProxyView::from_value(v.get("reverse_proxy")),
            warnings: v
                .get("warnings")
                .and_then(Value::as_array)
                .map(|w| {
                    w.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
        }
    }
}

/// The runtime knobs surface: per-knob value + provenance (which layer
/// set it), rendered read-only in v1 — the web console's Runtimes tab
/// parity surface.
#[derive(Clone, Debug, Default)]
pub struct RuntimeConfigData {
    pub writable: bool,
    pub workspace_root: String,
    pub workspace_root_source: String,
    pub workspace_allowed_paths: String,
    pub workspace_allowed_paths_source: String,
    pub workspace_blocked_paths: String,
    pub workspace_blocked_paths_source: String,
    pub client_workspace_scope_overrides: bool,
    pub client_workspace_scope_overrides_source: String,
    /// Launch-folder trust (default true server-side): an agent may write
    /// in the folder it was started from.
    pub trust_client_launch_folder: bool,
    pub trust_client_launch_folder_source: String,
    /// Gateway default posture: "whitelist" (deny all, allow listed) or
    /// "blacklist" (allow all, refuse listed) — what users inherit.
    pub workspace_default_mode: String,
    pub workspace_default_mode_source: String,
    /// Per-user policy overrides as pretty JSON ("" = none) — edited as
    /// text; the gateway deep-validates on save.
    pub user_workspace_policies: String,
    pub user_workspace_policies_source: String,
    /// (knob, rendered value, source) — enumerated from the payload,
    /// never a hardcoded knob list (the gateway grows knobs).
    pub knobs: Vec<(String, String, String)>,
    /// Executor rows: "codex (default)" style, availability-honest.
    pub executors: Vec<String>,
    /// Browser-apps settings (`apps.<name>`, mission Z), enumerated from
    /// the payload's `apps` registry — never a hardcoded list.
    pub apps: Vec<AppsSetting>,
    /// Default agent workflow per agent interface
    /// (`agents.default_workflow.<interface>`), in the gateway's order.
    pub agent_defaults: Vec<AgentDefault>,
    /// The skills shelf (`skills.shelf`); None when the gateway does not
    /// report it.
    pub skills_shelf: Option<SkillsShelf>,
    /// `agents.streaming_default`; None when this gateway's read lacks it
    /// (the knob row then says "not available on this gateway").
    pub streaming_default: Option<StreamingDefault>,
}

/// `agents.streaming_default`: whether interactive runs that do not ask
/// either way stream their replies live.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct StreamingDefault {
    pub key: String,
    pub value: bool,
    /// stored | default
    pub source: String,
    pub label: String,
    pub help: String,
}

/// Parse `agents.streaming_default` ({key, value: bool, source, label,
/// help}) of GET /admin/runtime-config. A missing block or a non-boolean
/// value is None — never guessed as "off".
pub fn streaming_default_from(v: &Value) -> Option<StreamingDefault> {
    let r = v.get("agents")?.get("streaming_default")?;
    let value = r.get("value")?.as_bool()?;
    Some(StreamingDefault {
        key: s(r, "key").unwrap_or_else(|| "agents.streaming_default".into()),
        value,
        source: s(r, "source").unwrap_or_else(|| "?".into()),
        label: s(r, "label").unwrap_or_else(|| "Stream replies by default".into()),
        help: s(r, "help").unwrap_or_default(),
    })
}

/// `skills.shelf`: where the gateway reads skills from.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SkillsShelf {
    /// The saved/environment value ("" when the gateway's own copy is used).
    pub value: String,
    /// stored | env | seeded | checkout | none
    pub source: String,
    pub resolved: String,
    pub available: bool,
    pub reason: String,
    pub default_path: String,
    pub bundled_version: String,
    pub warnings: Vec<String>,
}

pub fn skills_shelf_from(v: &Value) -> Option<SkillsShelf> {
    let r = v.get("skills")?.get("shelf")?;
    if !r.is_object() {
        return None;
    }
    Some(SkillsShelf {
        value: s(r, "value").unwrap_or_default(),
        source: s(r, "source").unwrap_or_else(|| "?".into()),
        resolved: s(r, "resolved").unwrap_or_default(),
        available: b(r, "available").unwrap_or(false),
        reason: s(r, "reason").unwrap_or_default(),
        default_path: s(r, "default_path").unwrap_or_default(),
        bundled_version: s(r, "bundled_version").unwrap_or_default(),
        warnings: r
            .get("warnings")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|w| w.as_str().map(str::to_string)).collect())
            .unwrap_or_default(),
    })
}

/// One `agents.default_workflow.<interface>` row: what "Gateway default"
/// runs for that interface, or why it cannot.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AgentDefault {
    pub interface: String,
    pub key: String,
    /// The saved/built-in value (`[catalog:]bundle[@ver]:flow`), "" = none.
    pub value: String,
    /// stored | default
    pub source: String,
    pub available: bool,
    pub reason: String,
    /// Exact workflow id it runs now ("" when unavailable).
    pub workflow_id: String,
    pub name: String,
    /// The built-in default value ("" = none).
    pub builtin: String,
    /// Values an admin can choose (entrypoints declaring the interface).
    pub eligible: Vec<String>,
}

/// Parse `agents.default_workflow` of GET /admin/runtime-config.
pub fn agent_defaults_from(v: &Value) -> Vec<AgentDefault> {
    let Some(map) = v
        .get("agents")
        .and_then(|a| a.get("default_workflow"))
        .and_then(Value::as_object)
    else {
        return Vec::new();
    };
    map.iter()
        .map(|(iface, row)| {
            let resolved = row.get("resolved").filter(|r| r.is_object());
            AgentDefault {
                interface: iface.clone(),
                key: s(row, "key").unwrap_or_else(|| format!("agents.default_workflow.{iface}")),
                value: s(row, "value").unwrap_or_default(),
                source: s(row, "source").unwrap_or_else(|| "?".into()),
                available: b(row, "available").unwrap_or(false),
                reason: s(row, "reason").unwrap_or_default(),
                workflow_id: resolved.and_then(|r| s(r, "workflow_id")).unwrap_or_default(),
                name: resolved.and_then(|r| s(r, "name")).unwrap_or_default(),
                builtin: s(row, "default").unwrap_or_default(),
                eligible: row
                    .get("eligible")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(|e| s(e, "value")).collect())
                    .unwrap_or_default(),
            }
        })
        .collect()
}

/// One `apps.<name>` runtime-config setting (label/help from the gateway).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppsSetting {
    pub name: String,
    pub key: String,
    pub label: String,
    pub help: String,
    pub placeholder: String,
    /// The value in effect ("" = none, e.g. ports on their default).
    pub value: String,
    /// stored | env | default
    pub source: String,
    pub note: String,
    /// An invalid stored/env value the gateway set aside (it says why).
    pub invalid: String,
}

impl RuntimeConfigData {
    pub fn from_value(v: &Value) -> RuntimeConfigData {
        let mut knobs = Vec::new();
        let mut workspace_root = String::new();
        let mut workspace_root_source = "default".to_string();
        let mut workspace_allowed_paths = String::new();
        let mut workspace_allowed_paths_source = "default".to_string();
        let mut workspace_blocked_paths = String::new();
        let mut workspace_blocked_paths_source = "default".to_string();
        let mut client_workspace_scope_overrides = false;
        let mut client_workspace_scope_overrides_source = "default".to_string();
        let mut trust_client_launch_folder = true;
        let mut trust_client_launch_folder_source = "default".to_string();
        let mut workspace_default_mode = "whitelist".to_string();
        let mut workspace_default_mode_source = "default".to_string();
        let mut user_workspace_policies = String::new();
        let mut user_workspace_policies_source = "default".to_string();
        if let Some(obj) = v.as_object() {
            for (key, val) in obj {
                // Knobs are the {value, source} objects; other keys
                // (writable, executors) render separately.
                let (Some(value), Some(source)) = (val.get("value"), val.get("source")) else {
                    continue;
                };
                if key == "workspace_root" {
                    workspace_root = match value {
                        Value::Null => String::new(),
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    };
                    workspace_root_source = source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                if key == "workspace_mounts" {
                    let mounts_text = match value {
                        Value::Null => String::new(),
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    };
                    if workspace_allowed_paths.trim().is_empty() {
                        workspace_allowed_paths = mounts_text
                            .lines()
                            .map(|line| line.trim())
                            .filter(|line| !line.is_empty())
                            .map(|line| {
                                line.split_once('=')
                                    .map(|(_, path)| path.trim())
                                    .unwrap_or(line)
                            })
                            .collect::<Vec<_>>()
                            .join("\n");
                        workspace_allowed_paths_source = source.as_str().unwrap_or("?").to_string();
                    }
                    continue;
                }
                if key == "workspace_allowed_paths" {
                    workspace_allowed_paths = match value {
                        Value::Null => String::new(),
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    };
                    workspace_allowed_paths_source = source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                if key == "workspace_blocked_paths" {
                    workspace_blocked_paths = match value {
                        Value::Null => String::new(),
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    };
                    workspace_blocked_paths_source = source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                if key == "client_workspace_scope_overrides" {
                    client_workspace_scope_overrides = match value {
                        Value::Bool(b) => *b,
                        Value::String(s) => matches!(s.as_str(), "1" | "true" | "yes" | "on"),
                        _ => false,
                    };
                    client_workspace_scope_overrides_source =
                        source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                if key == "trust_client_launch_folder" {
                    trust_client_launch_folder = match value {
                        Value::Bool(b) => *b,
                        Value::String(s) => matches!(s.as_str(), "1" | "true" | "yes" | "on"),
                        _ => true,
                    };
                    trust_client_launch_folder_source = source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                if key == "workspace_default_mode" {
                    if let Some(m) = value.as_str() {
                        if m == "whitelist" || m == "blacklist" {
                            workspace_default_mode = m.to_string();
                        }
                    }
                    workspace_default_mode_source = source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                if key == "user_workspace_policies" {
                    user_workspace_policies = match value {
                        Value::Object(map) if map.is_empty() => String::new(),
                        Value::Object(_) => serde_json::to_string_pretty(value).unwrap_or_default(),
                        _ => String::new(),
                    };
                    user_workspace_policies_source = source.as_str().unwrap_or("?").to_string();
                    continue;
                }
                let rendered = match value {
                    Value::Null => "—".to_string(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                knobs.push((
                    key.clone(),
                    rendered,
                    source.as_str().unwrap_or("?").to_string(),
                ));
            }
        }
        knobs.sort();
        let executors = v
            .get("executors")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|e| {
                        let id = s(e, "id")?;
                        let display = s(e, "display").unwrap_or_else(|| id.clone());
                        let mut label = display;
                        if b(e, "default").unwrap_or(false) {
                            label.push_str(" (default)");
                        }
                        if !b(e, "available").unwrap_or(true) {
                            label.push_str(" — unavailable");
                        }
                        Some(label)
                    })
                    .collect()
            })
            .unwrap_or_default();
        let apps = v
            .get("apps")
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .map(|(name, row)| AppsSetting {
                        name: name.clone(),
                        key: s(row, "key").unwrap_or_else(|| format!("apps.{name}")),
                        label: s(row, "label").unwrap_or_default(),
                        help: s(row, "help").unwrap_or_default(),
                        placeholder: s(row, "placeholder").unwrap_or_default(),
                        value: match row.get("value") {
                            Some(Value::String(x)) => x.clone(),
                            Some(Value::Null) | None => String::new(),
                            Some(other) => other.to_string(),
                        },
                        source: s(row, "source").unwrap_or_else(|| "?".into()),
                        note: s(row, "note").unwrap_or_default(),
                        invalid: s(row, "invalid_stored")
                            .or_else(|| s(row, "invalid_env"))
                            .unwrap_or_default(),
                    })
                    .collect()
            })
            .unwrap_or_default();
        RuntimeConfigData {
            writable: b(v, "writable").unwrap_or(false),
            workspace_root,
            workspace_root_source,
            workspace_allowed_paths,
            workspace_allowed_paths_source,
            workspace_blocked_paths,
            workspace_blocked_paths_source,
            client_workspace_scope_overrides,
            client_workspace_scope_overrides_source,
            trust_client_launch_folder,
            trust_client_launch_folder_source,
            workspace_default_mode,
            workspace_default_mode_source,
            user_workspace_policies,
            user_workspace_policies_source,
            knobs,
            executors,
            apps,
            agent_defaults: agent_defaults_from(v),
            skills_shelf: skills_shelf_from(v),
            streaming_default: streaming_default_from(v),
        }
    }
}

// ---------------------------------------------------------------------
// HOST STATE — the "agentic OS" resources snapshot (`GET /host/state`):
// memory + device + GPU gauges, resident models (row_v1), session
// prompt caches. Every section is independently best-effort server-side;
// `degraded`/`reasons` carry the sections that could not answer and the
// UI renders those as muted notes, never blank-success.
// ---------------------------------------------------------------------

/// RAM gauge facts (memory.ram). All optional — absence renders "—".
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MemGauge {
    pub total_bytes: Option<u64>,
    pub used_bytes: Option<u64>,
    pub available_bytes: Option<u64>,
    pub percent: Option<f64>,
}

/// Device (unified/GPU memory) gauge facts (memory.device).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct DeviceGauge {
    /// e.g. "metal", "cuda" — labels the gauge; empty = unknown backend.
    pub backend: String,
    /// PROCESS-LOCAL allocation as the runtime reports it. On Metal this
    /// counts only the buffers THIS gateway process allocated: a 93 GB
    /// GGUF held resident by LM Studio reads **0** here (live capture:
    /// `allocated_bytes: 0` while `host_in_use_bytes: 105743990784`).
    /// Never the meter's first choice — see [`DeviceGauge::meter`].
    pub allocated_bytes: Option<u64>,
    pub total_bytes: Option<u64>,
    pub free_bytes: Option<u64>,
    /// Accelerator heap in use across EVERY process on the machine
    /// (ioreg "In use system memory"). A genuine accelerator counter —
    /// driver-allocated buffers — and BLIND to memory-mapped GGUF
    /// weights, so it is never the machine's memory use.
    pub host_in_use_bytes: Option<u64>,
    /// The OS's wired limit for that all-processes pool — the
    /// accelerator-heap ceiling that makes `host_in_use_bytes` a meter.
    pub wired_limit_bytes: Option<u64>,
}

/// Which reading an accelerator meter is showing. The label MUST say so:
/// a process-local number presented as every process's is the exact lie
/// that made the meter read 0 B while 98 GB of weights were resident.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceScope {
    /// `host_in_use_bytes / wired_limit_bytes` — every process on the machine.
    Host,
    /// `allocated_bytes / total_bytes` — this gateway process only.
    Process,
}

impl DeviceScope {
    /// The scope words that ride the meter (SPEC PART A2/A3) — EXACTLY
    /// `all processes` and `this process only`. The old "host-wide"
    /// spelling is FORBIDDEN: it reads as whole-machine usage, which an
    /// accelerator-heap counter is not.
    pub fn label(self) -> &'static str {
        match self {
            DeviceScope::Host => "all processes",
            DeviceScope::Process => "this process only",
        }
    }
}

/// THE ACCELERATOR NOTE (SPEC PART A2), one spelling for the gauge and
/// for the breakdown's accelerator reference: `host_in_use_bytes` counts
/// DRIVER-ALLOCATED buffers only. llama.cpp mmaps a `.gguf` and wraps
/// the pages with `newBufferWithBytesNoCopy`, so 90 GB of weights can be
/// resident while this counter reads 1 GB.
pub const ACCELERATOR_NOTE: &str = "memory-mapped GGUF weights are not counted here";

/// THE ACCELERATOR LABEL (SPEC PART A2), verbatim:
/// `Accelerator heap · <backend> (all processes)`. An unknown/empty
/// backend prints the literal `device`.
pub fn accelerator_label(backend: &str, scope: DeviceScope) -> String {
    let backend = backend.trim();
    let backend = if backend.is_empty() {
        "device"
    } else {
        backend
    };
    format!("Accelerator heap · {backend} ({})", scope.label())
}

impl DeviceGauge {
    /// THE ACCELERATOR FIGURE + its scope: the all-processes reading
    /// wins whenever it exists, the process-local pair is the LABELLED
    /// fallback, and the ceiling is OPTIONAL — the figure is a fact even
    /// when nothing bounds it (a bar needs a denominator; a reference
    /// line does not).
    pub fn accelerator(&self) -> Option<(u64, Option<u64>, DeviceScope)> {
        let pos = |v: Option<u64>| v.filter(|t| *t > 0);
        if let Some(used) = self.host_in_use_bytes {
            // A figure with no wired limit still beats the process-local
            // one: the device total is its ceiling.
            let ceiling = pos(self.wired_limit_bytes).or_else(|| pos(self.total_bytes));
            return Some((used, ceiling, DeviceScope::Host));
        }
        self.allocated_bytes
            .map(|used| (used, pos(self.total_bytes), DeviceScope::Process))
    }

    /// THE DEVICE-METER RULE: [`Self::accelerator`] WITH a ceiling — a
    /// bar is never drawn from nothing, nor without its denominator.
    pub fn meter(&self) -> Option<(u64, u64, DeviceScope)> {
        match self.accelerator() {
            Some((used, Some(total), scope)) => Some((used, total, scope)),
            _ => None,
        }
    }

    /// This gauge's PART A2 label — backend + the scope actually shown.
    pub fn label(&self) -> Option<String> {
        self.accelerator()
            .map(|(_, _, scope)| accelerator_label(&self.backend, scope))
    }
}

/// One resident-model row, the frozen `model_residency_row_v1` shape.
/// EVERY field is optional by contract (absent means unknown, never
/// false) — in particular `resident` is TRI-STATE: `None` renders as a
/// distinct "unknown", never as "no".
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ModelRow {
    pub runtime_id: Option<String>,
    pub task: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub source: Option<String>,
    pub resident: Option<bool>,
    pub state: Option<String>,
    pub pinned: Option<bool>,
    pub default: Option<bool>,
    pub locked: Option<bool>,
    pub lockable: Option<bool>,
    pub modalities: Vec<String>,
    pub size_bytes: Option<u64>,
    pub size_vram_bytes: Option<u64>,
    /// The gateway's ESTIMATE of the weight footprint (derived from the
    /// artifact on disk, not measured on the host) — the third and last
    /// choice of [`ModelRow::display_size`], and the reason that helper
    /// returns an "estimated" flag instead of a bare number.
    pub est_weights_bytes: Option<u64>,
    /// KV/prompt cache the runtime holds FOR THIS MODEL (row_v1's
    /// `cache_bytes`) — a SECOND figure beside the weights, never folded
    /// into the size.
    pub cache_bytes: Option<u64>,
    pub context_length: Option<u64>,
    pub calibrated_context_length: Option<u64>,
    pub context_calibrated: Option<bool>,
    pub host_name: Option<String>,
    pub loaded_at: Option<String>,
    pub last_used_at: Option<String>,
}

impl ModelRow {
    pub fn from_value(v: &Value) -> ModelRow {
        let u = |key: &str| v.get(key).and_then(Value::as_u64);
        ModelRow {
            runtime_id: s(v, "runtime_id"),
            task: s(v, "task"),
            provider: s(v, "provider"),
            model: s(v, "model"),
            source: s(v, "source"),
            resident: b(v, "resident"),
            state: s(v, "state"),
            pinned: b(v, "pinned"),
            default: b(v, "default"),
            locked: b(v, "locked"),
            lockable: b(v, "lockable"),
            modalities: str_list(v, "modalities"),
            size_bytes: u("size_bytes"),
            size_vram_bytes: u("size_vram_bytes"),
            est_weights_bytes: u("est_weights_bytes"),
            cache_bytes: u("cache_bytes"),
            context_length: u("context_length"),
            calibrated_context_length: u("calibrated_context_length"),
            context_calibrated: b(v, "context_calibrated"),
            host_name: s(v, "host_name"),
            loaded_at: s(v, "loaded_at"),
            last_used_at: s(v, "last_used_at"),
        }
    }

    /// THE DISPLAY-SIZE COALESCE, shared by every surface that prints a
    /// per-model footprint: first KNOWN of `size_bytes` →
    /// `size_vram_bytes` → `est_weights_bytes`. The flag is `true` only
    /// for the third — an estimate the gateway derived from the artifact,
    /// not a measurement of the resident process — so no caller can print
    /// it as measured (see [`size_marked`]).
    pub fn display_size(&self) -> Option<(u64, bool)> {
        self.size_bytes
            .map(|b| (b, false))
            .or(self.size_vram_bytes.map(|b| (b, false)))
            .or(self.est_weights_bytes.map(|b| (b, true)))
    }

    /// Does this row claim residency? `resident` is TRI-STATE: only an
    /// explicit `true` is a yes.
    pub fn is_resident(&self) -> bool {
        self.resident == Some(true)
    }
}

/// THE ESTIMATE MARKER, one spelling for every surface: `3.1 GB` is a
/// figure the host REPORTED, `~3.1 GB` is one it ESTIMATED. Nothing else
/// in this crate is allowed to invent a second marker.
pub fn size_marked(bytes: u64, estimated: bool) -> String {
    if estimated {
        format!("~{}", human_bytes(bytes))
    } else {
        human_bytes(bytes)
    }
}

/// What the lock key offers on a row — the ONE authority behind the key
/// handler, the hint line and the tests.
///
/// The operator's rule: EVERY resident line offers the lock verb,
/// including sweep/externally-loaded rows (LM Studio, ollama), because
/// `POST /models/lock` now ADOPTS them. `locked` outranks residency —
/// a locked-but-evicted row keeps its Unlock. A row that is not resident
/// carries no lock verb at all (estimate only).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LockAction {
    Unlock,
    /// Lock; `true` when the row looks externally loaded (an adoption).
    Lock {
        adopt: bool,
    },
    /// Not offered — the reason, verbatim, for the refusal notice.
    Refused(&'static str),
}

pub fn lock_action(r: &ModelRow) -> LockAction {
    if r.locked == Some(true) {
        return LockAction::Unlock;
    }
    if !r.is_resident() {
        return LockAction::Refused(match r.resident {
            Some(false) => "not resident — only a loaded model can be locked (w warms one up)",
            _ => "residency unknown — refresh the snapshot before locking",
        });
    }
    if r.lockable == Some(false) {
        return LockAction::Refused("this runtime reports the model as not lockable");
    }
    // `lockable: null` is UNKNOWN, never a "no": the gateway is the
    // authority and it now adopts externally-loaded models.
    LockAction::Lock {
        // THE ADOPT SELECTOR (SPEC PART D1), one rule on all four
        // surfaces: `source == "provider_server"` and nothing else.
        // That is the ONLY string the gateway stamps on a row the host
        // loaded outside the Gateway (core `server/app.py`, runtime
        // `_merge_host_sweep_into_text_records`); the old tolerant
        // aliases (`sweep`, `external`) never ride the wire, and
        // `lockable` is NOT the selector — the sweep stamps it `true`.
        adopt: r.source.as_deref() == Some("provider_server"),
    }
}

/// May the unload verb target this row? A row the host says is NOT
/// resident has nothing to unload; an UNKNOWN residency still may (the
/// tri-state's third answer is not a "no", and the gateway is the
/// authority on what it holds).
pub fn unload_refusal(r: &ModelRow) -> Option<&'static str> {
    match r.resident {
        Some(false) => Some("not resident — there is nothing to unload (e estimates its context)"),
        _ => None,
    }
}

/// The resident TRI-STATE vocabulary — ONE spelling for the table cell,
/// the worker's verify proof and the tests. `None` is "unknown", a real
/// third answer that must never collapse into "no".
pub fn resident_label(resident: Option<bool>) -> &'static str {
    match resident {
        Some(true) => "yes",
        Some(false) => "no",
        None => "unknown",
    }
}

/// One session prompt-cache row (`GET /sessions/prompt_cache` caches[]).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SessionCacheRow {
    pub key: String,
    pub provider: String,
    pub model: String,
    pub session_id: String,
    pub bytes: Option<u64>,
    pub token_count: Option<u64>,
}

impl SessionCacheRow {
    pub fn from_value(v: &Value) -> Option<SessionCacheRow> {
        Some(SessionCacheRow {
            key: s(v, "key")?,
            provider: s(v, "provider").unwrap_or_default(),
            model: s(v, "model").unwrap_or_default(),
            session_id: s(v, "session_id").unwrap_or_default(),
            bytes: v.get("bytes").and_then(Value::as_u64),
            token_count: v.get("token_count").and_then(Value::as_u64),
        })
    }
}

/// `GET /host/state`, folded for the Models tab.
#[derive(Clone, Debug, Default)]
pub struct HostStateData {
    pub ram: Option<MemGauge>,
    pub process_rss: Option<u64>,
    pub device: Option<DeviceGauge>,
    pub gpu_supported: bool,
    pub gpu_util_pct: Option<f64>,
    pub host_name: Option<String>,
    pub models: Vec<ModelRow>,
    pub caches: Vec<SessionCacheRow>,
    /// Totals as the SERVER computed them (bytes stay None when no row
    /// carried a size — never a fabricated 0).
    pub model_bytes: Option<u64>,
    pub cache_bytes: Option<u64>,
    /// `totals.cache_bytes_models` — the server's sum of the per-model KV
    /// caches. Used by the breakdown ONLY when no row carried its own
    /// `cache_bytes` (so the attribution is not silently short).
    pub model_cache_bytes: Option<u64>,
    /// Section names the gateway could not answer ("memory", "gpu",
    /// "models", "session_caches") + why. Rendered as muted notes.
    pub degraded: Vec<String>,
    pub reasons: HashMap<String, String>,
}

impl HostStateData {
    /// Re-derive the byte totals after the worker swaps rows in place
    /// (post-mutation verify) — same sum-of-known rule the server uses.
    pub fn recount(&mut self) {
        let sum = |it: &mut dyn Iterator<Item = Option<u64>>| -> Option<u64> {
            let known: Vec<u64> = it.flatten().collect();
            if known.is_empty() {
                None
            } else {
                Some(known.iter().sum())
            }
        };
        self.model_bytes = sum(&mut self.models.iter().map(|m| m.size_bytes));
        self.cache_bytes = sum(&mut self.caches.iter().map(|c| c.bytes));
        self.model_cache_bytes = sum(&mut self.models.iter().map(|m| m.cache_bytes));
    }
}

/// What a [`BreakdownLine`] IS (SPEC PART B). The three kinds are NOT
/// summable with one another and the renderer must keep them apart.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BreakdownKind {
    /// A fact the framework knows, labelled with what it measures.
    Item,
    /// A separate counter beside the items — NEVER added to them.
    Reference,
    /// Prose: the GGUF explanation. Rendered whole (wrapped, never cut,
    /// never reworded).
    Note,
}

/// One line of the memory breakdown. `size` is already rendered (and
/// already carries the `~` estimate marker when it applies); `bytes` is
/// the RAW figure behind it, which is what tests pin — the four surfaces
/// format bytes differently and none of that is contract.
#[derive(Clone, Debug, PartialEq)]
pub struct BreakdownLine {
    /// The stable key, identical on every surface: `model:<runtime_id>`,
    /// `model_caches`, `session_caches`, `process_rss`,
    /// `sum_model_weights`, `ram`, `accelerator`, `gguf_note`.
    pub key: String,
    pub kind: BreakdownKind,
    /// The line's NAME. For a [`BreakdownKind::Note`] the whole sentence
    /// lives here — a note has no name/detail split.
    pub label: String,
    /// The rendered value. Empty for a note.
    pub size: String,
    /// The raw byte figure behind `size`: the value for the item lines
    /// and `sum_model_weights`, the USED side for the pair-valued `ram`
    /// and `accelerator` references, `None` for a note.
    pub bytes: Option<u64>,
    /// The line's DETAIL — what the number measures, verbatim per spec.
    /// Empty for a note.
    pub note: String,
    /// True for the per-resident-model item lines. A renderer short of
    /// rows caps THESE (the table below lists every one of them anyway)
    /// and never the fixed tail.
    pub per_model: bool,
}

/// THE GGUF NOTE (SPEC PART B3), verbatim. Emitted only when the summed
/// model weights EXCEED the accelerator heap — the normal case here, not
/// an inconsistency.
pub const GGUF_NOTE: &str = "Σ model weights exceeds the accelerator heap. That is the normal case for memory-mapped GGUF weights: llama.cpp maps them from disk, so they are resident as process RSS and are not counted in the accelerator heap.";

/// THE MEMORY BREAKDOWN (SPEC PART B) — one rule set, shared verbatim
/// with the web console, abstractflow and the abstractcode TUI. Three
/// kinds of line, same keys, same order, from the same payload:
///
/// * [`BreakdownKind::Item`] — facts the framework knows, each labelled
///   with WHAT it measures: one line per resident model that has a known
///   display size (`model:<runtime_id>`), the model KV caches, the
///   session caches, this process's RSS.
/// * [`BreakdownKind::Reference`] — separate counters that must NOT be
///   added to the items: `Σ model weights`, `RAM used`, and the
///   accelerator heap under its PART A2 label.
/// * [`BreakdownKind::Note`] — the GGUF explanation, emitted only when
///   `Σ model weights` exceeds the accelerator figure.
///
/// THE EMISSION RULE, identical on every surface: a line is emitted when
/// its value is KNOWN and omitted when it is unknown. A known `0` IS
/// emitted. No line is "always emitted", none is conditional on being
/// non-zero, and a resident row with no known size is SKIPPED rather
/// than given an invented zero.
///
/// There is NO remainder line and no replacement for it.
/// `host_in_use_bytes` is an accelerator counter blind to memory-mapped
/// GGUF weights, so `host_in_use − (models + caches + rss)` subtracted
/// RAM-dimensioned quantities from it, computed ~−79 GB on this host and
/// clamped to `0 B`. That was a category error, not an overlap, and a
/// second wrong remainder (against RAM) would need per-process
/// accounting the framework does not have.
pub fn memory_breakdown(d: &HostStateData) -> Vec<BreakdownLine> {
    let mut items: Vec<BreakdownLine> = Vec::new();
    let mut refs: Vec<BreakdownLine> = Vec::new();
    let mut weights_sum: u64 = 0;
    let mut model_items = 0usize;
    // The `~` marker rides the SUM too: a total built from an estimate
    // is an estimate, and this crate prints those marked or not at all.
    let mut any_estimated = false;

    for m in d.models.iter().filter(|m| m.is_resident()) {
        // No known size → NO line (an invented zero is worse than a
        // missing one; the Loaded table still lists the row).
        let Some((bytes, estimated)) = m.display_size() else {
            continue;
        };
        // Which field supplied the number — the display-size coalesce
        // order, said out loud.
        let source_phrase = if m.size_bytes.is_some() {
            "reported by the model server (size_bytes)"
        } else if m.size_vram_bytes.is_some() {
            "reported by the model server (size_vram_bytes)"
        } else {
            "estimated on-disk weight size (est_weights_bytes)"
        };
        // THE ITEM KEY RULE, shared by all four surfaces: the
        // `runtime_id` when it is a non-empty string, else the
        // `provider:model` pair — real sweep rows reach the wire with
        // `runtime_id: null`. No index suffix, no task segment, no
        // empty segment. Two rows that genuinely collide KEEP the same
        // key: a duplicate provider+model row is itself worth seeing.
        let key = match m.runtime_id.as_deref() {
            Some(id) if !id.is_empty() => format!("model:{id}"),
            _ => {
                let bits: Vec<&str> = [m.provider.as_deref(), m.model.as_deref()]
                    .into_iter()
                    .flatten()
                    .filter(|s| !s.is_empty())
                    .collect();
                format!("model:{}", bits.join(":"))
            }
        };
        weights_sum = weights_sum.saturating_add(bytes);
        model_items += 1;
        any_estimated |= estimated;
        items.push(BreakdownLine {
            key,
            kind: BreakdownKind::Item,
            label: m.model.clone().unwrap_or_else(|| "—".into()),
            size: size_marked(bytes, estimated),
            bytes: Some(bytes),
            note: format!("resident model weights · {source_phrase}"),
            per_model: true,
        });
    }

    // `totals.cache_bytes_models` when the server sent it, else the sum
    // over the resident rows that carried their own `cache_bytes`.
    let model_caches = d.model_cache_bytes.or_else(|| {
        let known: Vec<u64> = d
            .models
            .iter()
            .filter(|m| m.is_resident())
            .filter_map(|m| m.cache_bytes)
            .collect();
        (!known.is_empty()).then(|| known.iter().sum())
    });
    if let Some(b) = model_caches {
        items.push(BreakdownLine {
            key: "model_caches".into(),
            kind: BreakdownKind::Item,
            label: "model KV caches".into(),
            size: human_bytes(b),
            bytes: Some(b),
            note: "prompt-cache bytes held for resident models".into(),
            per_model: false,
        });
    }

    // `totals.session_cache_bytes` when known, else the sum over the
    // session-cache list.
    let session_caches = d.cache_bytes.or_else(|| {
        let known: Vec<u64> = d.caches.iter().filter_map(|c| c.bytes).collect();
        (!known.is_empty()).then(|| known.iter().sum())
    });
    if let Some(b) = session_caches {
        items.push(BreakdownLine {
            key: "session_caches".into(),
            kind: BreakdownKind::Item,
            label: "session caches".into(),
            size: human_bytes(b),
            bytes: Some(b),
            note: "prompt-cache bytes held by gateway sessions".into(),
            per_model: false,
        });
    }

    if let Some(b) = d.process_rss {
        items.push(BreakdownLine {
            key: "process_rss".into(),
            kind: BreakdownKind::Item,
            label: "gateway process RSS".into(),
            size: human_bytes(b),
            bytes: Some(b),
            note: "resident set size of the gateway process — includes memory-mapped GGUF weights"
                .into(),
            per_model: false,
        });
    }

    if model_items > 0 {
        refs.push(BreakdownLine {
            key: "sum_model_weights".into(),
            kind: BreakdownKind::Reference,
            label: "Σ model weights".into(),
            size: size_marked(weights_sum, any_estimated),
            bytes: Some(weights_sum),
            note: "sum of the resident model weights above".into(),
            per_model: false,
        });
    }

    if let Some(used) = d.ram.as_ref().and_then(|r| r.used_bytes) {
        let total = d.ram.as_ref().and_then(|r| r.total_bytes);
        refs.push(BreakdownLine {
            key: "ram".into(),
            kind: BreakdownKind::Reference,
            label: "RAM used".into(),
            size: match total {
                Some(t) => format!("{} / {}", human_bytes(used), human_bytes(t)),
                None => human_bytes(used),
            },
            bytes: Some(used),
            note: "system memory in use / installed".into(),
            per_model: false,
        });
    }

    let accelerator = d.device.as_ref().and_then(|dev| {
        dev.accelerator()
            .map(|(used, ceiling, scope)| (used, ceiling, accelerator_label(&dev.backend, scope)))
    });
    if let Some((used, ceiling, label)) = &accelerator {
        refs.push(BreakdownLine {
            key: "accelerator".into(),
            kind: BreakdownKind::Reference,
            label: label.clone(),
            size: match ceiling {
                Some(c) => format!("{} / {}", human_bytes(*used), human_bytes(*c)),
                None => human_bytes(*used),
            },
            bytes: Some(*used),
            note: ACCELERATOR_NOTE.into(),
            per_model: false,
        });
    }

    let mut out = items;
    out.append(&mut refs);
    // The GGUF case, named: the weights EXCEED the accelerator heap
    // because llama.cpp maps them from disk. Both figures must be known.
    if let (true, Some((used, _, _))) = (model_items > 0, &accelerator) {
        if weights_sum > *used {
            out.push(BreakdownLine {
                key: "gguf_note".into(),
                kind: BreakdownKind::Note,
                label: GGUF_NOTE.into(),
                size: String::new(),
                bytes: None,
                note: String::new(),
                per_model: false,
            });
        }
    }
    out
}

pub fn host_state_from_payload(v: &Value) -> HostStateData {
    let memory = v.get("memory");
    let ram = memory
        .and_then(|m| m.get("ram"))
        .filter(|r| r.is_object())
        .map(|r| MemGauge {
            total_bytes: r.get("total_bytes").and_then(Value::as_u64),
            used_bytes: r.get("used_bytes").and_then(Value::as_u64),
            available_bytes: r.get("available_bytes").and_then(Value::as_u64),
            percent: r.get("percent").and_then(Value::as_f64),
        });
    let device = memory
        .and_then(|m| m.get("device"))
        .filter(|d| d.is_object())
        .map(|d| DeviceGauge {
            backend: s(d, "backend").unwrap_or_default(),
            allocated_bytes: d.get("allocated_bytes").and_then(Value::as_u64),
            total_bytes: d.get("total_bytes").and_then(Value::as_u64),
            free_bytes: d.get("free_bytes").and_then(Value::as_u64),
            host_in_use_bytes: d.get("host_in_use_bytes").and_then(Value::as_u64),
            wired_limit_bytes: d.get("wired_limit_bytes").and_then(Value::as_u64),
        });
    let gpu = v.get("gpu").filter(|g| g.is_object());
    let totals = v.get("totals");
    HostStateData {
        ram,
        process_rss: memory
            .and_then(|m| m.get("process"))
            .and_then(|p| p.get("rss_bytes"))
            .and_then(Value::as_u64),
        device,
        gpu_supported: gpu.and_then(|g| b(g, "supported")).unwrap_or(false),
        gpu_util_pct: gpu
            .and_then(|g| g.get("utilization_gpu_pct"))
            .and_then(Value::as_f64),
        host_name: v.get("host").and_then(|h| s(h, "host_name")).or_else(|| {
            memory
                .and_then(|m| m.get("host"))
                .and_then(|h| s(h, "host_name"))
        }),
        models: v
            .get("models")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(ModelRow::from_value).collect())
            .unwrap_or_default(),
        caches: v
            .get("session_caches")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(SessionCacheRow::from_value).collect())
            .unwrap_or_default(),
        model_bytes: totals
            .and_then(|t| t.get("model_bytes"))
            .and_then(Value::as_u64),
        cache_bytes: totals
            .and_then(|t| t.get("session_cache_bytes"))
            .and_then(Value::as_u64),
        model_cache_bytes: totals
            .and_then(|t| t.get("cache_bytes_models"))
            .and_then(Value::as_u64),
        degraded: str_list(v, "degraded"),
        reasons: v
            .get("reasons")
            .and_then(Value::as_object)
            .map(|o| {
                o.iter()
                    .filter_map(|(k, val)| val.as_str().map(|t| (k.clone(), t.to_string())))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

/// Rows from `GET /models/loaded` (the verify read after a model
/// mutation): normalized row_v1 lives under "rows"; "models" is the
/// host-state spelling, tolerated for shape drift.
pub fn model_rows_from_payload(v: &Value) -> Vec<ModelRow> {
    ["rows", "models"]
        .iter()
        .find_map(|k| v.get(*k).and_then(Value::as_array))
        .map(|a| a.iter().map(ModelRow::from_value).collect())
        .unwrap_or_default()
}

/// Rows from `GET /sessions/prompt_cache` (the verify read after a
/// clear-all): the list lives under "caches".
pub fn session_caches_from_payload(v: &Value) -> Vec<SessionCacheRow> {
    rows_from(v, &["caches"], SessionCacheRow::from_value)
}

/// One applied write + its GET verification — the review screen's rows.
#[derive(Clone, Debug)]
pub struct JournalEntry {
    pub when: String,
    /// e.g. "PUT capability route output.voice"
    pub action: String,
    pub outcome: Result<String, String>,
    /// What the follow-up GET showed (verify-after-write law).
    pub verified: Option<Result<String, String>>,
}

/// An in-flight operation for the busy strip (elapsed rendering).
#[derive(Clone, Debug)]
pub struct BusyOp {
    pub id: u64,
    pub label: String,
    pub started: Instant,
}

#[derive(Clone, Copy)]
pub struct Store {
    pub conn: Signal<ConnPhase>,
    /// Bumped by the worker whenever a WRITE fails at the transport
    /// layer — the health authority's trigger channel for failures
    /// that don't land on a domain slot.
    pub net_fail_seq: Signal<u64>,
    /// Domains that already spent their one automatic post-verify
    /// retry — cleared by user action (r / refresh / reset), so the
    /// authority can never loop against an endpoint that keeps
    /// failing while /ping answers.
    pub conn_retry_spent: Signal<Vec<&'static str>>,
    /// Discards stale probe settles (a settle carries the generation
    /// it was spawned under; only the latest wins).
    pub probe_gen: Signal<u64>,
    pub providers: Signal<Loadable<ProvidersData>>,
    pub profiles: Signal<Loadable<ProfilesData>>,
    pub routes: Signal<Loadable<RoutesData>>,
    /// Local weight availability on the EXECUTION HOST
    /// (`/models/availability`) — whether the configured models are
    /// actually there.
    pub availability: Signal<Loadable<AvailabilityData>>,
    /// The download job the operator most recently started, polled to
    /// completion by the worker. `None` = no download this session.
    pub download: Signal<Option<DownloadStatus>>,
    /// The `GET /host/state` snapshot behind the Models tab: memory +
    /// GPU gauges, resident models, session prompt caches.
    pub host_state: Signal<Loadable<HostStateData>>,
    /// Generation gate for the host-state poll chain. A `PollHostState`
    /// result (and its reschedule) is honored ONLY while the generation
    /// it was spawned under is current — leaving the Models tab (or a
    /// gateway reset, or `r`) bumps it and the old chain dies.
    pub host_poll_gen: Signal<u64>,
    /// (provider, model) of an unload the gateway just refused with
    /// HTTP 409 model_locked — the Models tab's effect offers the
    /// "Force unload?" second confirm and clears the slot.
    ///
    /// SINGLE SLOT BY CHOICE (accepted limitation): two 409s landing
    /// within one UI round-trip would overwrite the first force-offer.
    /// Reaching that takes two unload confirms racing through the ONE
    /// serial worker lane before a frame renders; the journal records
    /// every refusal regardless, and pressing `u` on the dropped row
    /// re-offers. A queue would buy correctness for a race the
    /// confirm-gated flow cannot realistically produce.
    pub unload_locked: Signal<Option<(String, String)>>,
    pub users: Signal<Loadable<UsersData>>,
    pub entities: Signal<Loadable<Vec<EntityRow>>>,
    pub runtimes: Signal<Loadable<Vec<RuntimeRow>>>,
    /// The registered workflow registry: one row per bundle, plus the
    /// versions the gateway refused to serve.
    pub workflows: Signal<Loadable<WorkflowsData>>,
    pub runtime_config: Signal<Loadable<RuntimeConfigData>>,
    /// `GET /about` of the connected gateway (About modal).
    pub about: Signal<Loadable<Value>>,
    /// Network exposure + reachable addresses (Connection screen).
    pub network: Signal<Loadable<NetworkData>>,
    /// Per-provider model lists (route editor + provider browser).
    pub models: Signal<HashMap<String, Loadable<Vec<String>>>>,
    /// Result of the LAST discover-models call — a single slot, which
    /// is safe only because exactly one form modal exists at a time
    /// (open_form closes any predecessor); openers must reset it.
    pub discover: Signal<Loadable<DiscoverOutcome>>,
    pub sandbox: Signal<Loadable<SandboxOutcome>>,
    /// Voice catalog for the route editor's voice picker. The (provider,
    /// model) pair the list was fetched FOR rides with the data, so a
    /// late response for a stale pick can never dress the wrong pair.
    pub voices: Signal<Loadable<VoicesData>>,
    /// Result of the route editor's Test verb (single slot — one form
    /// modal at a time; openers reset it).
    pub route_test: Signal<Loadable<RouteTestOutcome>>,
    /// Manage snapshot for the SELECTED entity (single slot, keyed by
    /// name inside the data — consumers check it matches their row).
    pub entity_detail: Signal<Loadable<EntityDetail>>,
    /// Tool-policy editor state (single slot, keyed by entity name).
    pub entity_policy: Signal<Loadable<ToolPolicyData>>,
    /// Prompt overlay viewer state (single slot, keyed by entity name).
    pub entity_prompt: Signal<Loadable<PromptData>>,
    /// Candidates review state: (entity, rows).
    pub entity_candidates: Signal<Loadable<(String, Vec<CandidateRow>)>>,
    /// Recent root runs of ONE runtime plane (follows the Runtimes
    /// screen's selection; scope rides with the rows).
    pub runs: Signal<Loadable<RunsData>>,
    pub data_homes: Signal<Loadable<Vec<DataHomeRow>>>,
    /// Artifact metadata (deliverables — never caches), one page at a time.
    pub artifacts: Signal<Loadable<ArtifactsData>>,
    /// Log FILES across this gateway's registered log homes.
    pub logs: Signal<Loadable<Vec<LogFileRow>>>,
    /// The open artifact preview's text (None = nothing fetched yet).
    pub artifact_text: Signal<Option<String>>,
    /// The open artifact preview's DECODED image. abstracttui renders
    /// bitmaps as cell mosaics (and pixel protocols where the terminal
    /// supports them), so an image artifact previews for real.
    pub artifact_image: Signal<Option<std::sync::Arc<abstracttui::prelude::Bitmap>>>,
    /// The open log tail's text.
    pub log_text: Signal<Option<String>>,
    /// WHICH preview the open modal is showing — `artifact:<run>/<id>` or
    /// `log:<home>/<file>`, empty when no preview modal is open.
    ///
    /// The worker is ONE serial lane and the three slots above are
    /// GLOBAL. Open three image artifacts in a row and the third modal
    /// receives the first two results before its own — each painting
    /// under the wrong header, which is the "an error flashes, then the
    /// image appears" report. Results for anything but the current
    /// target are dropped instead of rendered.
    pub preview_target: Signal<String>,
    pub reservations: Signal<Loadable<Vec<ReservationRow>>>,
    pub journal: Signal<Vec<JournalEntry>>,
    pub busy: Signal<Vec<BusyOp>>,
    /// Monotonic tick driving elapsed displays while ops are in flight.
    pub tick: Signal<u64>,
    /// One-line transient notice (also mirrored as a toast).
    pub notice: Signal<Option<String>>,
    /// The last probe's acknowledgment record (None = never probed).
    pub last_probe: Signal<Option<ProbeReport>>,
}

#[derive(Clone, Debug, Default)]
pub struct DiscoverOutcome {
    pub ok: bool,
    pub available: bool,
    pub models: Vec<String>,
    pub error: Option<String>,
}

impl DiscoverOutcome {
    pub fn from_value(v: &Value) -> DiscoverOutcome {
        DiscoverOutcome {
            ok: b(v, "ok").unwrap_or(false),
            available: b(v, "available").unwrap_or(false),
            models: models_from_payload(v),
            error: s(v, "error").filter(|e| !e.is_empty()),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct SandboxOutcome {
    pub ok: bool,
    /// Body-level error text when ok:false carries one.
    pub error: Option<String>,
    pub response: String,
    pub routed_provider: Option<String>,
    pub profile: Option<String>,
    pub usage: Option<String>,
    /// provider + model the test ran against (request side).
    pub provider: String,
    pub model: String,
}

impl SandboxOutcome {
    pub fn from_value(provider: &str, model: &str, v: &Value) -> SandboxOutcome {
        SandboxOutcome {
            ok: b(v, "ok").unwrap_or(false),
            error: s(v, "error")
                .or_else(|| s(v, "detail"))
                .or_else(|| v.get("errors").map(|e| e.to_string()))
                .filter(|e| !e.is_empty() && e != "[]"),
            response: s(v, "response").unwrap_or_default(),
            routed_provider: s(v, "routed_provider"),
            profile: s(v, "provider_endpoint_profile"),
            usage: v.get("usage").map(|u| u.to_string()),
            provider: provider.to_string(),
            model: model.to_string(),
        }
    }
}

/// TTS voices for one (provider, model) pair — the route editor's
/// voice picker (web parity: GET /voice/voices?provider&model&compact).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct VoicesData {
    pub provider: String,
    pub model: String,
    pub voices: Vec<String>,
}

impl VoicesData {
    pub fn from_value(provider: &str, model: &str, v: &Value) -> VoicesData {
        let mut voices: Vec<String> = Vec::new();
        if let Some(items) = v.get("items").and_then(Value::as_array) {
            for it in items {
                // The catalog's voice id lives in profile_id; params.voice
                // and label are tolerant fallbacks (payload evolves).
                let vid = s(it, "profile_id")
                    .or_else(|| {
                        it.get("params")
                            .and_then(|p| p.get("voice"))
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .or_else(|| s(it, "label"));
                if let Some(vid) = vid {
                    if !vid.is_empty() && !voices.contains(&vid) {
                        voices.push(vid);
                    }
                }
            }
        }
        VoicesData {
            provider: provider.to_string(),
            model: model.to_string(),
            voices,
        }
    }
}

/// Outcome of the route editor's Test verb — a REAL generation through
/// the production lane (voice tts run or sandbox generate).
#[derive(Clone, Debug, Default)]
pub struct RouteTestOutcome {
    pub ok: bool,
    /// One human line: what ran, how long, against what pair.
    pub summary: String,
    /// Response snippet / artifact reference / error detail.
    pub detail: Option<String>,
}

/// One root run row (cancel/steer targets on the Runtimes screen).
#[derive(Clone, Debug, Default)]
pub struct RunRow {
    pub run_id: String,
    pub workflow_id: String,
    pub status: String,
    pub updated_at: String,
    pub paused: bool,
    /// Present on the per-plane drill-in payload; the generic /runs
    /// listing is already root-filtered server-side (root_only=true),
    /// so it folds None there. The plane lane filters on it client-side
    /// (the drill-in endpoint has no root_only parameter).
    pub parent_run_id: Option<String>,
}

pub fn runs_from_payload(v: &Value) -> Vec<RunRow> {
    rows_from(v, &["items", "runs"], |r| {
        Some(RunRow {
            run_id: s(r, "run_id")?,
            workflow_id: s(r, "workflow_id").unwrap_or_default(),
            status: s(r, "status").unwrap_or_default(),
            updated_at: s(r, "updated_at").unwrap_or_default(),
            paused: b(r, "paused").unwrap_or(false),
            parent_run_id: s(r, "parent_run_id").filter(|p| !p.is_empty()),
        })
    })
}

/// WHOSE runs the runs panel shows. Runs live in per-runtime stores —
/// there is no global run list to filter client-side (live-verified
/// 2026-07-25: /runs rows carry no runtime/tenant field because the
/// runtime is implied by the caller; the endpoint refuses unknown query
/// params, so no server filter exists on that lane either). Selecting a
/// runtime row therefore switches ENDPOINTS: the admin drill-in
/// GET /admin/runtimes/{kind}/{tenant}/{runtime}/runs serves one plane.
#[derive(Clone, Debug, Default, PartialEq)]
pub enum RunScope {
    /// GET /runs — the calling principal's own run store (richer rows:
    /// paused + server-side root_only). For the admin console this IS
    /// the default plane (live-verified: identical run ids), and it is
    /// the only lane whose /commands inbox this console can reach —
    /// cancel/steer are honest only here.
    #[default]
    Own,
    /// One runtime plane via the admin drill-in (kind: default|user|entity).
    Plane {
        kind: String,
        tenant_id: String,
        runtime_id: String,
        /// Human name for panel copy ("castor", "default/bob").
        label: String,
    },
}

impl RunScope {
    /// Map a selected runtime row to the scope that serves its runs.
    /// The default plane maps to Own deliberately: same store
    /// (live-verified), richer payload, and actionable (see above).
    pub fn of_runtime(r: &RuntimeRow) -> RunScope {
        if r.kind == "default" {
            return RunScope::Own;
        }
        let label = if r.label.is_empty() {
            format!("{}/{}", r.tenant_id, r.runtime_id)
        } else {
            r.label.clone()
        };
        RunScope::Plane {
            kind: r.kind.clone(),
            tenant_id: r.tenant_id.clone(),
            runtime_id: r.runtime_id.clone(),
            label,
        }
    }

    /// One line for the panel: what the runs below belong to.
    pub fn describe(&self) -> String {
        match self {
            RunScope::Own => "your principal's runs (the default plane)".into(),
            RunScope::Plane { kind, label, .. } => format!("{kind} plane: {label}"),
        }
    }

    /// Short name for empty states and refusal notices.
    pub fn short(&self) -> String {
        match self {
            RunScope::Own => "your runtime".into(),
            RunScope::Plane { label, .. } => label.clone(),
        }
    }

    /// Cancel/steer reachability: durable commands land in the CALLING
    /// principal's command inbox and are consumed by ITS runner — a
    /// command aimed at another plane's run would be accepted and then
    /// sit unconsumed forever (live-verified: POST /commands appends to
    /// svc.runner.command_store with no cross-plane routing; only
    /// inject_guidance even checks the run exists).
    pub fn actionable(&self) -> bool {
        matches!(self, RunScope::Own)
    }
}

/// The runs slot: rows + the scope they were loaded FOR (the VoicesData
/// pattern — a late response for a stale selection can never dress the
/// wrong plane; consumers compare scope against the current selection).
#[derive(Clone, Debug, Default)]
pub struct RunsData {
    pub scope: RunScope,
    pub rows: Vec<RunRow>,
    /// The REQUEST this data answers (design adversary Q4: with filters
    /// held only in ui state, a filter change never invalidated the held
    /// payload and the toolbar was dead on arrival).
    pub status: String,
    pub query: String,
    pub root_only: bool,
    pub offset: u32,
    pub has_more: bool,
}

/// One registered data home (purge targets).
#[derive(Clone, Debug, Default)]
pub struct DataHomeRow {
    pub name: String,
    pub path: String,
    pub kind: String,
    pub owner: String,
    pub safe_to_purge: bool,
    pub description: String,
    /// false = the registered path no longer exists (stale row).
    pub exists: bool,
    /// None until the sized pass lands (two-phase load).
    pub size_bytes: Option<u64>,
}

/// One artifact's metadata row (the deliverable itself streams through
/// the web console's Open action; a terminal lists, never renders).
#[derive(Clone, Debug)]
pub struct ArtifactRow {
    pub name: String,
    pub kind: String,
    pub size_bytes: Option<u64>,
    pub run_id: String,
    pub created_at: String,
    pub artifact_id: String,
    pub content_type: String,
    /// Where the bytes live on the gateway host (admins only — the
    /// server omits it for everyone else).
    pub content_path: String,
    pub workflow_id: String,
    pub session_id: String,
}

/// One page of artifacts + the request it answers.
#[derive(Clone, Debug, Default)]
pub struct ArtifactsData {
    pub rows: Vec<ArtifactRow>,
    pub total: u64,
    pub has_more: bool,
    pub offset: u32,
    pub modality: String,
    pub query: String,
}

/// One log file inside a registered log home.
#[derive(Clone, Debug, Default)]
pub struct LogFileRow {
    pub name: String,
    pub home: String,
    pub size_bytes: Option<u64>,
    pub modified_at: String,
}

/// The [`Store::preview_target`] key for one artifact preview. Built in
/// exactly one place so the opener and the worker cannot spell it apart.
pub fn artifact_preview_key(run_id: &str, artifact_id: &str) -> String {
    format!("artifact:{run_id}/{artifact_id}")
}

/// The [`Store::preview_target`] key for one log tail.
pub fn log_preview_key(home: &str, file: &str) -> String {
    format!("log:{home}/{file}")
}

pub fn log_files_from_payload(v: &Value) -> Vec<LogFileRow> {
    let mut out: Vec<LogFileRow> = Vec::new();
    for home in v
        .get("homes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        // Stale (missing) homes are hygiene, not logs — the Cache tab owns
        // them, exactly as the web console does.
        if home
            .get("missing")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let hname = s(home, "home").unwrap_or_default();
        for f in home
            .get("files")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(name) = s(f, "name") else { continue };
            out.push(LogFileRow {
                name,
                home: hname.clone(),
                size_bytes: f.get("size_bytes").and_then(Value::as_u64),
                modified_at: s(f, "modified_at").unwrap_or_default(),
            });
        }
    }
    out.sort_by(|a, b| b.modified_at.cmp(&a.modified_at));
    out
}

pub fn artifacts_from_payload(v: &Value) -> Vec<ArtifactRow> {
    rows_from(v, &["items"], |a| {
        let content_type = s(a, "content_type").unwrap_or_default();
        let filename = s(a, "filename").unwrap_or_default();
        Some(ArtifactRow {
            name: s(a, "filename").or_else(|| s(a, "artifact_id"))?,
            // The STORE's precedence, mirroring the web console exactly:
            // render_kind -> content_type -> filename extension. NEVER
            // semantic_kind (it carries non-render values like
            // "transcript"/"workflow_snapshot"), which is what this parsed
            // first before parity work.
            kind: artifact_render_kind(
                &s(a, "render_kind").unwrap_or_default(),
                &content_type,
                &filename,
            ),
            size_bytes: a.get("size_bytes").and_then(Value::as_u64),
            run_id: s(a, "run_id").unwrap_or_default(),
            created_at: s(a, "created_at").unwrap_or_default(),
            artifact_id: s(a, "artifact_id").unwrap_or_default(),
            content_type,
            content_path: s(a, "content_path").unwrap_or_default(),
            workflow_id: s(a, "workflow_id").unwrap_or_default(),
            session_id: s(a, "session_id").unwrap_or_default(),
        })
    })
}

pub fn artifact_render_kind(render_kind: &str, content_type: &str, filename: &str) -> String {
    if !render_kind.is_empty() {
        return render_kind.to_ascii_lowercase();
    }
    let ct = content_type.to_ascii_lowercase();
    if ct.starts_with("image/") {
        return "image".into();
    }
    if ct.starts_with("video/") {
        return "video".into();
    }
    if ct.starts_with("audio/") {
        return "audio".into();
    }
    if ct == "application/json" || ct.ends_with("+json") {
        return "json".into();
    }
    if ct == "text/markdown" {
        return "markdown".into();
    }
    if ct == "text/html" {
        return "html".into();
    }
    if ct == "application/pdf" {
        return "document".into();
    }
    if ct.starts_with("text/") {
        return "text".into();
    }
    let ext = filename
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase();
    match ext.as_str() {
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "bmp" => "image".into(),
        "mp4" | "webm" | "mov" => "video".into(),
        "mp3" | "wav" | "ogg" | "flac" | "m4a" => "audio".into(),
        "md" | "markdown" => "markdown".into(),
        "json" => "json".into(),
        "html" | "htm" => "html".into(),
        "txt" | "log" | "csv" => "text".into(),
        "pdf" => "document".into(),
        _ => "binary".into(),
    }
}

pub fn artifacts_data_from_payload(
    v: &Value,
    offset: u32,
    modality: &str,
    query: &str,
) -> ArtifactsData {
    ArtifactsData {
        rows: artifacts_from_payload(v),
        total: v.get("total").and_then(Value::as_u64).unwrap_or(0),
        has_more: v.get("has_more").and_then(Value::as_bool).unwrap_or(false),
        offset,
        modality: modality.to_string(),
        query: query.to_string(),
    }
}

pub fn data_homes_from_payload(v: &Value) -> Vec<DataHomeRow> {
    rows_from(v, &["homes"], |h| {
        Some(DataHomeRow {
            name: s(h, "name")?,
            path: s(h, "path").unwrap_or_default(),
            kind: s(h, "kind").unwrap_or_default(),
            owner: s(h, "owner").unwrap_or_default(),
            safe_to_purge: b(h, "safe_to_purge").unwrap_or(false),
            description: s(h, "description").unwrap_or_default(),
            exists: b(h, "exists").unwrap_or(true),
            size_bytes: h.get("size_bytes").and_then(Value::as_u64),
        })
    })
}

/// One retained runtime plane (deleted user's data — transfer/purge).
#[derive(Clone, Debug, Default)]
pub struct ReservationRow {
    pub tenant_id: String,
    pub runtime_id: String,
    pub owner_user_id: String,
    pub reason: String,
    pub data_exists: bool,
}

pub fn reservations_from_payload(v: &Value) -> Vec<ReservationRow> {
    rows_from(v, &["runtime_reservations"], |r| {
        Some(ReservationRow {
            tenant_id: s(r, "tenant_id").unwrap_or_default(),
            runtime_id: s(r, "runtime_id")?,
            owner_user_id: s(r, "owner_user_id").unwrap_or_default(),
            reason: s(r, "reason").unwrap_or_default(),
            data_exists: b(r, "data_exists").unwrap_or(false),
        })
    })
}

/// Per-phase tool grants + the grantable-tool option set.
#[derive(Clone, Debug, Default)]
pub struct ToolPolicyData {
    pub entity: String,
    /// (phase id, granted tools, source: default|custom).
    pub phases: Vec<(String, Vec<String>, String)>,
    /// Every grantable tool name (from the capability matrix; falls
    /// back to the union of granted tools when the matrix read fails).
    pub all_tools: Vec<String>,
}

impl ToolPolicyData {
    pub fn fold(entity: &str, policy: &Value, matrix: Option<&Value>) -> ToolPolicyData {
        let mut phases: Vec<(String, Vec<String>, String)> = Vec::new();
        if let Some(obj) = policy.get("phases").and_then(Value::as_object) {
            for (phase, spec) in obj {
                let tools: Vec<String> = str_list(spec, "tools");
                phases.push((phase.clone(), tools, s(spec, "source").unwrap_or_default()));
            }
        }
        // Stable phase order: the matrix declares it; fall back to the
        // policy's own (BTree) order.
        if let Some(m) = matrix {
            if let Some(order) = m.get("phases").and_then(Value::as_array) {
                let ranked: Vec<String> = order.iter().filter_map(|p| s(p, "id")).collect();
                phases.sort_by_key(|(id, _, _)| {
                    ranked.iter().position(|r| r == id).unwrap_or(usize::MAX)
                });
            }
        }
        let mut all_tools: Vec<String> = Vec::new();
        if let Some(m) = matrix {
            if let Some(sections) = m.get("sections").and_then(Value::as_array) {
                for sec in sections {
                    if let Some(items) = sec.get("items").and_then(Value::as_array) {
                        for it in items {
                            if let Some(id) = s(it, "id") {
                                if !all_tools.contains(&id) {
                                    all_tools.push(id);
                                }
                            }
                        }
                    }
                }
            }
        }
        if all_tools.is_empty() {
            for (_, tools, _) in &phases {
                for t in tools {
                    if !all_tools.contains(t) {
                        all_tools.push(t.clone());
                    }
                }
            }
        }
        ToolPolicyData {
            entity: entity.to_string(),
            phases,
            all_tools,
        }
    }
}

/// Prompt overlay layers (name, text) — viewer v1.
#[derive(Clone, Debug, Default)]
pub struct PromptData {
    pub entity: String,
    pub layers: Vec<(String, String)>,
}

impl PromptData {
    pub fn from_value(entity: &str, v: &Value) -> PromptData {
        let mut layers = Vec::new();
        if let Some(obj) = v.get("layers").and_then(Value::as_object) {
            for (name, spec) in obj {
                let text = s(spec, "text")
                    .or_else(|| spec.as_str().map(str::to_string))
                    .unwrap_or_default();
                layers.push((name.clone(), text));
            }
        }
        PromptData {
            entity: entity.to_string(),
            layers,
        }
    }
}

/// One consolidation candidate awaiting waking review.
#[derive(Clone, Debug, Default)]
pub struct CandidateRow {
    pub record_id: String,
    pub title: String,
    pub digest: String,
    pub kind: String,
}

pub fn candidates_from_payload(entity: &str, v: &Value) -> (String, Vec<CandidateRow>) {
    let rows = rows_from(v, &["candidates"], |c| {
        Some(CandidateRow {
            record_id: s(c, "record_id")?,
            title: s(c, "title").unwrap_or_default(),
            digest: s(c, "digest").unwrap_or_default(),
            kind: s(c, "kind").unwrap_or_default(),
        })
    });
    (entity.to_string(), rows)
}

/// Per-entity configuration snapshot for the manage menu (substrate,
/// voice, work order, own-time state) — the web Manage drawer's reads.
#[derive(Clone, Debug, Default)]
pub struct EntityDetail {
    pub name: String,
    /// Mind substrate: (provider, model) + where it came from.
    pub substrate: Option<(String, String)>,
    pub substrate_source: String,
    /// The SET voice triple (provider, model, voice) — None = unset.
    pub voice: Option<(String, String, String)>,
    /// What actually applies (falls back to gateway defaults).
    pub voice_effective: Option<String>,
    pub work_order: Option<String>,
    pub loop_running: Option<bool>,
    pub loop_phase: Option<String>,
    /// personal-grant summary line (None = no grant block reported).
    pub grant: Option<String>,
    pub state: Option<String>,
}

impl EntityDetail {
    /// Folds the four manage reads; each part is optional so one failed
    /// read degrades that section, never the whole snapshot.
    pub fn fold(
        name: &str,
        substrate: Option<&Value>,
        voice: Option<&Value>,
        work_order: Option<&Value>,
        cognition: Option<&Value>,
    ) -> EntityDetail {
        let mut d = EntityDetail {
            name: name.to_string(),
            ..EntityDetail::default()
        };
        if let Some(v) = substrate {
            if let (Some(p), Some(m)) = (s(v, "provider"), s(v, "model")) {
                if !p.is_empty() && !m.is_empty() {
                    d.substrate = Some((p, m));
                }
            }
            d.substrate_source = s(v, "source").unwrap_or_default();
        }
        if let Some(v) = voice {
            if let (Some(p), Some(m)) = (s(v, "provider"), s(v, "model")) {
                // Same filter as the substrate arm: the gateway's GET
                // answers `provider: ""` for an UNSET voice (the worker's
                // SaveEntityVoice verify reads that exact shape) — an
                // all-empty triple must render "unset", never " / / ".
                if !p.is_empty() && !m.is_empty() {
                    d.voice = Some((p, m, s(v, "voice").unwrap_or_default()));
                }
            }
            if let Some(eff) = v.get("effective") {
                let triple = [
                    s(eff, "provider").unwrap_or_default(),
                    s(eff, "model").unwrap_or_default(),
                    s(eff, "voice").unwrap_or_default(),
                ];
                if triple.iter().any(|t| !t.is_empty()) {
                    d.voice_effective = Some(format!(
                        "{} ({})",
                        triple.join(" / "),
                        s(eff, "source").unwrap_or_else(|| "?".into())
                    ));
                }
            }
        }
        if let Some(v) = work_order {
            d.work_order = s(v, "order").filter(|o| !o.is_empty());
        }
        if let Some(v) = cognition {
            d.state = v.get("state").and_then(|st| s(st, "state"));
            if let Some(lp) = v.get("loop") {
                d.loop_running = lp.get("running").and_then(Value::as_bool);
                d.loop_phase = s(lp, "phase");
            }
            // The grant block's location/shape is payload-version
            // dependent — render whatever object is there as a short
            // summary rather than guessing fields.
            for key in ["personal_grant", "personal"] {
                if let Some(g) = v.get(key) {
                    if !g.is_null() {
                        let txt = g.to_string();
                        d.grant = Some(txt.chars().take(120).collect());
                        break;
                    }
                }
            }
        }
        d
    }
}

impl Store {
    pub fn create(cx: Scope) -> Store {
        Store {
            conn: cx.signal(ConnPhase::default()),
            net_fail_seq: cx.signal(0),
            conn_retry_spent: cx.signal(Vec::new()),
            probe_gen: cx.signal(0),
            providers: cx.signal(Loadable::default()),
            profiles: cx.signal(Loadable::default()),
            routes: cx.signal(Loadable::default()),
            availability: cx.signal(Loadable::default()),
            download: cx.signal(None),
            host_state: cx.signal(Loadable::default()),
            host_poll_gen: cx.signal(0),
            unload_locked: cx.signal(None),
            users: cx.signal(Loadable::default()),
            entities: cx.signal(Loadable::default()),
            runtimes: cx.signal(Loadable::default()),
            workflows: cx.signal(Loadable::default()),
            runtime_config: cx.signal(Loadable::default()),
            about: cx.signal(Loadable::default()),
            network: cx.signal(Loadable::default()),
            models: cx.signal(HashMap::new()),
            discover: cx.signal(Loadable::default()),
            sandbox: cx.signal(Loadable::default()),
            voices: cx.signal(Loadable::default()),
            route_test: cx.signal(Loadable::default()),
            entity_detail: cx.signal(Loadable::default()),
            entity_policy: cx.signal(Loadable::default()),
            entity_prompt: cx.signal(Loadable::default()),
            entity_candidates: cx.signal(Loadable::default()),
            runs: cx.signal(Loadable::default()),
            data_homes: cx.signal(Loadable::default()),
            artifacts: cx.signal(Loadable::default()),
            logs: cx.signal(Loadable::default()),
            artifact_text: cx.signal(None),
            artifact_image: cx.signal(None),
            log_text: cx.signal(None),
            preview_target: cx.signal(String::new()),
            reservations: cx.signal(Loadable::default()),
            journal: cx.signal(Vec::new()),
            busy: cx.signal(Vec::new()),
            tick: cx.signal(0),
            notice: cx.signal(None),
            last_probe: cx.signal(None),
        }
    }

    /// Forget every cached REMOTE domain (new gateway / new principal).
    /// The ONE list — both reset call sites (`Ctx::reset_domains` for
    /// UI-initiated probes, the worker's `Connect` arm for the boot
    /// auto-probe) call this, so a slot added to the store can never be
    /// forgotten by one copy (the cycle-1 P1 class: stale gateway-A
    /// data rendering under gateway B's header). Journal, last_probe
    /// and conn deliberately survive (session audit).
    pub fn reset_domains(&self) {
        // EXHAUSTIVE destructure, no `..` (round-4 P2-2): adding a
        // field to Store now FAILS COMPILATION here, forcing the
        // reset-or-exempt decision at the one site it matters — the F1
        // class (a new domain slot silently surviving a gateway
        // switch) is structurally impossible instead of
        // test-caught-if-remembered. Exemptions are the named `_`
        // bindings, each with its reason.
        let Store {
            conn: _,          // survives: the probe writes it
            net_fail_seq: _,  // trigger channel, not domain data
            probe_gen: _,     // probe bookkeeping
            conn_retry_spent, // re-armed on reset: new world, new budget
            last_probe: _,    // survives: session audit
            journal: _,       // survives: session audit
            busy: _,          // transient op bookkeeping
            tick: _,          // clock
            notice: _,        // transient toast
            download: _,      // survives: the job is on the OLD host, and
            // its status line is the only record of it
            host_poll_gen, // bumped below: live poll chains must die
            // with the world they were reading
            providers,
            profiles,
            routes,
            availability,
            host_state,
            unload_locked,
            users,
            entities,
            runtimes,
            workflows,
            runtime_config,
            about,
            network,
            models,
            discover,
            sandbox,
            voices,
            route_test,
            entity_detail,
            entity_policy,
            entity_prompt,
            entity_candidates,
            runs,
            data_homes,
            artifacts,
            logs,
            artifact_text,
            artifact_image,
            log_text,
            preview_target,
            reservations,
        } = *self;
        conn_retry_spent.set(Vec::new());
        providers.set(Loadable::NotAsked);
        profiles.set(Loadable::NotAsked);
        routes.set(Loadable::NotAsked);
        // Weights are EXECUTION-HOST state: a different gateway is a
        // different machine, so this domain must never survive a switch.
        availability.set(Loadable::NotAsked);
        // Host state is the same class of machine truth — and its poll
        // chain must not keep painting the OLD host under the new
        // header: the generation bump kills any in-flight chain.
        host_state.set(Loadable::NotAsked);
        host_poll_gen.update(|g| *g += 1);
        unload_locked.set(None);
        users.set(Loadable::NotAsked);
        entities.set(Loadable::NotAsked);
        runtimes.set(Loadable::NotAsked);
        workflows.set(Loadable::NotAsked);
        runtime_config.set(Loadable::NotAsked);
        about.set(Loadable::NotAsked);
        network.set(Loadable::NotAsked);
        models.update(|m| m.clear());
        discover.set(Loadable::NotAsked);
        sandbox.set(Loadable::NotAsked);
        voices.set(Loadable::NotAsked);
        route_test.set(Loadable::NotAsked);
        entity_detail.set(Loadable::NotAsked);
        entity_policy.set(Loadable::NotAsked);
        entity_prompt.set(Loadable::NotAsked);
        entity_candidates.set(Loadable::NotAsked);
        runs.set(Loadable::NotAsked);
        data_homes.set(Loadable::NotAsked);
        artifacts.set(Loadable::NotAsked);
        logs.set(Loadable::NotAsked);
        artifact_text.set(None);
        artifact_image.set(None);
        log_text.set(None);
        preview_target.set(String::new());
        reservations.set(Loadable::NotAsked);
    }

    /// True when a preview result keyed `key` still belongs on screen.
    ///
    /// The one predicate behind [`Store::preview_target`] — the worker
    /// asks before publishing, so a result that lost its race is dropped
    /// rather than painted under the next artifact's header.
    pub fn preview_wanted(&self, key: &str) -> bool {
        self.preview_target.with_untracked(|t| t == key)
    }

    pub fn push_journal(&self, entry: JournalEntry) {
        self.journal.update(|j| j.push(entry));
    }

    pub fn begin_busy(&self, id: u64, label: &str) {
        let label = label.to_string();
        self.busy.update(move |ops| {
            ops.push(BusyOp {
                id,
                label,
                started: Instant::now(),
            })
        });
    }

    pub fn end_busy(&self, id: u64) {
        self.busy.update(move |ops| ops.retain(|o| o.id != id));
    }
}

/// Human-readable byte size (runtimes table, resources strip).
///
/// BINARY math with BINARY labels (IEC), byte-identical to the gateway console
/// (`_fmtBytes`), abstractcode-tui (`ui::modals::human_bytes`), abstractflow
/// (`formatBytes`) and `@abstractframework/monitor-memory`. Memory is binary
/// wherever it is configured or reported — this host reads 137,438,953,472 B =
/// 128.0 GiB exactly, and `sysctl iogpu.wired_limit_mb=110000` lands on
/// 115,343,360,000 B = 107.4 GiB — so the `/1024` here was always right. The
/// `GB`/`MB`/`KB` LABELS on it were the bug: they read as decimal, and the web
/// console really did divide by 1e9, so one 89,986,353,824 B GGUF rendered
/// `89.99 GB` there and `83.8 GB` here. Same math, same units, one decimal
/// place, on all five surfaces. Do not relabel back to `GB`.
pub fn human_bytes(n: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = n as f64;
    let mut u = 0;
    while v >= 1024.0 && u < UNITS.len() - 1 {
        v /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", UNITS[u])
    }
}

/// Timestamp for journal rows (UTC, HH:MM:SSZ — unambiguous in a
/// config journal).
pub fn now_hms() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Render in UTC (a config tool's journal favors unambiguous time).
    let (h, m, s) = ((secs / 3600) % 24, (secs / 60) % 60, secs % 60);
    format!("{h:02}:{m:02}:{s:02}Z")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// THE ROUTE HIERARCHY, AT THE ROW-MODEL LEVEL — the same body as
    /// AbstractCore's console-TUI test of the same name, because the two
    /// grids render the same payload and must not diverge (operator
    /// question 2026-08-01: "why do we have output.image AND
    /// t2i/i2i/upscale — are those remnants?"). They are not:
    /// `output.image` is the PARENT, the one value that serves every
    /// image task without a row of its own.
    #[test]
    fn route_rows_render_the_broad_task_hierarchy() {
        let parent = |covered: bool| {
            RouteRow::from_value(&json!({
                "key": "output.image", "kind": "output", "modality": "image",
                "label": "Image Output", "configured": false,
                "source": "not_configured",
                "task_keys": ["output.image.text_to_image", "output.image.image_to_image",
                              "output.image.image_upscale"],
                "covered_by_tasks": covered
            }))
            .unwrap()
        };
        let child = RouteRow::from_value(&json!({
            "key": "output.image.text_to_image", "kind": "output", "modality": "image",
            "label": "Image Generation", "configured": true, "provider": "mlx-gen",
            "model": "AbstractFramework/flux.2-klein-9b-8bit",
            "source": "abstractcore.gateway_runtime",
            "broad_key": "output.image"
        }))
        .unwrap();

        let covered = parent(true);
        assert!(covered.is_task_parent());
        assert_eq!(covered.task_keys.len(), 3);
        assert_eq!(
            covered.display_key(),
            "output.image",
            "the parent is not indented"
        );
        assert_eq!(
            covered.state_label(),
            "not needed",
            "an unset parent every task row already covers is benign, not a red flag"
        );
        assert!(
            covered.editable(),
            "the parent stays settable — one image model for every task is the simple path"
        );

        assert_eq!(
            parent(false).state_label(),
            "not configured",
            "a parent with an uncovered task under it IS the missing setting"
        );

        assert!(!child.is_task_parent());
        assert_eq!(child.broad_key.as_deref(), Some("output.image"));
        assert_eq!(
            child.display_key(),
            "  └ text_to_image",
            "task rows indent under the parent and drop its repeated prefix"
        );
        // The WRITE path still gets the full three-segment key.
        assert_eq!(child.task.as_deref(), Some("text_to_image"));
        assert_eq!(child.state_label(), "configured");
    }

    /// THE MIRROR, and the shape a FRESH INSTALL has: the seed writes
    /// `output.image` alone, so the three task rows have no value of
    /// their own while the parent answers every one of them. Painting
    /// them "not configured" says "image editing is not set up" about a
    /// machine where it demonstrably is.
    #[test]
    fn task_rows_answered_by_a_configured_parent_read_as_inherited() {
        let inherited = RouteRow::from_value(&json!({
            "key": "output.image.image_upscale", "kind": "output", "modality": "image",
            "label": "Image Restore / Upscale", "configured": false,
            "source": "not_configured", "broad_key": "output.image",
            "inherits_broad": true
        }))
        .unwrap();
        assert_eq!(inherited.state_label(), "inherited");
        assert_eq!(inherited.display_key(), "  └ image_upscale");
        assert!(inherited.editable(), "an inherited row is still settable");

        // Without a configured parent the row is honestly unconfigured.
        let orphan = RouteRow::from_value(&json!({
            "key": "output.video.text_to_video", "kind": "output", "modality": "video",
            "label": "Video Generation", "configured": false,
            "source": "not_configured", "broad_key": "output.video"
        }))
        .unwrap();
        assert_eq!(orphan.state_label(), "not configured");
    }

    /// A modality with NO task rows (voice/sound/music, every input
    /// route) is the PRIMARY key, not a fallback — it must carry no
    /// hierarchy decoration at all. This is why the broad row shape can
    /// never be deleted.
    #[test]
    fn broad_only_modalities_carry_no_hierarchy() {
        let voice = RouteRow::from_value(&json!({
            "key": "output.voice", "kind": "output", "modality": "voice",
            "label": "Voice Output", "configured": true, "provider": "supertonic",
            "model": "supertonic-3", "source": "abstractcore.gateway_runtime"
        }))
        .unwrap();
        assert!(!voice.is_task_parent());
        assert!(voice.broad_key.is_none());
        assert!(!voice.covered_by_tasks);
        assert_eq!(voice.display_key(), "output.voice");
        assert_eq!(voice.state_label(), "configured");
    }

    /// Data-tab attribution: longest data_dir prefix wins (planes nest
    /// under the default root), boundaries are path segments (never
    /// "/a/bc" under "/a/b"), and no match = shared.
    #[test]
    fn home_plane_index_longest_boundary_prefix() {
        let plane = |kind: &str, dir: &str| RuntimeRow {
            kind: kind.into(),
            tenant_id: "default".into(),
            runtime_id: "x".into(),
            label: String::new(),
            owners: vec![],
            data_dir: dir.into(),
            size_bytes: None,
            size_note: None,
            state: None,
            liveness: None,
            note: None,
        };
        let planes = vec![
            plane("default", "/tmp/runtime"),
            plane("entity", "/tmp/runtime/entities/testor"),
        ];
        // Nested store → the entity plane, NOT the default root.
        assert_eq!(
            home_plane_index(&planes, "/tmp/runtime/entities/testor/artifacts"),
            Some(1)
        );
        // Directly under the root → the default plane.
        assert_eq!(home_plane_index(&planes, "/tmp/runtime/artifacts"), Some(0));
        // Exact dir match counts.
        assert_eq!(
            home_plane_index(&planes, "/tmp/runtime/entities/testor"),
            Some(1)
        );
        // Path-BOUNDARY: "/tmp/runtimeX" is NOT under "/tmp/runtime".
        assert_eq!(home_plane_index(&planes, "/tmp/runtimeX/store"), None);
        // Outside every plane → shared.
        assert_eq!(home_plane_index(&planes, "/Users/x/.abstractcore"), None);
    }

    /// Round-2 P2-3: the four folds the consolidation rewrote via
    /// string replacement are exactly the parsers with zero coverage —
    /// pin each against a shape-faithful payload, including the
    /// items|runs key alternation and required-key row drops.
    #[test]
    fn runs_fold_reads_both_keys_and_drops_incomplete_rows() {
        let items = json!({"items": [
            {"run_id": "r-1", "workflow_id": "wf", "status": "running",
             "updated_at": "2026-07-24T10:00:00Z", "paused": false},
            {"workflow_id": "no-run-id-row"},
        ]});
        let rows = runs_from_payload(&items);
        assert_eq!(rows.len(), 1, "missing run_id row dropped");
        assert_eq!(rows[0].run_id, "r-1");
        assert_eq!(rows[0].status, "running");

        let runs_key = json!({"runs": [{"run_id": "r-2"}]});
        let rows = runs_from_payload(&runs_key);
        assert_eq!(rows.len(), 1, "the 'runs' key alternation reads");
        assert_eq!(rows[0].run_id, "r-2");

        assert!(runs_from_payload(&json!({})).is_empty());
    }

    #[test]
    fn data_homes_fold_reads_rows() {
        let v = json!({"homes": [
            {"name": "workspaces", "path": "/tmp/w", "kind": "workspace",
             "owner": "gateway", "safe_to_purge": true, "description": "d"},
            {"path": "/no-name"},
        ]});
        let rows = data_homes_from_payload(&v);
        assert_eq!(rows.len(), 1, "missing name row dropped");
        assert_eq!(rows[0].name, "workspaces");
        assert!(rows[0].safe_to_purge);
    }

    #[test]
    fn reservations_fold_reads_rows() {
        let v = json!({"runtime_reservations": [
            {"tenant_id": "default", "runtime_id": "rt-1",
             "owner_user_id": "bob", "reason": "deleted", "data_exists": true},
            {"tenant_id": "no-runtime-id"},
        ]});
        let rows = reservations_from_payload(&v);
        assert_eq!(rows.len(), 1, "missing runtime_id row dropped");
        assert_eq!(rows[0].runtime_id, "rt-1");
        assert!(rows[0].data_exists);
    }

    #[test]
    fn candidates_fold_reads_rows_and_carries_entity() {
        let v = json!({"candidates": [
            {"record_id": "ex:1", "title": "t", "digest": "d", "kind": "interest"},
            {"title": "no-record-id"},
        ]});
        let (entity, rows) = candidates_from_payload("castor", &v);
        assert_eq!(entity, "castor");
        assert_eq!(rows.len(), 1, "missing record_id row dropped");
        assert_eq!(rows[0].record_id, "ex:1");
    }

    /// The c5308 contract migration: `principal_kind` is the ONE kind
    /// source when present (a human row carrying a legacy "entity"
    /// role string must NOT be misfiled); the roles convention only
    /// classifies rows from pre-contract gateways.
    #[test]
    fn users_partition_prefers_principal_kind_over_roles() {
        let v = json!({"users": [
            {"user_id": "admin", "roles": ["admin", "user"], "principal_kind": "human"},
            {"user_id": "castorp", "roles": ["entity"], "principal_kind": "entity"},
            // Contract field WINS over a contradictory roles list.
            {"user_id": "oddball", "roles": ["entity"], "principal_kind": "human"},
            // Pre-contract row (no field): the roles fallback applies.
            {"user_id": "legacy-entity", "roles": ["entity"]},
            {"user_id": "legacy-human", "roles": ["user"]},
        ]});
        let d = users_from_payload(&v);
        let names: Vec<&str> = d.humans.iter().map(|u| u.user_id.as_str()).collect();
        assert_eq!(names, vec!["admin", "oddball", "legacy-human"]);
        assert_eq!(d.entity_principals, 2);
    }

    /// THE provider-name join law, pinned against the LIVE payload
    /// shapes (2026-07-25 dump): managed rows carry no provider_id and
    /// answer to endpoint:<id>; synthetic rows carry a bare provider_id
    /// and MUST use it — the gateway answers "Unknown provider:
    /// endpoint:anthropic" for the prefixed form (live-verified).
    #[test]
    fn profile_provider_name_join_law() {
        let managed = Profile::from_value(&json!({
            "id": "airelay", "virtual_provider": "endpoint:airelay",
            "provider_family": "openai-compatible", "scope": "gateway",
            "enabled": true, "api_key_set": true
        }))
        .unwrap();
        assert!(!managed.synthetic, "managed row (both flags absent)");
        assert_eq!(managed.provider_name(), "endpoint:airelay");

        let synthetic = Profile::from_value(&json!({
            "id": "anthropic", "provider_id": "anthropic",
            "provider_family": "anthropic", "scope": "environment",
            "enabled": true, "managed": false, "synthetic": true,
            "source": "environment", "discovered_model_count": 0
        }))
        .unwrap();
        assert!(synthetic.synthetic);
        assert_eq!(synthetic.provider_name(), "anthropic");
        assert_eq!(synthetic.source.as_deref(), Some("environment"));

        // managed:false alone (web checks the disjunction) still
        // classifies as synthetic.
        let managed_false = Profile::from_value(&json!({
            "id": "lmstudio", "provider_id": "lmstudio", "managed": false,
            "source": "reachable-default", "scope": "core",
            "discovered_model_count": 48
        }))
        .unwrap();
        assert!(managed_false.synthetic);
        assert_eq!(managed_false.discovered_model_count, Some(48));
    }

    /// The unconfigured-backends fold against the live join topology:
    /// profile `airelay` ↔ discovery `endpoint:airelay`, synthetic
    /// `anthropic` ↔ bare `anthropic`; discovery-only names (ollama
    /// not probed-up, mlx, and the live orphan `endpoint:unrelated`)
    /// surface; nothing double-lists, nothing vanishes.
    #[test]
    fn unconfigured_provider_names_join() {
        let profiles = ProfilesData::from_value(&json!({
            "profiles": [
                {"id": "airelay", "provider_family": "openai-compatible", "scope": "gateway"},
                {"id": "anthropic", "provider_id": "anthropic", "managed": false,
                 "synthetic": true, "source": "environment", "scope": "environment"},
                {"id": "lmstudio", "provider_id": "lmstudio", "managed": false,
                 "synthetic": true, "source": "reachable-default", "scope": "core"}
            ]
        }));
        let providers = ProvidersData::from_value(&json!({
            "items": [
                {"name": "anthropic"}, {"name": "endpoint:airelay"},
                {"name": "endpoint:unrelated"}, {"name": "lmstudio"},
                {"name": "mlx"}, {"name": "ollama"}
            ]
        }));
        let free = unconfigured_provider_names(&profiles, &providers);
        assert_eq!(free, vec!["endpoint:unrelated", "mlx", "ollama"]);
    }

    /// The runtime→runs join (COMPLAINT B): the default plane maps to
    /// the Own lane (same store, live-verified; richer rows; the only
    /// actionable inbox), every other plane to the admin drill-in —
    /// and only Own is cancel/steer-reachable.
    #[test]
    fn run_scope_maps_planes_and_gates_actions() {
        let default_row = RuntimeRow::from_value(&json!({
            "kind": "default", "tenant_id": "default", "runtime_id": "default",
            "label": "Gateway default runtime"
        }))
        .unwrap();
        assert_eq!(RunScope::of_runtime(&default_row), RunScope::Own);
        assert!(RunScope::of_runtime(&default_row).actionable());

        let entity_row = RuntimeRow::from_value(&json!({
            "kind": "entity", "tenant_id": "default",
            "runtime_id": "runtime_castor", "label": "castor"
        }))
        .unwrap();
        let scope = RunScope::of_runtime(&entity_row);
        assert_eq!(
            scope,
            RunScope::Plane {
                kind: "entity".into(),
                tenant_id: "default".into(),
                runtime_id: "runtime_castor".into(),
                label: "castor".into(),
            }
        );
        assert!(!scope.actionable(), "foreign planes are not actionable");
        assert_eq!(scope.short(), "castor");

        // Label falls back to tenant/runtime when the row has none.
        let unlabeled = RuntimeRow::from_value(&json!({
            "kind": "user", "tenant_id": "default", "runtime_id": "bob"
        }))
        .unwrap();
        assert_eq!(
            RunScope::of_runtime(&unlabeled).short(),
            "default/bob",
            "label fallback"
        );
    }

    /// The drill-in payload carries parent_run_id (children included —
    /// no root_only param exists on that endpoint); the fold keeps it
    /// so the plane lane can root-filter client-side.
    #[test]
    fn runs_fold_carries_parent_run_id_for_root_filtering() {
        let v = json!({"items": [
            {"run_id": "root-1", "parent_run_id": null},
            {"run_id": "child-1", "parent_run_id": "root-1"},
            {"run_id": "root-2", "parent_run_id": ""},
        ]});
        let rows = runs_from_payload(&v);
        assert_eq!(rows.len(), 3);
        assert!(rows[0].parent_run_id.is_none());
        assert_eq!(rows[1].parent_run_id.as_deref(), Some("root-1"));
        assert!(
            rows[2].parent_run_id.is_none(),
            "empty string folds to None (root)"
        );
    }

    /// The host-state fold against the endpoint's documented shape:
    /// gauges, tri-state resident, additive-optional lock fields,
    /// degraded sections with reasons, server totals kept verbatim.
    #[test]
    fn host_state_fold_reads_gauges_rows_and_degradation() {
        let d = host_state_from_payload(&json!({
            "ok": true, "ts": 1756252800.0,
            "host": {"host_id": "h-1", "host_name": "studio.local"},
            "memory": {
                "ram": {"total_bytes": 128_000u64, "available_bytes": 48_000u64,
                        "used_bytes": 80_000u64, "percent": 62.5},
                "process": {"rss_bytes": 10_000u64},
                "device": {"backend": "metal", "allocated_bytes": 5_000u64,
                           "total_bytes": 50_000u64, "free_bytes": 45_000u64}
            },
            "gpu": {"supported": false},
            "models": [
                {"runtime_id": "r1", "task": "text_generation", "provider": "mlx",
                 "model": "qwen", "resident": true, "locked": true, "lockable": true,
                 "size_bytes": 1024u64, "context_length": 8192u64,
                 "calibrated_context_length": 4096u64, "context_calibrated": true,
                 "modalities": ["input.text", "output.text"], "default": true},
                // resident: null — the tri-state's third answer.
                {"runtime_id": "r2", "task": null, "provider": "lmstudio",
                 "model": "mystery", "resident": null}
            ],
            "session_caches": [
                {"key": "agw.pc.v1.s-sess1:session", "provider": "mlx", "model": "qwen",
                 "session_id": "sess1", "bytes": 4096u64, "token_count": 100u64}
            ],
            "totals": {"models": 2, "model_bytes": 1024u64,
                       "session_caches": 1, "session_cache_bytes": 4096u64},
            "degraded": ["gpu"],
            "reasons": {"gpu": "gpu metrics probe returned a non-dict payload"}
        }));
        let ram = d.ram.as_ref().expect("ram gauge");
        assert_eq!(ram.percent, Some(62.5));
        assert_eq!(ram.available_bytes, Some(48_000));
        assert_eq!(d.process_rss, Some(10_000));
        let dev = d.device.as_ref().expect("device gauge");
        assert_eq!(dev.backend, "metal");
        assert_eq!(dev.allocated_bytes, Some(5_000));
        assert!(!d.gpu_supported);
        assert_eq!(d.host_name.as_deref(), Some("studio.local"));
        assert_eq!(d.models.len(), 2);
        assert_eq!(d.models[0].resident, Some(true));
        assert_eq!(d.models[0].locked, Some(true));
        assert_eq!(d.models[0].context_calibrated, Some(true));
        assert!(d.models[1].resident.is_none(), "null resident stays None");
        assert!(d.models[1].task.is_none(), "null task stays None");
        assert_eq!(d.caches.len(), 1);
        assert_eq!(d.caches[0].session_id, "sess1");
        assert_eq!(d.model_bytes, Some(1024));
        assert_eq!(d.cache_bytes, Some(4096));
        assert_eq!(d.degraded, vec!["gpu"]);
        assert!(
            d.reasons.contains_key("gpu"),
            "reason travels with the section"
        );
    }

    /// The LIVE wire shape, captured from `GET /api/gateway/host/state`
    /// on the operator's Mac (2026-08-28) — pinned verbatim because this
    /// crate was twice bitten by fixtures invented to match the parser
    /// instead of the gateway. `allocated_bytes` really is **0** while
    /// 98.5 GB of weights are resident: it is PROCESS-LOCAL on Metal.
    fn live_metal_payload() -> Value {
        json!({
            "ok": true, "ts": 1787894010.297934f64,
            "memory": {
                "ram": {"total_bytes": 137438953472u64, "available_bytes": 20000000000u64,
                        "used_bytes": 117438953472u64, "percent": 85.4},
                "process": {"rss_bytes": 412000000u64},
                "device": {"backend": "metal",
                           "allocated_bytes": 0u64,
                           "total_bytes": 137438953472u64,
                           "free_bytes": 31694962688u64,
                           "host_in_use_bytes": 105743990784u64,
                           "wired_limit_bytes": 115343360000u64},
                "host": {"host_name": "studio.local"}
            },
            "gpu": {"supported": false},
            "models": [
                // Externally loaded (LM Studio), found by the residency
                // sweep: no measured size, only an ESTIMATE — and lock
                // now ADOPTS it, so it is offered the lock verb. The
                // wire stamps such a row `source: "provider_server"` and
                // `lockable: true` (the wave's sweep does), which is why
                // ADOPT keys off the SOURCE and never off `lockable`.
                {"runtime_id": null, "task": "text_generation", "provider": "lmstudio",
                 "model": "glm-4.6-gguf", "source": "provider_server", "resident": true,
                 "state": "provider_loaded", "locked": false, "lockable": true,
                 "size_bytes": null, "size_vram_bytes": null,
                 "est_weights_bytes": 99857989632u64, "cache_bytes": 2147483648u64,
                 "context_length": 131072u64, "calibrated_context_length": null,
                 "modalities": ["input.text", "output.text"], "default": false},
                // A configured row the host does NOT hold: no size at all.
                {"task": "text_generation", "provider": "mlx", "model": "qwen3-8b",
                 "source": "config", "resident": false, "locked": null,
                 "lockable": null, "est_weights_bytes": null, "cache_bytes": null}
            ],
            "session_caches": [],
            "totals": {"models": 2, "models_resident": 1, "model_bytes": null,
                       "cache_bytes_models": 2147483648u64,
                       "session_caches": 0, "session_cache_bytes": null},
            "degraded": [], "reasons": {}
        })
    }

    /// The additive row_v1 fields fold, and the display-size coalesce
    /// runs `size_bytes` → `size_vram_bytes` → `est_weights_bytes` with
    /// the estimate FLAGGED (never printed as measured).
    #[test]
    fn row_v1_estimate_and_cache_fields_fold_and_coalesce() {
        let d = host_state_from_payload(&live_metal_payload());
        let sweep = &d.models[0];
        assert_eq!(sweep.est_weights_bytes, Some(99_857_989_632));
        assert_eq!(sweep.cache_bytes, Some(2_147_483_648));
        assert_eq!(sweep.size_bytes, None, "the sweep row measured nothing");
        assert_eq!(sweep.display_size(), Some((99_857_989_632, true)));
        assert_eq!(size_marked(99_857_989_632, true), "~93.0 GiB");
        assert_eq!(size_marked(99_857_989_632, false), "93.0 GiB");
        assert_eq!(d.model_cache_bytes, Some(2_147_483_648));

        // Coalesce order, pinned: reported RAM size wins over VRAM wins
        // over the estimate; nothing known stays None (never a 0).
        let both = ModelRow {
            size_bytes: Some(10),
            size_vram_bytes: Some(20),
            est_weights_bytes: Some(30),
            ..ModelRow::default()
        };
        assert_eq!(both.display_size(), Some((10, false)));
        let vram = ModelRow {
            size_vram_bytes: Some(20),
            est_weights_bytes: Some(30),
            ..ModelRow::default()
        };
        assert_eq!(vram.display_size(), Some((20, false)));
        assert_eq!(ModelRow::default().display_size(), None);
    }

    /// BINARY math, BINARY labels, every tier. The math was always `/1024`
    /// here; the `GB`/`MB`/`KB` labels on it were the bug — they read as
    /// decimal, and the gateway web console really did divide by 1e9, so one
    /// 89,986,353,824 B GGUF rendered `83.8 GB` here and `89.99 GB` there.
    #[test]
    fn byte_humanizing_is_binary_with_binary_labels() {
        assert_eq!(human_bytes(0), "0 B");
        assert_eq!(human_bytes(512), "512 B");
        assert_eq!(human_bytes(1023), "1023 B");
        assert_eq!(human_bytes(1024), "1.0 KiB");
        assert_eq!(human_bytes(1024 * 1024), "1.0 MiB");
        assert_eq!(human_bytes(1024 * 1024 * 1024), "1.0 GiB");
        assert_eq!(human_bytes(1024u64.pow(4)), "1.0 TiB");
        for s in [human_bytes(1024), human_bytes(1024u64.pow(3))] {
            assert!(
                !s.ends_with(" KB") && !s.ends_with(" GB"),
                "a binary quotient may never carry a decimal unit: {s}"
            );
        }
    }

    /// THE CROSS-SURFACE PIN. These six byte counts are the shared set; the
    /// same assertions exist in `abstractcode-tui` (`ui::modals::human_bytes`),
    /// the gateway web console (`_fmtBytes`), abstractflow (`formatBytes`) and
    /// `@abstractframework/monitor-memory` (`formatBytes`). If this test and
    /// its four siblings stop agreeing string-for-string, one model reads as
    /// two different sizes depending on which surface the operator opens.
    #[test]
    fn byte_humanizing_matches_every_other_surface_on_the_shared_set() {
        // the operator's sharded GGUF
        assert_eq!(human_bytes(89_986_353_824), "83.8 GiB");
        // Σ model weights
        assert_eq!(human_bytes(93_096_269_257), "86.7 GiB");
        // `sysctl iogpu.wired_limit_mb=110000`
        assert_eq!(human_bytes(115_343_360_000), "107.4 GiB");
        // this machine's RAM total — exactly 128 GiB, which is what the
        // hardware is sold as and why binary is the right math for memory
        assert_eq!(human_bytes(137_438_953_472), "128.0 GiB");
        // lmstudio qwen3-vl-4b
        assert_eq!(human_bytes(3_109_915_433), "2.9 GiB");
        // session caches
        assert_eq!(human_bytes(4_352_519_172), "4.1 GiB");
    }

    /// THE METAL BUG: `allocated_bytes` is process-local and reads 0 with
    /// 98.5 GB resident. The meter must prefer the all-processes pair and
    /// SAY that is what it shows — a 0 B bar beside a loaded accelerator
    /// is the defect this rule exists to kill. The SCOPE WORDS are the
    /// spec's (PART A2/A3): `all processes` / `this process only`, and
    /// the label is the exact `Accelerator heap · <backend> (<scope>)`.
    #[test]
    fn device_meter_prefers_the_host_figure_over_the_process_local_zero() {
        let d = host_state_from_payload(&live_metal_payload());
        let dev = d.device.as_ref().expect("device gauge");
        assert_eq!(dev.allocated_bytes, Some(0), "the wire really says 0");
        assert_eq!(
            dev.meter(),
            Some((105_743_990_784, 115_343_360_000, DeviceScope::Host))
        );
        assert_eq!(DeviceScope::Host.label(), "all processes");
        assert_eq!(DeviceScope::Process.label(), "this process only");
        assert_eq!(
            dev.label().as_deref(),
            Some("Accelerator heap · metal (all processes)")
        );
        assert_eq!(
            accelerator_label("", DeviceScope::Process),
            "Accelerator heap · device (this process only)",
            "an unknown backend prints the literal `device`"
        );
        // The FIGURE survives a missing ceiling: a reference line needs
        // no denominator even though a bar does.
        let no_ceiling = DeviceGauge {
            backend: "cuda".into(),
            host_in_use_bytes: Some(7),
            ..DeviceGauge::default()
        };
        assert_eq!(no_ceiling.accelerator(), Some((7, None, DeviceScope::Host)));
        assert_eq!(no_ceiling.meter(), None, "no ceiling, no bar");

        // No host figure at all → the process-local pair, LABELLED.
        let process_only = DeviceGauge {
            allocated_bytes: Some(5_000),
            total_bytes: Some(50_000),
            ..DeviceGauge::default()
        };
        assert_eq!(
            process_only.meter(),
            Some((5_000, 50_000, DeviceScope::Process))
        );
        // A host figure with no wired limit still outranks it.
        let no_limit = DeviceGauge {
            allocated_bytes: Some(0),
            total_bytes: Some(50_000),
            host_in_use_bytes: Some(40_000),
            ..DeviceGauge::default()
        };
        assert_eq!(no_limit.meter(), Some((40_000, 50_000, DeviceScope::Host)));
        // Nothing known → nothing drawn.
        assert_eq!(DeviceGauge::default().meter(), None);
    }

    /// THE SHARED FIXTURE (SPEC PART C) — the live payload from the
    /// operator's Mac, the SAME JSON the web console, abstractflow and
    /// the abstractcode TUI pin, so the four surfaces cannot drift. A
    /// fully offloaded (`n_gpu_layers=-1`) three-shard GGUF: 89,986,353,824 B
    /// of weights resident, process RSS 76,762,775,552 B, `allocated_bytes: 0`
    /// and an accelerator heap of 1,042,120,704 B — 0.76% of a 137 GB
    /// machine. The itemized weights EXCEED the accelerator figure, and
    /// that is the NORMAL memory-mapped case, not an inconsistency.
    fn spec_part_c_payload() -> Value {
        json!({
          "ok": true,
          "memory": {
            "ram": {"total_bytes": 137438953472u64, "available_bytes": 96368312320u64,
                    "used_bytes": 33741111296u64, "percent": 29.9},
            "process": {"rss_bytes": 76762775552u64},
            "device": {"backend": "metal", "allocated_bytes": 0u64,
                       "total_bytes": 137438953472u64, "free_bytes": null,
                       "host_in_use_bytes": 1042120704u64,
                       "wired_limit_bytes": 115343360000u64}
          },
          "models": [
            {"runtime_id": "local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
             "task": "text_generation", "provider": "huggingface",
             "model": "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
             "source": "provider_server", "resident": true, "state": "provider_loaded",
             "locked": false, "lockable": true,
             "est_weights_bytes": 89986353824u64, "cache_bytes": 2147483648u64,
             "details": {"est_weights_bytes": 89986353824u64, "cache_bytes": 2147483648u64}}
          ],
          "totals": {"models": 1, "models_resident": 1, "model_bytes": 89986353824u64,
                     "session_caches": 3, "session_cache_bytes": 4352519172u64}
        })
    }

    /// THE BREAKDOWN RULE SET (SPEC PART B), pinned on the PART C
    /// payload: ITEMS the framework can name, REFERENCE counters that
    /// must never be added to them, and the GGUF NOTE when the summed
    /// weights exceed the accelerator heap. Keys and BYTES are the
    /// contract — the formatting is this surface's own (binary math with
    /// a `GB` label, pre-existing and out of scope).
    ///
    /// The old `unattributed` remainder is GONE: it subtracted
    /// RAM-dimensioned quantities from an accelerator counter, computed
    /// ~−79 GB on this very payload and clamped the lie to `0 B`.
    #[test]
    fn memory_breakdown_items_references_and_the_gguf_note() {
        let d = host_state_from_payload(&spec_part_c_payload());
        let b = memory_breakdown(&d);
        let of_kind = |k: BreakdownKind| -> Vec<&BreakdownLine> {
            b.iter().filter(|l| l.kind == k).collect()
        };
        let items = of_kind(BreakdownKind::Item);
        let refs = of_kind(BreakdownKind::Reference);
        let notes = of_kind(BreakdownKind::Note);

        // 1 — the ITEM keys, in order.
        assert_eq!(
            items.iter().map(|l| l.key.as_str()).collect::<Vec<_>>(),
            vec![
                "model:local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
                "model_caches",
                "session_caches",
                "process_rss",
            ]
        );
        // 2 — and their BYTE values, in order.
        assert_eq!(
            items.iter().map(|l| l.bytes).collect::<Vec<_>>(),
            vec![
                Some(89_986_353_824),
                Some(2_147_483_648),
                Some(4_352_519_172),
                Some(76_762_775_552),
            ]
        );
        // 3 — the REFERENCE keys, in order, all of them AFTER the items.
        assert_eq!(
            refs.iter().map(|l| l.key.as_str()).collect::<Vec<_>>(),
            vec!["sum_model_weights", "ram", "accelerator"]
        );
        assert!(
            b.iter()
                .position(|l| l.kind == BreakdownKind::Reference)
                .unwrap()
                > b.iter()
                    .rposition(|l| l.kind == BreakdownKind::Item)
                    .unwrap(),
            "references come after every item"
        );
        // 4 — Σ model weights is the sum of the model items.
        assert_eq!(refs[0].bytes, Some(89_986_353_824));
        assert_eq!(refs[0].label, "Σ model weights");
        assert_eq!(refs[0].note, "sum of the resident model weights above");
        // 5 — the accelerator reference wears the PART A2 label and the
        // note that says what the counter is blind to.
        assert!(
            refs[2]
                .label
                .contains("Accelerator heap · metal (all processes)"),
            "the A2 label, verbatim: {}",
            refs[2].label
        );
        assert_eq!(
            refs[2].note,
            "memory-mapped GGUF weights are not counted here"
        );
        assert_eq!(refs[2].bytes, Some(1_042_120_704));
        // 6 — 89_986_353_824 > 1_042_120_704, so the GGUF case is NAMED,
        // in the spec's words and no others.
        assert_eq!(notes.len(), 1, "the note is emitted exactly once");
        assert_eq!(
            notes[0].label,
            "Σ model weights exceeds the accelerator heap. That is the normal case for \
             memory-mapped GGUF weights: llama.cpp maps them from disk, so they are resident \
             as process RSS and are not counted in the accelerator heap."
        );
        // 7 — the remainder is GONE from every field of every line.
        assert!(
            !b.iter().any(|l| [&l.key, &l.label, &l.size, &l.note]
                .iter()
                .any(|s| s.contains("nattributed"))),
            "no line may still speak of an unattributed remainder: {b:#?}"
        );
        // 8 — and so is the forbidden scope word.
        assert!(
            !b.iter()
                .any(|l| l.label.contains("host-wide") || l.note.contains("host-wide")),
            "`host-wide` is a deleted scope name: {b:#?}"
        );

        // The item lines say WHAT they measure and WHICH field supplied
        // the number, and an estimate stays MARKED (never printed as
        // measured) — including in the Σ built from it.
        assert_eq!(items[0].label, "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL");
        assert_eq!(
            items[0].note,
            "resident model weights · estimated on-disk weight size (est_weights_bytes)"
        );
        assert_eq!(items[0].size, "~83.8 GiB");
        assert_eq!(refs[0].size, "~83.8 GiB");
        assert_eq!(items[1].label, "model KV caches");
        assert_eq!(items[1].note, "prompt-cache bytes held for resident models");
        assert_eq!(items[2].label, "session caches");
        assert_eq!(items[2].note, "prompt-cache bytes held by gateway sessions");
        assert_eq!(items[3].label, "gateway process RSS");
        assert_eq!(
            items[3].note,
            "resident set size of the gateway process — includes memory-mapped GGUF weights"
        );
        assert_eq!(refs[1].label, "RAM used");
        assert_eq!(refs[1].note, "system memory in use / installed");
        assert_eq!(refs[1].size, "31.4 GiB / 128.0 GiB");

        // THE EMISSION RULE, one for every surface: a KNOWN value is
        // emitted — a known ZERO included — and an unknown one is
        // omitted. No line is "always emitted"; none is conditional on
        // being non-zero.
        let mut zeroed = d.clone();
        zeroed.process_rss = Some(0);
        assert!(
            memory_breakdown(&zeroed)
                .iter()
                .any(|l| l.key == "process_rss" && l.bytes == Some(0)),
            "a known 0 IS a fact"
        );
        let mut unknown = d.clone();
        unknown.process_rss = None;
        unknown.cache_bytes = None;
        unknown.model_cache_bytes = None;
        unknown.models[0].cache_bytes = None;
        assert_eq!(
            memory_breakdown(&unknown)
                .iter()
                .filter(|l| l.kind == BreakdownKind::Item)
                .map(|l| l.key.as_str())
                .collect::<Vec<_>>(),
            vec!["model:local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL"],
            "unknown values produce NO line, never a fabricated 0"
        );

        // A resident row with no known size is SKIPPED — and with no
        // model item there is no Σ, hence no note to compare against.
        let mut sizeless = d.clone();
        sizeless.models[0].est_weights_bytes = None;
        let b2 = memory_breakdown(&sizeless);
        assert!(!b2.iter().any(|l| l.per_model), "no size, no invented line");
        assert!(!b2.iter().any(|l| l.key == "sum_model_weights"));
        assert!(!b2.iter().any(|l| l.kind == BreakdownKind::Note));

        // The note is CONDITIONAL, not decorative: an accelerator heap
        // bigger than the weights (an MLX host) gets no explanation.
        let mut mlx_like = d.clone();
        mlx_like.device.as_mut().unwrap().host_in_use_bytes = Some(100_000_000_000);
        assert!(
            !memory_breakdown(&mlx_like)
                .iter()
                .any(|l| l.kind == BreakdownKind::Note),
            "weights below the heap need no GGUF note"
        );
        // THE ITEM KEY RULE for the rows the wire sends WITHOUT a
        // runtime_id (every real sweep row): `model:<provider>:<model>`,
        // no index suffix, no task segment, no empty segment.
        let sweep = HostStateData {
            models: vec![ModelRow {
                provider: Some("lmstudio".into()),
                model: Some("qwen/qwen3-vl-4b".into()),
                resident: Some(true),
                est_weights_bytes: Some(4_000_000_000),
                ..ModelRow::default()
            }],
            ..HostStateData::default()
        };
        assert_eq!(
            memory_breakdown(&sweep)[0].key,
            "model:lmstudio:qwen/qwen3-vl-4b"
        );

        // No accelerator figure at all → no accelerator reference, and
        // no note (a comparison against nothing is a fabrication).
        let mut blind = d.clone();
        let dev = blind.device.as_mut().unwrap();
        dev.host_in_use_bytes = None;
        dev.allocated_bytes = None;
        let b3 = memory_breakdown(&blind);
        assert!(!b3.iter().any(|l| l.key == "accelerator"));
        assert!(!b3.iter().any(|l| l.kind == BreakdownKind::Note));
    }

    /// Refinement 1: EVERY resident line offers the lock verb — including
    /// the rows the host loaded outside the Gateway, which
    /// `POST /models/lock` now adopts. Locked outranks residency (a
    /// locked-but-evicted row keeps Unlock); a row the host says is not
    /// resident gets no lock verb and no unload, with the reason named
    /// (the F2/F3 refusal law).
    ///
    /// THE ADOPT SELECTOR (SPEC PART D1) is `source == "provider_server"`
    /// and nothing else — not `lockable`, which the sweep stamps `true`,
    /// and not the old tolerant aliases, which never ride the wire.
    #[test]
    fn lock_action_follows_residency_and_adopts_sweep_rows() {
        let d = host_state_from_payload(&live_metal_payload());
        assert_eq!(
            lock_action(&d.models[0]),
            LockAction::Lock { adopt: true },
            "the wire's provider_server row is lockable, and locking ADOPTS it"
        );
        // The SET is gone: exactly `provider_server` adopts.
        for source in ["local", "config", "sweep", "external", "PROVIDER_SERVER"] {
            assert_eq!(
                lock_action(&ModelRow {
                    resident: Some(true),
                    source: Some(source.into()),
                    ..ModelRow::default()
                }),
                LockAction::Lock { adopt: false },
                "`{source}` is not the gateway's adoption stamp"
            );
        }
        assert_eq!(
            lock_action(&ModelRow {
                resident: Some(true),
                source: None,
                ..ModelRow::default()
            }),
            LockAction::Lock { adopt: false },
            "an unknown source is not an adoption claim"
        );
        assert!(unload_refusal(&d.models[0]).is_none());
        assert!(
            matches!(lock_action(&d.models[1]), LockAction::Refused(_)),
            "resident:false offers no lock"
        );
        assert!(unload_refusal(&d.models[1]).is_some());

        let locked_evicted = ModelRow {
            locked: Some(true),
            resident: Some(false),
            ..ModelRow::default()
        };
        assert_eq!(
            lock_action(&locked_evicted),
            LockAction::Unlock,
            "a locked-but-evicted row keeps its Unlock"
        );
        let refused = ModelRow {
            resident: Some(true),
            lockable: Some(false),
            ..ModelRow::default()
        };
        assert!(matches!(lock_action(&refused), LockAction::Refused(_)));
        let unknown = ModelRow {
            resident: None,
            ..ModelRow::default()
        };
        assert!(
            matches!(lock_action(&unknown), LockAction::Refused(_)),
            "residency unknown is not a claim of residency"
        );
        assert!(
            unload_refusal(&unknown).is_none(),
            "…but unknown is not a 'no' either: the gateway is the authority"
        );
    }

    /// The tri-state vocabulary: None is a THIRD answer, never "no".
    #[test]
    fn resident_label_is_tri_state() {
        assert_eq!(resident_label(Some(true)), "yes");
        assert_eq!(resident_label(Some(false)), "no");
        assert_eq!(resident_label(None), "unknown");
    }

    /// The verify read (`/models/loaded`) serves rows under "rows";
    /// recount re-derives totals with the sum-of-known rule (no row
    /// with a size → None, never a fabricated 0).
    #[test]
    fn model_rows_payload_and_recount() {
        let rows = model_rows_from_payload(&json!({"rows": [
            {"runtime_id": "r1", "provider": "mlx", "model": "qwen", "size_bytes": 10u64},
            {"runtime_id": "r2", "provider": "mlx", "model": "other"}
        ]}));
        assert_eq!(rows.len(), 2);
        let mut d = HostStateData {
            models: rows,
            ..HostStateData::default()
        };
        d.recount();
        assert_eq!(d.model_bytes, Some(10));
        assert_eq!(d.cache_bytes, None, "no cache row carries bytes → None");
        d.models.clear();
        d.recount();
        assert_eq!(d.model_bytes, None, "no sized rows → None, not 0");
    }

    /// Round-2 P2-2 regression pin: an all-empty voice triple from the
    /// gateway means UNSET (the same shape the worker's save-verify
    /// reads) — it must fold to None, exactly like the substrate arm.
    #[test]
    fn entity_detail_fold_treats_empty_voice_as_unset() {
        let voice = json!({"provider": "", "model": "", "voice": ""});
        let d = EntityDetail::fold("castor", None, Some(&voice), None, None);
        assert!(d.voice.is_none(), "all-empty triple is unset");

        let set = json!({"provider": "kokoro", "model": "m1", "voice": "af"});
        let d = EntityDetail::fold("castor", None, Some(&set), None, None);
        assert_eq!(d.voice, Some(("kokoro".into(), "m1".into(), "af".into())));
    }
}
