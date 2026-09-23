//! `HttpTransport` against a local fake gateway (std TcpListener on a
//! free loopback port — never a real gateway, never :8080).
//!
//! Pins the seam the shared Models/Engines screens stand on: each
//! `ConsoleTransport` method hits the gateway route contract H names,
//! with the body the gateway expects, and each answer class maps to the
//! screens' error kinds (401 → Unauthorized, 403/409 → Refused WITH the
//! body so `delete_blockers` show, 404 → NotFound, 5xx → Failed,
//! timeout → Timeout, non-JSON → Protocol, no connection → Unavailable).

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use abstractcore_console::{ConsoleTransport, TransportErrorKind};
use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::transport_http::{client_slot, publish, HttpTransport};

/// One request the fake gateway saw.
#[derive(Clone, Debug)]
struct Seen {
    method: String,
    path: String,
    auth: Option<String>,
    body: Option<Value>,
}

struct FakeGateway {
    url: String,
    seen: Arc<Mutex<Vec<Seen>>>,
}

impl FakeGateway {
    fn start() -> FakeGateway {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind a free port");
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen: Arc<Mutex<Vec<Seen>>> = Arc::new(Mutex::new(Vec::new()));
        let seen_srv = seen.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                let seen = seen_srv.clone();
                std::thread::spawn(move || serve(stream, &seen));
            }
        });
        FakeGateway { url, seen }
    }

    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }

    fn last(&self, path_prefix: &str) -> Seen {
        self.seen()
            .into_iter()
            .rev()
            .find(|s| s.path.starts_with(path_prefix))
            .unwrap_or_else(|| panic!("no request to {path_prefix}: {:?}", self.seen()))
    }
}

fn serve(mut stream: TcpStream, seen: &Mutex<Vec<Seen>>) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        return;
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();
    let mut len = 0usize;
    let mut auth = None;
    loop {
        let mut h = String::new();
        if reader.read_line(&mut h).is_err() || h == "\r\n" || h.is_empty() {
            break;
        }
        let (k, v) = h.split_once(':').unwrap_or((&h, ""));
        let v = v.trim().to_string();
        match k.to_ascii_lowercase().as_str() {
            "content-length" => len = v.parse().unwrap_or(0),
            "authorization" => auth = Some(v),
            _ => {}
        }
    }
    let mut buf = vec![0u8; len];
    let _ = reader.read_exact(&mut buf);
    let body = serde_json::from_slice::<Value>(&buf).ok();
    seen.lock().unwrap().push(Seen {
        method: method.clone(),
        path: path.clone(),
        auth: auth.clone(),
        body,
    });

    let (status, text): (u16, String) = if auth.as_deref() == Some("Bearer bad") {
        (401, json!({"detail": "invalid token"}).to_string())
    } else {
        route(&method, &path)
    };
    if status == 0 {
        // The timeout case: hold the socket past the client's deadline.
        std::thread::sleep(Duration::from_millis(1500));
        return;
    }
    let reason = match status {
        200 => "OK",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        _ => "Error",
    };
    let resp = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
        text.len()
    );
    let _ = stream.write_all(resp.as_bytes());
}

fn job(id: &str, status: &str) -> String {
    json!({"schema": "host_job_v1", "job_id": id, "kind": "download",
           "status": status, "provider": "ollama", "artifact": "qwen3:8b"})
    .to_string()
}

/// The fake gateway's routes: the contract mirrors plus the refusal /
/// failure shapes the Python routes answer with.
fn route(method: &str, path: &str) -> (u16, String) {
    let p = path.strip_prefix("/api/gateway").unwrap_or(path);
    match (method, p) {
        ("GET", "/host/profile") => (
            200,
            json!({"schema": "host_profile_v1", "os": "darwin"}).to_string(),
        ),
        ("GET", "/host/state") => (
            200,
            json!({"ok": true, "host": {"host_id": "h1", "host_name": "studio", "kind": "local"}})
                .to_string(),
        ),
        ("GET", p) if p.starts_with("/engines?") => (
            200,
            json!({"schema": "engines_status_v1", "engines": []}).to_string(),
        ),
        ("GET", p) if p.starts_with("/models/catalog?") => (
            200,
            json!({"schema": "model_catalog_v1", "rows": []}).to_string(),
        ),
        ("GET", p) if p.starts_with("/models/installed") => (
            200,
            json!({"schema": "models_installed_v1", "rows": []}).to_string(),
        ),
        ("POST", "/models/download") => (200, job("dl_1", "running")),
        // FastAPI HTTPException(409, detail={...}) — the envelope the
        // transport unwraps so the blockers sit at the top level.
        ("POST", "/models/delete") => (
            409,
            json!({"detail": {"ok": false, "status": "refused",
                              "message": "gemma3:1b is loaded — unload it or force",
                              "delete_blockers": ["loaded"],
                              "error": {"message": "loaded", "type": "refused"}}})
            .to_string(),
        ),
        ("POST", "/engines/ollama/install") => (
            403,
            json!({"ok": false, "status": "refused", "reason": "not_allowed",
                   "message": "engine installs are disabled on this gateway",
                   "error": {"message": "not allowed", "type": "refused"}})
            .to_string(),
        ),
        ("POST", "/engines/mlx/install") => (
            409,
            json!({"ok": false, "reason": "busy", "message": "a job is already running"})
                .to_string(),
        ),
        ("GET", "/jobs/dl_1") => (200, job("dl_1", "completed")),
        ("POST", "/jobs/dl_1/cancel") => (200, job("dl_1", "cancelled")),
        ("GET", "/jobs/boom") => (500, json!({"detail": "internal error"}).to_string()),
        ("GET", "/jobs/notjson") => (200, "<html>not json</html>".to_string()),
        ("GET", "/jobs/slow") => (0, String::new()),
        _ => (404, json!({"detail": format!("unknown {p}")}).to_string()),
    }
}

fn transport(url: &str, token: &str) -> HttpTransport {
    let slot = client_slot();
    publish(
        &slot,
        Some(GatewayClient::new(url, Some(token)).with_read_timeout(Duration::from_millis(400))),
    );
    HttpTransport::new(slot, url)
}

#[test]
fn every_method_hits_its_gateway_route_with_the_contract_body() {
    let gw = FakeGateway::start();
    let t = transport(&gw.url, "good");

    assert_eq!(t.host_profile().unwrap()["schema"], "host_profile_v1");
    assert_eq!(gw.last("/api/gateway/host/profile").method, "GET");
    assert_eq!(
        gw.last("/api/gateway/host/profile").auth.as_deref(),
        Some("Bearer good")
    );

    t.engines_status(true).unwrap();
    assert_eq!(
        gw.last("/api/gateway/engines").path,
        "/api/gateway/engines?probe=1"
    );
    t.engines_status(false).unwrap();
    assert_eq!(
        gw.last("/api/gateway/engines").path,
        "/api/gateway/engines?probe=0"
    );

    t.models_catalog("qwen 3", Some("ollama"), true).unwrap();
    assert_eq!(
        gw.last("/api/gateway/models/catalog").path,
        "/api/gateway/models/catalog?q=qwen%203&engine=ollama&fits=1"
    );
    t.models_catalog("", None, false).unwrap();
    assert_eq!(
        gw.last("/api/gateway/models/catalog").path,
        "/api/gateway/models/catalog?q=&fits=0"
    );

    t.models_installed(Some("lmstudio")).unwrap();
    assert_eq!(
        gw.last("/api/gateway/models/installed").path,
        "/api/gateway/models/installed?provider=lmstudio"
    );
    t.models_installed(None).unwrap();
    assert_eq!(
        gw.last("/api/gateway/models/installed").path,
        "/api/gateway/models/installed"
    );

    let j = t.start_download("ollama", "qwen3:8b").unwrap();
    assert_eq!(j["job_id"], "dl_1");
    let s = gw.last("/api/gateway/models/download");
    assert_eq!(s.method, "POST");
    assert_eq!(
        s.body,
        Some(json!({"provider": "ollama", "artifact": "qwen3:8b", "dry_run": false}))
    );

    let _ = t.delete_model("ollama", "gemma3:1b", true);
    let s = gw.last("/api/gateway/models/delete");
    assert_eq!(s.method, "POST");
    assert_eq!(
        s.body,
        Some(json!({"provider": "ollama", "artifact": "gemma3:1b",
                    "dry_run": false, "force": true}))
    );

    let _ = t.engine_install("ollama", true);
    let s = gw.last("/api/gateway/engines/ollama/install");
    assert_eq!(s.method, "POST");
    assert_eq!(s.body, Some(json!({"dry_run": true})));

    assert_eq!(t.job("dl_1").unwrap()["status"], "completed");
    assert_eq!(gw.last("/api/gateway/jobs/dl_1").method, "GET");
    assert_eq!(t.cancel_job("dl_1").unwrap()["status"], "cancelled");
    assert_eq!(gw.last("/api/gateway/jobs/dl_1/cancel").method, "POST");

    // Nothing ever went anywhere but the contract routes (+ the one
    // /host/state read that names the host).
    for s in gw.seen() {
        assert!(s.path.starts_with("/api/gateway/"), "{s:?}");
    }
}

#[test]
fn status_codes_map_to_the_screens_error_classes() {
    let gw = FakeGateway::start();
    let t = transport(&gw.url, "good");

    // 409 + FastAPI detail envelope → Refused, body unwrapped: the
    // screens' reasons() finds the blockers.
    let e = t.delete_model("ollama", "gemma3:1b", false).unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Refused, "{e:?}");
    assert_eq!(e.code, Some(409));
    assert_eq!(e.message, "gemma3:1b is loaded — unload it or force");
    assert_eq!(e.reasons(), vec!["loaded"]);

    // 403 → Refused, the machine reason named.
    let e = t.engine_install("ollama", false).unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Refused);
    assert_eq!(e.code, Some(403));
    assert_eq!(
        e.message,
        "engine installs are disabled on this gateway (not_allowed)"
    );
    assert_eq!(e.body.as_ref().unwrap()["reason"], "not_allowed");

    // 409 busy → Refused.
    let e = t.engine_install("mlx", false).unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Refused);
    assert!(e.message.contains("already running"), "{}", e.message);

    // 404 → NotFound.
    let e = t.job("missing").unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::NotFound);
    assert_eq!(e.code, Some(404));

    // 5xx → Failed.
    let e = t.job("boom").unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Failed);
    assert_eq!(e.code, Some(500));
    assert_eq!(e.message, "internal error");

    // A 200 that is not JSON → Protocol.
    let e = t.job("notjson").unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Protocol);

    // No answer inside the read deadline → Timeout.
    let started = Instant::now();
    let e = t.job("slow").unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Timeout, "{e:?}");
    assert!(started.elapsed() < Duration::from_millis(1400));

    // 401 → Unauthorized.
    let bad = transport(&gw.url, "bad");
    let e = bad.host_profile().unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Unauthorized);
    assert_eq!(e.code, Some(401));
}

#[test]
fn no_connection_and_a_dead_port_are_unavailable() {
    // Empty slot: the console has not verified any gateway yet.
    let t = HttpTransport::new(client_slot(), "http://127.0.0.1:9");
    let e = t.engines_status(false).unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Unavailable);

    // A port nobody listens on (bound, then released).
    let port = {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let url = format!("http://127.0.0.1:{port}");
    let t = transport(&url, "good");
    let e = t.host_profile().unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::Unavailable, "{e:?}");
}

#[test]
fn the_host_label_names_the_gateway_host_once_host_state_answers() {
    let gw = FakeGateway::start();
    let t = transport(&gw.url, "good");
    let hp = gw.url.trim_start_matches("http://").to_string();
    // Before any read: the URL's host, flagged as this machine.
    assert_eq!(t.host_label(), format!("gateway host {hp} (this machine)"));
    t.host_profile().unwrap();
    // /host/state is read OFF the lane; the name lands shortly after.
    let deadline = Instant::now() + Duration::from_secs(3);
    while !t.host_label().contains("studio") && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(
        t.host_label(),
        format!("gateway host studio ({hp}, this machine)")
    );
    // ONE /host/state read per gateway, however often the profile loads.
    t.host_profile().unwrap();
    t.host_profile().unwrap();
    std::thread::sleep(Duration::from_millis(50));
    let n = gw
        .seen()
        .iter()
        .filter(|s| s.path == "/api/gateway/host/state")
        .count();
    assert_eq!(n, 1);
    // A reconnect to another gateway switches the transport with it.
    let other = FakeGateway::start();
    let slot = t.slot();
    publish(&slot, Some(GatewayClient::new(&other.url, Some("good"))));
    t.engines_status(false).unwrap();
    assert!(other
        .seen()
        .iter()
        .any(|s| s.path.starts_with("/api/gateway/engines")));
    // …and the label follows: the new gateway has not been named yet.
    assert!(!t.host_label().contains("studio"), "{}", t.host_label());
}
