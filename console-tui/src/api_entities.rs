//! Entity parity with the web console: summon (create) with templates +
//! dry-run validate, spark-template management, the identity card, the
//! hosted chat visit ("Talk"), and the voice audition.
//!
//! Client methods (an `impl GatewayClient` block — a child module of
//! `api`, so it reuses the one transport) plus the typed folds of the
//! payloads those routes serve. Every route here is the SAME route the
//! web console calls (`console.py` — createEntity, tplSave, loadEntities,
//! loadEntityOverview, entityChatOpen/Send/Close, entityVoiceAudition).

use serde_json::{json, Map, Value};

use super::{err_from_ureq, urlencode, ApiError, ApiErrorKind, ApiResult, GatewayClient};

/// `/entities/{name}/<leaf>` with the name urlencoded.
fn entity_leaf(name: &str, leaf: &str) -> String {
    format!("/entities/{}/{}", urlencode(name), leaf)
}

impl GatewayClient {
    // ---- creation kit (the web's loadEntities + loadSubstrateDropdowns) --

    /// The spark-template gallery: builtin floor + operator templates at
    /// their current version (`{templates, warnings}`).
    pub fn entity_templates(&self) -> ApiResult<Value> {
        self.get("/entities/templates", false)
    }

    /// One template's append-only version history.
    pub fn entity_template_versions(&self, template_id: &str) -> ApiResult<Value> {
        self.get(
            &format!("/entities/templates/{}/versions", urlencode(template_id)),
            false,
        )
    }

    /// One template at its current version (the template save's verify).
    pub fn entity_template(&self, template_id: &str) -> ApiResult<Value> {
        self.get(
            &format!("/entities/templates/{}", urlencode(template_id)),
            false,
        )
    }

    /// Create a NEW operator template (admin): version 1 is written.
    pub fn create_entity_template(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/entities/templates", body, false)
    }

    /// Edit an operator template (admin): appends a new version.
    pub fn put_entity_template(&self, template_id: &str, body: &Value) -> ApiResult<Value> {
        self.send(
            "PUT",
            &format!("/entities/templates/{}", urlencode(template_id)),
            body,
            false,
        )
    }

    /// The gateway's default substrate + embedding for the "Gateway
    /// default" options (each field degrades to null + a labeled warning).
    pub fn entity_creation_defaults(&self) -> ApiResult<Value> {
        self.get("/entities/creation-defaults", false)
    }

    /// A provider's models filtered by output type (`text` for the mind,
    /// `embeddings` for the birth embedder) — the web's cascade reads.
    pub fn provider_models_of_type(&self, provider: &str, output_type: &str) -> ApiResult<Value> {
        self.get(
            &format!(
                "/discovery/providers/{}/models?output_type={}",
                urlencode(provider),
                urlencode(output_type)
            ),
            true,
        )
    }

    // ---- summon ------------------------------------------------------------

    /// DRY-RUN the create pre-checks (lint, name, drift, embedder match)
    /// without writing anything.
    pub fn validate_entity(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("POST", &entity_leaf(name, "validate"), body, false)
    }

    /// The irreversible birth: `POST /entities` (idempotent for an
    /// identical spark → `created:false`).
    pub fn create_entity(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/entities", body, true)
    }

    /// The identity card (pure read) — the web's Manage → Overview.
    pub fn entity_card(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_leaf(name, "card"), false)
    }

    // ---- talk (hosted chat visit) -------------------------------------------

    /// Open the visit (prelude + memory; may yield the own-time loop).
    pub fn entity_chat_open(&self, name: &str) -> ApiResult<Value> {
        self.send("POST", &entity_leaf(name, "chat/open"), &json!({}), true)
    }

    /// One turn — a real LLM round trip, answered as one JSON document
    /// (the web console does not stream this route either).
    pub fn entity_chat_turn(&self, name: &str, chat_id: &str, text: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!(
                "/entities/{}/chat/{}/turn",
                urlencode(name),
                urlencode(chat_id)
            ),
            &json!({ "text": text }),
            true,
        )
    }

    /// Close the visit with the reflection pass.
    pub fn entity_chat_close(&self, name: &str, chat_id: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!(
                "/entities/{}/chat/{}/close",
                urlencode(name),
                urlencode(chat_id)
            ),
            &json!({ "reflect": true }),
            true,
        )
    }

    /// Is a visit open on this home right now? (open/close verify).
    pub fn entity_chat_status(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_leaf(name, "chat"), false)
    }

    // ---- voice audition -----------------------------------------------------

    /// Speak AS the entity through the production TTS lane (the unsaved
    /// selection rides the body; `timeout_s` bounds a wedged backend).
    pub fn entity_voice_tts(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("POST", &entity_leaf(name, "voice/tts"), body, true)
    }

    /// The synthesized audio's bytes — the exact URL the web's audio
    /// element loads (`/runs/{run}/artifacts/{id}/content`).
    pub fn run_artifact_content(&self, run_id: &str, artifact_id: &str) -> ApiResult<Vec<u8>> {
        let path = format!(
            "/runs/{}/artifacts/{}/content",
            urlencode(run_id),
            urlencode(artifact_id)
        );
        let resp = self
            .with_auth(self.slow_agent.get(&self.url(&path)))
            .call()
            .map_err(|e| err_from_ureq(&path, e))?;
        let mut out: Vec<u8> = Vec::new();
        std::io::Read::read_to_end(&mut resp.into_reader(), &mut out).map_err(|e| ApiError {
            kind: ApiErrorKind::Unreachable,
            message: format!("failed reading audio bytes: {e}"),
            body: None,
            timed_out: false,
        })?;
        Ok(out)
    }
}

// ---------------------------------------------------------------------
// Typed folds
// ---------------------------------------------------------------------

fn st(v: &Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|s| !s.is_empty())
}

fn strs(v: &Value, key: &str) -> Vec<String> {
    v.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// One spark template as the gallery serves it.
#[derive(Clone, Debug, PartialEq)]
pub struct TemplateRow {
    pub id: String,
    pub name: String,
    pub description: String,
    /// "builtin" | "operator" | legacy file sources.
    pub source: String,
    /// Only operator templates are editable (the builtin is the floor).
    pub editable: bool,
    pub version: Option<u64>,
    pub spark: Value,
    /// Locked core values — kept for life, cannot be removed.
    pub core_values: Vec<String>,
}

impl TemplateRow {
    pub fn from_value(v: &Value) -> Option<TemplateRow> {
        let id = st(v, "id")?;
        Some(TemplateRow {
            name: st(v, "name").unwrap_or_else(|| id.clone()),
            description: st(v, "description").unwrap_or_default(),
            source: st(v, "source").unwrap_or_default(),
            editable: v.get("editable").and_then(Value::as_bool).unwrap_or(false),
            version: v.get("version").and_then(Value::as_u64),
            spark: v.get("spark").cloned().unwrap_or_else(|| json!({})),
            core_values: strs(v, "core_values"),
            id,
        })
    }

    /// The web's template description line: `description (vN)`.
    pub fn description_line(&self) -> String {
        match self.version {
            Some(n) if n > 0 => format!("{} (v{n})", self.description).trim().to_string(),
            _ => self.description.clone(),
        }
    }
}

pub fn templates_from_payload(v: &Value) -> (Vec<TemplateRow>, Vec<String>) {
    let rows = v
        .get("templates")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(TemplateRow::from_value).collect())
        .unwrap_or_default();
    (rows, strs(v, "warnings"))
}

/// `v1 (note) · v2` — the web's version-history line.
pub fn versions_line(v: &Value) -> String {
    let parts: Vec<String> = v
        .get("versions")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|x| {
                    let n = x
                        .get("version")
                        .map(|n| match n.as_u64() {
                            Some(u) => u.to_string(),
                            None => n.as_str().unwrap_or("?").to_string(),
                        })
                        .unwrap_or_else(|| "?".into());
                    match st(x, "note") {
                        Some(note) => format!("v{n} ({note})"),
                        None => format!("v{n}"),
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    if parts.is_empty() {
        String::new()
    } else {
        format!("versions: {}", parts.join(" · "))
    }
}

/// The per-phase capability grid of the create form (the web's
/// `matrixFromSpec`): phases in the served order, the tools of the
/// `tools` section (else the first), each phase's DEFAULT grant.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CreateMatrix {
    /// (phase id, label).
    pub phases: Vec<(String, String)>,
    /// (tool id, label).
    pub tools: Vec<(String, String)>,
    /// Default granted tool ids, one list per phase (same order).
    pub defaults: Vec<Vec<String>>,
}

pub fn matrix_from_spec(spec: &Value) -> CreateMatrix {
    let phases: Vec<(String, String)> = spec
        .get("phases")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|p| {
                    let id = st(p, "id")?;
                    Some((id.clone(), st(p, "label").unwrap_or(id)))
                })
                .collect()
        })
        .unwrap_or_default();
    let sections = spec
        .get("sections")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let section = sections
        .iter()
        .find(|s| s.get("id").and_then(Value::as_str) == Some("tools"))
        .or_else(|| sections.first())
        .cloned()
        .unwrap_or_else(|| json!({"items": []}));
    let items = section
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let tools: Vec<(String, String)> = items
        .iter()
        .filter_map(|it| {
            let id = st(it, "id")?;
            Some((id.clone(), st(it, "label").unwrap_or(id)))
        })
        .collect();
    let defaults = phases
        .iter()
        .map(|(pid, _)| {
            items
                .iter()
                .filter(|it| {
                    it.get("cells")
                        .and_then(|c| c.get(pid))
                        .and_then(|c| c.get("resolved_value"))
                        .map(|v| match v {
                            Value::Bool(b) => *b,
                            Value::Null => false,
                            _ => true,
                        })
                        .unwrap_or(false)
                })
                .filter_map(|it| st(it, "id"))
                .collect()
        })
        .collect();
    CreateMatrix {
        phases,
        tools,
        defaults,
    }
}

impl CreateMatrix {
    /// The web's `readMatrix`: ONLY phases the operator changed from the
    /// rendered defaults; a phase cleared to empty is sent as `null`
    /// (revert to the framework default), never `[]` (a silent deny-all).
    pub fn delta(&self, current: &[Vec<String>]) -> Map<String, Value> {
        let mut out = Map::new();
        for (i, (pid, _)) in self.phases.iter().enumerate() {
            let Some(now) = current.get(i) else { continue };
            let mut now_sorted = now.clone();
            now_sorted.sort();
            let mut was = self.defaults.get(i).cloned().unwrap_or_default();
            was.sort();
            if now_sorted == was {
                continue;
            }
            out.insert(
                pid.clone(),
                if now_sorted.is_empty() {
                    Value::Null
                } else {
                    Value::Array(now_sorted.into_iter().map(Value::String).collect())
                },
            );
        }
        out
    }
}

/// Everything the summon form reads before a birth. Templates are the
/// anchor read; every other part degrades with its own labeled note.
#[derive(Clone, Debug, Default)]
pub struct CreationKit {
    pub templates: Vec<TemplateRow>,
    pub template_warnings: Vec<String>,
    /// (provider, model) of the gateway-wide entity substrate, if any.
    pub default_substrate: Option<(String, String)>,
    /// (provider, model) of the door's resolved embedder, if any.
    pub default_embedding: Option<(String, String)>,
    /// `creation-defaults` warnings + degraded-discovery notes.
    pub notes: Vec<String>,
    /// Discovered LLM providers (for the Advanced substrate picker).
    pub providers: Vec<String>,
    /// Embedding models of the default embedding provider.
    pub embedding_models: Vec<String>,
    pub matrix: Option<CreateMatrix>,
    pub matrix_error: Option<String>,
    /// (provider, text models or the error) — the provider cascade.
    pub models: Option<(String, Result<Vec<String>, String>)>,
    /// (template id, versions line or the error) — the templates modal.
    pub versions: Option<(String, Result<String, String>)>,
}

impl CreationKit {
    pub fn template(&self, id: &str) -> Option<&TemplateRow> {
        self.templates.iter().find(|t| t.id == id)
    }

    /// Fold the creation-defaults payload (None = the read failed).
    pub fn apply_defaults(&mut self, defaults: Result<&Value, String>) {
        match defaults {
            Ok(d) => {
                let pair = |k: &str| {
                    let o = d.get(k)?;
                    Some((st(o, "provider")?, st(o, "model")?))
                };
                self.default_substrate = pair("substrate");
                self.default_embedding = pair("embedding");
                self.notes.extend(strs(d, "warnings"));
            }
            Err(e) => self
                .notes
                .push(format!("creation defaults unavailable: {e}")),
        }
    }

    /// The "Gateway default" option label, naming the resolved pair.
    pub fn substrate_default_label(&self) -> String {
        match &self.default_substrate {
            Some((p, m)) => format!("Gateway default ({p} / {m})"),
            None => "Gateway default".into(),
        }
    }

    pub fn embedding_default_label(&self) -> String {
        match &self.default_embedding {
            Some((_, m)) => format!("Gateway default ({m})"),
            None => "Gateway default".into(),
        }
    }
}

/// The names of the models in a discovery payload (`{models: [..]}`).
pub fn model_names(v: &Value) -> Vec<String> {
    v.get("models")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|m| match m {
                    Value::String(s) => Some(s.trim().to_string()),
                    other => other
                        .get("id")
                        .and_then(Value::as_str)
                        .map(|s| s.trim().to_string()),
                })
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// Provider names from `/discovery/providers` (`{items: [{name}]}`).
pub fn provider_names(v: &Value) -> Vec<String> {
    v.get("items")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|p| st(p, "name"))
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// The dry-run's verdict for one name.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CreateCheck {
    pub name: String,
    pub ok: bool,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
    pub would_conflict: bool,
    pub exists: bool,
}

impl CreateCheck {
    pub fn from_value(name: &str, v: &Value) -> CreateCheck {
        CreateCheck {
            name: name.to_string(),
            ok: v.get("ok").and_then(Value::as_bool).unwrap_or(false),
            errors: strs(v, "errors"),
            warnings: strs(v, "warnings"),
            would_conflict: v
                .get("would_conflict")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            exists: v.get("exists").and_then(Value::as_bool).unwrap_or(false),
        }
    }

    /// The web's refusal sentence (`Cannot create: …`).
    pub fn refusal(&self) -> String {
        let why = if !self.errors.is_empty() {
            self.errors.join("; ")
        } else if self.would_conflict {
            "an entity with this name already exists with a different spark".into()
        } else {
            "validation failed".into()
        };
        format!("Cannot create: {why}")
    }
}

/// The body both validate and create receive (the web's `createBody`):
/// the template's spark with the name filled, plus the optional birth
/// embedder.
pub fn create_body(name: &str, template_spark: &Value, embedding: &str) -> Value {
    let mut spark = match template_spark {
        Value::Object(m) => m.clone(),
        _ => Map::new(),
    };
    spark.insert("name".into(), Value::String(name.to_string()));
    let mut body = json!({ "name": name, "spark": Value::Object(spark) });
    let emb = embedding.trim();
    if !emb.is_empty() {
        body["embedding_model"] = Value::String(emb.to_string());
    }
    body
}

/// The identity card, folded to the web overview's rows.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EntityCard {
    pub name: String,
    /// (label, value) — empty values are omitted, never faked.
    pub rows: Vec<(String, String)>,
    /// The last six moments: (when, what).
    pub moments: Vec<(String, String)>,
}

fn short_when(s: &str) -> String {
    s.chars().take(16).collect::<String>().replace('T', " ")
}

impl EntityCard {
    pub fn from_value(name: &str, card: &Value) -> EntityCard {
        let manifest_id = card.get("manifest").and_then(|m| st(m, "entity_id"));
        let entity_id = st(card, "entity_id").or(manifest_id);
        let state = match card.get("state") {
            Some(Value::Object(_)) => {
                let s = card.get("state").unwrap();
                let word = st(s, "state").unwrap_or_default();
                match st(s, "mode") {
                    Some(m) => format!("{word} ({m})"),
                    None => word,
                }
            }
            Some(Value::String(s)) => s.clone(),
            _ => String::new(),
        };
        let mind = card
            .get("mind_substrate")
            .filter(|m| m.is_object())
            .map(|m| {
                let mut s = format!(
                    "{} / {}",
                    st(m, "provider").unwrap_or_else(|| "?".into()),
                    st(m, "model").unwrap_or_else(|| "?".into())
                );
                if let Some(t) = st(m, "thinking") {
                    s.push_str(&format!(" / reasoning {t}"));
                }
                s
            })
            .unwrap_or_default();
        let sleep = card.get("sleep_stats").cloned().unwrap_or(Value::Null);
        let sleeps = sleep
            .get("sleeps")
            .or_else(|| sleep.get("sleep_count"))
            .filter(|v| !v.is_null())
            .map(|v| match v.as_u64() {
                Some(n) => n.to_string(),
                None => v.to_string(),
            })
            .unwrap_or_default();
        let age = card
            .get("age_days")
            .filter(|v| !v.is_null())
            .map(|v| match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .unwrap_or_default();
        let rows_all = vec![
            (
                "Entity ID",
                st(card, "handle")
                    .or_else(|| entity_id.clone())
                    .unwrap_or_default(),
            ),
            (
                "Internal ID (birth marker)",
                entity_id.clone().unwrap_or_default(),
            ),
            (
                "Born",
                st(card, "born")
                    .or_else(|| st(card, "created_at"))
                    .unwrap_or_default(),
            ),
            ("Age (days)", age),
            ("State", state),
            ("Mind", mind),
            ("Sleeps", sleeps),
        ];
        let rows = rows_all
            .into_iter()
            .filter(|(_, v)| !v.is_empty())
            .map(|(k, v)| (k.to_string(), v))
            .collect();
        let all_moments: Vec<(String, String)> = card
            .get("moments")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .map(|m| {
                        let at = short_when(&st(m, "at").unwrap_or_default());
                        let kind = st(m, "kind").unwrap_or_else(|| "?".into());
                        let what = match m.get("details").and_then(|d| st(d, "reason")) {
                            Some(r) => format!("{kind} — {r}"),
                            None => kind,
                        };
                        (at, what)
                    })
                    .collect()
            })
            .unwrap_or_default();
        let skip = all_moments.len().saturating_sub(6);
        EntityCard {
            name: name.to_string(),
            rows,
            moments: all_moments.into_iter().skip(skip).collect(),
        }
    }
}

/// One transcript line of the Talk panel.
#[derive(Clone, Debug, PartialEq)]
pub struct ChatLine {
    /// "you" or the entity's name.
    pub who: String,
    pub text: String,
}

/// The console's ONE live visit (the web holds one chat_id at a time).
/// The transcript is local, like the web's: the route answers turns,
/// the page keeps what was said.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ChatState {
    /// The entity this panel talks to (empty = none yet).
    pub entity: String,
    /// The open visit's id; None = no visit open.
    pub chat_id: Option<String>,
    pub lines: Vec<ChatLine>,
    /// The status line under the transcript (the web's chat status).
    pub status: String,
    /// A request (open / turn / close) is in flight.
    pub busy: bool,
}

impl ChatState {
    pub fn fresh(entity: &str) -> ChatState {
        ChatState {
            entity: entity.to_string(),
            ..ChatState::default()
        }
    }

    /// Fold the open response: chat id, status bits, salvaged reply.
    pub fn apply_open(&mut self, v: &Value) {
        self.chat_id = st(v, "chat_id");
        self.lines.clear();
        let mut bits = vec![format!(
            "visit open ({})",
            self.chat_id.clone().unwrap_or_default()
        )];
        if v.get("yielded_loop").and_then(Value::as_bool) == Some(true) {
            bits.push("own-time loop yielded for this visit".into());
        }
        let warnings = strs(v, "warnings");
        if !warnings.is_empty() {
            bits.push(warnings.join(" | "));
        }
        self.status = bits.join(" — ");
        if let Some(reply) = v.get("salvage").and_then(|s| st(s, "reply")) {
            self.lines.push(ChatLine {
                who: self.entity.clone(),
                text: format!("(salvaged look-back) {reply}"),
            });
        }
    }

    /// Fold one turn's response: the reply line + the status bits.
    pub fn apply_turn(&mut self, v: &Value) {
        self.lines.push(ChatLine {
            who: self.entity.clone(),
            text: v
                .get("reply")
                .map(|r| match r.as_str() {
                    Some(s) => s.to_string(),
                    None if r.is_null() => String::new(),
                    None => r.to_string(),
                })
                .unwrap_or_default(),
        });
        let mut bits: Vec<String> = Vec::new();
        let tools = strs(v, "tools_ran");
        if !tools.is_empty() {
            bits.push(format!("tools: {}", tools.join(", ")));
        }
        if let Some(n) = v.get("memories_in_context").and_then(Value::as_u64) {
            bits.push(format!("{n} memories in context"));
        }
        let diary = v
            .get("diary_entries")
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        if diary > 0 {
            bits.push(format!(
                "{diary} diary entr{}",
                if diary == 1 { "y" } else { "ies" }
            ));
        }
        self.status = bits.join(" · ");
    }

    /// Fold a failed close: a 4xx means the server already has no such
    /// session (clear it); anything else keeps the visit marked open
    /// with the retry affordance (the web's rule — a silent clear would
    /// render "Open visit" over a live server session).
    pub fn apply_close_error(&mut self, e: &ApiError) {
        match e.status() {
            Some(code) if (400..500).contains(&code) => {
                self.chat_id = None;
                self.status = format!("session already closed server-side: {e}");
            }
            _ => {
                self.status = format!("close FAILED — the visit is still open; retry: {e}");
            }
        }
    }
}

/// What the voice audition produced — the terminal's honest twin of
/// the web's inline audio player: the audio saved to a file whose path
/// is shown, playable from the console only when a local player exists.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AuditionOutcome {
    pub entity: String,
    /// "Synthesized in 3.2s with p/m/v — this is the unsaved selection…"
    pub summary: String,
    /// Where the audio bytes were written (None = synthesis or download
    /// failed; `error` says why).
    pub path: Option<String>,
    pub bytes: usize,
    pub error: Option<String>,
    /// The local command-line player found on PATH, if any.
    pub player: Option<String>,
}

/// File extension for a synthesized audio artifact: the served
/// content type first, then the artifact's own filename, else `audio`.
pub fn audio_extension(artifact: &Value) -> String {
    let ct = artifact
        .get("content_type")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let by_type = match ct.split(';').next().unwrap_or("").trim() {
        "audio/wav" | "audio/x-wav" | "audio/wave" | "audio/vnd.wave" => Some("wav"),
        "audio/mpeg" | "audio/mp3" => Some("mp3"),
        "audio/ogg" | "audio/opus" => Some("ogg"),
        "audio/flac" | "audio/x-flac" => Some("flac"),
        "audio/aac" => Some("aac"),
        "audio/mp4" | "audio/m4a" | "audio/x-m4a" => Some("m4a"),
        "audio/webm" => Some("webm"),
        _ => None,
    };
    if let Some(ext) = by_type {
        return ext.to_string();
    }
    artifact
        .get("filename")
        .and_then(Value::as_str)
        .and_then(|f| std::path::Path::new(f).extension())
        .and_then(|e| e.to_str())
        .filter(|e| !e.is_empty() && e.chars().all(|c| c.is_ascii_alphanumeric()))
        .map(|e| e.to_ascii_lowercase())
        .unwrap_or_else(|| "audio".into())
}

/// The artifact id of a gateway artifact reference (`$artifact`, with
/// `artifact_id` / `id` tolerated — the web's `sandboxArtifactUrl`).
pub fn artifact_id(artifact: &Value) -> Option<String> {
    if let Some(s) = artifact.as_str() {
        return Some(s.to_string()).filter(|s| !s.is_empty());
    }
    ["$artifact", "artifact_id", "id"]
        .iter()
        .find_map(|k| st(artifact, k))
}

// The audio-player table lives in `crate::audio` (one table for the
// audition and the sandbox); re-exported for this module's callers.
pub use crate::audio::{find_player_in, spawn_player, AUDIO_PLAYERS};

/// The web create form's reasoning-effort options ("not set" sends
/// nothing) — `console.py` `#entity-new-thinking`.
pub const ENTITY_THINKING_LEVELS: [&str; 6] = ["none", "minimal", "low", "medium", "high", "xhigh"];

#[cfg(test)]
mod tests {
    use super::*;

    /// Recorded from the hermetic gateway's `/entities/templates`.
    fn gallery() -> Value {
        json!({
            "schema_version": 1,
            "templates": [
                {"id": "framework-default", "name": "Framework default",
                 "description": "The floor", "source": "builtin", "editable": false,
                 "version": 1, "spark": {"name": "", "core_values": ["shared_vulnerability"]},
                 "core_values": ["shared_vulnerability"]},
                {"id": "researcher", "name": "Researcher", "description": "digs",
                 "source": "operator", "editable": true, "version": 3,
                 "spark": {"name": ""}, "core_values": []},
                {"name": "no id — skipped"}
            ],
            "warnings": ["#FALLBACK skipped template 'x'"]
        })
    }

    #[test]
    fn templates_fold_keeps_id_rows_and_warnings() {
        let (rows, warnings) = templates_from_payload(&gallery());
        assert_eq!(rows.len(), 2, "a row without id is dropped");
        assert_eq!(rows[0].id, "framework-default");
        assert!(!rows[0].editable);
        assert_eq!(rows[0].core_values, vec!["shared_vulnerability"]);
        assert!(rows[1].editable);
        assert_eq!(rows[1].description_line(), "digs (v3)");
        assert_eq!(warnings.len(), 1);
    }

    #[test]
    fn versions_line_names_each_version_and_note() {
        let v = json!({"versions": [{"version": 1, "note": "created"}, {"version": 2}]});
        assert_eq!(versions_line(&v), "versions: v1 (created) · v2");
        assert_eq!(versions_line(&json!({"versions": []})), "");
    }

    #[test]
    fn create_body_fills_the_name_and_optional_embedder() {
        let spark = json!({"name": "", "core_values": ["shared_vulnerability"]});
        let b = create_body("Castor", &spark, "");
        assert_eq!(b["name"], "Castor");
        assert_eq!(b["spark"]["name"], "Castor");
        assert_eq!(b["spark"]["core_values"][0], "shared_vulnerability");
        assert!(
            b.get("embedding_model").is_none(),
            "blank = gateway default"
        );
        let b = create_body("Castor", &spark, " nomic ");
        assert_eq!(b["embedding_model"], "nomic");
    }

    #[test]
    fn create_check_refusal_matches_the_web_sentence() {
        let c = CreateCheck::from_value("X", &json!({"ok": false, "errors": ["a", "b"]}));
        assert!(!c.ok);
        assert_eq!(c.refusal(), "Cannot create: a; b");
        let c = CreateCheck::from_value("X", &json!({"ok": false, "would_conflict": true}));
        assert_eq!(
            c.refusal(),
            "Cannot create: an entity with this name already exists with a different spark"
        );
        let c = CreateCheck::from_value("X", &json!({"ok": true, "warnings": ["w"]}));
        assert!(c.ok);
        assert_eq!(c.warnings, vec!["w"]);
    }

    fn matrix_spec() -> Value {
        json!({
            "phases": [{"id": "visit", "label": "Visit"}, {"id": "sleep"}],
            "sections": [
                {"id": "other", "items": [{"id": "zzz"}]},
                {"id": "tools", "items": [
                    {"id": "recall", "label": "Recall", "cells": {
                        "visit": {"resolved_value": true}, "sleep": {"resolved_value": true}}},
                    {"id": "web", "cells": {
                        "visit": {"resolved_value": true}, "sleep": {"resolved_value": false}}}
                ]}
            ]
        })
    }

    #[test]
    fn matrix_reads_the_tools_section_and_default_grants() {
        let m = matrix_from_spec(&matrix_spec());
        assert_eq!(m.phases[0], ("visit".into(), "Visit".into()));
        assert_eq!(m.phases[1], ("sleep".into(), "sleep".into()));
        assert_eq!(m.tools.len(), 2, "the tools section, not the first");
        assert_eq!(m.defaults[0], vec!["recall", "web"]);
        assert_eq!(m.defaults[1], vec!["recall"]);
    }

    #[test]
    fn matrix_delta_sends_changed_phases_only_and_null_for_cleared() {
        let m = matrix_from_spec(&matrix_spec());
        // Untouched (order-insensitive) → nothing.
        let same = vec![vec!["web".into(), "recall".into()], vec!["recall".into()]];
        assert!(m.delta(&same).is_empty());
        // visit narrowed, sleep cleared.
        let changed = vec![vec!["recall".into()], vec![]];
        let d = m.delta(&changed);
        assert_eq!(d["visit"], json!(["recall"]));
        assert_eq!(d["sleep"], Value::Null, "cleared = default, never deny-all");
    }

    #[test]
    fn card_folds_the_web_overview_rows_and_last_six_moments() {
        let moments: Vec<Value> = (0..8)
            .map(|i| json!({"at": format!("2026-09-27T10:0{i}:00Z"), "kind": format!("k{i}")}))
            .collect();
        let card = json!({
            "handle": "castor@192.168.1.5",
            "entity_id": "entity:abc",
            "born": "2026-09-27T10:00:00Z",
            "age_days": 0,
            "state": {"state": "asleep", "mode": "resting"},
            "mind_substrate": {"provider": "lmstudio", "model": "qwen", "thinking": "low"},
            "sleep_stats": {"sleeps": 2},
            "moments": moments,
        });
        let c = EntityCard::from_value("castor", &card);
        let get = |k: &str| c.rows.iter().find(|(l, _)| l == k).map(|(_, v)| v.clone());
        assert_eq!(get("Entity ID").as_deref(), Some("castor@192.168.1.5"));
        assert_eq!(
            get("Internal ID (birth marker)").as_deref(),
            Some("entity:abc")
        );
        assert_eq!(get("State").as_deref(), Some("asleep (resting)"));
        assert_eq!(
            get("Mind").as_deref(),
            Some("lmstudio / qwen / reasoning low")
        );
        assert_eq!(get("Sleeps").as_deref(), Some("2"));
        assert_eq!(get("Age (days)").as_deref(), Some("0"));
        assert_eq!(c.moments.len(), 6);
        assert_eq!(c.moments[0], ("2026-09-27 10:02".into(), "k2".into()));
        // Absent fields are omitted, never faked.
        let bare = EntityCard::from_value("x", &json!({"manifest": {"entity_id": "entity:m"}}));
        assert_eq!(
            bare.rows,
            vec![
                ("Entity ID".to_string(), "entity:m".to_string()),
                (
                    "Internal ID (birth marker)".to_string(),
                    "entity:m".to_string()
                ),
            ]
        );
    }

    /// Recorded web-format frames of the chat routes (the shapes
    /// `entity_chat.py` returns — open, turn, close refusal).
    #[test]
    fn chat_folds_open_turn_and_close_errors_like_the_web() {
        let mut c = ChatState::fresh("Castor");
        c.apply_open(&json!({
            "chat_id": "chat_1", "yielded_loop": true, "warnings": ["w1"],
            "salvage": {"reply": "last time we spoke"}
        }));
        assert_eq!(c.chat_id.as_deref(), Some("chat_1"));
        assert_eq!(
            c.status,
            "visit open (chat_1) — own-time loop yielded for this visit — w1"
        );
        assert_eq!(c.lines[0].text, "(salvaged look-back) last time we spoke");
        c.apply_turn(&json!({
            "reply": "Hello.", "turn_id": "t1", "tools_ran": ["recall"],
            "memories_in_context": 4, "diary_entries": [{"x": 1}]
        }));
        assert_eq!(c.lines.last().unwrap().who, "Castor");
        assert_eq!(c.lines.last().unwrap().text, "Hello.");
        assert_eq!(
            c.status,
            "tools: recall · 4 memories in context · 1 diary entry"
        );
        // A 5xx close keeps the visit open (retry affordance).
        c.apply_close_error(&ApiError::new(ApiErrorKind::Http(502), "upstream"));
        assert_eq!(c.chat_id.as_deref(), Some("chat_1"));
        assert!(c.status.starts_with("close FAILED"));
        // A 4xx close means the server has no such session.
        c.apply_close_error(&ApiError::new(ApiErrorKind::Http(409), "already closed"));
        assert_eq!(c.chat_id, None);
        assert!(c.status.starts_with("session already closed server-side"));
    }

    #[test]
    fn audio_extension_prefers_type_then_filename() {
        assert_eq!(
            audio_extension(&json!({"content_type": "audio/wav"})),
            "wav"
        );
        assert_eq!(
            audio_extension(&json!({"content_type": "audio/mpeg; x=1"})),
            "mp3"
        );
        assert_eq!(
            audio_extension(
                &json!({"content_type": "application/octet-stream", "filename": "a.OGG"})
            ),
            "ogg"
        );
        assert_eq!(audio_extension(&json!({})), "audio");
        assert_eq!(
            artifact_id(&json!({"$artifact": "art1"})).as_deref(),
            Some("art1")
        );
        assert_eq!(artifact_id(&json!({"id": "art2"})).as_deref(), Some("art2"));
        assert_eq!(artifact_id(&json!({"x": 1})), None);
    }

    #[test]
    fn provider_and_model_names_fold() {
        assert_eq!(
            provider_names(&json!({"items": [{"name": "ollama"}, {"name": ""}, {}]})),
            vec!["ollama"]
        );
        assert_eq!(
            model_names(&json!({"models": ["a", {"id": "b"}, " "]})),
            vec!["a", "b"]
        );
    }
}
