//! The worker thread: owns the `GatewayClient` and every HTTP call.
//!
//! Threading contract (the engine law): the UI thread owns all signals;
//! this thread never touches them directly — it posts closures through
//! `WakeHandle`, which run on the UI thread in the next frame's USER
//! phase. Commands flow the other way through an mpsc channel.
//!
//! Writes are three-phase BY CONSTRUCTION: write → verify via GET →
//! post {journal entry + refreshed domain}. Verify-after-write is the
//! epic's validation law; putting it here makes it impossible for a
//! screen to forget.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, Sender};

use abstracttui::reactive::WakeHandle;
use serde_json::Value;

use crate::api::{ApiError, ApiErrorKind, ApiResult, GatewayClient};
use crate::store::{
    entities_from_payload, models_from_payload, runtimes_from_payload, users_from_payload,
    AvailabilityData, ConnPhase, DiscoverOutcome, DownloadStatus, Identity, JournalEntry, Loadable,
    ProbeReport, ProfilesData, ProvidersData, RouteTestOutcome, RoutesData, RuntimeConfigData,
    SandboxOutcome, Store, VoicesData,
};

/// Probe sequence — every probe gets a visible number so repeated
/// probes are visibly distinct even when the outcome is identical.
static PROBE_SEQ: AtomicU64 = AtomicU64::new(1);

static OP_ID: AtomicU64 = AtomicU64::new(1);

/// Busy-op ids. `pub` for the Models tab's mutation senders: those ops
/// begin at ENQUEUE (UI side) so the busy strip shows the confirmed
/// action immediately even while a silent host-state poll holds the
/// serial lane — the id then rides the Cmd and the worker closes it.
pub fn next_op() -> u64 {
    OP_ID.fetch_add(1, Ordering::Relaxed)
}

static FORM_ID: AtomicU64 = AtomicU64::new(1);

/// Correlates a write command with the form modal that issued it, so
/// the form can close on success and stay open (data intact) on failure.
pub fn next_form_id() -> u64 {
    FORM_ID.fetch_add(1, Ordering::Relaxed)
}

/// Commands the UI sends to the worker. Debug is test surface: harnesses
/// assert on drained commands — but SECRETS NEVER PRINT (structural:
/// the `Secret`/`Body` newtypes redact in their own Debug, so a new
/// variant cannot forget).
#[derive(Clone, Debug)]
pub enum Cmd {
    /// (Re)connect: build a client for url+token, ping, then `/me`.
    Connect {
        url: String,
        token: Secret,
    },
    LoadProviders,
    LoadProfiles,
    LoadRoutes,
    /// `GET /models/availability` — are the configured models' WEIGHTS
    /// on the execution host?
    LoadAvailability,
    /// Start a download job. The ONLY command here that spends gigabytes,
    /// and it only ever runs from an explicit, confirmed `w` on a route
    /// the operator selected. Returns as soon as the gateway has a job id;
    /// watching it is `PollDownload`'s business.
    DownloadModel {
        provider: String,
        artifact: String,
    },
    /// ONE progress read of a running download job, then reschedule itself.
    ///
    /// This is why a download no longer blocks the console. The worker lane
    /// is serial, so a poll loop that slept 1.5s between reads held it for
    /// the whole download -- up to an hour -- and every other action the
    /// operator took queued behind it. Now each poll is one short GET and
    /// the next one arrives on the channel afterwards, so navigation, edits
    /// and refreshes interleave with a running download.
    PollDownload {
        job: String,
        provider: String,
        artifact: String,
        /// The busy-strip entry opened by `DownloadModel`, closed by the
        /// final poll -- the operator sees one continuous operation.
        op: u64,
        started_ms: u64,
    },
    /// ONE `GET /host/state` read (SLOW — GPU probe + residency
    /// listing), then reschedule itself ~4s later — the download-poll
    /// pattern: each poll is one short-lived command on the serial
    /// lane, the wait happens on a throwaway timer thread. GENERATION
    /// GATED: the result is published (and the next poll armed) only
    /// while `store.host_poll_gen` still equals `gen`, checked on the
    /// UI thread — leaving the Models tab bumps the generation and the
    /// chain dies. `first` = show the busy label (poll refreshes are
    /// silent); a failed poll publishes the honest Failed state and
    /// STOPS (recovery is `r` or tab re-entry — never a retry storm).
    PollHostState {
        gen: u64,
        first: bool,
    },
    /// Warm up (load) a model on the execution host (`w` on the Models
    /// tab). Slow: a cold load pulls weights into memory. `op` is the
    /// busy entry the SENDER already began at enqueue (all five model
    /// mutations carry one): a confirmed action must show in the strip
    /// immediately, not after the silent poll ahead of it on the
    /// serial lane finishes — the worker closes it via `finish_busy`.
    WarmupModel {
        task: Option<String>,
        provider: String,
        model: String,
        lock: bool,
        op: u64,
    },
    /// Unload a model. `force: true` only after the second confirm the
    /// gateway's HTTP 409 model_locked refusal triggers.
    UnloadModel {
        provider: String,
        model: String,
        force: bool,
        op: u64,
    },
    /// lock=true pins the model against unload/eviction; false releases.
    LockModel {
        provider: String,
        model: String,
        lock: bool,
        op: u64,
    },
    /// Clear every runtime-minted prompt cache of ONE session (admin;
    /// `c` on the Models tab's Caches sub-tab, danger-confirmed).
    ClearSessionCaches {
        session_id: String,
        op: u64,
    },
    /// Context/KV estimate for one row (`e`) — the answer lands as a
    /// notice line (confidence + predicted max + first note).
    ContextEstimate {
        provider: String,
        model: String,
        context_length: Option<u64>,
        op: u64,
    },
    LoadUsers,
    LoadEntities,
    LoadRuntimes,
    /// The registered workflow registry (bundles + the versions refused).
    LoadWorkflows {
        include_drafts: bool,
    },
    /// Remove one version, or every version when `version` is empty.
    /// There is NO undo — the gateway unlinks the file.
    DeleteWorkflow {
        bundle_id: String,
        version: String,
    },
    /// Write one version's ORIGINAL bytes to a LOCAL path. The TUI may be on
    /// a different machine than the gateway, so this is the operator's own
    /// filesystem, never the server's.
    ExportWorkflow {
        bundle_id: String,
        version: String,
        dest: String,
    },
    /// One page of artifact metadata (deliverables).
    LoadArtifacts {
        offset: u32,
        modality: String,
        query: String,
    },
    /// Log files across this gateway's registered log homes.
    LoadLogs,
    /// One artifact's IMAGE preview (fetched + decoded on the worker —
    /// decoding a 1.2 MB PNG on the UI thread would stutter the frame).
    LoadArtifactImage {
        run_id: String,
        artifact_id: String,
    },
    /// One artifact's text preview (bounded).
    LoadArtifactText {
        run_id: String,
        artifact_id: String,
    },
    /// One log file's tail.
    LoadLogText {
        home: String,
        file: String,
        max_bytes: u32,
    },
    /// Forget stale data-home registry rows (disk untouched).
    ForgetDataHomes {
        body: Body,
        all_stale: bool,
    },
    /// The gateway runtime/workspace settings surface.
    LoadRuntimeConfig,
    SaveRuntimeConfig {
        body: Body,
        form_id: Option<u64>,
    },
    /// ONE user's workspace policy (single-entry PUT — never the map
    /// replace, so it cannot clobber other users' entries).
    SaveUserWorkspacePolicy {
        tenant_id: String,
        user_id: String,
        body: Body,
        form_id: Option<u64>,
    },
    /// Per-provider model list (route editor pickers, provider drill-in).
    LoadModels {
        provider: String,
    },
    /// Test-connection for a profile form: saved id or unsaved draft.
    DiscoverModels {
        body: Body,
    },
    /// Create (`create=true`) or update a provider endpoint profile.
    SaveProfile {
        create: bool,
        id: String,
        body: Body,
        /// Present when a form modal awaits the outcome (close/stay-open).
        form_id: Option<u64>,
    },
    DeleteProfile {
        id: String,
    },
    /// Set a capability route (kind/modality[/task] from the row key).
    PutRoute {
        kind: String,
        modality: String,
        task: Option<String>,
        body: Body,
        key: String,
        form_id: Option<u64>,
    },
    ClearRoute {
        kind: String,
        modality: String,
        task: Option<String>,
        key: String,
    },
    /// Make the execution host's routes match the framework
    /// recommendation. `force` also replaces routes the operator
    /// configured differently; without it those are kept and named in
    /// the response, which is what the journal line reports.
    ApplyRecommendedRoutes {
        force: bool,
    },
    CreateUser {
        body: Body,
        form_id: Option<u64>,
    },
    /// PATCH — enable/disable, roles, rotate_token.
    PatchUser {
        user_id: String,
        tenant_id: String,
        body: Body,
        form_id: Option<u64>,
    },
    DeleteUser {
        user_id: String,
        tenant_id: String,
    },
    /// A real text generation — the wizard's "Test" verb.
    SandboxTest {
        provider: String,
        model: String,
        prompt: String,
    },
    /// Voice catalog for one (provider, model) — the route editor's
    /// voice picker (output.voice routes only).
    LoadVoices {
        provider: String,
        model: String,
    },
    /// The route editor's Test verb: output.voice synthesizes through
    /// the production TTS lane; every other capability goes through
    /// /sandbox/generate with the route's own key (web parity).
    TestRoute {
        key: String,
        provider: String,
        model: String,
        /// options.voice for the voice lane (None = provider default).
        voice: Option<String>,
        controls: Value,
        /// Session-scoped run id for the voice lane, minted UI-side
        /// from the connected principal — the same id shape the web
        /// console uses, so both UIs share one test-run plane.
        voice_run_id: String,
    },
    /// The manage snapshot: substrate + voice + work-order + cognition
    /// for one entity (each read degrades independently).
    LoadEntityDetail {
        name: String,
    },
    /// POST /entities/{name}/state — awake | asleep(+dream) | paused.
    EntityState {
        name: String,
        body: Body,
    },
    SaveEntitySubstrate {
        name: String,
        body: Body,
        form_id: Option<u64>,
    },
    SaveEntityVoice {
        name: String,
        body: Body,
        form_id: Option<u64>,
    },
    SaveEntityWorkOrder {
        name: String,
        body: Body,
        form_id: Option<u64>,
    },
    SavePersonalGrant {
        name: String,
        body: Body,
    },
    /// start=true → loop/start; false → loop/stop (body carries mode).
    EntityLoop {
        name: String,
        start: bool,
        body: Body,
    },
    EntityReembed {
        name: String,
        body: Body,
        form_id: Option<u64>,
    },
    /// GET /entities/{name}/verify — action-like read, result → notice.
    EntityVerify {
        name: String,
    },
    /// tool-policy + capability-matrix, folded for the editor.
    LoadToolPolicy {
        name: String,
    },
    SaveToolPolicy {
        name: String,
        body: Body,
        form_id: Option<u64>,
    },
    LoadEntityPrompt {
        name: String,
    },
    SaveEntityPrompt {
        name: String,
        body: Body,
        form_id: Option<u64>,
    },
    LoadCandidates {
        name: String,
    },
    /// Promote (with optional corroboration) or reject one candidate.
    CandidateAct {
        name: String,
        record_id: String,
        promote: bool,
        reason: String,
    },
    /// Load the runs of ONE runtime plane (the scope names which — the
    /// Runtimes screen's selection effect owns this slot end-to-end).
    LoadRuns {
        /// The filters this request answers (parity with the web Runs
        /// toolbar); they ride back in RunsData so a filter change
        /// invalidates held data.
        status: String,
        query: String,
        offset: u32,
        scope: crate::store::RunScope,
    },
    /// Durable cancel command (consumed at the run's next tick).
    CancelRun {
        run_id: String,
    },
    /// Durable inject_guidance command (steer).
    SteerRun {
        run_id: String,
        guidance: String,
    },
    LoadDataHomes {
        /// false = the fast no-walk listing (first paint); true = the
        /// sized pass. Two commands, so the serial worker is never
        /// blocked for the whole size walk before anything renders.
        sizes: bool,
    },
    /// Dry-run FIRST, then the real purge with confirm_name — one
    /// command so the dry-run can veto (its error stops everything).
    PurgeDataHome {
        name: String,
    },
    LoadReservations,
    ReservationTransfer {
        runtime_id: String,
        tenant_id: String,
        target_user_id: String,
    },
    ReservationPurge {
        runtime_id: String,
        tenant_id: String,
    },
}

/// A bearer/API secret: Debug prints «redacted» — a secret can never
/// leak through a NEW Cmd variant (F7: redaction was per-variant
/// vigilance in a 281-line manual Debug impl; now it is structural).
#[derive(Clone)]
pub struct Secret(pub String);

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "«redacted»")
    }
}

impl From<String> for Secret {
    fn from(v: String) -> Self {
        Secret(v)
    }
}

/// A write body: Debug prints the redacted rendering (api_key/token/
/// secret/authorization scrubbed, one level into "options" — route
/// bodies nest free-form JSON there). Derefs to Value so worker code
/// reads it like the payload it wraps.
#[derive(Clone)]
pub struct Body(pub Value);

impl std::fmt::Debug for Body {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        fn scrub(obj: &mut serde_json::Map<String, Value>) {
            for key in ["api_key", "token", "secret", "authorization"] {
                if obj.contains_key(key) {
                    obj.insert(key.into(), Value::String("«redacted»".into()));
                }
            }
        }
        let mut c = self.0.clone();
        if let Some(obj) = c.as_object_mut() {
            scrub(obj);
            if let Some(Value::Object(opts)) = obj.get_mut("options") {
                scrub(opts);
            }
        }
        write!(f, "{c}")
    }
}

impl From<Value> for Body {
    fn from(v: Value) -> Self {
        Body(v)
    }
}

impl std::ops::Deref for Body {
    type Target = Value;
    fn deref(&self) -> &Value {
        &self.0
    }
}

/// Spawned from the UI thread after mount. `on_token` receives the
/// once-shown token from create-user / rotate-token responses (the one
/// deliberate secret display, routed to a dedicated modal).
pub fn spawn(
    store: Store,
    wake: WakeHandle,
    rx: Receiver<Cmd>,
    // The worker's OWN handle back onto the command channel. A download
    // reschedules its next progress read through this instead of sleeping
    // on the lane, which is what keeps the console usable mid-download.
    tx: Sender<Cmd>,
    on_token: impl Fn(String, String) + Send + 'static,
    on_done: impl Fn(u64, Result<String, String>) + Send + 'static,
) -> std::thread::JoinHandle<()> {
    std::thread::Builder::new()
        .name("gateway-worker".into())
        .spawn(move || {
            let mut client: Option<GatewayClient> = None;
            while let Ok(cmd) = rx.recv() {
                // A panic in one command must not silently kill the
                // worker: later sends would vanish and the busy strip
                // would spin forever. Catch, clear busy, say so — and
                // RELEASE the form that issued the command (its
                // in_flight would otherwise show "applying…" forever).
                let panicked_form = cmd_form_id(&cmd);
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    handle(&mut client, &store, &wake, &tx, cmd, &on_token, &on_done);
                }));
                if let Err(payload) = result {
                    let msg = payload
                        .downcast_ref::<String>()
                        .cloned()
                        .or_else(|| payload.downcast_ref::<&str>().map(|s| s.to_string()))
                        .unwrap_or_else(|| "unknown panic".into());
                    if let Some(fid) = panicked_form {
                        on_done(fid, Err(format!("internal error: {msg}")));
                    }
                    let s = store;
                    wake.post(move || {
                        s.busy.update(|b| b.clear());
                        s.notice.set(Some(format!(
                            "internal error: the operation was abandoned ({msg})"
                        )));
                    });
                }
            }
        })
        .expect("spawn gateway worker")
}

/// The form (if any) awaiting this command's outcome — the panic path
/// must release it or the form stays wedged on "applying…".
fn cmd_form_id(cmd: &Cmd) -> Option<u64> {
    match cmd {
        Cmd::SaveProfile { form_id, .. }
        | Cmd::PutRoute { form_id, .. }
        | Cmd::CreateUser { form_id, .. }
        | Cmd::PatchUser { form_id, .. }
        | Cmd::SaveEntitySubstrate { form_id, .. }
        | Cmd::SaveEntityVoice { form_id, .. }
        | Cmd::SaveEntityWorkOrder { form_id, .. }
        | Cmd::SaveToolPolicy { form_id, .. }
        | Cmd::SaveEntityPrompt { form_id, .. }
        | Cmd::EntityReembed { form_id, .. } => *form_id,
        _ => None,
    }
}

/// The confirmed stored row, in the ONE sentence both consoles print
/// after a route write: what the store now holds and where it applies.
fn route_proof(row: &crate::store::RouteRow) -> String {
    let mut out = row.pair_text();
    if let Some(r) = row.reasoning.as_deref() {
        out.push_str(&format!(" · reasoning {r}"));
    }
    if !row.source.is_empty() {
        out.push_str(&format!(" (source: {})", row.source));
    }
    out
}

/// One line summarising an `applied_recommended` report.
///
/// Says BOTH halves: what changed, and what was deliberately kept. A
/// summary that only listed the writes would make "nothing happened to
/// my text route" look like a silent failure instead of the safety rule
/// it is.
pub fn applied_recommended_summary(payload: &Value) -> String {
    let Some(rows) = payload
        .get("applied_recommended")
        .and_then(|r| r.get("routes"))
        .and_then(Value::as_array)
    else {
        return String::new();
    };
    let pair = |row: Option<&Value>| -> String {
        let row = row.unwrap_or(&Value::Null);
        let get = |k: &str| {
            row.get(k)
                .and_then(Value::as_str)
                .unwrap_or("-")
                .to_string()
        };
        format!("{}/{}", get("provider"), get("model"))
    };
    let mut changed: Vec<String> = Vec::new();
    let mut kept: Vec<String> = Vec::new();
    for row in rows {
        let key = row.get("key").and_then(Value::as_str).unwrap_or("?");
        let action = row.get("action").and_then(Value::as_str).unwrap_or("");
        if row.get("changed").and_then(Value::as_bool).unwrap_or(false) {
            changed.push(format!(
                "{key}: {} → {}",
                pair(row.get("before")),
                pair(row.get("after"))
            ));
        } else if action == "kept" {
            kept.push(format!("{key} ({})", pair(row.get("before"))));
        }
    }
    let mut parts: Vec<String> = Vec::new();
    if !changed.is_empty() {
        parts.push(changed.join("; "));
    }
    if !kept.is_empty() {
        parts.push(format!("kept yours on {}", kept.join(", ")));
    }
    if parts.is_empty() {
        return "every recommended route already matched".to_string();
    }
    parts.join(" · ")
}

/// Busy bracket: marks the op in the busy strip for its whole duration.
/// Publish a PREVIEW result into the store's shared slot — but only if
/// the open modal still wants this target.
///
/// This worker is one serial lane and `artifact_text` / `artifact_image`
/// / `log_text` are global slots. Open three image artifacts in a row and
/// the third modal receives the first two results before its own: each
/// paints under the wrong header, which is the "an error flashes, then
/// the image appears" report. `store.preview_target` is stamped when a
/// preview modal opens; a result for anything else is DROPPED here rather
/// than rendered under the wrong title.
fn post_preview(
    wake: &WakeHandle,
    store: &Store,
    key: String,
    apply: impl FnOnce(&Store) + Send + 'static,
) {
    let s = *store;
    wake.post(move || {
        if !s.preview_wanted(&key) {
            return;
        }
        apply(&s);
    });
}

fn with_busy<T>(store: &Store, wake: &WakeHandle, label: &str, f: impl FnOnce() -> T) -> T {
    let id = next_op();
    let s = *store;
    let l = label.to_string();
    wake.post(move || s.begin_busy(id, &l));
    let out = f();
    let s = *store;
    wake.post(move || s.end_busy(id));
    out
}

/// `with_busy` for an op the ENQUEUE side already began: the model
/// mutations open their busy entry UI-side at send time (the strip must
/// show a confirmed action immediately — a silent steady-state
/// host-state poll can hold the serial lane for a whole slow GET, and a
/// keypress that shows nothing until then reads as dead). This half
/// only CLOSES the op when the work completes; the elapsed display
/// honestly spans queue wait + work, both of which the operator waited.
fn finish_busy<T>(store: &Store, wake: &WakeHandle, op: u64, f: impl FnOnce() -> T) -> T {
    let out = f();
    let s = *store;
    wake.post(move || s.end_busy(op));
    out
}

fn require_client(client: &Option<GatewayClient>) -> Result<GatewayClient, ApiError> {
    client.clone().ok_or(ApiError {
        kind: ApiErrorKind::NotConnected,
        message: "no gateway connection".into(),
    })
}

fn handle(
    client: &mut Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    cmd: Cmd,
    on_token: &(impl Fn(String, String) + Send + 'static),
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    match cmd {
        Cmd::Connect { url, token } => {
            let s = *store;
            wake.post(move || {
                s.conn.set(ConnPhase::Probing);
                // A probe targets a possibly-different gateway or
                // principal: every cached domain is now unvouched-for.
                // The ONE reset list lives in the store (P1: stale data
                // from gateway A must never render under gateway B's
                // header). This call site stays deliberately — the boot
                // auto-probe sends Cmd::Connect directly and never
                // passes through Ctx::reset_domains.
                s.reset_domains();
            });
            let candidate = GatewayClient::new(&url, Some(&token.0));
            let started = std::time::Instant::now();
            let outcome = with_busy(store, wake, "probing gateway", || {
                candidate.ping().and_then(|_| candidate.me())
            });
            let took_ms = started.elapsed().as_millis() as u64;
            let probe_seq = PROBE_SEQ.fetch_add(1, Ordering::Relaxed);
            let s = *store;
            // Every probe acknowledges itself: a numbered, timestamped
            // report + a toast — even when the outcome equals the
            // previous state (the operator's "nothing happens" incident).
            let ack = |ok: bool, outcome: String| {
                let report = ProbeReport {
                    seq: probe_seq,
                    at: crate::store::now_hms(),
                    ok,
                    outcome: outcome.clone(),
                    took_ms,
                };
                let note = format!(
                    "{} probe #{}: {} ({}ms)",
                    if ok { "✓" } else { "✗" },
                    probe_seq,
                    outcome,
                    took_ms
                );
                wake.post(move || {
                    s.last_probe.set(Some(report.clone()));
                    s.notice.set(Some(note.clone()));
                });
            };
            match outcome {
                Ok(me) => {
                    *client = Some(candidate);
                    let identity = Identity::from_me(&me);
                    match identity {
                        Some(id) => {
                            ack(
                                true,
                                format!(
                                    "connected as {}@{} ({}){}",
                                    id.user_id,
                                    id.tenant_id,
                                    id.auth_mode,
                                    if id.admin { ", admin" } else { "" }
                                ),
                            );
                            wake.post(move || s.conn.set(ConnPhase::Connected(id)));
                        }
                        None => {
                            ack(false, "unexpected /me payload (no principal)".into());
                            wake.post(move || {
                                s.conn.set(ConnPhase::Unauthorized(
                                    "unexpected /me payload (no principal)".into(),
                                ))
                            });
                        }
                    }
                }
                Err(e) => {
                    *client = None;
                    ack(false, e.to_string());
                    wake.post(move || {
                        s.conn.set(crate::store::phase_from_probe_error(&e));
                    });
                }
            }
        }

        Cmd::LoadProviders => load(store, wake, "loading providers", store.providers, || {
            require_client(client)?
                .discovery_providers()
                .map(|v| ProvidersData::from_value(&v))
        }),

        Cmd::LoadProfiles => load(store, wake, "loading profiles", store.profiles, || {
            require_client(client)?
                .profiles()
                .map(|v| ProfilesData::from_value(&v))
        }),

        Cmd::LoadRoutes => load(
            store,
            wake,
            "loading capability routes",
            store.routes,
            || {
                require_client(client)?
                    .capability_defaults()
                    .map(|v| RoutesData::from_value(&v))
            },
        ),

        Cmd::LoadAvailability => load(
            store,
            wake,
            "probing model weights",
            store.availability,
            || {
                require_client(client)?
                    .model_availability()
                    .map(|v| AvailabilityData::from_value(&v))
            },
        ),

        Cmd::DownloadModel { provider, artifact } => {
            handle_download(client, store, wake, tx, &provider, &artifact)
        }

        Cmd::PollDownload {
            job,
            provider,
            artifact,
            op,
            started_ms,
        } => handle_poll_download(
            client,
            store,
            wake,
            tx,
            &PollTarget {
                job,
                provider,
                artifact,
                op,
                started_ms,
            },
        ),

        Cmd::PollHostState { gen, first } => {
            // The GET runs on the worker lane; the busy label shows only
            // on the chain's FIRST hop (a label every 4s would strobe).
            let result = if first {
                with_busy(store, wake, "reading host state", || {
                    require_client(client).and_then(|c| c.host_state())
                })
            } else {
                require_client(client).and_then(|c| c.host_state())
            };
            let s = *store;
            let tx2 = tx.clone();
            // Publish AND reschedule on the UI thread, where the
            // generation gate can be read: a stale chain's result is
            // dropped whole (no publish, no next poll) — the one place
            // the chain can die is the one place that knows it should.
            wake.post(move || {
                if s.host_poll_gen.get_untracked() != gen {
                    return; // tab exited / reset / r restarted the chain
                }
                match result {
                    Ok(v) => {
                        s.host_state
                            .set(Loadable::Ready(crate::store::host_state_from_payload(&v)));
                        let tx3 = tx2.clone();
                        std::thread::Builder::new()
                            .name("host-state-poll-timer".into())
                            .spawn(move || {
                                std::thread::sleep(HOST_STATE_POLL_INTERVAL);
                                let _ = tx3.send(Cmd::PollHostState { gen, first: false });
                            })
                            .ok();
                    }
                    Err(e) => {
                        // Honest failure, and the chain STOPS: against a
                        // gateway that keeps failing, a 4s retry loop
                        // would be a storm. Recovery is r / re-entry.
                        s.host_state.set(Loadable::Failed(e));
                    }
                }
            });
        }

        Cmd::WarmupModel {
            task,
            provider,
            model,
            lock,
            op,
        } => {
            let action = format!("POST models/load {provider}/{model}");
            let (write, verify) = finish_busy(store, wake, op, || {
                let mut body = serde_json::json!({"provider": provider, "model": model});
                if let Some(t) = &task {
                    body["task"] = Value::String(t.clone());
                }
                if lock {
                    body["lock"] = Value::Bool(true);
                }
                let write = require_client(client).and_then(|c| c.load_model(&body));
                let verify = require_client(client).and_then(|c| c.models_loaded());
                (write, verify)
            });
            let (p2, m2) = (provider.clone(), model.clone());
            let verified = verify.as_ref().ok().map(|v| {
                let rows = crate::store::model_rows_from_payload(v);
                match rows.iter().find(|r| {
                    r.provider.as_deref() == Some(p2.as_str())
                        && r.model.as_deref() == Some(m2.as_str())
                }) {
                    Some(r) => Ok(format!(
                        "GET lists {p2}/{m2} (resident: {})",
                        crate::store::resident_label(r.resident)
                    )),
                    None => Err(format!("GET does not list {p2}/{m2}")),
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_model_rows(store, wake, &v);
            }
        }

        Cmd::UnloadModel {
            provider,
            model,
            force,
            op,
        } => {
            let action = format!(
                "POST models/unload {provider}/{model}{}",
                if force { " (force)" } else { "" }
            );
            let (write, verify) = finish_busy(store, wake, op, || {
                let mut body = serde_json::json!({"provider": provider, "model": model});
                if force {
                    body["force"] = Value::Bool(true);
                }
                let write = require_client(client).and_then(|c| c.unload_model(&body));
                // No verify read after a refusal — the state did not change.
                let verify = if write.is_ok() {
                    Some(require_client(client).and_then(|c| c.models_loaded()))
                } else {
                    None
                };
                (write, verify)
            });
            // HTTP 409 is the model_locked refusal BY CONTRACT (the one
            // unload failure with its own next step) — hand it to the
            // Models tab, whose effect offers the force-unload confirm.
            if let Err(e) = &write {
                if matches!(e.kind, ApiErrorKind::Http(409)) {
                    let s = *store;
                    let pair = (provider.clone(), model.clone());
                    wake.post(move || s.unload_locked.set(Some(pair)));
                }
            }
            let (p2, m2) = (provider.clone(), model.clone());
            let verified = verify.as_ref().and_then(|v| v.as_ref().ok()).map(|v| {
                let rows = crate::store::model_rows_from_payload(v);
                let still = rows.iter().any(|r| {
                    r.provider.as_deref() == Some(p2.as_str())
                        && r.model.as_deref() == Some(m2.as_str())
                        && r.resident == Some(true)
                });
                if still {
                    Err(format!("GET still lists {p2}/{m2} resident"))
                } else {
                    Ok(format!("GET no longer lists {p2}/{m2} as resident"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Some(Ok(v)) = verify {
                publish_model_rows(store, wake, &v);
            }
        }

        Cmd::LockModel {
            provider,
            model,
            lock,
            op,
        } => {
            let verb = if lock { "lock" } else { "unlock" };
            let action = format!("POST models/{verb} {provider}/{model}");
            let (write, verify) = finish_busy(store, wake, op, || {
                let body = serde_json::json!({"provider": provider, "model": model});
                let write = require_client(client).and_then(|c| {
                    if lock {
                        c.lock_model(&body)
                    } else {
                        c.unlock_model(&body)
                    }
                });
                let verify = require_client(client).and_then(|c| c.models_loaded());
                (write, verify)
            });
            let (p2, m2) = (provider.clone(), model.clone());
            let verified = verify.as_ref().ok().map(|v| {
                let rows = crate::store::model_rows_from_payload(v);
                match rows.iter().find(|r| {
                    r.provider.as_deref() == Some(p2.as_str())
                        && r.model.as_deref() == Some(m2.as_str())
                }) {
                    Some(r) => Ok(format!(
                        "GET shows {p2}/{m2} locked: {}",
                        match r.locked {
                            Some(true) => "yes",
                            Some(false) => "no",
                            None => "unreported",
                        }
                    )),
                    None => Err(format!("GET does not list {p2}/{m2}")),
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_model_rows(store, wake, &v);
            }
        }

        Cmd::ClearSessionCaches { session_id, op } => {
            let action = format!("clear prompt caches of session '{session_id}'");
            let (write, verify) = finish_busy(store, wake, op, || {
                let write =
                    require_client(client).and_then(|c| c.clear_session_prompt_caches(&session_id));
                let verify = require_client(client).and_then(|c| c.session_prompt_caches());
                (write, verify)
            });
            let sid = session_id.clone();
            let cleared_n = write
                .as_ref()
                .ok()
                .and_then(|v| v.get("count").and_then(Value::as_u64));
            let verified = verify.as_ref().ok().map(|v| {
                let rows = crate::store::session_caches_from_payload(v);
                let still = rows.iter().filter(|r| r.session_id == sid).count();
                if still > 0 {
                    Err(format!("GET still lists {still} cache(s) for '{sid}'"))
                } else {
                    Ok(format!(
                        "GET lists no caches for '{sid}'{}",
                        match cleared_n {
                            Some(n) => format!(" ({n} cleared)"),
                            None => String::new(),
                        }
                    ))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                let rows = crate::store::session_caches_from_payload(&v);
                let s = *store;
                wake.post(move || {
                    s.host_state.update(|hs| {
                        if let Loadable::Ready(d) = hs {
                            d.caches = rows;
                            d.recount();
                        }
                    });
                });
            }
        }

        Cmd::ContextEstimate {
            provider,
            model,
            context_length,
            op,
        } => {
            let result = finish_busy(store, wake, op, || {
                require_client(client)
                    .and_then(|c| c.context_estimate(&provider, &model, context_length))
            });
            let s = *store;
            let note = match &result {
                Ok(v) => {
                    // Body-over-transport: ok:false is a real "cannot
                    // answer" and must not dress up as an estimate.
                    if v.get("ok").and_then(Value::as_bool) == Some(false) {
                        format!(
                            "✗ context estimate {provider}/{model}: {}",
                            v.get("error")
                                .and_then(Value::as_str)
                                .unwrap_or("the host facade cannot answer")
                        )
                    } else {
                        let confidence = v
                            .get("confidence")
                            .and_then(Value::as_str)
                            .unwrap_or("unknown");
                        let predicted = v
                            .get("predicted_max_context")
                            .and_then(Value::as_u64)
                            .map(|n| format!(" — predicted max context {n}"))
                            .unwrap_or_default();
                        let first_note = v
                            .get("notes")
                            .and_then(Value::as_array)
                            .and_then(|a| a.first())
                            .and_then(Value::as_str)
                            .map(|n| format!(" · {n}"))
                            .unwrap_or_default();
                        format!(
                            "context estimate {provider}/{model}: {confidence}{predicted}{first_note}"
                        )
                    }
                }
                Err(e) => format!("✗ context estimate {provider}/{model}: {e}"),
            };
            wake.post(move || s.notice.set(Some(note.clone())));
        }

        Cmd::LoadUsers => load(store, wake, "loading users", store.users, || {
            require_client(client)?
                .users()
                .map(|v| users_from_payload(&v))
        }),

        Cmd::LoadEntities => load(store, wake, "loading entities", store.entities, || {
            require_client(client)?
                .entities()
                .map(|v| entities_from_payload(&v))
        }),

        Cmd::LoadWorkflows { include_drafts } => {
            load(store, wake, "loading workflows", store.workflows, || {
                require_client(client)?
                    .bundles(include_drafts)
                    .map(|v| crate::store::workflows_from_payload(&v))
            })
        }

        Cmd::DeleteWorkflow { bundle_id, version } => {
            let label = if version.is_empty() {
                bundle_id.clone()
            } else {
                format!("{bundle_id}@{version}")
            };
            let action = format!("DELETE workflow '{label}'");
            let (write, verify) = with_busy(store, wake, &format!("deleting {label}"), || {
                let write =
                    require_client(client).and_then(|c| c.delete_bundle(&bundle_id, &version));
                let verify = require_client(client).and_then(|c| c.bundles(true));
                (write, verify)
            });
            // VERIFY BY READING BACK: the gateway unlinks a file, and a write
            // that reports success while the row survives (duplicate files
            // claiming one bundle id) would otherwise read as done.
            let bid = bundle_id.clone();
            let ver = version.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let data = crate::store::workflows_from_payload(v);
                let still = data.rows.iter().any(|r| {
                    r.bundle_id == bid
                        && (ver.is_empty() || r.versions.iter().any(|(x, _, _, _)| *x == ver))
                });
                if still {
                    Err(format!("GET still lists '{label}'"))
                } else {
                    Ok(format!("GET no longer lists '{label}'"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.workflows,
                    crate::store::workflows_from_payload(&v),
                );
            }
        }

        Cmd::ExportWorkflow {
            bundle_id,
            version,
            dest,
        } => {
            let label = format!("{bundle_id}@{version}");
            let action = format!("EXPORT workflow '{label}' to {dest}");
            let write = with_busy(store, wake, &format!("exporting {label}"), || {
                let bytes =
                    require_client(client).and_then(|c| c.download_bundle(&bundle_id, &version))?;
                let path = std::path::PathBuf::from(&dest);
                if let Some(parent) = path.parent() {
                    if !parent.as_os_str().is_empty() {
                        std::fs::create_dir_all(parent).map_err(|e| crate::api::ApiError {
                            kind: crate::api::ApiErrorKind::Unreachable,
                            message: format!("cannot create {}: {e}", parent.display()),
                        })?;
                    }
                }
                // Refuse to clobber: an export that silently overwrote a file
                // would be a second destructive act hiding inside a safe verb.
                if path.exists() {
                    return Err(crate::api::ApiError {
                        kind: crate::api::ApiErrorKind::Unreachable,
                        message: format!("{} already exists", path.display()),
                    });
                }
                std::fs::write(&path, &bytes).map_err(|e| crate::api::ApiError {
                    kind: crate::api::ApiErrorKind::Unreachable,
                    message: format!("cannot write {}: {e}", path.display()),
                })?;
                Ok(serde_json::json!({
                    "ok": true, "path": dest, "bytes": bytes.len(),
                }))
            });
            let verified = Some(Ok(format!("wrote {dest}")));
            finish_write(store, wake, action, write, verified, None, on_done);
        }

        Cmd::LoadRuntimes => load(store, wake, "loading runtimes", store.runtimes, || {
            require_client(client)?
                .runtimes()
                .map(|v| runtimes_from_payload(&v))
        }),

        Cmd::LoadArtifacts {
            offset,
            modality,
            query,
        } => load(store, wake, "loading artifacts", store.artifacts, || {
            let v = require_client(client)?.artifacts_search(100, offset, &modality, &query)?;
            Ok(crate::store::artifacts_data_from_payload(
                &v, offset, &modality, &query,
            ))
        }),

        Cmd::LoadArtifactImage {
            run_id,
            artifact_id,
        } => {
            let key = crate::store::artifact_preview_key(&run_id, &artifact_id);
            // 12 MB cap: a screenshot is ~1 MB; past this the mosaic is
            // pointless and the memory is not.
            let out = with_busy(store, wake, "decoding image", || {
                require_client(client)
                    .and_then(|c| c.artifact_bytes(&run_id, &artifact_id, 12 * 1024 * 1024))
            });
            match out {
                Ok(bytes) => match abstracttui::gfx::decode_image(&bytes) {
                    Ok(bmp) => {
                        let handle = std::sync::Arc::new(bmp);
                        post_preview(wake, store, key, move |s| {
                            s.artifact_image.set(Some(handle))
                        });
                    }
                    Err(e) => {
                        let msg = format!("cannot decode this image here: {e}");
                        post_preview(wake, store, key, move |s| s.artifact_text.set(Some(msg)));
                    }
                },
                Err(e) => {
                    let msg = format!("preview failed: {e}");
                    post_preview(wake, store, key, move |s| s.artifact_text.set(Some(msg)));
                }
            }
        }

        Cmd::LoadArtifactText {
            run_id,
            artifact_id,
        } => {
            let key = crate::store::artifact_preview_key(&run_id, &artifact_id);
            let out = with_busy(store, wake, "reading artifact", || {
                require_client(client)
                    .and_then(|c| c.artifact_text(&run_id, &artifact_id, 256 * 1024))
            });
            let text = match out {
                Ok(t) if t.is_empty() => "(empty file)".to_string(),
                Ok(t) => {
                    // JSON artifacts arrive as ONE enormous line: pretty-print
                    // so the viewer has lines to scroll through at all.
                    match serde_json::from_str::<Value>(&t) {
                        Ok(v) => serde_json::to_string_pretty(&v).unwrap_or(t),
                        Err(_) => t,
                    }
                }
                Err(e) => format!("preview failed: {e}"),
            };
            post_preview(wake, store, key, move |s| s.artifact_text.set(Some(text)));
        }

        Cmd::LoadLogText {
            home,
            file,
            max_bytes,
        } => {
            // Same lane, same slot, same race as the artifact previews:
            // tail two files in a row and the first read lands under the
            // second file's header.
            let key = crate::store::log_preview_key(&home, &file);
            let out = with_busy(store, wake, "reading log", || {
                require_client(client).and_then(|c| c.log_read(&home, &file, max_bytes))
            });
            let text = match out {
                Ok(v) => {
                    let body = v
                        .get("content")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    if body.is_empty() {
                        "(empty file)".to_string()
                    } else {
                        body
                    }
                }
                Err(e) => format!("read failed: {e}"),
            };
            post_preview(wake, store, key, move |s| s.log_text.set(Some(text)));
        }

        Cmd::LoadLogs => load(store, wake, "loading logs", store.logs, || {
            require_client(client)?
                .logs()
                .map(|v| crate::store::log_files_from_payload(&v))
        }),

        Cmd::ForgetDataHomes { body, all_stale } => {
            let action = if all_stale {
                "forget all stale data-home rows".to_string()
            } else {
                "forget stale data-home row".to_string()
            };
            let (write, verify) = with_busy(store, wake, &action, || {
                let write = require_client(client).and_then(|c| c.forget_data_homes(&body));
                // Verify-after-write: re-read WITHOUT sizes (fast) so the
                // list reflects the forget immediately; the sized pass is
                // the operator's next explicit refresh.
                let verify = require_client(client).and_then(|c| c.data_homes(false));
                (write, verify)
            });
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok("GET reloaded the registry".to_string()));
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.data_homes,
                    crate::store::data_homes_from_payload(&v),
                );
            }
        }

        Cmd::LoadRuntimeConfig => load(
            store,
            wake,
            "loading runtime config",
            store.runtime_config,
            || {
                require_client(client)?
                    .runtime_config()
                    .map(|v| RuntimeConfigData::from_value(&v))
            },
        ),

        Cmd::SaveRuntimeConfig { body, form_id } => {
            let action = "POST runtime config".to_string();
            let (write, verify) = with_busy(store, wake, "saving runtime config", || {
                let write = require_client(client).and_then(|c| c.save_runtime_config(&body));
                let verify = require_client(client).and_then(|c| c.runtime_config());
                (write, verify)
            });
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok("GET reloaded runtime config".to_string()));
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.runtime_config,
                    RuntimeConfigData::from_value(&v),
                );
            }
        }

        Cmd::SaveUserWorkspacePolicy {
            tenant_id,
            user_id,
            body,
            form_id,
        } => {
            let action = format!("PUT workspace policy {tenant_id}:{user_id}");
            let (write, verify) = with_busy(store, wake, "saving workspace policy", || {
                let write = require_client(client)
                    .and_then(|c| c.save_user_workspace_policy(&tenant_id, &user_id, &body));
                // Verify-after-write: the knobs surface carries the per-user
                // map, so reloading it re-renders every summary honestly.
                let verify = require_client(client).and_then(|c| c.runtime_config());
                (write, verify)
            });
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok("GET reloaded runtime config".to_string()));
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.runtime_config,
                    RuntimeConfigData::from_value(&v),
                );
            }
        }

        Cmd::LoadModels { provider } => {
            let s = *store;
            let p = provider.clone();
            wake.post(move || {
                s.models
                    .update(|m| drop(m.insert(p.clone(), Loadable::Loading)))
            });
            let result = with_busy(store, wake, &format!("models: {provider}"), || {
                require_client(client)?
                    .provider_models(&provider)
                    .map(|v| models_from_payload(&v))
            });
            let s = *store;
            let p = provider.clone();
            wake.post(move || {
                let entry = match result {
                    Ok(models) => Loadable::Ready(models),
                    Err(e) => Loadable::Failed(e),
                };
                s.models
                    .update(|m| drop(m.insert(p.clone(), entry.clone())));
            });
        }

        Cmd::DiscoverModels { body } => load(
            store,
            wake,
            "testing endpoint (discover models)",
            store.discover,
            || {
                require_client(client)?
                    .discover_models(&body)
                    .map(|v| DiscoverOutcome::from_value(&v))
            },
        ),

        Cmd::SaveProfile {
            create,
            id,
            body,
            form_id,
        } => {
            let label = if create {
                format!("creating profile {id}")
            } else {
                format!("updating profile {id}")
            };
            let action = format!(
                "{} provider endpoint profile '{}'",
                if create { "POST" } else { "PUT" },
                id
            );
            // Write AND verify inside one busy bracket: the strip must
            // not clear while the verify GET is still running.
            let (write, verify) = with_busy(store, wake, &label, || {
                let write = require_client(client).and_then(|c| {
                    if create {
                        c.create_profile(&body)
                    } else {
                        c.update_profile(&id, &body)
                    }
                });
                let verify = require_client(client).and_then(|c| c.profiles());
                (write, verify)
            });
            let id2 = id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let data = ProfilesData::from_value(v);
                match data.profiles.iter().find(|p| p.id == id2) {
                    Some(p) => Ok(format!(
                        "GET shows '{}': {} @ {} (key {})",
                        p.id,
                        p.family,
                        if p.base_url.is_empty() {
                            p.default_base_url.clone().unwrap_or_default()
                        } else {
                            p.base_url.clone()
                        },
                        if p.api_key_set { "stored" } else { "none" }
                    )),
                    None => Err(format!("GET does not list profile '{id2}'")),
                }
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            // Refresh the domain from the verify GET we already paid for.
            if let Ok(v) = verify {
                publish_ready(wake, store.profiles, ProfilesData::from_value(&v));
            }
        }

        Cmd::DeleteProfile { id } => {
            let action = format!("DELETE provider endpoint profile '{id}'");
            let (write, verify) = with_busy(store, wake, &format!("deleting profile {id}"), || {
                let write = require_client(client).and_then(|c| c.delete_profile(&id));
                let verify = require_client(client).and_then(|c| c.profiles());
                (write, verify)
            });
            let id2 = id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let data = ProfilesData::from_value(v);
                if data.profiles.iter().any(|p| p.id == id2 && !p.synthetic) {
                    Err(format!("GET still lists profile '{id2}'"))
                } else {
                    Ok(format!("GET no longer lists profile '{id2}'"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.profiles, ProfilesData::from_value(&v));
            }
        }

        Cmd::PutRoute {
            kind,
            modality,
            task,
            body,
            key,
            form_id,
        } => {
            let action = format!("PUT capability route {key}");
            // The PUT response IS the refreshed payload; still verify via
            // an explicit GET (the epic's law) and re-render from it —
            // both halves inside one busy bracket.
            let (write, verify) = with_busy(store, wake, &format!("setting route {key}"), || {
                let write = require_client(client)
                    .and_then(|c| c.put_route(&kind, &modality, task.as_deref(), &body));
                let verify = require_client(client).and_then(|c| c.capability_defaults());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let data = RoutesData::from_value(v);
                match data.rows.iter().find(|r| r.key == key) {
                    Some(r) if r.configured => Ok(format!("GET shows {key} = {}", route_proof(r))),
                    Some(_) => Err(format!("GET shows {key} still not configured")),
                    None => Err(format!("GET no longer lists route {key}")),
                }
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.routes, RoutesData::from_value(&v));
            }
        }

        Cmd::ClearRoute {
            kind,
            modality,
            task,
            key,
        } => {
            let action = format!("DELETE capability route {key}");
            let (write, verify) = with_busy(store, wake, &format!("clearing route {key}"), || {
                let write = require_client(client)
                    .and_then(|c| c.clear_route(&kind, &modality, task.as_deref()));
                let verify = require_client(client).and_then(|c| c.capability_defaults());
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let data = RoutesData::from_value(v);
                match data.rows.iter().find(|r| r.key == key) {
                    Some(r) if !r.configured || r.covered_by.is_some() => Ok(format!(
                        "GET shows {} cleared ({})",
                        key,
                        if r.covered_by.is_some() {
                            "now covered"
                        } else {
                            "not configured"
                        }
                    )),
                    Some(r) => Err(format!(
                        "GET still shows {} configured ({} / {})",
                        key,
                        r.provider.clone().unwrap_or_default(),
                        r.model.clone().unwrap_or_default()
                    )),
                    None => Err(format!("GET no longer lists route {key}")),
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.routes, RoutesData::from_value(&v));
            }
        }

        Cmd::ApplyRecommendedRoutes { force } => {
            let action = if force {
                "POST apply-recommended routes (--force)".to_string()
            } else {
                "POST apply-recommended routes".to_string()
            };
            let (write, verify) = with_busy(store, wake, "applying recommended routes", || {
                let write = require_client(client).and_then(|c| c.apply_recommended_routes(force));
                let verify = require_client(client).and_then(|c| c.capability_defaults());
                (write, verify)
            });
            // THE PROOF IS THE REPORT, not a fixed provider/model. Which
            // routes changed is AbstractCore's decision (a route the
            // operator configured differently is KEPT), so asserting a
            // specific pair here would either duplicate that decision or
            // fail the write for doing exactly what it was asked.
            let summary = write
                .as_ref()
                .ok()
                .map(applied_recommended_summary)
                .unwrap_or_default();
            let verified = verify.as_ref().ok().map(|_| {
                if summary.is_empty() {
                    Ok("GET re-read the route grid".to_string())
                } else {
                    Ok(format!("GET re-read the route grid — {summary}"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.routes, RoutesData::from_value(&v));
            }
            // The recommendation may now name models that are not on the
            // host — the weights banner must say so without a manual `r`.
            // Queued (not called inline) so it rides the serial lane like
            // every other read.
            let _ = tx.send(Cmd::LoadAvailability);
        }

        Cmd::CreateUser { body, form_id } => {
            let user_id = body
                .get("user_id")
                .and_then(Value::as_str)
                .unwrap_or("?")
                .to_string();
            let action = format!("POST user '{user_id}'");
            let (write, verify) =
                with_busy(store, wake, &format!("creating user {user_id}"), || {
                    let write = require_client(client).and_then(|c| c.create_user(&body));
                    let verify = require_client(client).and_then(|c| c.users());
                    (write, verify)
                });
            // The token is returned exactly once — route it to the token
            // modal, never to the journal/logs.
            if let Ok(v) = &write {
                if let Some(tok) = v.get("token").and_then(Value::as_str) {
                    on_token(user_id.clone(), tok.to_string());
                }
            }
            let uid = user_id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let users = users_from_payload(v);
                if users.humans.iter().any(|u| u.user_id == uid) {
                    Ok(format!("GET lists user '{uid}'"))
                } else {
                    Err(format!("GET does not list user '{uid}'"))
                }
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.users, users_from_payload(&v));
            }
        }

        Cmd::PatchUser {
            user_id,
            tenant_id,
            body,
            form_id,
        } => {
            let rotating = body
                .get("rotate_token")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let action = format!(
                "PATCH user '{user_id}'{}",
                if rotating { " (rotate token)" } else { "" }
            );
            let (write, verify) =
                with_busy(store, wake, &format!("updating user {user_id}"), || {
                    let write = require_client(client)
                        .and_then(|c| c.patch_user(&user_id, &tenant_id, &body));
                    let verify = require_client(client).and_then(|c| c.users());
                    (write, verify)
                });
            if let Ok(v) = &write {
                if let Some(tok) = v.get("token").and_then(Value::as_str) {
                    on_token(user_id.clone(), tok.to_string());
                }
            }
            let uid = user_id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let users = users_from_payload(v);
                match users.humans.iter().find(|u| u.user_id == uid) {
                    Some(u) => Ok(format!(
                        "GET shows '{}': roles [{}], {}",
                        u.user_id,
                        u.roles.join(", "),
                        if u.enabled { "enabled" } else { "disabled" }
                    )),
                    None => Err(format!("GET does not list user '{uid}'")),
                }
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.users, users_from_payload(&v));
            }
        }

        Cmd::DeleteUser { user_id, tenant_id } => {
            let action = format!("DELETE user '{user_id}'");
            let (write, verify) =
                with_busy(store, wake, &format!("deleting user {user_id}"), || {
                    let write =
                        require_client(client).and_then(|c| c.delete_user(&user_id, &tenant_id));
                    let verify = require_client(client).and_then(|c| c.users());
                    (write, verify)
                });
            let uid = user_id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let users = users_from_payload(v);
                if users.humans.iter().any(|u| u.user_id == uid) {
                    Err(format!("GET still lists user '{uid}'"))
                } else {
                    Ok(format!("GET no longer lists user '{uid}'"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(wake, store.users, users_from_payload(&v));
            }
        }

        Cmd::SandboxTest {
            provider,
            model,
            prompt,
        } => {
            let label = format!("sandbox test: {provider}/{model}");
            load(store, wake, &label, store.sandbox, || {
                require_client(client)?
                    .sandbox_generate("output.text", &provider, &model, &prompt, 64)
                    .map(|v| SandboxOutcome::from_value(&provider, &model, &v))
            });
        }

        Cmd::LoadVoices { provider, model } => {
            let label = format!("voices: {provider}/{model}");
            load(store, wake, &label, store.voices, || {
                require_client(client)?
                    .voices(&provider, &model)
                    .map(|v| VoicesData::from_value(&provider, &model, &v))
            });
        }

        Cmd::TestRoute {
            key,
            provider,
            model,
            voice,
            controls,
            voice_run_id,
        } => {
            let s = *store;
            wake.post(move || s.route_test.set(Loadable::Loading));
            let started = std::time::Instant::now();
            let is_voice = key == "output.voice";
            let result = with_busy(
                store,
                wake,
                &format!("testing {key}: {provider}/{model}"),
                || {
                    let c = require_client(client)?;
                    if is_voice {
                        // Real synthesis; timeout_s makes a wedged TTS
                        // backend fail fast (watchdog 504), never hang
                        // this modal (the 2026-07-17 wedge history).
                        let mut body = serde_json::json!({
                            "text": "Hello — this is the voice you selected, speaking from the gateway.",
                            "provider": provider,
                            "model": model,
                            "request_id": format!("tui_test_{}", next_op()),
                            "timeout_s": 25,
                        });
                        if let Some(v) = &voice {
                            body["voice"] = Value::String(v.clone());
                        }
                        c.run_voice_tts(&voice_run_id, &body)
                    } else {
                        c.sandbox_generate_with_controls(
                            &key,
                            &provider,
                            &model,
                            "Reply with the single word: ready.",
                            16,
                            &controls,
                        )
                    }
                },
            );
            let secs = started.elapsed().as_secs_f32();
            let pair = format!("{provider}/{model}");
            let outcome = result.map(|v| {
                if is_voice {
                    let artifact = v
                        .get("audio_artifact")
                        .map(artifact_ref_label)
                        .filter(|a| !a.is_empty());
                    RouteTestOutcome {
                        ok: v.get("ok").and_then(Value::as_bool).unwrap_or(true),
                        summary: format!(
                            "synthesized in {secs:.1}s with {pair}{}",
                            match &voice {
                                Some(vc) => format!(" / {vc}"),
                                None => " (provider default voice)".into(),
                            }
                        ),
                        detail: artifact.map(|a| format!("audio artifact: {a}")),
                    }
                } else {
                    // Body-level failure on a 200 is still a failure
                    // (body-over-transport law).
                    let err = v
                        .get("error")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                        .filter(|e| !e.is_empty());
                    let ok = v.get("ok").and_then(Value::as_bool).unwrap_or(false) && err.is_none();
                    let response: String = v
                        .get("response")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .chars()
                        .take(120)
                        .collect();
                    let mtp = match v.get("speculation") {
                        Some(outcome)
                            if outcome.get("used").and_then(Value::as_bool) == Some(true) =>
                        {
                            format!(
                                "MTP used{}",
                                outcome
                                    .get("num_draft_tokens")
                                    .map(|n| format!(" (depth {n})"))
                                    .unwrap_or_default()
                            )
                        }
                        Some(outcome) if !outcome.is_null() => format!(
                            "MTP not used{}",
                            outcome
                                .get("reason")
                                .and_then(Value::as_str)
                                .map(|reason| format!(": {reason}"))
                                .unwrap_or_default()
                        ),
                        _ => "MTP execution not reported".to_string(),
                    };
                    RouteTestOutcome {
                        ok,
                        summary: format!("responded in {secs:.1}s with {pair} · {mtp}"),
                        detail: err.or(if response.is_empty() {
                            Some("(empty response)".into())
                        } else {
                            Some(response)
                        }),
                    }
                }
            });
            let s = *store;
            wake.post(move || {
                s.route_test.set(match outcome {
                    Ok(o) => Loadable::Ready(o),
                    Err(e) => Loadable::Failed(e),
                })
            });
        }

        Cmd::LoadEntityDetail { name } => {
            let label = format!("entity detail: {name}");
            load(store, wake, &label, store.entity_detail, || {
                load_entity_detail(client, &name)
            });
        }

        Cmd::EntityState { name, body } => {
            let target = body
                .get("state")
                .and_then(Value::as_str)
                .unwrap_or("?")
                .to_string();
            let action = format!("POST entity '{name}' state → {target}");
            let (write, verify) = with_busy(store, wake, &format!("{name}: → {target}"), || {
                let write = require_client(client).and_then(|c| c.entity_state(&name, &body));
                let verify = require_client(client).and_then(|c| c.entity_cognition(&name));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let now = v
                    .get("state")
                    .and_then(|st| st.get("state"))
                    .and_then(Value::as_str)
                    .unwrap_or("?");
                if now == target {
                    Ok(format!("GET shows '{name}' {now}"))
                } else {
                    // Not necessarily wrong: state is intent, actuality
                    // settles (the cognition payload's own authority
                    // note) — surface both truthfully.
                    Err(format!("GET shows '{name}' {now} (asked for {target})"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            refresh_entities(client, store, wake);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::SaveEntitySubstrate {
            name,
            body,
            form_id,
        } => {
            let action = format!("PUT entity '{name}' substrate");
            let (write, verify) = with_busy(store, wake, &format!("{name}: substrate"), || {
                let write =
                    require_client(client).and_then(|c| c.put_entity_substrate(&name, &body));
                let verify = require_client(client).and_then(|c| c.entity_substrate(&name));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                Ok(format!(
                    "GET shows mind: {} / {}",
                    v.get("provider").and_then(Value::as_str).unwrap_or("—"),
                    v.get("model").and_then(Value::as_str).unwrap_or("—")
                ))
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::SaveEntityVoice {
            name,
            body,
            form_id,
        } => {
            let clearing = body.get("clear").and_then(Value::as_bool).unwrap_or(false);
            let action = format!(
                "PUT entity '{name}' voice{}",
                if clearing { " (clear)" } else { "" }
            );
            let (write, verify) = with_busy(store, wake, &format!("{name}: voice"), || {
                let write = require_client(client).and_then(|c| c.put_entity_voice(&name, &body));
                let verify = require_client(client).and_then(|c| c.entity_voice(&name));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let set = v.get("provider").and_then(Value::as_str).unwrap_or("");
                if set.is_empty() {
                    Ok("GET shows voice unset (gateway default applies)".to_string())
                } else {
                    Ok(format!(
                        "GET shows voice: {} / {} / {}",
                        set,
                        v.get("model").and_then(Value::as_str).unwrap_or("—"),
                        v.get("voice").and_then(Value::as_str).unwrap_or("—")
                    ))
                }
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::SaveEntityWorkOrder {
            name,
            body,
            form_id,
        } => {
            let clearing = body.get("clear").and_then(Value::as_bool).unwrap_or(false);
            let action = format!(
                "PUT entity '{name}' work order{}",
                if clearing { " (clear)" } else { "" }
            );
            let (write, verify) = with_busy(store, wake, &format!("{name}: work order"), || {
                let write =
                    require_client(client).and_then(|c| c.put_entity_work_order(&name, &body));
                let verify = require_client(client).and_then(|c| c.entity_work_order(&name));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                match v
                    .get("order")
                    .and_then(Value::as_str)
                    .filter(|o| !o.is_empty())
                {
                    Some(o) => Ok(format!(
                        "GET shows work order: {}",
                        o.chars().take(60).collect::<String>()
                    )),
                    None => Ok("GET shows no active work order".to_string()),
                }
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::SavePersonalGrant { name, body } => {
            let mode = body
                .get("mode")
                .and_then(Value::as_str)
                .unwrap_or("?")
                .to_string();
            let action = format!("PUT entity '{name}' personal grant → {mode}");
            let (write, verify) =
                with_busy(store, wake, &format!("{name}: own-time grant"), || {
                    let write = require_client(client)
                        .and_then(|c| c.put_entity_personal_grant(&name, &body));
                    let verify = require_client(client).and_then(|c| c.entity_cognition(&name));
                    (write, verify)
                });
            let verified = verify
                .as_ref()
                .ok()
                .map(|_| Ok(format!("grant now {mode} (cognition re-read)")));
            finish_write(store, wake, action, write, verified, None, on_done);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::EntityLoop { name, start, body } => {
            let action = if start {
                format!("POST entity '{name}' loop/start")
            } else {
                format!(
                    "POST entity '{name}' loop/stop ({})",
                    body.get("mode")
                        .and_then(Value::as_str)
                        .unwrap_or("graceful")
                )
            };
            let (write, verify) = with_busy(store, wake, &format!("{name}: own-time loop"), || {
                let write = require_client(client).and_then(|c| {
                    if start {
                        c.entity_loop_start(&name, &body)
                    } else {
                        c.entity_loop_stop(&name, &body)
                    }
                });
                let verify = require_client(client).and_then(|c| c.entity_cognition(&name));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let running = v
                    .get("loop")
                    .and_then(|l| l.get("running"))
                    .and_then(Value::as_bool);
                Ok(format!(
                    "GET shows loop {}",
                    match running {
                        Some(true) => "running",
                        Some(false) => "stopped",
                        // A stop is a durable command consumed at the
                        // next tick boundary — "not yet settled" is
                        // the honest read.
                        None => "state unreported",
                    }
                ))
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::EntityReembed {
            name,
            body,
            form_id,
        } => {
            let action = format!("POST entity '{name}' reembed");
            let (write, verify) =
                with_busy(store, wake, &format!("{name}: re-embed (slow)"), || {
                    let write = require_client(client).and_then(|c| c.entity_reembed(&name, &body));
                    let verify = require_client(client).and_then(|c| c.entity_embedding(&name));
                    (write, verify)
                });
            let verified = verify.as_ref().ok().map(|v| {
                Ok(format!(
                    "GET shows embedder pin: {} (dim {})",
                    v.get("model")
                        .and_then(Value::as_str)
                        .or_else(|| v.get("embedding_model").and_then(Value::as_str))
                        .unwrap_or("?"),
                    v.get("dimension")
                        .and_then(Value::as_u64)
                        .map(|d| d.to_string())
                        .unwrap_or_else(|| "?".into())
                ))
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            refresh_entity_detail(client, store, wake, &name);
        }

        Cmd::LoadToolPolicy { name } => {
            let label = format!("{name}: tool policy");
            load(store, wake, &label, store.entity_policy, || {
                let c = require_client(client)?;
                let policy = c.entity_tool_policy(&name)?;
                // The matrix is the OPTION SET; its failure degrades to
                // the union of granted tools, never blocks the editor.
                let matrix = c.entity_capability_matrix().ok();
                Ok(crate::store::ToolPolicyData::fold(
                    &name,
                    &policy,
                    matrix.as_ref(),
                ))
            });
        }

        Cmd::SaveToolPolicy {
            name,
            body,
            form_id,
        } => {
            let action = format!("PUT entity '{name}' tool policy");
            let (write, verify) = with_busy(store, wake, &format!("{name}: tool policy"), || {
                let write =
                    require_client(client).and_then(|c| c.put_entity_tool_policy(&name, &body));
                let verify = require_client(client).and_then(|c| c.entity_tool_policy(&name));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                let counts: Vec<String> = v
                    .get("phases")
                    .and_then(Value::as_object)
                    .map(|obj| {
                        obj.iter()
                            .map(|(phase, spec)| {
                                format!(
                                    "{phase}:{}",
                                    spec.get("tools")
                                        .and_then(Value::as_array)
                                        .map(|a| a.len())
                                        .unwrap_or(0)
                                )
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                Ok(format!("GET shows grants — {}", counts.join(" ")))
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            // Refresh the editor state from the verify we already paid for.
            if let Ok(policy) = verify {
                let matrix = require_client(client)
                    .ok()
                    .and_then(|c| c.entity_capability_matrix().ok());
                let folded = crate::store::ToolPolicyData::fold(&name, &policy, matrix.as_ref());
                publish_ready(wake, store.entity_policy, folded);
            }
        }

        Cmd::LoadEntityPrompt { name } => {
            let label = format!("{name}: prompt overlay");
            load(store, wake, &label, store.entity_prompt, || {
                require_client(client)?
                    .entity_prompt(&name)
                    .map(|v| crate::store::PromptData::from_value(&name, &v))
            });
        }

        Cmd::SaveEntityPrompt {
            name,
            body,
            form_id,
        } => {
            let action = format!("PUT entity '{name}' prompt overlay");
            let (write, verify) =
                with_busy(store, wake, &format!("{name}: prompt overlay"), || {
                    let write =
                        require_client(client).and_then(|c| c.put_entity_prompt(&name, &body));
                    let verify = require_client(client).and_then(|c| c.entity_prompt(&name));
                    (write, verify)
                });
            let verified = verify.as_ref().ok().map(|v| {
                let d = crate::store::PromptData::from_value(&name, v);
                let sizes: Vec<String> = d
                    .layers
                    .iter()
                    .map(|(l, t)| format!("{l}:{}ch", t.chars().count()))
                    .collect();
                Ok(format!("GET shows layers — {}", sizes.join(" ")))
            });
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if let Ok(v) = verify {
                let folded = crate::store::PromptData::from_value(&name, &v);
                publish_ready(wake, store.entity_prompt, folded);
            }
        }

        Cmd::LoadCandidates { name } => {
            let label = format!("{name}: candidates");
            load(store, wake, &label, store.entity_candidates, || {
                require_client(client)?
                    .entity_candidates(&name)
                    .map(|v| crate::store::candidates_from_payload(&name, &v))
            });
        }

        Cmd::CandidateAct {
            name,
            record_id,
            promote,
            reason,
        } => {
            let verb = if promote { "promote" } else { "reject" };
            let short = suffix_chars(&record_id, 8);
            let action = format!("{verb} candidate …{short} on '{name}'");
            let (write, verify) = with_busy(store, wake, &action, || {
                let body = if promote {
                    serde_json::json!({ "corroborating_ids": [], "reason": reason })
                } else {
                    serde_json::json!({ "reason": reason })
                };
                let write = require_client(client)
                    .and_then(|c| c.entity_candidate_act(&name, &record_id, promote, &body));
                let verify = require_client(client).and_then(|c| c.entity_candidates(&name));
                (write, verify)
            });
            let rid = record_id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let (_, rows) = crate::store::candidates_from_payload(&name, v);
                if rows.iter().any(|r| r.record_id == rid) {
                    Err(format!("GET still lists candidate {rid}"))
                } else {
                    Ok(format!(
                        "GET no longer lists it ({} candidate(s) remain)",
                        rows.len()
                    ))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                let folded = crate::store::candidates_from_payload(&name, &v);
                publish_ready(wake, store.entity_candidates, folded);
            }
        }

        Cmd::LoadRuns {
            scope,
            status,
            query,
            offset,
        } => {
            let label = format!("loading runs: {}", scope.short());
            load(store, wake, &label, store.runs, || {
                let c = require_client(client)?;
                let (rows, has_more, root_only) = match &scope {
                    crate::store::RunScope::Own => {
                        // Server-side filters + paging (the web console's lane).
                        let v = c.runs(100, offset, &status, &query, true)?;
                        let more = v.get("has_more").and_then(Value::as_bool).unwrap_or(false);
                        (crate::store::runs_from_payload(&v), more, true)
                    }
                    crate::store::RunScope::Plane {
                        kind,
                        tenant_id,
                        runtime_id,
                        ..
                    } => {
                        // The drill-in serves NO status/query/root_only —
                        // exactly like the web console's read-only plane
                        // view, which shows a pager and no toolbar. The
                        // old client-side root filter is GONE: filtering a
                        // server-paged list client-side made the pager
                        // describe a different set than the rows.
                        let v = c.runtime_runs(kind, tenant_id, runtime_id, 100, offset)?;
                        let more = v.get("has_more").and_then(Value::as_bool).unwrap_or(false);
                        (crate::store::runs_from_payload(&v), more, false)
                    }
                };
                Ok(crate::store::RunsData {
                    scope,
                    rows,
                    status,
                    query,
                    root_only,
                    offset,
                    has_more,
                })
            })
        }

        Cmd::CancelRun { run_id } => {
            let action = format!("cancel run {}", short_id(&run_id));
            let (write, verify) = with_busy(store, wake, &action, || {
                let body = serde_json::json!({
                    "command_id": format!("tui-cancel-{}", next_op()),
                    "type": "cancel",
                    "run_id": run_id,
                });
                let write = require_client(client).and_then(|c| c.post_command(&body));
                let verify = require_client(client).and_then(|c| c.run_status(&run_id));
                (write, verify)
            });
            // The command is DURABLE: it lands at the run's next tick
            // boundary, so "still running" right after is honest.
            let verified = verify.as_ref().ok().map(|v| {
                Ok(format!(
                    "GET shows status={} (cancel is consumed at the next tick boundary)",
                    v.get("status").and_then(Value::as_str).unwrap_or("?")
                ))
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            refresh_runs(client, store, wake);
        }

        Cmd::SteerRun { run_id, guidance } => {
            let action = format!("steer run {}", short_id(&run_id));
            let (write, verify) = with_busy(store, wake, &action, || {
                let body = serde_json::json!({
                    "command_id": format!("tui-steer-{}", next_op()),
                    "type": "inject_guidance",
                    "run_id": run_id,
                    "payload": { "guidance": guidance },
                });
                let write = require_client(client).and_then(|c| c.post_command(&body));
                let verify = require_client(client).and_then(|c| c.run_status(&run_id));
                (write, verify)
            });
            let verified = verify.as_ref().ok().map(|v| {
                Ok(format!(
                    "GET shows status={} (guidance folds at the next loop boundary)",
                    v.get("status").and_then(Value::as_str).unwrap_or("?")
                ))
            });
            finish_write(store, wake, action, write, verified, None, on_done);
        }

        Cmd::LoadDataHomes { sizes } => {
            load(store, wake, "loading data homes", store.data_homes, || {
                require_client(client)?
                    .data_homes(sizes)
                    .map(|v| crate::store::data_homes_from_payload(&v))
            })
        }

        Cmd::PurgeDataHome { name } => {
            let action = format!("purge data home '{name}'");
            // Dry-run first — its failure VETOES the purge entirely.
            let dry = with_busy(store, wake, &format!("{name}: purge dry-run"), || {
                require_client(client).and_then(|c| {
                    c.purge_data_home(&serde_json::json!({ "name": name, "dry_run": true }))
                })
            });
            // Body-over-transport (round-4 P2-3): a 200 with
            // `ok: false` is a REFUSED dry-run — it must veto exactly
            // like a transport failure (finish_write and TestRoute
            // already enforce this law; this gate was the outlier).
            let dry = dry.and_then(|v| {
                if v.get("ok").and_then(Value::as_bool) == Some(false) {
                    let why = v
                        .get("errors")
                        .map(|e| e.to_string())
                        .or_else(|| v.get("error").and_then(Value::as_str).map(str::to_string))
                        .unwrap_or_else(|| "dry-run answered ok:false".into());
                    Err(ApiError {
                        kind: ApiErrorKind::Protocol,
                        message: why,
                    })
                } else {
                    Ok(v)
                }
            });
            match dry {
                Err(e) => {
                    let s = *store;
                    let msg = format!("purge dry-run failed — nothing purged: {e}");
                    wake.post(move || s.notice.set(Some(msg.clone())));
                }
                Ok(_) => {
                    let (write, verify) =
                        with_busy(store, wake, &format!("purging {name}"), || {
                            let write = require_client(client).and_then(|c| {
                                c.purge_data_home(
                                    &serde_json::json!({ "name": name, "confirm_name": name }),
                                )
                            });
                            // Sizes ON here: a purge's whole point is the
                            // freed bytes, so the verify read must show them.
                            let verify = require_client(client).and_then(|c| c.data_homes(true));
                            (write, verify)
                        });
                    let verified = verify
                        .as_ref()
                        .ok()
                        .map(|_| Ok("data homes re-read after purge".to_string()));
                    finish_write(store, wake, action, write, verified, None, on_done);
                    if let Ok(v) = verify {
                        publish_ready(
                            wake,
                            store.data_homes,
                            crate::store::data_homes_from_payload(&v),
                        );
                    }
                }
            }
        }

        Cmd::LoadReservations => load(
            store,
            wake,
            "loading runtime reservations",
            store.reservations,
            || {
                require_client(client)?
                    .runtime_reservations()
                    .map(|v| crate::store::reservations_from_payload(&v))
            },
        ),

        Cmd::ReservationTransfer {
            runtime_id,
            tenant_id,
            target_user_id,
        } => {
            let action = format!("transfer runtime '{runtime_id}' → '{target_user_id}'");
            let (write, verify) = with_busy(store, wake, &action, || {
                let body = serde_json::json!({
                    "tenant_id": tenant_id,
                    "target_user_id": target_user_id,
                    "confirm_runtime_id": runtime_id,
                });
                let write =
                    require_client(client).and_then(|c| c.reservation_transfer(&runtime_id, &body));
                let verify = require_client(client).and_then(|c| c.runtime_reservations());
                (write, verify)
            });
            let rid = runtime_id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let rows = crate::store::reservations_from_payload(v);
                if rows.iter().any(|r| r.runtime_id == rid) {
                    Err(format!("GET still lists reservation '{rid}'"))
                } else {
                    Ok(format!("GET no longer lists reservation '{rid}'"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.reservations,
                    crate::store::reservations_from_payload(&v),
                );
            }
        }

        Cmd::ReservationPurge {
            runtime_id,
            tenant_id,
        } => {
            let action = format!("purge retained runtime '{runtime_id}'");
            let (write, verify) = with_busy(store, wake, &action, || {
                let body = serde_json::json!({
                    "tenant_id": tenant_id,
                    "confirm_runtime_id": runtime_id,
                    "delete_data": true,
                });
                let write =
                    require_client(client).and_then(|c| c.reservation_purge(&runtime_id, &body));
                let verify = require_client(client).and_then(|c| c.runtime_reservations());
                (write, verify)
            });
            let rid = runtime_id.clone();
            let verified = verify.as_ref().ok().map(|v| {
                let rows = crate::store::reservations_from_payload(v);
                if rows.iter().any(|r| r.runtime_id == rid) {
                    Err(format!("GET still lists reservation '{rid}'"))
                } else {
                    Ok(format!("GET no longer lists reservation '{rid}'"))
                }
            });
            finish_write(store, wake, action, write, verified, None, on_done);
            if let Ok(v) = verify {
                publish_ready(
                    wake,
                    store.reservations,
                    crate::store::reservations_from_payload(&v),
                );
            }
        }

        Cmd::EntityVerify { name } => {
            let result = with_busy(store, wake, &format!("{name}: verify chain"), || {
                require_client(client).and_then(|c| c.entity_verify(&name))
            });
            let s = *store;
            let note = match &result {
                Ok(v) => {
                    let ok = v.get("ok").and_then(Value::as_bool).unwrap_or(false);
                    if ok {
                        format!("✓ '{name}' verified — chain, spark and manifest agree")
                    } else {
                        format!(
                            "✗ '{name}' verification FAILED: {}",
                            v.get("errors")
                                .map(|e| e.to_string())
                                .or_else(|| v.get("error").map(|e| e.to_string()))
                                .unwrap_or_else(|| "no detail".into())
                        )
                    }
                }
                Err(e) => format!("✗ '{name}' verify call failed: {e}"),
            };
            wake.post(move || s.notice.set(Some(note.clone())));
        }
    }
}

/// The four manage reads, folded — one failed section degrades that
/// section (None), the primary substrate read carries the error.
fn load_entity_detail(
    client: &Option<GatewayClient>,
    name: &str,
) -> ApiResult<crate::store::EntityDetail> {
    let c = require_client(client)?;
    // Substrate is the anchor read: if IT fails, the snapshot fails
    // (auth/reachability class); the rest degrade independently.
    let substrate = c.entity_substrate(name)?;
    let voice = c.entity_voice(name).ok();
    let work = c.entity_work_order(name).ok();
    let cog = c.entity_cognition(name).ok();
    Ok(crate::store::EntityDetail::fold(
        name,
        Some(&substrate),
        voice.as_ref(),
        work.as_ref(),
        cog.as_ref(),
    ))
}

/// Post-write refreshes. DELIBERATELY outside any busy bracket: the
/// write's own busy label has already cleared and these follow-up GETs
/// are silent freshness reads — wrapping them would extend the busy
/// strip past the acknowledged write (round-2 P2-5: the old comment
/// claimed busy labels these reads never had).
fn refresh_entity_detail(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    name: &str,
) {
    let result = load_entity_detail(client, name);
    let s = *store;
    wake.post(move || {
        s.entity_detail.set(match result {
            Ok(d) => Loadable::Ready(d),
            Err(e) => Loadable::Failed(e),
        })
    });
}

fn refresh_entities(client: &Option<GatewayClient>, store: &Store, wake: &WakeHandle) {
    if let Ok(v) = require_client(client).and_then(|c| c.entities()) {
        let s = *store;
        wake.post(move || s.entities.set(Loadable::Ready(entities_from_payload(&v))));
    }
}

fn refresh_runs(client: &Option<GatewayClient>, store: &Store, wake: &WakeHandle) {
    // Cancel is gated to the Own scope (RunScope::actionable), so the
    // post-cancel freshness read is always the own-runs lane. The
    // post-write read re-asks page 0 UNFILTERED — the panel effect
    // re-requests with the live toolbar the moment it renders.
    if let Ok(v) = require_client(client).and_then(|c| c.runs(100, 0, "", "", true)) {
        let s = *store;
        wake.post(move || {
            let has_more = v.get("has_more").and_then(Value::as_bool).unwrap_or(false);
            s.runs.set(Loadable::Ready(crate::store::RunsData {
                scope: crate::store::RunScope::Own,
                rows: crate::store::runs_from_payload(&v),
                status: String::new(),
                query: String::new(),
                root_only: true,
                offset: 0,
                has_more,
            }))
        });
    }
}

/// First 8 chars of a run id — enough to correlate with the journal.
fn short_id(id: &str) -> String {
    id.chars().take(8).collect()
}

/// Last `n` chars of a string (round-4 P3-3b: says what the old double
/// chars().rev() dance meant).
fn suffix_chars(s: &str, n: usize) -> String {
    let count = s.chars().count();
    s.chars().skip(count.saturating_sub(n)).collect()
}

/// The human label for a gateway artifact reference. The live shape is
/// `{"$artifact": id, content_type, filename}` (F3: the old read tried
/// `artifact_id`, a key the gateway never ships, so every successful
/// voice test rendered the fallback). `artifact_id` stays as tolerance;
/// a bare string is accepted too.
fn artifact_ref_label(a: &Value) -> String {
    if let Some(s) = a.as_str() {
        return s.to_string();
    }
    ["$artifact", "artifact_id"]
        .iter()
        .find_map(|k| a.get(*k).and_then(Value::as_str))
        .unwrap_or("audio artifact")
        .to_string()
}

/// Swap the fresh `/models/loaded` rows into the held host-state
/// snapshot — the verify GET we already paid for, without re-running
/// the slow full-snapshot probe. Gauges/caches keep their last polled
/// values; the next poll refreshes them.
fn publish_model_rows(store: &Store, wake: &WakeHandle, v: &Value) {
    let rows = crate::store::model_rows_from_payload(v);
    let s = *store;
    wake.post(move || {
        s.host_state.update(|hs| {
            if let Loadable::Ready(d) = hs {
                d.models = rows;
                d.recount();
            }
        });
    });
}

/// Gap between host-state polls (the endpoint is a GPU probe + residency
/// listing — the contract says no faster than ~4s, tab-active only).
const HOST_STATE_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(4);

/// Publish a verify GET's parse onto a domain signal — the refresh we
/// already paid for (F13: was 12 verbatim wake.post tails).
fn publish_ready<T: Clone + Send + 'static>(
    wake: &WakeHandle,
    signal: abstracttui::reactive::Signal<Loadable<T>>,
    value: T,
) {
    wake.post(move || signal.set(Loadable::Ready(value)));
}

/// Shared read-path shape: Loading → Ready/Failed on one signal.
/// Start a download job, then poll it to completion, publishing the
/// gateway's own progress line as it goes.
///
/// WHY THIS BLOCKS THE WORKER LANE, DELIBERATELY. The POST returns a job
/// id immediately — the gateway runs the provider tool on its own
/// thread, so nothing about the download is happening in this process.
/// What blocks here is the POLL, and that is the honest trade: the
/// console has one command lane, the busy strip names the operation and
/// its elapsed time the whole way, and a background poller would need a
/// second definition of "in flight" for one screen's benefit. `r`
/// re-reads everything afterwards; the job also survives this console
/// entirely (it lives in the gateway), so quitting mid-download does not
/// cancel it.
fn handle_download(
    client: &mut Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    provider: &str,
    artifact: &str,
) {
    let op = next_op();
    let label = format!("downloading {provider} {artifact}");
    let s = *store;
    wake.post(move || s.begin_busy(op, &label));

    let started = (|| -> ApiResult<DownloadStatus> {
        let started_job = require_client(client)?.start_model_download(provider, artifact)?;
        let job_value = started_job.get("job").cloned().unwrap_or(Value::Null);
        let status = DownloadStatus::from_job(&job_value);
        if status.job.is_empty() {
            return Err(ApiError {
                kind: ApiErrorKind::Protocol,
                message: "POST /models/download started no job".to_string(),
            });
        }
        Ok(status)
    })();

    match started {
        Ok(status) => {
            let s = *store;
            let first = status.clone();
            wake.post(move || s.download.set(Some(first)));
            // Hand the lane straight back. The gateway is downloading; this
            // console only watches, and it watches between other commands.
            schedule_poll(
                tx,
                &PollTarget {
                    job: status.job.clone(),
                    provider: provider.to_string(),
                    artifact: artifact.to_string(),
                    op,
                    started_ms: now_ms(),
                },
                true,
            );
        }
        Err(e) => finish_download(store, wake, op, provider, artifact, Err(e)),
    }
}

/// Everything one download watch needs to identify itself across polls.
#[derive(Clone)]
struct PollTarget {
    job: String,
    provider: String,
    artifact: String,
    /// The busy-strip entry opened when the job started, closed by the
    /// final poll -- the operator sees one continuous operation.
    op: u64,
    started_ms: u64,
}

/// Re-arm one `PollDownload` after the poll interval.
///
/// The sleep happens on a throwaway timer thread that touches nothing but
/// the channel, so the worker lane stays free for whatever the operator
/// does next. A dead channel (console closing) just drops the timer.
fn schedule_poll(tx: &Sender<Cmd>, target: &PollTarget, immediate: bool) {
    let cmd = Cmd::PollDownload {
        job: target.job.clone(),
        provider: target.provider.clone(),
        artifact: target.artifact.clone(),
        op: target.op,
        started_ms: target.started_ms,
    };
    let tx = tx.clone();
    if immediate {
        let _ = tx.send(cmd);
        return;
    }
    std::thread::Builder::new()
        .name("download-poll-timer".into())
        .spawn(move || {
            std::thread::sleep(DOWNLOAD_POLL_INTERVAL);
            let _ = tx.send(cmd);
        })
        .ok();
}

fn handle_poll_download(
    client: &mut Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    tx: &Sender<Cmd>,
    target: &PollTarget,
) {
    let PollTarget {
        job,
        provider,
        artifact,
        op,
        started_ms,
    } = target;
    let (op, started_ms) = (*op, *started_ms);
    let polled = require_client(client).and_then(|c| c.model_download_job(job));
    let mut status = match polled {
        Ok(value) => DownloadStatus::from_job(&value.get("job").cloned().unwrap_or(Value::Null)),
        Err(e) => {
            finish_download(store, wake, op, provider, artifact, Err(e));
            return;
        }
    };

    if status.running() && now_ms().saturating_sub(started_ms) > DOWNLOAD_POLL_LIMIT_MS {
        // Give up WATCHING, not the download: the job keeps running on the
        // gateway and `w` again re-attaches to it (the POST joins a running
        // job rather than starting a second one).
        status.message = format!(
            "still running after {}m — the gateway keeps downloading; press w again to re-attach",
            DOWNLOAD_POLL_LIMIT_MS / 60_000
        );
        let s = *store;
        let snapshot = status.clone();
        wake.post(move || s.download.set(Some(snapshot)));
        finish_download(store, wake, op, provider, artifact, Ok(status));
        return;
    }

    let s = *store;
    let snapshot = status.clone();
    wake.post(move || s.download.set(Some(snapshot)));

    if status.running() {
        schedule_poll(tx, target, false);
    } else {
        finish_download(store, wake, op, provider, artifact, Ok(status));
    }
}

fn finish_download(
    store: &Store,
    wake: &WakeHandle,
    op: u64,
    provider: &str,
    artifact: &str,
    outcome: ApiResult<DownloadStatus>,
) {
    let action = format!("download {provider} {artifact}");
    let (notice, journal) = match &outcome {
        Ok(status) => (status.line(), Ok(status.status.clone())),
        Err(e) => (format!("{provider} {artifact}: {e}"), Err(e.to_string())),
    };
    let s = *store;
    wake.post(move || {
        s.end_busy(op);
        s.push_journal(JournalEntry {
            when: crate::store::now_hms(),
            action,
            outcome: journal,
            // The download's OWN terminal status is the verification —
            // the gateway re-probes availability on the next `r`, and a
            // second GET here would only restate the job we just polled.
            verified: None,
        });
        s.notice.set(Some(notice));
    });
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Gap between progress reads. Each read is one short GET on the shared
/// worker lane; the wait happens off it.
const DOWNLOAD_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_millis(1500);

/// How long this console will WATCH a download before handing it back
/// to the operator. The gateway keeps downloading either way.
const DOWNLOAD_POLL_LIMIT_MS: u64 = 3_600_000;

fn load<T: Clone + Send + 'static>(
    store: &Store,
    wake: &WakeHandle,
    label: &str,
    signal: abstracttui::reactive::Signal<Loadable<T>>,
    f: impl FnOnce() -> ApiResult<T>,
) {
    wake.post(move || signal.set(Loadable::Loading));
    let result = with_busy(store, wake, label, f);
    wake.post(move || {
        signal.set(match result {
            Ok(t) => Loadable::Ready(t),
            Err(e) => Loadable::Failed(e),
        })
    });
}

/// Journal the write + its verification; notice on failure; route the
/// outcome back to the issuing form when one is waiting.
/// The journal/notice text for a body-level (`ok:false`) write failure.
/// Prefers the plural `errors`; residency-style in-band refusals
/// (`{ok:false, error:"model_not_resident", detail:"…load with lock:true…"}`)
/// carry their actionable text under singular `error` + `detail` — journaling
/// a bare "ok:false" would drop exactly the part that tells the operator
/// what to do.
fn write_failure_text(v: &Value) -> String {
    if let Some(errs) = v.get("errors") {
        return errs.to_string();
    }
    let error = v.get("error").map(|e| match e.as_str() {
        Some(s) => s.to_string(),
        None => e.to_string(),
    });
    let detail = v
        .get("detail")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|d| !d.is_empty());
    match (error.filter(|e| !e.is_empty()), detail) {
        (Some(e), Some(d)) => format!("{e} — {d}"),
        (Some(e), None) => e,
        (None, Some(d)) => d,
        (None, None) => "ok:false".into(),
    }
}

fn finish_write(
    store: &Store,
    wake: &WakeHandle,
    action: String,
    write: ApiResult<Value>,
    verified: Option<Result<String, String>>,
    form_id: Option<u64>,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    let s = *store;
    let outcome = match &write {
        Ok(v) => {
            // 200 with ok:false is a body-level failure (authority
            // unreachable etc.) — transport success is never operation
            // success.
            let ok_flag = v.get("ok").and_then(Value::as_bool);
            match ok_flag {
                Some(false) => Err(format!(
                    "gateway reports failure: {}",
                    write_failure_text(v)
                )),
                _ => Ok("applied".to_string()),
            }
        }
        Err(e) => Err(e.to_string()),
    };
    // WRITES participate in the health authority: a transport-class
    // write failure has no domain slot to land on, so it bumps the
    // trigger channel instead (the UI-thread effect then verifies the
    // connection once — writes are never blind-retried).
    if matches!(
        &write,
        Err(ApiError {
            kind: ApiErrorKind::Unreachable,
            ..
        })
    ) {
        wake.post(move || s.net_fail_seq.update(|n| *n += 1));
    }
    let entry = JournalEntry {
        when: crate::store::now_hms(),
        action: action.clone(),
        outcome: outcome.clone(),
        verified,
    };
    if let Some(fid) = form_id {
        on_done(fid, outcome.clone());
    }
    wake.post(move || {
        let note = match &entry.outcome {
            Ok(_) => match &entry.verified {
                Some(Ok(v)) => format!("{} — verified: {}", entry.action, v),
                Some(Err(v)) => format!("{} — VERIFY FAILED: {}", entry.action, v),
                None => format!("{} — applied (verify unavailable)", entry.action),
            },
            Err(e) => format!("{} — FAILED: {}", entry.action, e),
        };
        s.push_journal(entry);
        s.notice.set(Some(note));
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// In-band write refusals journal their ACTIONABLE text: plural `errors`
    /// wins, else singular `error` + `detail` (the residency refusal shape),
    /// and only a payload with none of them degrades to "ok:false".
    #[test]
    fn write_failure_text_extracts_error_and_detail() {
        assert_eq!(
            write_failure_text(&json!({"ok": false, "errors": ["a", "b"]})),
            "[\"a\",\"b\"]"
        );
        assert_eq!(
            write_failure_text(&json!({
                "ok": false,
                "error": "model_not_resident",
                "detail": "Model lmstudio/qwen is not resident; load it first (load with lock:true) before locking."
            })),
            "model_not_resident — Model lmstudio/qwen is not resident; load it first (load with lock:true) before locking."
        );
        assert_eq!(
            write_failure_text(&json!({"ok": false, "error": "model_locked"})),
            "model_locked"
        );
        assert_eq!(
            write_failure_text(&json!({"ok": false, "detail": "only a detail"})),
            "only a detail"
        );
        // Non-string error values still stringify rather than vanish.
        assert_eq!(
            write_failure_text(&json!({"ok": false, "error": {"code": "busy"}})),
            "{\"code\":\"busy\"}"
        );
        assert_eq!(write_failure_text(&json!({"ok": false})), "ok:false");
    }

    /// The secrets-discipline pin: no Debug render of any command may
    /// contain a secret (test panics and future logging print commands
    /// verbatim — this is the invariant the manual impl exists for).
    #[test]
    fn debug_render_never_leaks_secrets() {
        let cases: Vec<(Cmd, &str)> = vec![
            (
                Cmd::Connect {
                    url: "http://127.0.0.1:8080".into(),
                    token: Secret("agw_SECRET_TOKEN_VALUE".into()),
                },
                "agw_SECRET_TOKEN_VALUE",
            ),
            (
                Cmd::SaveProfile {
                    create: true,
                    id: "acme".into(),
                    body: json!({"id": "acme", "api_key": "sk-SECRET-KEY"}).into(),
                    form_id: None,
                },
                "sk-SECRET-KEY",
            ),
            (
                Cmd::DiscoverModels {
                    body: json!({"provider_family": "openai", "api_key": "sk-DRAFT-SECRET"}).into(),
                },
                "sk-DRAFT-SECRET",
            ),
            (
                Cmd::PutRoute {
                    kind: "output".into(),
                    modality: "voice".into(),
                    task: None,
                    body: json!({"provider": "x", "model": "y",
                                 "options": {"api_key": "sk-NESTED-SECRET"}})
                    .into(),
                    key: "output.voice".into(),
                    form_id: None,
                },
                "sk-NESTED-SECRET",
            ),
        ];
        for (cmd, secret) in cases {
            let rendered = format!("{cmd:?}");
            assert!(
                !rendered.contains(secret),
                "secret leaked through Debug: {rendered}"
            );
            assert!(
                rendered.contains("«redacted»"),
                "redaction marker present: {rendered}"
            );
        }
    }

    /// F3: the live gateway ships `{"$artifact": id, ...}` — the label
    /// must read THAT key (with `artifact_id` + bare-string tolerance).
    #[test]
    fn artifact_ref_label_reads_the_live_shape() {
        assert_eq!(
            artifact_ref_label(&json!({"$artifact": "art-123", "content_type": "audio/wav"})),
            "art-123"
        );
        assert_eq!(
            artifact_ref_label(&json!({"artifact_id": "art-9"})),
            "art-9"
        );
        assert_eq!(artifact_ref_label(&json!("art-str")), "art-str");
        assert_eq!(artifact_ref_label(&json!({"other": 1})), "audio artifact");
    }
}
