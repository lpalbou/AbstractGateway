//! Blocking HTTP client for the AbstractGateway admin API.
//!
//! Lives on the worker thread only — the UI never blocks on HTTP. Every
//! method returns `Result<Value, ApiError>`; interpretation of payloads
//! (including 200-with-`ok:false` bodies) belongs to the store layer.
//!
//! Error taxonomy (the "honest states" law): a refused connection, a
//! 401, a 403 and a 4xx/5xx with a `detail` body are four different
//! truths and must never collapse into one "error".

use std::time::Duration;

use serde_json::{json, Value};

/// What kind of failure this is — drives which honest state the UI shows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApiErrorKind {
    /// No client exists yet — the app never probed (round-4 transport
    /// audit: this used to ride Unreachable, whose panel taught
    /// "press r to retry" while r itself refused when disconnected —
    /// contradictory layered teaching).
    NotConnected,
    /// TCP/TLS/DNS failure, timeout — the gateway was never reached.
    Unreachable,
    /// HTTP 401 — reached, but the token is wrong or missing.
    Unauthorized,
    /// HTTP 403 — authenticated but not allowed (admin routes).
    Forbidden,
    /// Any other HTTP status (400 validation, 404, 502 backend…).
    Http(u16),
    /// The body was not the JSON we expected.
    Protocol,
}

#[derive(Debug, Clone)]
pub struct ApiError {
    pub kind: ApiErrorKind,
    /// Verbatim `detail` text when the gateway sent one (its messages
    /// are actionable by design), else a transport description.
    pub message: String,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.kind {
            // "network failure", not "gateway unreachable": this text
            // reaches journals and form errors for SINGLE failed
            // requests — one dead socket proves nothing about the
            // gateway (the connection screen words its own probe
            // verdict, where the stronger claim is earned).
            ApiErrorKind::NotConnected => write!(f, "not connected: {}", self.message),
            ApiErrorKind::Unreachable => write!(f, "network failure: {}", self.message),
            ApiErrorKind::Unauthorized => write!(f, "unauthorized (401): {}", self.message),
            ApiErrorKind::Forbidden => write!(f, "forbidden (403): {}", self.message),
            ApiErrorKind::Http(s) => write!(f, "HTTP {s}: {}", self.message),
            ApiErrorKind::Protocol => write!(f, "protocol: {}", self.message),
        }
    }
}

pub type ApiResult<T> = Result<T, ApiError>;

/// One GET attempt's failure: the error plus whether the SOCKET died
/// (retryable on a fresh connection) vs the gateway/route being the
/// problem (retrying doubles time-to-truth).
struct AttemptFail {
    error: ApiError,
    retryable: bool,
}

/// The socket-death io kinds: the request died EN ROUTE on an
/// established connection. Deliberately excludes ConnectionRefused
/// (down gateway) and TimedOut/WouldBlock (retrying doubles the wait).
fn io_death_kind(k: std::io::ErrorKind) -> bool {
    matches!(
        k,
        std::io::ErrorKind::ConnectionReset
            | std::io::ErrorKind::ConnectionAborted
            | std::io::ErrorKind::BrokenPipe
            | std::io::ErrorKind::UnexpectedEof
    )
}

/// ureq taxonomy: only Transport errors of io kind qualify; the
/// io::Error rides the source chain (ureq wraps read-side errors one
/// level deep — walk the chain, never assume depth).
fn io_death(e: &ureq::Error) -> bool {
    let ureq::Error::Transport(t) = e else {
        return false;
    };
    if t.kind() != ureq::ErrorKind::Io {
        return false;
    }
    let mut src = std::error::Error::source(e);
    while let Some(s) = src {
        if let Some(ioe) = s.downcast_ref::<std::io::Error>() {
            return io_death_kind(ioe.kind());
        }
        src = s.source();
    }
    false
}

fn err_from_ureq(label: &str, e: ureq::Error) -> ApiError {
    match e {
        ureq::Error::Status(code, resp) => {
            let body = resp.into_string().unwrap_or_default();
            // FastAPI errors are {"detail": "..."} — surface detail verbatim.
            let detail = serde_json::from_str::<Value>(&body)
                .ok()
                .and_then(|v| {
                    v.get("detail")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                        .or_else(|| v.get("detail").map(|d| d.to_string()))
                        // as_str first: `to_string` on a JSON string
                        // renders literal quotes ("boom") in the UI.
                        .or_else(|| v.get("error").and_then(Value::as_str).map(str::to_string))
                        .or_else(|| v.get("error").map(|e| e.to_string()))
                })
                .unwrap_or_else(|| {
                    let t = body.trim();
                    if t.is_empty() {
                        format!("{label} failed")
                    } else {
                        t.chars().take(400).collect()
                    }
                });
            let kind = match code {
                401 => ApiErrorKind::Unauthorized,
                403 => ApiErrorKind::Forbidden,
                c => ApiErrorKind::Http(c),
            };
            ApiError {
                kind,
                message: detail,
            }
        }
        ureq::Error::Transport(t) => ApiError {
            kind: ApiErrorKind::Unreachable,
            message: t.to_string(),
        },
    }
}

/// The gateway connection: base URL + bearer token + two agents (normal
/// calls vs the documented slow ones — model discovery and sandbox
/// generation legitimately take tens of seconds).
#[derive(Clone)]
pub struct GatewayClient {
    base_url: String,
    token: Option<String>,
    agent: ureq::Agent,
    slow_agent: ureq::Agent,
}

impl GatewayClient {
    pub fn new(base_url: &str, token: Option<&str>) -> GatewayClient {
        // POOLED agents (ureq defaults: 1 idle socket per host) — the
        // field standard, kept deliberately after two adversarial
        // transport reviews (2026-07-25). The stale-socket hazard is
        // real and MEASURED (uvicorn FINs idle keep-alives at exactly
        // 5.00s; macOS reaps FIN_WAIT_2 at +60s and RSTs the next
        // touch; ureq's pool-checkout peek propagates that RST as the
        // request's error — reproduced verbatim at >65s idle, the
        // operator incident), but the cure is the bounded GET retry in
        // get() below, NOT pooling-off: pool-off killed only the peek
        // class while mid-body deaths and gateway-bounce resets
        // survived it identically. Per-host pool of 1 also bounds the
        // retry: a socket that errors is dropped, so attempt two can
        // never draw a second stale socket.
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout_read(Duration::from_secs(60))
            .timeout_write(Duration::from_secs(30))
            .build();
        let slow_agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout_read(Duration::from_secs(300))
            .timeout_write(Duration::from_secs(30))
            .build();
        GatewayClient {
            base_url: base_url.trim_end_matches('/').to_string(),
            token: token.map(str::to_string).filter(|t| !t.is_empty()),
            agent,
            slow_agent,
        }
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    fn url(&self, path: &str) -> String {
        format!("{}/api/gateway{}", self.base_url, path)
    }

    fn with_auth(&self, req: ureq::Request) -> ureq::Request {
        match &self.token {
            Some(t) => req.set("Authorization", &format!("Bearer {t}")),
            None => req,
        }
    }

    fn read_json(label: &str, resp: ureq::Response) -> ApiResult<Value> {
        let body = resp.into_string().map_err(|e| ApiError {
            kind: ApiErrorKind::Unreachable,
            message: format!("{label}: read failed: {e}"),
        })?;
        serde_json::from_str(&body).map_err(|e| ApiError {
            kind: ApiErrorKind::Protocol,
            message: format!("{label}: invalid JSON: {e}"),
        })
    }

    fn get(&self, path: &str, slow: bool) -> ApiResult<Value> {
        // GET is idempotent by contract: ONE immediate retry when the
        // first attempt died on the SOCKET (see io_death — reset /
        // aborted / broken pipe / unexpected EOF), never on
        // connect-refused (the gateway is down; retrying doubles
        // time-to-truth), never on timeouts (doubling a 60s wait),
        // never for writes (the gateway's write endpoints are not
        // uniformly idempotent). This covers every socket-death class
        // both transport reviews found: the pool-checkout peek hole
        // (ureq's server_closed()? propagates an RST-latched socket's
        // error before its own retry arms — empirically reproduced at
        // >65s idle: uvicorn FINs at 5.00s, macOS reaps FIN_WAIT_2 at
        // +60s and RSTs the next touch), a response dying MID-BODY
        // (ureq never retries those — the head was consumed), and
        // rustls reporting a reaped TLS socket as UnexpectedEof (which
        // ureq's connection_closed() does not accept).
        match self.get_once(path, slow) {
            Err(f) if f.retryable => self.get_once(path, slow).map_err(|f| f.error),
            r => r.map_err(|f| f.error),
        }
    }

    /// A GET whose body is BYTES, not JSON — the export lane. No retry: a
    /// half-written bundle must never be silently re-fetched over itself.
    fn get_bytes(&self, path: &str) -> ApiResult<Vec<u8>> {
        let req = self
            .with_auth(self.slow_agent.get(&self.url(path)))
            .set("Accept", "application/octet-stream");
        let resp = req.call().map_err(|e| err_from_ureq(path, e))?;
        let mut out: Vec<u8> = Vec::new();
        std::io::Read::read_to_end(&mut resp.into_reader(), &mut out).map_err(|e| ApiError {
            kind: ApiErrorKind::Unreachable,
            message: format!("failed reading bundle bytes: {e}"),
        })?;
        Ok(out)
    }

    /// One full GET attempt = call + body read + parse. The body read
    /// is INSIDE the attempt: mid-body death is the one stale-socket
    /// path ureq can never retry internally.
    fn get_once(&self, path: &str, slow: bool) -> Result<Value, AttemptFail> {
        let agent = if slow { &self.slow_agent } else { &self.agent };
        let req = self.with_auth(agent.get(&self.url(path)).set("Accept", "application/json"));
        let resp = req.call().map_err(|e| AttemptFail {
            retryable: io_death(&e),
            error: err_from_ureq(path, e),
        })?;
        let body = resp.into_string().map_err(|e| AttemptFail {
            retryable: io_death_kind(e.kind()),
            error: ApiError {
                kind: ApiErrorKind::Unreachable,
                message: format!("{path}: read failed: {e}"),
            },
        })?;
        serde_json::from_str(&body).map_err(|e| AttemptFail {
            retryable: false,
            error: ApiError {
                kind: ApiErrorKind::Protocol,
                message: format!("{path}: invalid JSON: {e}"),
            },
        })
    }

    fn send(&self, method: &str, path: &str, payload: &Value, slow: bool) -> ApiResult<Value> {
        let agent = if slow { &self.slow_agent } else { &self.agent };
        let req = self.with_auth(
            agent
                .request(method, &self.url(path))
                .set("Accept", "application/json"),
        );
        let resp = req
            .set("Content-Type", "application/json")
            .send_string(&payload.to_string())
            .map_err(|e| err_from_ureq(path, e))?;
        Self::read_json(path, resp)
    }

    fn delete(&self, path: &str) -> ApiResult<Value> {
        let req = self.with_auth(
            self.agent
                .delete(&self.url(path))
                .set("Accept", "application/json"),
        );
        let resp = req.call().map_err(|e| err_from_ureq(path, e))?;
        Self::read_json(path, resp)
    }

    // ---- connection ----------------------------------------------------

    /// The login check. Ping validates reachability + auth, nothing else
    /// (the gateway's own docstring law: never use discovery as login).
    pub fn ping(&self) -> ApiResult<Value> {
        self.get("/ping", false)
    }

    pub fn me(&self) -> ApiResult<Value> {
        self.get("/me", false)
    }

    // ---- providers -----------------------------------------------------

    pub fn discovery_providers(&self) -> ApiResult<Value> {
        self.get("/discovery/providers", false)
    }

    pub fn provider_models(&self, provider: &str) -> ApiResult<Value> {
        // URL-encode the provider name (endpoint:x carries a colon —
        // harmless in a path segment, but be strict).
        let enc = urlencode(provider);
        self.get(&format!("/discovery/providers/{enc}/models"), true)
    }

    pub fn profiles(&self) -> ApiResult<Value> {
        self.get("/config/provider-endpoint-profiles", false)
    }

    pub fn create_profile(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/config/provider-endpoint-profiles", body, false)
    }

    pub fn update_profile(&self, id: &str, body: &Value) -> ApiResult<Value> {
        let enc = urlencode(id);
        self.send(
            "PUT",
            &format!("/config/provider-endpoint-profiles/{enc}"),
            body,
            false,
        )
    }

    pub fn delete_profile(&self, id: &str) -> ApiResult<Value> {
        let enc = urlencode(id);
        self.delete(&format!("/config/provider-endpoint-profiles/{enc}"))
    }

    /// "Test connection" for a profile form — works on a saved profile
    /// (`profile_id`) or an unsaved draft (family + base_url + key).
    pub fn discover_models(&self, body: &Value) -> ApiResult<Value> {
        self.send(
            "POST",
            "/config/provider-endpoint-profiles/discover-models",
            body,
            true,
        )
    }

    // ---- capability routes ----------------------------------------------

    pub fn capability_defaults(&self) -> ApiResult<Value> {
        self.get("/config/capability-defaults", false)
    }

    /// The capability grid annotated with LOCAL WEIGHT availability on
    /// the execution host. `slow` because the probe talks to LM Studio
    /// and Ollama on that host: a stalled local daemon costs seconds,
    /// and answering "unknown" late beats answering wrongly fast.
    pub fn model_availability(&self) -> ApiResult<Value> {
        self.get("/models/availability", true)
    }

    /// Start a download; returns a JOB, not the bytes. The POST is fast
    /// by contract — the gateway runs the provider tool on a worker
    /// thread — so this never holds the console's lane for a download.
    pub fn start_model_download(&self, provider: &str, artifact: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            "/models/download",
            &serde_json::json!({"provider": provider, "artifact": artifact}),
            false,
        )
    }

    pub fn model_download_job(&self, job: &str) -> ApiResult<Value> {
        self.get(&format!("/models/download/{}", urlencode(job)), false)
    }

    fn route_path(kind: &str, modality: &str, task: Option<&str>) -> String {
        match task {
            Some(t) => format!(
                "/config/capability-defaults/{}/{}/{}",
                urlencode(kind),
                urlencode(modality),
                urlencode(t)
            ),
            None => format!(
                "/config/capability-defaults/{}/{}",
                urlencode(kind),
                urlencode(modality)
            ),
        }
    }

    /// PUT returns the full refreshed payload — re-render from it.
    pub fn put_route(
        &self,
        kind: &str,
        modality: &str,
        task: Option<&str>,
        body: &Value,
    ) -> ApiResult<Value> {
        self.send("PUT", &Self::route_path(kind, modality, task), body, true)
    }

    pub fn clear_route(&self, kind: &str, modality: &str, task: Option<&str>) -> ApiResult<Value> {
        self.delete(&Self::route_path(kind, modality, task))
    }

    /// Apply the framework's recommended routes to the execution host's
    /// store. `slow` because it writes and re-reads the whole grid.
    ///
    /// Safe by default: without `force` the gateway KEEPS every route the
    /// operator configured differently and reports them in
    /// `applied_recommended` — the same decision the CLI's
    /// `abstractcore config apply-recommended` makes, taken once in
    /// AbstractCore rather than re-derived here.
    pub fn apply_recommended_routes(&self, force: bool) -> ApiResult<Value> {
        self.send(
            "POST",
            "/config/capability-defaults/apply-recommended",
            &json!({ "force": force }),
            true,
        )
    }

    // ---- host state & model residency (the "agentic OS" surface) ----------

    /// One-call resources snapshot: memory + GPU gauges, resident
    /// models, session prompt caches. SLOW by contract — the gateway
    /// probes the GPU and enumerates residency; callers poll no faster
    /// than ~4s and only while the Models tab is on screen.
    pub fn host_state(&self) -> ApiResult<Value> {
        self.get("/host/state", true)
    }

    /// The resident-model rows alone (row_v1 under "rows") — the
    /// verify read after a model mutation (cheaper than the full
    /// host-state GPU probe).
    pub fn models_loaded(&self) -> ApiResult<Value> {
        self.get("/models/loaded", true)
    }

    /// Warm up (load) a model. Slow: a cold load pulls weights into
    /// memory and can take tens of seconds.
    pub fn load_model(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/models/load", body, true)
    }

    /// Unload a model. A locked model answers **HTTP 409** (the one
    /// unload failure with its own next step: unlock, or force).
    pub fn unload_model(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/models/unload", body, true)
    }

    /// Pin a resident model against unload/eviction.
    pub fn lock_model(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/models/lock", body, false)
    }

    /// Release a lock set through /models/lock.
    pub fn unlock_model(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/models/unlock", body, false)
    }

    /// Context/KV memory estimate for one provider+model. Confidence is
    /// in-band: calibrated | estimated | unknown.
    pub fn context_estimate(
        &self,
        provider: &str,
        model: &str,
        context_length: Option<u64>,
    ) -> ApiResult<Value> {
        let mut path = format!(
            "/models/context_estimate?provider={}&model={}",
            urlencode(provider),
            urlencode(model)
        );
        if let Some(n) = context_length {
            path.push_str(&format!("&context_length={n}"));
        }
        self.get(&path, false)
    }

    /// Every session's runtime-minted prompt caches (the enumeration
    /// lane — cannot miss caches whose keys the gateway never derived).
    pub fn session_prompt_caches(&self) -> ApiResult<Value> {
        self.get("/sessions/prompt_cache", false)
    }

    /// One-call unload of every prompt cache for one session (admin).
    pub fn clear_session_prompt_caches(&self, session_id: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/sessions/{}/prompt_cache/clear_all", urlencode(session_id)),
            &json!({}),
            false,
        )
    }

    // ---- users & entities -------------------------------------------------

    pub fn users(&self) -> ApiResult<Value> {
        self.get("/admin/users", false)
    }

    pub fn create_user(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/admin/users", body, false)
    }

    pub fn patch_user(&self, user_id: &str, tenant_id: &str, body: &Value) -> ApiResult<Value> {
        let path = format!(
            "/admin/users/{}?tenant_id={}",
            urlencode(user_id),
            urlencode(tenant_id)
        );
        self.send("PATCH", &path, body, false)
    }

    pub fn delete_user(&self, user_id: &str, tenant_id: &str) -> ApiResult<Value> {
        self.delete(&format!(
            "/admin/users/{}?tenant_id={}",
            urlencode(user_id),
            urlencode(tenant_id)
        ))
    }

    pub fn entities(&self) -> ApiResult<Value> {
        self.get("/entities", false)
    }

    // ---- entity configuration + state (the web's Manage drawer) ---------
    // Creation/summon/visits stay deliberately out of scope (rituals);
    // these are the operator CONFIG controls the web console exposes.

    pub fn entity_cognition(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "cognition"), false)
    }

    /// Operator state verb: awake | asleep (optionally with a dream
    /// pass) | paused. The gateway records reason + principal.
    pub fn entity_state(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("POST", &entity_path(name, "state"), body, true)
    }

    pub fn entity_substrate(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "substrate"), false)
    }

    pub fn put_entity_substrate(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &entity_path(name, "substrate"), body, false)
    }

    pub fn entity_voice(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "voice"), false)
    }

    pub fn put_entity_voice(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &entity_path(name, "voice"), body, false)
    }

    pub fn entity_work_order(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "work-order"), false)
    }

    pub fn put_entity_work_order(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &entity_path(name, "work-order"), body, false)
    }

    pub fn put_entity_personal_grant(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &entity_path(name, "personal-grant"), body, false)
    }

    pub fn entity_loop_start(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("POST", &entity_path(name, "loop/start"), body, true)
    }

    pub fn entity_loop_stop(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("POST", &entity_path(name, "loop/stop"), body, true)
    }

    /// Operator-gated vector-index rewrite (all-or-nothing, takes the
    /// home lease) — slow by nature.
    pub fn entity_reembed(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("POST", &entity_path(name, "reembed"), body, true)
    }

    /// Chain/spark/manifest verification — an action-like READ.
    pub fn entity_verify(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "verify"), true)
    }

    /// The home's embedder pin (model + dimension) — reembed's verify.
    pub fn entity_embedding(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "embedding"), false)
    }

    /// Per-phase tool grants (resolved values + provenance).
    pub fn entity_tool_policy(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "tool-policy"), false)
    }

    /// The full grantable-tool matrix spec (options for the editor).
    pub fn entity_capability_matrix(&self) -> ApiResult<Value> {
        self.get("/entities/inventory/capability-matrix", false)
    }

    /// Write phase grants: {policy: {phase: [tools] | null | []}} —
    /// null resets the phase to the framework default, [] denies all.
    pub fn put_entity_tool_policy(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &entity_path(name, "tool-policy"), body, false)
    }

    /// Prompt overlay layers.
    pub fn entity_prompt(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "prompt"), false)
    }

    /// Write the overlay: {overlay: {layer: text}} (web parity — all
    /// layers ride every save).
    pub fn put_entity_prompt(&self, name: &str, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &entity_path(name, "prompt"), body, false)
    }

    /// Sleep-consolidation candidates awaiting waking review.
    pub fn entity_candidates(&self, name: &str) -> ApiResult<Value> {
        self.get(&entity_path(name, "candidates"), false)
    }

    pub fn entity_candidate_act(
        &self,
        name: &str,
        record_id: &str,
        promote: bool,
        body: &Value,
    ) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!(
                "/entities/{}/candidates/{}/{}",
                urlencode(name),
                urlencode(record_id),
                if promote { "promote" } else { "reject" }
            ),
            body,
            false,
        )
    }

    // ---- workflows (the registered bundle registry) ------------------------

    /// Every registered workflow, one item per (bundle, version), plus the
    /// versions the gateway REFUSED to serve and why.
    pub fn bundles(&self, include_drafts: bool) -> ApiResult<Value> {
        let drafts = if include_drafts { "1" } else { "0" };
        self.get(
            &format!("/bundles?all_versions=true&include_drafts={drafts}&include_deprecated=true"),
            true,
        )
    }

    /// Remove one version, or every version when `version` is empty.
    pub fn delete_bundle(&self, bundle_id: &str, version: &str) -> ApiResult<Value> {
        let path = if version.is_empty() {
            format!("/bundles/{}?reload=true", urlencode(bundle_id))
        } else {
            format!(
                "/bundles/{}?bundle_version={}&reload=true",
                urlencode(bundle_id),
                urlencode(version)
            )
        };
        self.delete(&path)
    }

    /// The ORIGINAL `.flow` bytes for one version.
    pub fn download_bundle(&self, bundle_id: &str, version: &str) -> ApiResult<Vec<u8>> {
        let path = format!(
            "/bundles/{}/download?bundle_version={}",
            urlencode(bundle_id),
            urlencode(version)
        );
        self.get_bytes(&path)
    }

    // ---- runtimes ---------------------------------------------------------

    pub fn runtimes(&self) -> ApiResult<Value> {
        self.get("/admin/runtimes?include_sizes=true", true)
    }

    /// One PAGE of runs with the console's filters (parity with the web
    /// console's Runs toolbar). `include_ledger_len=false` matches the web
    /// and skips a per-row cost the server warns is slow on file ledgers.
    pub fn runs(
        &self,
        limit: u32,
        offset: u32,
        status: &str,
        query: &str,
        root_only: bool,
    ) -> ApiResult<Value> {
        let mut path = format!(
            "/runs?limit={limit}&offset={offset}&root_only={root_only}&include_ledger_len=false"
        );
        if !status.is_empty() {
            path.push_str(&format!("&status={}", urlencode(status)));
        }
        if !query.is_empty() {
            path.push_str(&format!("&query={}", urlencode(query)));
        }
        self.get(&path, false)
    }

    /// Drill-in run summaries for ONE runtime plane (admin). The
    /// endpoint caps limit at 200 and lists children too — the caller
    /// root-filters on parent_run_id (no root_only param exists here).
    pub fn runtime_runs(
        &self,
        kind: &str,
        tenant_id: &str,
        runtime_id: &str,
        limit: u32,
        offset: u32,
    ) -> ApiResult<Value> {
        self.get(
            &format!(
                "/admin/runtimes/{}/{}/{}/runs?limit={}&offset={}",
                urlencode(kind),
                urlencode(tenant_id),
                urlencode(runtime_id),
                limit.min(200),
                offset
            ),
            false,
        )
    }

    /// Durable gateway command (cancel / inject_guidance) — consumed at
    /// the run's next tick boundary, exactly like the web console.
    pub fn post_command(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/commands", body, false)
    }

    pub fn run_status(&self, run_id: &str) -> ApiResult<Value> {
        self.get(&format!("/runs/{}", urlencode(run_id)), false)
    }

    /// The data-home registry. `sizes=false` answers WITHOUT walking any
    /// tree — the fast first paint; the sized pass follows (the walk takes
    /// tens of seconds and this worker is a single serial lane, so a sized
    /// first call blocks cancels, steers and refreshes behind it).
    pub fn data_homes(&self, sizes: bool) -> ApiResult<Value> {
        if sizes {
            self.get("/admin/data-homes", true)
        } else {
            self.get("/admin/data-homes?sizes=0", false)
        }
    }

    /// Remove stale registry ROWS (disk untouched). The gateway refuses
    /// rows whose path still exists — that 409 renders verbatim.
    pub fn forget_data_homes(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/admin/data-homes/forget", body, false)
    }

    /// Registered LOG homes with their files.
    pub fn logs(&self) -> ApiResult<Value> {
        self.get("/admin/logs", false)
    }

    /// Tail ONE log file. Terminal windows stay small on purpose (the
    /// payload is JSON-parsed, cloned into the store, then split into
    /// lines by the viewer — a 1 MB tail is ~3-4 MB resident).
    pub fn log_read(&self, home: &str, file: &str, max_bytes: u32) -> ApiResult<Value> {
        self.get(
            &format!(
                "/admin/logs/read?home={}&file={}&max_bytes={}",
                urlencode(home),
                urlencode(file),
                max_bytes
            ),
            false,
        )
    }

    /// Purge a registered data home. The gateway's own contract: call
    /// with dry_run first, then with confirm_name to execute.
    pub fn purge_data_home(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/admin/data-homes/purge", body, true)
    }

    pub fn runtime_reservations(&self) -> ApiResult<Value> {
        self.get("/admin/runtime-reservations", false)
    }

    pub fn reservation_transfer(&self, runtime_id: &str, body: &Value) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!(
                "/admin/runtime-reservations/{}/transfer",
                urlencode(runtime_id)
            ),
            body,
            false,
        )
    }

    pub fn reservation_purge(&self, runtime_id: &str, body: &Value) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!(
                "/admin/runtime-reservations/{}/purge",
                urlencode(runtime_id)
            ),
            body,
            true,
        )
    }

    /// Artifact metadata, most recent first — the deliverables (images,
    /// video, audio, text) runs produced. Never conflated with caches.
    /// Artifact bytes as TEXT, hard-capped. The content route streams the
    /// whole file with no ceiling, and `get()` always JSON-parses — so a
    /// preview needs its own bounded reader (design adversary BLOCKER-5).
    pub fn artifact_text(
        &self,
        run_id: &str,
        artifact_id: &str,
        max_bytes: usize,
    ) -> ApiResult<String> {
        use std::io::Read;
        let url = format!(
            "{}/api/gateway/runs/{}/artifacts/{}/content?access=preview",
            self.base_url,
            urlencode(run_id),
            urlencode(artifact_id)
        );
        let mut req = self.agent.get(&url);
        if let Some(tok) = &self.token {
            req = req.set("Authorization", &format!("Bearer {tok}"));
        }
        let resp = req
            .call()
            .map_err(|e| err_from_ureq("artifact content", e))?;
        let mut buf = String::new();
        resp.into_reader()
            .take(max_bytes as u64)
            .read_to_string(&mut buf)
            .map_err(|e| ApiError {
                kind: ApiErrorKind::Protocol,
                message: format!("artifact content unreadable: {e}"),
            })?;
        Ok(buf)
    }

    /// Artifact bytes, hard-capped — the image preview's source. Binary,
    /// so it cannot ride `get()` (which always JSON-parses).
    pub fn artifact_bytes(
        &self,
        run_id: &str,
        artifact_id: &str,
        max_bytes: usize,
    ) -> ApiResult<Vec<u8>> {
        use std::io::Read;
        let url = format!(
            "{}/api/gateway/runs/{}/artifacts/{}/content?access=preview",
            self.base_url,
            urlencode(run_id),
            urlencode(artifact_id)
        );
        let mut req = self.agent.get(&url);
        if let Some(tok) = &self.token {
            req = req.set("Authorization", &format!("Bearer {tok}"));
        }
        let resp = req
            .call()
            .map_err(|e| err_from_ureq("artifact content", e))?;
        let mut buf: Vec<u8> = Vec::new();
        resp.into_reader()
            .take(max_bytes as u64)
            .read_to_end(&mut buf)
            .map_err(|e| ApiError {
                kind: ApiErrorKind::Protocol,
                message: format!("artifact content unreadable: {e}"),
            })?;
        Ok(buf)
    }

    pub fn artifacts_search(
        &self,
        limit: u32,
        offset: u32,
        modality: &str,
        query: &str,
    ) -> ApiResult<Value> {
        let mut path = format!(
            "/artifacts/search?scope=all&limit={limit}&offset={offset}&order_by=created_at&order=desc"
        );
        if !modality.is_empty() {
            path.push_str(&format!("&modality={}", urlencode(modality)));
        }
        if !query.is_empty() {
            path.push_str(&format!("&query={}", urlencode(query)));
        }
        self.get(&path, false)
    }

    /// The runtime knobs surface (admin-only). The payload carries
    /// per-knob `{value, source}` provenance plus the admin-editable
    /// workspace policy fields.
    pub fn runtime_config(&self) -> ApiResult<Value> {
        self.get("/admin/runtime-config", false)
    }

    pub fn save_runtime_config(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/admin/runtime-config", body, false)
    }

    /// ONE user's workspace policy (per-runtime settings form). Identity
    /// components are the registry's safe charset ([A-Za-z0-9_.@-]) — no
    /// URL-encoding needed.
    pub fn save_user_workspace_policy(
        &self,
        tenant_id: &str,
        user_id: &str,
        body: &Value,
    ) -> ApiResult<Value> {
        self.send(
            "PUT",
            &format!("/admin/user-workspace-policy?tenant_id={tenant_id}&user_id={user_id}"),
            body,
            false,
        )
    }

    // ---- voice ------------------------------------------------------------

    /// TTS voice catalog for one (provider, model) pair — feeds the
    /// route editor's voice picker (server-side filtering, compact
    /// items; the same read the web console does).
    pub fn voices(&self, provider: &str, model: &str) -> ApiResult<Value> {
        self.get(
            &format!(
                "/voice/voices?compact=true&provider={}&model={}",
                urlencode(provider),
                urlencode(model)
            ),
            true,
        )
    }

    /// Real synthesis through the production TTS lane — the voice
    /// route's Test verb (the body carries timeout_s so a wedged
    /// backend fails fast with the watchdog's honest 504).
    pub fn run_voice_tts(&self, run_id: &str, body: &Value) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/runs/{}/voice/tts", urlencode(run_id)),
            body,
            true,
        )
    }

    // ---- sandbox ----------------------------------------------------------

    /// A real generation through the gateway — the wizard's "Test" verb.
    /// `capability` is the route key (output.text, input.text, …); the
    /// gateway resolves the right engine for it.
    pub fn sandbox_generate(
        &self,
        capability: &str,
        provider: &str,
        model: &str,
        prompt: &str,
        max_tokens: u32,
    ) -> ApiResult<Value> {
        self.sandbox_generate_with_controls(
            capability,
            provider,
            model,
            prompt,
            max_tokens,
            &json!({}),
        )
    }

    /// Request controls belong to the current audition, not a separate store.
    pub fn sandbox_generate_with_controls(
        &self,
        capability: &str,
        provider: &str,
        model: &str,
        prompt: &str,
        max_tokens: u32,
        controls: &Value,
    ) -> ApiResult<Value> {
        let mut body = json!({
            "capability": capability, "provider": provider, "model": model,
            "prompt": prompt, "max_tokens": max_tokens,
        });
        for key in ["reasoning", "speculation"] {
            if let Some(value) = controls.get(key).filter(|value| !value.is_null()) {
                body[key] = value.clone();
            }
        }
        self.send("POST", "/sandbox/generate", &body, true)
    }
}

/// Minimal percent-encoding for path/query components (RFC 3986
/// unreserved set stays literal). Gateway ids are conservative, but ids
/// are user input — never interpolate them raw.
/// Per-entity route path: every entity endpoint is `/entities/{name}/<leaf>`
/// with the name urlencoded — one constructor instead of 20 format! copies.
fn entity_path(name: &str, leaf: &str) -> String {
    format!("/entities/{}/{}", urlencode(name), leaf)
}

pub fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The socket-death classification (round-4): reset/aborted/pipe/
    /// eof retry; refused/timeout/DNS-shaped do NOT (retrying a down
    /// gateway or a 60s timeout doubles time-to-truth).
    #[test]
    fn io_death_classification_table() {
        use std::io::ErrorKind as K;
        for k in [
            K::ConnectionReset,
            K::ConnectionAborted,
            K::BrokenPipe,
            K::UnexpectedEof,
        ] {
            assert!(io_death_kind(k), "{k:?} is socket-death");
            let ureq_err = ureq::Error::from(std::io::Error::new(k, "x"));
            assert!(io_death(&ureq_err), "{k:?} rides the source chain");
        }
        for k in [
            K::ConnectionRefused,
            K::TimedOut,
            K::WouldBlock,
            K::NotFound,
        ] {
            assert!(!io_death_kind(k), "{k:?} is NOT retryable");
            let ureq_err = ureq::Error::from(std::io::Error::new(k, "x"));
            assert!(!io_death(&ureq_err), "{k:?} must not retry");
        }
        // HTTP status errors never classify as socket death.
        // (Transport-only check: a synthetic Status error needs a
        // Response, which ureq does not expose for construction —
        // the Transport arm above pins the discriminant logic.)
    }
}
