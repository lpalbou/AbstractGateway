//! The gateway's answer to AbstractCore's shared Models / Engines
//! screens: [`HttpTransport`] implements `abstractcore_console`'s
//! [`ConsoleTransport`] over this crate's [`GatewayClient`].
//!
//! The screens (crate `abstractcore-console`) are implemented ONCE in
//! AbstractCore and mounted here as screens 9 ("Models", page id
//! `catalog`) and 0 ("Engines", page id `engines`). They never know
//! where their answers come from; this module maps each trait method
//! to the gateway's mirror of the AbstractCore route, whose JSON is the
//! same contract document core serves under `/acore`:
//!
//! | trait method | gateway route (under `/api/gateway`) | contract |
//! |---|---|---|
//! | `host_profile` | `GET /host/profile` | A `host_profile_v1` |
//! | `engines_status(probe)` | `GET /engines?probe=0\|1` | B `engines_status_v1` |
//! | `models_catalog(q, engine, fits)` | `GET /models/catalog?q=&engine=&fits=0\|1` | C `model_catalog_v1` |
//! | `models_installed(provider)` | `GET /models/installed[?provider=]` | D `models_installed_v1` |
//! | `start_download(p, a)` | `POST /models/download {provider, artifact, dry_run:false}` | E `host_job_v1` |
//! | `delete_model(p, a, force)` | `POST /models/delete {provider, artifact, dry_run:false, force}` | E |
//! | `engine_install(id, dry_run)` | `POST /engines/{id}/install {dry_run}` | E |
//! | `job(id)` | `GET /jobs/{id}` | E |
//! | `cancel_job(id)` | `POST /jobs/{id}/cancel {}` | E |
//! | `host_label()` | `/host/state` → `host.host_name`, else the URL's host | — |
//!
//! Errors keep the gateway's honest classes:
//!
//! | gateway answer | `TransportErrorKind` |
//! |---|---|
//! | no connection yet | `Unavailable` ("not connected") |
//! | network failure | `Unavailable` |
//! | connect/read timeout | `Timeout` |
//! | 401 | `Unauthorized` |
//! | 403, 409 | `Refused`, with the JSON body (its `delete_blockers` show) |
//! | 404 | `NotFound` |
//! | any other status | `Failed` |
//! | a 2xx that is not JSON | `Protocol` |
//!
//! # The client slot
//!
//! The gateway worker owns the connection: it probes, and on success it
//! PUBLISHES the verified client into a shared [`ClientSlot`] before it
//! reports "connected" — so the screens' own worker thread never talks to
//! a gateway the console has not verified, and a reconnect to another
//! gateway switches both lanes at once.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, RwLock};

use abstractcore_console::transport::{ConsoleTransport, TransportError, TransportErrorKind};
use serde_json::Value;

use crate::api::{ApiError, ApiErrorKind, GatewayClient};

/// The verified gateway connection, shared between the gateway worker
/// (the only writer) and the screens' transport (a reader).
pub type ClientSlot = Arc<RwLock<Option<GatewayClient>>>;

/// A fresh, empty slot.
pub fn client_slot() -> ClientSlot {
    Arc::new(RwLock::new(None))
}

/// Replace what the slot holds (poison-tolerant: a panicked reader must
/// not wedge the connection lane).
pub fn publish(slot: &ClientSlot, client: Option<GatewayClient>) {
    match slot.write() {
        Ok(mut g) => *g = client,
        Err(p) => *p.into_inner() = client,
    }
}

/// [`ConsoleTransport`] over the gateway's HTTP API.
pub struct HttpTransport {
    slot: ClientSlot,
    /// The URL the console was started against — the label's fallback
    /// before any connection exists.
    initial_url: String,
    /// Host names learned from `/host/state`, per gateway base URL
    /// (`None` = asked, the gateway did not say).
    names: Arc<Mutex<HashMap<String, Option<String>>>>,
}

impl HttpTransport {
    pub fn new(slot: ClientSlot, initial_url: impl Into<String>) -> HttpTransport {
        HttpTransport {
            slot,
            initial_url: initial_url.into(),
            names: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// The slot this transport reads (the worker publishes into it).
    pub fn slot(&self) -> ClientSlot {
        self.slot.clone()
    }

    fn client(&self) -> Result<GatewayClient, TransportError> {
        let current = match self.slot.read() {
            Ok(g) => g.clone(),
            Err(p) => p.into_inner().clone(),
        };
        current.ok_or_else(|| {
            TransportError::unavailable(
                "not connected to a gateway — probe on the Connection screen (1) first",
            )
        })
    }

    fn base_url(&self) -> String {
        match self.slot.read() {
            Ok(g) => g.as_ref().map(|c| c.base_url().to_string()),
            Err(p) => p.into_inner().as_ref().map(|c| c.base_url().to_string()),
        }
        .unwrap_or_else(|| self.initial_url.clone())
    }

    /// The gateway host's name, once learned for `base`.
    fn learned_name(&self, base: &str) -> Option<String> {
        self.names
            .lock()
            .ok()
            .and_then(|m| m.get(base).cloned().flatten())
    }

    /// Learn the gateway host's name from `/host/state` once per base
    /// URL, OFF the screens' lane (that read is slow by contract: it
    /// probes memory and GPUs). Whatever it finds labels the NEXT
    /// confirm prompt; until then the URL's host names the machine.
    fn learn_name_once(&self, client: &GatewayClient) {
        let base = client.base_url().to_string();
        {
            let Ok(mut m) = self.names.lock() else {
                return;
            };
            if m.contains_key(&base) {
                return;
            }
            m.insert(base.clone(), None);
        }
        let names = self.names.clone();
        let client = client.clone();
        let _ = std::thread::Builder::new()
            .name("gateway-host-name".into())
            .spawn(move || {
                let name = client.host_state().ok().and_then(|v| host_name_of(&v));
                if let (Some(n), Ok(mut m)) = (name, names.lock()) {
                    m.insert(base, Some(n));
                }
            });
    }
}

/// `/host/state` → the host's name: `host.host_name` (AbstractCore's
/// memory snapshot), tolerating `hostname` / `name` spellings.
pub fn host_name_of(state: &Value) -> Option<String> {
    let host = state.get("host")?;
    ["host_name", "hostname", "name"]
        .iter()
        .find_map(|k| host.get(*k).and_then(Value::as_str))
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// `http://10.0.0.5:8080/x` → `10.0.0.5:8080` (scheme and path dropped).
pub fn host_port_of(url: &str) -> String {
    let rest = url.split("://").nth(1).unwrap_or(url);
    let hp = rest.split(['/', '?', '#']).next().unwrap_or(rest);
    // user:pass@host — never show credentials.
    hp.rsplit('@').next().unwrap_or(hp).to_string()
}

/// True when the URL points at this machine.
pub fn is_loopback(host_port: &str) -> bool {
    let host = if let Some(rest) = host_port.strip_prefix('[') {
        rest.split(']').next().unwrap_or(rest)
    } else {
        host_port.rsplit_once(':').map_or(host_port, |(h, _)| h)
    };
    matches!(host, "127.0.0.1" | "localhost" | "::1") || host.starts_with("127.")
}

/// The words the install/delete confirms print after "runs on".
pub fn label_for(base_url: &str, name: Option<&str>) -> String {
    let hp = host_port_of(base_url);
    let local = if is_loopback(&hp) {
        ", this machine"
    } else {
        ""
    };
    match name {
        Some(n) => format!("gateway host {n} ({hp}{local})"),
        None => format!(
            "gateway host {hp}{}",
            if local.is_empty() {
                ""
            } else {
                " (this machine)"
            }
        ),
    }
}

/// The one human line for a refusal/failure body: its `message`, else
/// `error.message` / `error`, else `detail`, else the client's own text
/// — with the machine `reason` appended when the body names one.
fn body_message(body: Option<&Value>, fallback: &str) -> String {
    let Some(b) = body else {
        return fallback.to_string();
    };
    let text = b
        .get("message")
        .and_then(Value::as_str)
        .or_else(|| {
            b.get("error")
                .and_then(|e| e.get("message"))
                .and_then(Value::as_str)
        })
        .or_else(|| b.get("error").and_then(Value::as_str))
        .or_else(|| b.get("detail").and_then(Value::as_str))
        .filter(|s| !s.trim().is_empty())
        .unwrap_or(fallback)
        .to_string();
    match b.get("reason").and_then(Value::as_str) {
        Some(r) if !r.is_empty() && !text.contains(r) => format!("{text} ({r})"),
        _ => text,
    }
}

/// Map one gateway error into the screens' classes (module table).
pub fn map_error(e: ApiError) -> TransportError {
    let code = e.status().map(i32::from);
    let msg = body_message(e.body.as_ref(), &e.message);
    let kind = match e.kind {
        ApiErrorKind::NotConnected => TransportErrorKind::Unavailable,
        ApiErrorKind::Unreachable if e.timed_out => TransportErrorKind::Timeout,
        ApiErrorKind::Unreachable => TransportErrorKind::Unavailable,
        ApiErrorKind::Unauthorized => TransportErrorKind::Unauthorized,
        ApiErrorKind::Forbidden | ApiErrorKind::Http(409) => TransportErrorKind::Refused,
        ApiErrorKind::Http(404) => TransportErrorKind::NotFound,
        ApiErrorKind::Http(_) => TransportErrorKind::Failed,
        ApiErrorKind::Protocol => TransportErrorKind::Protocol,
    };
    let mut out = TransportError::new(kind, msg);
    if let Some(c) = code {
        out = out.with_code(c);
    }
    if let Some(b) = e.body {
        out = out.with_body(b);
    }
    out
}

impl ConsoleTransport for HttpTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        let c = self.client()?;
        let out = c.host_profile().map_err(map_error);
        if out.is_ok() {
            self.learn_name_once(&c);
        }
        out
    }

    fn engines_status(&self, probe: bool) -> Result<Value, TransportError> {
        self.client()?.engines_status(probe).map_err(map_error)
    }

    fn models_catalog(
        &self,
        q: &str,
        engine: Option<&str>,
        fits_only: bool,
    ) -> Result<Value, TransportError> {
        self.client()?
            .models_catalog(q, engine, fits_only)
            .map_err(map_error)
    }

    fn models_installed(&self, provider: Option<&str>) -> Result<Value, TransportError> {
        self.client()?.models_installed(provider).map_err(map_error)
    }

    fn start_download(&self, provider: &str, artifact: &str) -> Result<Value, TransportError> {
        self.client()?
            .models_download(provider, artifact, false)
            .map_err(map_error)
    }

    fn delete_model(
        &self,
        provider: &str,
        artifact: &str,
        force: bool,
    ) -> Result<Value, TransportError> {
        self.client()?
            .models_delete(provider, artifact, false, force)
            .map_err(map_error)
    }

    fn engine_install(&self, id: &str, dry_run: bool) -> Result<Value, TransportError> {
        self.client()?
            .engine_install(id, dry_run)
            .map_err(map_error)
    }

    fn job(&self, id: &str) -> Result<Value, TransportError> {
        self.client()?.host_job(id).map_err(map_error)
    }

    fn cancel_job(&self, id: &str) -> Result<Value, TransportError> {
        self.client()?.cancel_host_job(id).map_err(map_error)
    }

    /// Never blocks (the UI thread asks): the URL's host, plus the host
    /// name once `/host/state` has named it.
    fn host_label(&self) -> String {
        let base = self.base_url();
        label_for(&base, self.learned_name(&base).as_deref())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn labels_name_the_gateway_host_and_flag_this_machine() {
        assert_eq!(
            label_for("http://127.0.0.1:8080", None),
            "gateway host 127.0.0.1:8080 (this machine)"
        );
        assert_eq!(
            label_for("https://gw.lan:9443/", Some("studio")),
            "gateway host studio (gw.lan:9443)"
        );
        assert_eq!(
            label_for("http://localhost:8080", Some("mbp")),
            "gateway host mbp (localhost:8080, this machine)"
        );
        assert_eq!(host_port_of("http://u:p@10.0.0.5:8080/x"), "10.0.0.5:8080");
        assert!(is_loopback("[::1]:8080"));
        assert!(!is_loopback("10.0.0.5:8080"));
    }

    #[test]
    fn host_name_reads_the_memory_snapshot_host_block() {
        let v = json!({"host": {"host_id": "h1", "host_name": "studio", "kind": "local"}});
        assert_eq!(host_name_of(&v).as_deref(), Some("studio"));
        assert_eq!(host_name_of(&json!({"ok": true})), None);
        assert_eq!(host_name_of(&json!({"host": {"host_name": "  "}})), None);
    }

    #[test]
    fn error_classes_map_and_refusals_keep_their_body() {
        let mut e = ApiError::new(ApiErrorKind::Http(409), "{\"x\":1}");
        e.body = Some(json!({"ok": false, "message": "model is loaded",
                             "delete_blockers": ["loaded"]}));
        let t = map_error(e);
        assert_eq!(t.kind, TransportErrorKind::Refused);
        assert_eq!(t.code, Some(409));
        assert_eq!(t.message, "model is loaded");
        assert_eq!(t.reasons(), vec!["loaded"]);

        let mut e = ApiError::new(ApiErrorKind::Forbidden, "x");
        e.body = Some(json!({"message": "engine installs are disabled", "reason": "not_allowed"}));
        let t = map_error(e);
        assert!(t.is_refused());
        assert_eq!(t.message, "engine installs are disabled (not_allowed)");

        let mut e = ApiError::new(ApiErrorKind::Unreachable, "timed out reading");
        e.timed_out = true;
        assert_eq!(map_error(e).kind, TransportErrorKind::Timeout);
        for (k, want) in [
            (ApiErrorKind::NotConnected, TransportErrorKind::Unavailable),
            (ApiErrorKind::Unreachable, TransportErrorKind::Unavailable),
            (ApiErrorKind::Unauthorized, TransportErrorKind::Unauthorized),
            (ApiErrorKind::Http(404), TransportErrorKind::NotFound),
            (ApiErrorKind::Http(500), TransportErrorKind::Failed),
            (ApiErrorKind::Http(400), TransportErrorKind::Failed),
            (ApiErrorKind::Protocol, TransportErrorKind::Protocol),
        ] {
            assert_eq!(map_error(ApiError::new(k.clone(), "m")).kind, want, "{k:?}");
        }
    }

    #[test]
    fn an_empty_slot_is_unavailable_not_a_crash() {
        let t = HttpTransport::new(client_slot(), "http://127.0.0.1:1");
        let e = t.host_profile().unwrap_err();
        assert_eq!(e.kind, TransportErrorKind::Unavailable);
        assert!(e.message.contains("Connection screen"), "{}", e.message);
        assert_eq!(t.host_label(), "gateway host 127.0.0.1:1 (this machine)");
    }
}
