//! Live end-to-end against a REAL gateway — ignored by default.
//!
//! Run explicitly:
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:8080 \
//!   ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_e2e -- --ignored --nocapture --test-threads 1
//!
//! Exercises the exact client + verify-after-write path the app's worker
//! runs: profile create → discover models → capability route PUT →
//! verify via GET → clear → verify → profile delete → verify; user
//! create (token once) → rotate → delete → verify. Every write this
//! test makes it also cleans up — the gateway ends as it started.

use serde_json::json;

use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::store::{users_from_payload, ProfilesData, RoutesData};

fn live_client() -> Option<GatewayClient> {
    let url =
        std::env::var("ABSTRACTGATEWAY_URL").unwrap_or_else(|_| "http://127.0.0.1:8080".into());
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").ok()?;
    let client = GatewayClient::new(&url, Some(&token));
    client.ping().ok()?;
    Some(client)
}

const PROFILE_ID: &str = "console-e2e-test";
const USER_ID: &str = "console-e2e-user";
/// A deliberately low-blast-radius route: not configured on a stock
/// gateway, nothing routes through it while unconfigured.
const ROUTE: (&str, &str) = ("input", "music");

/// RAII cleanup: Drop runs on unwind too, so ANY failed assert between
/// a write and its restore still leaves the gateway as it started (a
/// polluted baseline would poison the next run's "before" snapshot).
struct Cleanup {
    c: GatewayClient,
    profile: bool,
    user: bool,
    route: bool,
    /// The pre-run configured body when the route was NOT unconfigured —
    /// restore means put-it-back, not clear (a clear would itself be
    /// pollution on a gateway that had the route configured).
    route_restore: Option<serde_json::Value>,
}

impl Drop for Cleanup {
    fn drop(&mut self) {
        if self.route {
            let outcome = match &self.route_restore {
                Some(body) => self
                    .c
                    .put_route(ROUTE.0, ROUTE.1, None, body)
                    .map(|_| "restored"),
                None => self
                    .c
                    .clear_route(ROUTE.0, ROUTE.1, None)
                    .map(|_| "cleared"),
            };
            match outcome {
                Ok(what) => eprintln!("[cleanup] route {}.{} {what}", ROUTE.0, ROUTE.1),
                Err(e) => eprintln!("[cleanup] restoring route failed: {e}"),
            }
        }
        if self.user {
            if let Err(e) = self.c.delete_user(USER_ID, "default") {
                eprintln!("[cleanup] deleting user failed: {e}");
            } else {
                eprintln!("[cleanup] user {USER_ID} deleted");
            }
        }
        if self.profile {
            if let Err(e) = self.c.delete_profile(PROFILE_ID) {
                eprintln!("[cleanup] deleting profile failed: {e}");
            } else {
                eprintln!("[cleanup] profile {PROFILE_ID} deleted");
            }
        }
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn wizard_writes_verify_and_clean_up() {
    let Some(c) = live_client() else {
        panic!("no live gateway (set ABSTRACTGATEWAY_URL / ABSTRACTGATEWAY_AUTH_TOKEN)");
    };
    let mut cleanup = Cleanup {
        c: c.clone(),
        profile: false,
        user: false,
        route: false,
        route_restore: None,
    };

    // ---- identity ------------------------------------------------------
    let me = c.me().expect("/me");
    assert_eq!(me["principal"]["admin"], true, "admin token required: {me}");
    println!(
        "connected as {}@{} (auth {})",
        me["principal"]["user_id"], me["principal"]["tenant_id"], me["auth"]["mode"]
    );

    // ---- provider endpoint profile: create → verify ---------------------
    // (idempotent: delete any leftover from a crashed prior run first)
    let _ = c.delete_profile(PROFILE_ID);
    let created = c
        .create_profile(&json!({
            "id": PROFILE_ID,
            "display_name": "console e2e",
            "description": "temporary profile created by abstractgateway-console live e2e",
            "provider_family": "openai-compatible",
            "base_url": "http://127.0.0.1:1234/v1",
            "scope": "user",
            "enabled": true,
        }))
        .expect("create profile");
    cleanup.profile = true;
    assert_eq!(created["ok"], true, "create ok: {created}");
    let profiles = ProfilesData::from_value(&c.profiles().expect("GET profiles"));
    let mine = profiles
        .profiles
        .iter()
        .find(|p| p.id == PROFILE_ID)
        .expect("GET lists the created profile");
    assert_eq!(mine.family, "openai-compatible");
    assert!(!mine.api_key_set, "no key was sent");
    println!("profile '{PROFILE_ID}' created and verified via GET");

    // ---- discover models on the saved profile (test-connection verb) ----
    let discover = c
        .discover_models(&json!({ "profile_id": PROFILE_ID }))
        .expect("discover-models");
    println!(
        "discover-models: ok={} available={} models={} error={:?}",
        discover["ok"],
        discover["available"],
        discover["models"].as_array().map(|a| a.len()).unwrap_or(0),
        discover.get("error")
    );

    // ---- update: attach an API key, then clear it -----------------------
    let updated = c
        .update_profile(PROFILE_ID, &json!({ "api_key": "sk-e2e-temp-key" }))
        .expect("update profile (set key)");
    assert_eq!(updated["ok"], true, "update ok: {updated}");
    let profiles = ProfilesData::from_value(&c.profiles().expect("GET profiles"));
    let mine = profiles
        .profiles
        .iter()
        .find(|p| p.id == PROFILE_ID)
        .unwrap();
    assert!(mine.api_key_set, "key stored after update");
    assert!(
        mine.api_key_fingerprint.is_some(),
        "public row carries a fingerprint, never the key"
    );
    let cleared = c
        .update_profile(PROFILE_ID, &json!({ "clear_api_key": true }))
        .expect("update profile (clear key)");
    assert_eq!(cleared["ok"], true);
    let profiles = ProfilesData::from_value(&c.profiles().expect("GET profiles"));
    let mine = profiles
        .profiles
        .iter()
        .find(|p| p.id == PROFILE_ID)
        .unwrap();
    assert!(!mine.api_key_set, "key cleared and verified via GET");
    println!("profile key set + cleared, verified via GET each time");

    // ---- capability route: snapshot → PUT → verify → restore ------------
    let before = RoutesData::from_value(&c.capability_defaults().expect("GET routes"));
    assert!(before.ok && before.writable, "routes writable");
    let row_before = before
        .rows
        .iter()
        .find(|r| r.key == format!("{}.{}", ROUTE.0, ROUTE.1))
        .expect("route row exists")
        .clone();
    println!(
        "route {} before: configured={} provider={:?}",
        row_before.key, row_before.configured, row_before.provider
    );

    // Arm the guard for BOTH baselines: unconfigured-before restores by
    // clearing; configured-before restores by putting the original back.
    cleanup.route = true;
    cleanup.route_restore = if row_before.configured && row_before.covered_by.is_none() {
        let mut body = json!({});
        if let Some(p) = &row_before.provider {
            body["provider"] = json!(p);
        }
        if let Some(m) = &row_before.model {
            body["model"] = json!(m);
        }
        if let Some(o) = &row_before.options {
            body["options"] = o.clone();
        }
        Some(body)
    } else {
        None
    };
    let put = c
        .put_route(
            ROUTE.0,
            ROUTE.1,
            None,
            &json!({ "provider": "lmstudio", "model": "console-e2e-model" }),
        )
        .expect("PUT route");
    assert_eq!(put["ok"], true, "PUT ok: {put}");
    let after = RoutesData::from_value(&c.capability_defaults().expect("GET routes"));
    let row_after = after
        .rows
        .iter()
        .find(|r| r.key == row_before.key)
        .expect("route row still listed");
    assert!(row_after.configured, "configured after PUT");
    assert_eq!(row_after.provider.as_deref(), Some("lmstudio"));
    assert_eq!(row_after.model.as_deref(), Some("console-e2e-model"));
    println!("route {} PUT + verified via GET", row_before.key);

    // Restore: the route was unconfigured on a stock gateway → DELETE;
    // if it had been configured, put the original pair back.
    if row_before.configured && row_before.covered_by.is_none() {
        let mut body = json!({});
        if let Some(p) = &row_before.provider {
            body["provider"] = json!(p);
        }
        if let Some(m) = &row_before.model {
            body["model"] = json!(m);
        }
        if let Some(o) = &row_before.options {
            body["options"] = o.clone();
        }
        c.put_route(ROUTE.0, ROUTE.1, None, &body)
            .expect("restore original route");
    } else {
        let del = c.clear_route(ROUTE.0, ROUTE.1, None).expect("DELETE route");
        assert_eq!(del["ok"], true, "clear ok: {del}");
    }
    let restored = RoutesData::from_value(&c.capability_defaults().expect("GET routes"));
    let row_restored = restored
        .rows
        .iter()
        .find(|r| r.key == row_before.key)
        .unwrap();
    assert_eq!(
        row_restored.configured, row_before.configured,
        "route restored to its original configured state"
    );
    cleanup.route = false; // restored in-band
    println!("route {} restored + verified via GET", row_before.key);

    // ---- user: create (token once) → rotate → delete → verify -----------
    let _ = c.delete_user(USER_ID, "default");
    let created = c
        .create_user(&json!({
            "user_id": USER_ID,
            "roles": ["user"],
            "enabled": true,
        }))
        .expect("create user");
    cleanup.user = true;
    let token1 = created["token"]
        .as_str()
        .expect("token shown once")
        .to_string();
    assert!(!token1.is_empty());
    let users = users_from_payload(&c.users().expect("GET users"));
    assert!(
        users.humans.iter().any(|u| u.user_id == USER_ID),
        "GET lists the created user"
    );
    let rotated = c
        .patch_user(USER_ID, "default", &json!({ "rotate_token": true }))
        .expect("rotate token");
    let token2 = rotated["token"]
        .as_str()
        .expect("new token once")
        .to_string();
    assert_ne!(token1, token2, "rotation mints a new token");
    let deleted = c.delete_user(USER_ID, "default").expect("delete user");
    cleanup.user = false; // deleted in-band
    println!("user delete response: {deleted}");
    let users = users_from_payload(&c.users().expect("GET users"));
    assert!(
        !users.humans.iter().any(|u| u.user_id == USER_ID),
        "GET no longer lists the deleted user"
    );
    println!("user '{USER_ID}' created (token once), rotated, deleted, verified via GET");

    // ---- profile cleanup -------------------------------------------------
    let del = c.delete_profile(PROFILE_ID).expect("delete profile");
    cleanup.profile = false; // deleted in-band
    assert_eq!(del["ok"], true, "delete ok: {del}");
    let profiles = ProfilesData::from_value(&c.profiles().expect("GET profiles"));
    assert!(
        !profiles.profiles.iter().any(|p| p.id == PROFILE_ID),
        "GET no longer lists the deleted profile"
    );
    println!("profile '{PROFILE_ID}' deleted + verified via GET — gateway restored");

    // ---- sandbox: a real generation through the configured default ------
    let routes = RoutesData::from_value(&c.capability_defaults().expect("GET routes"));
    if let Some(text_route) = routes
        .rows
        .iter()
        .find(|r| r.key == "input.text" && r.configured)
    {
        let (p, m) = (
            text_route.provider.clone().unwrap_or_default(),
            text_route.model.clone().unwrap_or_default(),
        );
        match c.sandbox_generate("output.text", &p, &m, "Reply with exactly: CONSOLE-OK", 64) {
            Ok(v) => println!(
                "sandbox {} / {}: ok={} routed={} response={:?}",
                p,
                m,
                v["ok"],
                v["routed_provider"],
                v["response"]
                    .as_str()
                    .map(|s| s.chars().take(60).collect::<String>())
            ),
            Err(e) => println!("sandbox {p} / {m}: provider error (honest): {e}"),
        }
    } else {
        println!("input.text not configured — sandbox test skipped");
    }
}

/// The Models/Engines seam against a REAL gateway, read-only (no
/// download, no delete, no install — only a DRY-RUN install plan).
///
/// Opt-in twice: `--ignored` AND an explicit URL — this case never
/// falls back to `http://127.0.0.1:8080`, which is usually the
/// operator's own gateway:
///   ABSTRACTGATEWAY_MODELS_E2E_URL=http://127.0.0.1:<port> \
///   ABSTRACTGATEWAY_AUTH_TOKEN=... \
///   cargo test --test live_e2e models_engines -- --ignored --nocapture
/// Without the URL it skips (passes) with a note.
#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn models_engines_routes_answer_the_contract_documents() {
    use abstractcore_console::{ConsoleTransport, TransportErrorKind};
    use abstractgateway_console::transport_http::{client_slot, publish, HttpTransport};

    let Ok(url) = std::env::var("ABSTRACTGATEWAY_MODELS_E2E_URL") else {
        eprintln!("skipped: set ABSTRACTGATEWAY_MODELS_E2E_URL (never defaults to :8080)");
        return;
    };
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").ok();
    let client = GatewayClient::new(&url, token.as_deref());
    client.ping().expect("the gateway answers /ping");
    let slot = client_slot();
    publish(&slot, Some(client));
    let t = HttpTransport::new(slot, url.clone());

    let hp = t.host_profile().expect("GET /host/profile");
    assert_eq!(hp["schema"], "host_profile_v1", "{hp}");
    let en = t.engines_status(false).expect("GET /engines");
    assert_eq!(en["schema"], "engines_status_v1", "{en}");
    assert!(
        en["engines"].as_array().is_some_and(|a| !a.is_empty()),
        "{en}"
    );
    let cat = t
        .models_catalog("qwen", None, false)
        .expect("GET /models/catalog");
    assert_eq!(cat["schema"], "model_catalog_v1");
    let inst = t.models_installed(None).expect("GET /models/installed");
    assert_eq!(inst["schema"], "models_installed_v1");
    println!(
        "host {} · {} engines · {} catalog rows · {} installed",
        hp["accelerator"],
        en["engines"].as_array().map_or(0, Vec::len),
        cat["rows"].as_array().map_or(0, Vec::len),
        inst["rows"].as_array().map_or(0, Vec::len),
    );

    // A dry-run install plan for the first engine that offers one: the
    // gateway answers a job with `dry_run: true` and the argv — or
    // refuses (403 when installs are disabled). Either way nothing runs.
    if let Some(id) = en["engines"].as_array().and_then(|a| {
        a.iter()
            .find(|e| e["install"]["available"] == true && e["installed"] == false)
            .and_then(|e| e["id"].as_str())
    }) {
        match t.engine_install(id, true) {
            Ok(job) => {
                assert_eq!(job["dry_run"], true, "{job}");
                println!("dry run {id}: {}", job["command"]);
            }
            Err(e) => {
                assert_eq!(e.kind, TransportErrorKind::Refused, "{e:?}");
                println!("install refused (as configured): {e}");
            }
        }
    }

    // An unknown job is "not found", never a crash.
    let e = t.job("no-such-job-e2e").unwrap_err();
    assert_eq!(e.kind, TransportErrorKind::NotFound, "{e:?}");
    println!("label: {}", t.host_label());
}
