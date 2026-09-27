//! Live entity-parity proof against a HERMETIC gateway — ignored by
//! default, and it REFUSES the operator's ports (8080/8081): a summon is
//! permanent (there is no entity delete), so this test must never touch
//! a real gateway's data.
//!
//! Run explicitly (a throwaway gateway, e.g. on 18864):
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18864 \
//!   ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_entities -- --ignored --nocapture --test-threads 1
//!
//! Drives the exact client methods + folds the console's worker runs:
//! templates → creation defaults → dry-run validate (red, then green) →
//! create → read back (roster + card) → template create/edit/versions →
//! chat open (a model-less gateway answers with its honest refusal) →
//! voice audition (same).

use serde_json::json;

use abstractgateway_console::api::entities as ent;
use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::store::entities_from_payload;

fn hermetic_client() -> Option<GatewayClient> {
    let url = std::env::var("ABSTRACTGATEWAY_URL").ok()?;
    assert!(
        !url.contains(":8080") && !url.contains(":8081"),
        "live_entities creates PERMANENT entities — never against the operator's gateway ({url})"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").ok()?;
    let client = GatewayClient::new(&url, Some(&token));
    client.ping().ok()?;
    Some(client)
}

#[test]
#[ignore]
fn live_entity_parity_against_a_hermetic_gateway() {
    let Some(c) = hermetic_client() else {
        eprintln!("skipped: set ABSTRACTGATEWAY_URL (hermetic) + ABSTRACTGATEWAY_AUTH_TOKEN");
        return;
    };
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();

    // 1. The creation kit (templates are the anchor).
    let (templates, warnings) = ent::templates_from_payload(&c.entity_templates().unwrap());
    println!(
        "templates: {:?} warnings: {warnings:?}",
        templates
            .iter()
            .map(|t| (&t.id, t.version))
            .collect::<Vec<_>>()
    );
    let tpl = templates.first().expect("at least the builtin template");
    let mut kit = ent::CreationKit::default();
    kit.apply_defaults(
        c.entity_creation_defaults()
            .as_ref()
            .map_err(|e| e.to_string()),
    );
    println!(
        "defaults: substrate {:?} embedding {:?} notes {:?}",
        kit.default_substrate, kit.default_embedding, kit.notes
    );
    let matrix = ent::matrix_from_spec(&c.entity_capability_matrix().unwrap());
    println!(
        "capability grid: {} phases x {} tools",
        matrix.phases.len(),
        matrix.tools.len()
    );

    // 2. A red dry-run: the body name disagrees with the path name.
    let name = format!("Paritor{stamp}");
    let bad = c
        .validate_entity(&name, &json!({"name": "Someone Else", "spark": tpl.spark}))
        .unwrap();
    let bad = ent::CreateCheck::from_value(&name, &bad);
    println!("red dry-run: {}", bad.refusal());
    assert!(!bad.ok);

    // 3. The green dry-run, then the birth, then the read-back.
    let body = ent::create_body(&name, &tpl.spark, "");
    let check = ent::CreateCheck::from_value(&name, &c.validate_entity(&name, &body).unwrap());
    println!(
        "green dry-run: ok={} warnings={:?}",
        check.ok, check.warnings
    );
    assert!(check.ok, "{}", check.refusal());
    let created = c.create_entity(&body).unwrap();
    println!(
        "POST /entities → created={} slug={}",
        created["created"], created["slug"]
    );
    let roster = entities_from_payload(&c.entities().unwrap());
    assert!(
        roster.iter().any(|e| e.name == name),
        "the roster lists {name}"
    );
    let card = ent::EntityCard::from_value(&name, &c.entity_card(&name).unwrap());
    println!("card rows: {:?}", card.rows);
    assert!(
        card.rows.iter().any(|(k, _)| k == "Entity ID"),
        "card carries the entity id"
    );

    // 4. Template management: new → edit (new version) → versions.
    let tid = format!("parity-{stamp}");
    let saved = c
        .create_entity_template(&json!({
            "id": tid, "spark": tpl.spark, "name": "Parity", "description": "live proof",
            "note": "created via console"
        }))
        .unwrap();
    println!("POST template → v{}", saved["version"]);
    // A re-save of identical spark content is a no-op server-side
    // (idempotent), so the edit changes the blueprint.
    let mut edited_spark = tpl.spark.clone();
    edited_spark["console_parity_probe"] = json!(stamp);
    let edited = c
        .put_entity_template(
            &tid,
            &json!({"id": tid, "spark": edited_spark, "name": "Parity", "description": "edited",
                    "note": "edited via console"}),
        )
        .unwrap();
    println!("PUT template → v{}", edited["version"]);
    let read = c.entity_template(&tid).unwrap();
    assert_eq!(
        read["version"], edited["version"],
        "GET reads the new version"
    );
    let line = ent::versions_line(&c.entity_template_versions(&tid).unwrap());
    println!("{line}");
    assert!(line.contains("v1") && line.contains("v2"), "{line}");

    // 5. Talk: open the visit. A gateway with no text model answers with
    //    its honest refusal — printed verbatim; a gateway WITH one opens,
    //    takes one turn, and closes with reflection.
    match c.entity_chat_open(&name) {
        Ok(open) => {
            let mut chat = ent::ChatState::fresh(&name);
            chat.apply_open(&open);
            println!("chat open: {}", chat.status);
            let id = chat.chat_id.clone().expect("chat id");
            match c.entity_chat_turn(&name, &id, "hello") {
                Ok(t) => {
                    chat.apply_turn(&t);
                    println!("turn: {:?} · {}", chat.lines.last(), chat.status);
                }
                Err(e) => println!("turn refused: {e}"),
            }
            println!(
                "close: {:?}",
                c.entity_chat_close(&name, &id).map(|_| "closed")
            );
        }
        Err(e) => println!("chat open refused (honest gateway error): {e}"),
    }
    println!("chat status: {}", c.entity_chat_status(&name).unwrap());

    // 6. Voice audition (same honesty rule).
    match c.entity_voice_tts(
        &name,
        &json!({"text": format!("Hello — I am {name}, and this is how I would sound."),
                "provider": "openai", "model": "gpt-4o-mini-tts", "timeout_s": 25}),
    ) {
        Ok(v) => println!(
            "tts: run {} artifact {:?}",
            v["run_id"],
            ent::artifact_id(&v["audio_artifact"])
        ),
        Err(e) => println!("voice audition refused (honest gateway error): {e}"),
    }
}
