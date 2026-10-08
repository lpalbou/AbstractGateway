//! R15 (DESIGN-TUI.md §8): the reference screens' tooltips are the web
//! console's sentences, byte for byte. The web's words live in
//! `tests/fixtures/r15_web_wording.json`, rebuilt from the web sources by
//! `scripts/extract_web_wording.py` (which fails when the copy is stale).
//!
//! - Accounts: every row action's tooltip == `ACCOUNT_TIPS[key](name)`.
//! - Apps: every action's tooltip matches one of the app card's
//!   `data-af-tip` templates (`{n}` = the app's name; a `${c ? "A" : "B"}`
//!   expression = exactly "A" or "B"; any other `${…}` = any text), or is
//!   the gateway's own sentence carried on the row (`update_tip`), or is
//!   "Show log" (the web's Show log button has no tooltip).
//! - Accounts confirmations: the sentence each one asks == the web's.
//! - "Email for everyone": each switch's label and description == the web's. Refused
//!   actions carry their reason instead and are not card tips.

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;
mod r8w4;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::apps::{AppJob, AppRow};
use abstractgateway_console::ui::{apps, users};
use serde_json::{json, Value};

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

// ---- Accounts ------------------------------------------------------------

/// The terminal action id → the web's ACCOUNT_TIPS key.
fn web_key(id: &str) -> &'static str {
    match id {
        "email" => "email",
        "openai" => "openai_api",
        "logs" => "logs",
        "workspaces" => "workspace",
        "preferences" => "preferences",
        "manage" => "manage",
        "rotate" => "rotate",
        "archive" => "archive",
        "unarchive" => "unarchive",
        other => panic!("Accounts action {other:?} has no web tooltip key; add it to the web's ACCOUNT_TIPS first"),
    }
}

#[test]
fn account_row_tooltips_are_the_web_account_tips() {
    let fx = fixture();
    let tips = fx["account_tips"].as_object().expect("account_tips");
    let mut alice =
        accounts_fixture::user_row("alice", false, "alice@example.test", "", true, false);
    alice["actions"]["openai_api"] = json!({"available": true, "reason": null});
    let castor = accounts_fixture::entity_row("castor", "awake", true);
    let mut carol = accounts_fixture::user_row("carol", false, "", "", false, false);
    carol["archived"] = json!(true);
    let rows = accounts_from_payload(&json!({"accounts": [alice, castor, carol]})).unwrap();
    let mut used = std::collections::BTreeSet::new();
    for r in &rows {
        for admin in [true, false] {
            for a in users::row_actions(r, admin) {
                let key = web_key(a.id);
                used.insert(key);
                let want = tips
                    .get(key)
                    .and_then(Value::as_str)
                    .unwrap_or_else(|| panic!("web ACCOUNT_TIPS has no {key:?}"))
                    .replace("{n}", &r.id);
                assert_eq!(
                    a.tooltip.as_deref(),
                    Some(want.as_str()),
                    "{} · {}",
                    r.id,
                    a.id
                );
            }
        }
    }
    // Every web tooltip is offered by some fixture row.
    for k in tips.keys() {
        assert!(
            used.contains(k.as_str()),
            "web tip {k:?} is never offered by the terminal"
        );
    }
}

// ---- Apps ------------------------------------------------------------------

#[derive(Debug)]
enum Seg {
    Lit(String),
    Alt(Vec<String>),
    Any,
}

/// A web template → segments (`name` substituted).
fn parse(tpl: &str, name: &str) -> Vec<Seg> {
    let tpl = tpl.replace("{n}", name);
    let mut out = Vec::new();
    let mut rest = tpl.as_str();
    while let Some(i) = rest.find("${") {
        out.push(Seg::Lit(rest[..i].to_string()));
        // The expression ends at the matching '}' (braces nest).
        let mut depth = 0usize;
        let mut end = None;
        for (j, ch) in rest[i + 1..].char_indices() {
            match ch {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = Some(i + 1 + j);
                        break;
                    }
                }
                _ => {}
            }
        }
        let end = end.unwrap_or_else(|| panic!("unclosed ${{ in {tpl:?}"));
        let expr = &rest[i + 2..end];
        // `cond ? "A" : "B"` → exactly A or B.
        let quoted: Vec<String> = expr
            .split('"')
            .skip(1)
            .step_by(2)
            .map(str::to_string)
            .collect();
        if expr.contains(" ? ") && quoted.len() == 2 {
            out.push(Seg::Alt(quoted));
        } else {
            out.push(Seg::Any);
        }
        rest = &rest[end + 1..];
    }
    out.push(Seg::Lit(rest.to_string()));
    out
}

fn matches(segs: &[Seg], s: &str) -> bool {
    match segs.split_first() {
        None => s.is_empty(),
        Some((Seg::Lit(l), tail)) => s.strip_prefix(l.as_str()).is_some_and(|r| matches(tail, r)),
        Some((Seg::Alt(alts), tail)) => alts
            .iter()
            .any(|a| s.strip_prefix(a.as_str()).is_some_and(|r| matches(tail, r))),
        Some((Seg::Any, tail)) => (0..=s.len())
            .filter(|&i| s.is_char_boundary(i))
            .any(|i| matches(tail, &s[i..])),
    }
}

fn row(v: Value) -> AppRow {
    AppRow::from_value(&v).expect("app row")
}

fn rows() -> Vec<(AppRow, Option<AppJob>, Option<AppJob>)> {
    let tui = |installed: bool| {
        json!({"kind": "tui", "installed": installed, "version": if installed { json!("0.9.0") } else { json!(null) },
               "launch_available": installed, "install_available": true, "command": "abstractcode",
               "update_available": installed, "latest_version": "0.9.1"})
    };
    let job = |state: &str| {
        AppJob::from_value(
            &json!({"id": "j", "kind": "install", "state": state, "progress": 0.3, "parts": []}),
        )
    };
    let stop = json!({"label": "Running", "tone": "ok", "busy": false, "action": "stop", "enabled": true, "tip": "Running — click to stop"});
    vec![
        // Not installed: plain, with the terminal part, with Node.js first.
        (
            row(
                json!({"id": "observer", "name": "Observer", "kind": "web", "installed": false, "status": "not_installed",
                    "actions": ["install"], "install_parts": ["web"]}),
            ),
            None,
            None,
        ),
        (
            row(
                json!({"id": "code", "name": "Code", "kind": "web", "installed": false, "status": "not_installed",
                    "actions": ["install"], "install_parts": ["web", "tui"], "needs_node_install": true,
                    "interfaces": [{"kind": "web"}, tui(false)]}),
            ),
            None,
            None,
        ),
        // Installed with a terminal app (update) / without one; running / stopped.
        (
            row(
                json!({"id": "code", "name": "Code", "kind": "web", "installed": true, "version": "0.11.0",
                    "running": true, "status": "running", "source": "gateway", "actions": ["open", "stop", "logs", "update"],
                    "update_available": true, "latest_version": "0.12.0",
                    "update_tip": "Install the newest Code (0.12.0); a running app restarts on it",
                    "install_parts": ["web", "tui"], "interfaces": [{"kind": "web"}, tui(true)], "status_control": stop}),
            ),
            None,
            None,
        ),
        (
            row(
                json!({"id": "code", "name": "Code", "kind": "web", "installed": true, "version": "0.11.0",
                    "running": false, "status": "stopped", "source": "gateway", "actions": ["launch", "logs"],
                    "install_parts": ["web", "tui"], "interfaces": [{"kind": "web"}, tui(false)]}),
            ),
            None,
            job("running"),
        ),
        // Entity with no entity yet: running / stopped.
        (
            row(
                json!({"id": "entity", "name": "Entity", "kind": "web", "installed": true, "running": true, "status": "running",
                    "actions": ["open", "stop"], "content_summary": {"entities_count": 0}}),
            ),
            None,
            None,
        ),
        (
            row(
                json!({"id": "entity", "name": "Entity", "kind": "web", "installed": true, "running": false, "status": "stopped",
                    "actions": ["launch"], "content_summary": {"entities_count": 0}}),
            ),
            None,
            None,
        ),
        // An install in flight.
        (
            row(
                json!({"id": "flow", "name": "Flow Editor", "kind": "web", "installed": false, "status": "not_installed",
                    "actions": ["install"]}),
            ),
            job("running"),
            None,
        ),
        // Continuum (its gear).
        (
            row(
                json!({"id": "continuum", "name": "Continuum", "kind": "web", "installed": true, "running": true,
                    "status": "running", "actions": ["open", "stop", "logs"]}),
            ),
            None,
            None,
        ),
        // The Assistant (desktop): not installed / running / stopped.
        (
            row(
                json!({"id": "assistant", "name": "AbstractAssistant", "kind": "desktop", "installed": false,
                    "status": "not_installed", "actions": ["install"], "desktop": {"launch_available": false}}),
            ),
            None,
            None,
        ),
        (
            row(
                json!({"id": "assistant", "name": "AbstractAssistant", "kind": "desktop", "installed": true, "running": true,
                    "status": "running", "actions": [], "desktop": {"launch_available": true}}),
            ),
            None,
            None,
        ),
        (
            row(
                json!({"id": "assistant", "name": "AbstractAssistant", "kind": "desktop", "installed": true, "running": false,
                    "status": "stopped", "actions": [], "desktop": {"launch_available": true}}),
            ),
            None,
            None,
        ),
    ]
}

#[test]
fn app_action_tooltips_are_the_web_card_tips() {
    let fx = fixture();
    let templates: Vec<String> = fx["app_tips"]
        .as_array()
        .expect("app_tips")
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    let mut checked = 0;
    for (r, job, tjob) in rows() {
        for a in apps::app_actions(&r, job.as_ref(), tjob.as_ref(), true) {
            let tip = a.tooltip.clone().unwrap_or_default();
            // A refused action is the web's faint off(label, reason): its
            // reason is the gateway's / the verb's sentence, not a card tip.
            if !a.is_enabled() {
                continue;
            }
            if a.id == "log" && tip == "Show log" {
                continue;
            }
            if r.update_tip.as_deref() == Some(tip.as_str()) {
                continue;
            }
            let hit = templates.iter().any(|t| matches(&parse(t, &r.name), &tip));
            assert!(
                hit,
                "{} · {}: tooltip {tip:?} is none of the web card's sentences:\n{templates:#?}",
                r.id, a.id
            );
            checked += 1;
        }
    }
    assert!(
        checked >= 14,
        "only {checked} tooltips checked — the fixture rows lost their actions"
    );
}

#[test]
fn the_template_matcher_is_exact() {
    let t =
        r#"Install {n}${withTui ? " for the browser and the terminal" : ""}${x ? " (Node)" : ""}"#;
    let s = parse(t, "Code");
    assert!(matches(&s, "Install Code"));
    assert!(matches(
        &s,
        "Install Code for the browser and the terminal (Node)"
    ));
    assert!(!matches(&s, "Install Code for the terminal"));
    assert!(!matches(&s, "Install Code "));
    assert!(matches(
        &parse("{n} settings", "Continuum"),
        "Continuum settings"
    ));
    assert!(!matches(
        &parse("{n} settings", "Continuum"),
        "Continuum settings (g)"
    ));
}

// ---- Accounts confirmations and the email switches -------------------------

fn page_view(
    ctx: &abstractgateway_console::ui::Ctx,
    cx: abstracttui::prelude::Scope,
) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

fn accounts_page() -> r8w4::Harness {
    let mut alice = accounts_fixture::user_row("alice", false, "", "", true, false);
    alice["actions"]["openai_api"] = json!({"available": true, "reason": null});
    let castor = accounts_fixture::entity_row("castor", "awake", true);
    let admin = {
        let mut a = accounts_fixture::user_row("admin", true, "", "", true, true);
        a["own"] = json!(true);
        a
    };
    let mut h = r8w4::harness((120, 40), r8w4::Mount::Page(page_view));
    h.admin();
    h.store
        .accounts
        .set(abstractgateway_console::store::Loadable::Ready(
            accounts_from_payload(&json!({"accounts": [admin, alice, castor]})).unwrap(),
        ));
    h.store
        .users
        .set(abstractgateway_console::store::Loadable::Ready(
            abstractgateway_console::store::users_from_payload(&json!({"users": [
                {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "enabled": true},
                {"user_id": "alice", "tenant_id": "default", "roles": ["user"], "enabled": true}
            ]})),
        ));
    h.store
        .entities
        .set(abstractgateway_console::store::Loadable::Ready(
            abstractgateway_console::store::entities_from_payload(
                &json!({"entities": [{"name": "castor", "state": "awake"}]}),
            ),
        ));
    h.turns(3);
    h.sent();
    h
}

#[test]
fn account_confirmations_ask_the_web_sentences() {
    let fx = fixture();
    let want = |k: &str, n: &str| {
        fx["account_confirms"][k]
            .as_str()
            .unwrap_or_else(|| panic!("web confirm {k:?} missing"))
            .replace("{n}", n)
    };
    let asked = || {
        abstractgateway_console::ui::w::confirm::asked()
            .last()
            .cloned()
            .unwrap_or_default()
    };
    for (who, key, web) in [
        ("alice", "t", "rotate"),
        ("alice", "d", "archive_user"),
        ("alice", " ", "deactivate"),
        ("castor", "d", "archive_entity"),
        ("castor", " ", "suspend"),
    ] {
        let mut h = accounts_page();
        h.ui.acc_key.set(Some(format!("default/{who}")));
        h.turns(2);
        h.key(key.as_bytes());
        h.turns(2);
        assert_eq!(asked(), want(web, who), "{who} · {web}");
    }
}

#[test]
fn email_for_everyone_switches_are_the_web_labels_and_descriptions() {
    let fx = fixture();
    let web = fx["email_switches"].as_array().expect("email_switches");
    let tui = [
        (users::MAILBOXES_LABEL, users::MAILBOXES_HELP),
        (users::AGENT_TOOLS_LABEL, users::AGENT_TOOLS_HELP),
        (users::RECOVERY_LABEL, users::RECOVERY_HELP),
    ];
    assert_eq!(web.len(), tui.len());
    for (w, (label, desc)) in web.iter().zip(tui) {
        assert_eq!(w["label"].as_str(), Some(label));
        assert_eq!(w["desc"].as_str(), Some(desc));
    }
    // And the card shows them.
    let mut h = accounts_page();
    let s = h.turns(2);
    for (label, _) in tui {
        assert!(s.contains(label), "{label:?} on the card:\n{s}");
    }
}
