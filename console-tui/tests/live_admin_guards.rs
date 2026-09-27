//! Live proof for the admin guards and the web-exact payloads (review 1,
//! M4 + M5) against a REAL, HERMETIC gateway — ignored by default (the
//! live_e2e.rs convention). Needs an admin token and a NON-admin user's
//! token on the same gateway:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18873 \
//!   ABSTRACTGATEWAY_AUTH_TOKEN=<admin> ABSTRACTGATEWAY_USER_TOKEN=<non-admin> \
//!   cargo test --test live_admin_guards -- --ignored --nocapture --test-threads 1
//!
//! The first test is the ground truth behind the console's gates: every
//! verb the console refuses for a non-admin earns a 403 from the gateway
//! for that principal (so a refusal is never a false "admin-only"), while
//! the verbs it leaves open are answered. The second test drives the M5
//! payloads through the real routes and reads the state back. It restores
//! what it changes.

use abstractgateway_console::api::sandbox_docs::route_test_body;
use abstractgateway_console::api::{ApiErrorKind, ApiResult, GatewayClient};
use abstractgateway_console::store::{RoutesData, RuntimeConfigData};
use abstractgateway_console::ui::routes::route_save_body;
use abstractgateway_console::ui::runtimes::WorkspaceDefaults;
use serde_json::{json, Value};

fn client(var: &str) -> GatewayClient {
    let url =
        std::env::var("ABSTRACTGATEWAY_URL").unwrap_or_else(|_| "http://127.0.0.1:18873".into());
    let token = std::env::var(var).unwrap_or_else(|_| panic!("{var} is required"));
    let c = GatewayClient::new(&url, Some(&token));
    c.ping().expect("gateway reachable");
    c
}

fn forbidden(what: &str, r: ApiResult<Value>) {
    match r {
        Err(e) if e.kind == ApiErrorKind::Forbidden => println!("403 as expected: {what} — {}", e.message),
        other => panic!("{what}: expected 403 for a non-admin, got {other:?}"),
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn every_console_gated_verb_is_admin_only_on_the_gateway() {
    let u = client("ABSTRACTGATEWAY_USER_TOKEN");
    let me = u.me().expect("GET /me");
    assert_eq!(me["principal"]["admin"], false, "the user token must be a non-admin: {me}");

    // Routes: w / a / D / C.
    forbidden("models download (w)", u.models_download("mlx", "none/none-4bit", true, None));
    forbidden("apply recommended (a)", u.apply_recommended_routes(false));
    forbidden("Download all (D)", u.download_recommended(true));
    forbidden("cancel a download (C)", u.cancel_model_download("grp_none"));
    // Resources: w / u / k / c.
    let pair = json!({"provider": "mlx", "model": "none"});
    forbidden("load (w)", u.load_model(&pair));
    forbidden("unload (u)", u.unload_model(&pair));
    forbidden("lock (k)", u.lock_model(&pair));
    forbidden("unlock (k)", u.unlock_model(&pair));
    forbidden("clear session caches (c)", u.clear_session_prompt_caches("s-none"));
    // Users: the registry and the retained runtimes.
    forbidden("list users", u.users());
    forbidden("add user (a)", u.create_user(&json!({"user_id": "mallory"})));
    forbidden("edit user (e)", u.patch_user("ana", "default", &json!({"enabled": true})));
    forbidden("delete user (d)", u.delete_user("nobody", "default"));
    forbidden("retained runtimes (v)", u.runtime_reservations());
    // Runtimes: the whole screen.
    forbidden("runtimes inventory", u.runtimes());
    forbidden("runtime knobs", u.runtime_config());
    forbidden("save workspace defaults", u.save_runtime_config(&json!({})));
    // Entity manage writes (checked before the entity is looked up).
    let body = json!({});
    forbidden("entity state", u.entity_state("nobody", &json!({"state": "awake"})));
    forbidden("entity substrate", u.put_entity_substrate("nobody", &body));
    forbidden("entity personal grant", u.put_entity_personal_grant("nobody", &json!({"mode": "disabled"})));
    forbidden("entity loop start", u.entity_loop_start("nobody", &body));
    forbidden("entity loop stop", u.entity_loop_stop("nobody", &body));
    forbidden("entity reembed", u.entity_reembed("nobody", &body));
    forbidden("entity tool policy", u.put_entity_tool_policy("nobody", &body));
    forbidden("entity prompt", u.put_entity_prompt("nobody", &body));
    forbidden(
        "candidate promote",
        u.entity_candidate_act("nobody", "rec", true, &json!({"corroborating_ids": ["a", "b"], "reason": "r"})),
    );
    // The setup guide's record (Ctrl+G's Finish / Skip).
    forbidden("first-run record", u.complete_first_run("skipped"));

    // What the console leaves open answers a non-admin.
    u.capability_defaults().expect("routes grid (read) is user-level");
    u.entities().expect("entity roster is user-level");
    u.bundles(false).expect("workflow list is user-level");
    u.host_state().expect("resources snapshot is user-level");
    u.my_workspace_policy().expect("own workspace policy is user-level");
    println!("user-level reads answered");
}

fn text_route(c: &GatewayClient) -> Value {
    let v = c.capability_defaults().expect("GET routes");
    v["routes"]
        .as_array()
        .and_then(|rows| rows.iter().find(|r| r["key"] == "input.text").cloned())
        .expect("input.text row")
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn web_exact_payloads_behave_like_the_web_on_the_gateway() {
    let a = client("ABSTRACTGATEWAY_AUTH_TOKEN");

    // --- Route save: an untouched base URL survives a save -------------
    a.put_route(
        "input",
        "text",
        None,
        &json!({"provider": "lmstudio", "model": "tiny-test", "base_url": "http://127.0.0.1:1234/v1"}),
    )
    .expect("seed the text route");
    let row = text_route(&a);
    let rows = RoutesData::from_value(&a.capability_defaults().unwrap());
    let shown = rows.rows.iter().find(|r| r.key == "input.text").unwrap();
    let shown_url = shown.base_url.clone().unwrap_or_default();
    assert_eq!(shown_url, "http://127.0.0.1:1234/v1", "seeded: {row}");
    // The editor saves a new model; base URL and options untouched.
    let body = route_save_body("lmstudio", "tiny-test-2", Some(""), (&shown_url, &shown_url), ("", ""))
        .unwrap();
    assert!(body.get("base_url").is_none() && body.get("options").is_none(), "{body}");
    a.put_route("input", "text", None, &body).expect("PUT route (web body)");
    let after = text_route(&a);
    println!("route after the web-shaped save: {after}");
    assert_eq!(after["model"], "tiny-test-2");
    assert_eq!(
        after["base_url"], "http://127.0.0.1:1234/v1",
        "the untouched base URL is kept by the store"
    );
    // Emptying the field IS sent, and clears it.
    let body = route_save_body("lmstudio", "tiny-test-2", Some(""), ("", &shown_url), ("", "")).unwrap();
    a.put_route("input", "text", None, &body).expect("PUT route (cleared URL)");
    let cleared = text_route(&a);
    assert!(
        cleared["base_url"].is_null() || cleared["base_url"] == "",
        "an emptied base URL clears: {cleared}"
    );
    a.clear_route("input", "text", None).expect("restore: clear the route");

    // --- Route Test: the web probe (no max_tokens) is accepted ---------
    // No provider key is set on this hermetic host, so the call fails — on
    // the provider, never on the request shape (no 422).
    let probe = route_test_body("output.text", "openai", "gpt-none", &json!({}));
    match a.sandbox_generate(&probe) {
        Ok(v) => println!("route test answered: ok={} error={}", v["ok"], v["error"]),
        Err(e) => {
            assert_ne!(e.kind, ApiErrorKind::Http(422), "the probe shape is valid: {e}");
            println!("route test refused by the provider (expected here): {e}");
        }
    }

    // --- Catalog download: expected_bytes rides the request -----------
    // A DRY RUN (nothing is fetched): the field is accepted by the route's
    // model (no 422) — the disk pre-check input the web's catalog sends.
    match a.models_download("mlx", "mlx-community/none-4bit", true, Some(1_000_000)) {
        Ok(v) => println!("download dry run with expected_bytes: {v}"),
        Err(e) => {
            assert_ne!(e.kind, ApiErrorKind::Http(422), "expected_bytes is part of the contract: {e}");
            println!("download dry run refused on the artifact (not the shape): {e}");
        }
    }

    // --- Workspace defaults: a save never promotes inherited values ----
    let before = RuntimeConfigData::from_value(&a.runtime_config().expect("GET knobs"));
    println!(
        "before: trust={} ({}) bypass={} ({}) root='{}' ({})",
        before.trust_client_launch_folder,
        before.trust_client_launch_folder_source,
        before.client_workspace_scope_overrides,
        before.client_workspace_scope_overrides_source,
        before.workspace_root,
        before.workspace_root_source
    );
    let body = WorkspaceDefaults::prefill(&before).body();
    println!("web-shaped save body: {body}");
    a.save_runtime_config(&body).expect("POST knobs (web body)");
    let after = RuntimeConfigData::from_value(&a.runtime_config().unwrap());
    println!(
        "after: trust={} ({}) bypass={} ({}) root='{}' ({})",
        after.trust_client_launch_folder,
        after.trust_client_launch_folder_source,
        after.client_workspace_scope_overrides,
        after.client_workspace_scope_overrides_source,
        after.workspace_root,
        after.workspace_root_source
    );
    for (name, was, now) in [
        ("trust_client_launch_folder", &before.trust_client_launch_folder_source, &after.trust_client_launch_folder_source),
        ("client_workspace_scope_overrides", &before.client_workspace_scope_overrides_source, &after.client_workspace_scope_overrides_source),
        ("workspace_root", &before.workspace_root_source, &after.workspace_root_source),
    ] {
        if was != "stored" {
            assert_ne!(now, "stored", "{name}: an inherited value was promoted to a stored setting");
        }
    }
    assert_eq!(after.trust_client_launch_folder, before.trust_client_launch_folder);
    assert_eq!(after.client_workspace_scope_overrides, before.client_workspace_scope_overrides);
}
