//! Worker half of entity parity (summon, templates, card, talk, voice
//! audition). A child module of `worker`, so every write rides the same
//! three-phase law (write → verify via GET → journal + notice) through
//! the shared helpers.

use serde_json::{json, Value};

use super::{
    finish_write, load, refresh_entities, refresh_entity_detail, require_client, with_busy, Body,
};
use crate::api::entities::{
    artifact_id, audio_extension, find_player_in, matrix_from_spec, model_names, provider_names,
    templates_from_payload, versions_line, AuditionOutcome, ChatState, CreateCheck, CreationKit,
    EntityCard,
};
use crate::api::GatewayClient;
use crate::store::{Loadable, Store};
use abstracttui::reactive::WakeHandle;

/// Entity-parity commands (one `Cmd::Entity` variant carries them, so
/// the shared enum grows by one line).
#[derive(Clone, Debug)]
pub enum EntityCmd {
    /// Templates (anchor) + creation defaults + providers + embedding
    /// models + the default capability grid.
    LoadCreationKit,
    /// The provider cascade: that provider's TEXT models.
    LoadCreateModels {
        provider: String,
    },
    /// One template's version history (templates modal).
    LoadTemplateVersions {
        id: String,
    },
    /// Create (POST, `create`) or edit (PUT) an operator template.
    SaveTemplate {
        id: String,
        create: bool,
        body: Body,
        form_id: Option<u64>,
    },
    /// The dry-run before the irreversible birth.
    ValidateEntity {
        name: String,
        body: Body,
    },
    /// The birth, then the optional admin configuration — each its own
    /// journaled write; the note composes like the web's.
    CreateEntity {
        name: String,
        body: Body,
        /// `PUT substrate` body (both provider and model chosen).
        substrate: Option<Body>,
        /// `PUT tool-policy` body (only changed phases).
        policy: Option<Body>,
        /// The dry-run's warnings (reviewed before the confirm).
        warnings: Vec<String>,
        /// UI-side notes (a half-chosen substrate was skipped, …).
        extra_note: String,
        form_id: Option<u64>,
    },
    LoadCard {
        name: String,
    },
    ChatOpen {
        name: String,
    },
    ChatTurn {
        name: String,
        chat_id: String,
        text: String,
    },
    ChatClose {
        name: String,
        chat_id: String,
    },
    /// Synthesize the UNSAVED voice selection as the entity, save the
    /// audio to a file.
    VoiceAudition {
        name: String,
        provider: String,
        model: String,
        voice: Option<String>,
    },
}

impl EntityCmd {
    /// The form awaiting this command (the panic path releases it).
    pub fn form_id(&self) -> Option<u64> {
        match self {
            EntityCmd::SaveTemplate { form_id, .. } | EntityCmd::CreateEntity { form_id, .. } => {
                *form_id
            }
            _ => None,
        }
    }
}

fn err_text(e: &crate::api::ApiError) -> String {
    e.to_string()
}

pub(super) fn handle(
    client: &Option<GatewayClient>,
    store: &Store,
    wake: &WakeHandle,
    cmd: EntityCmd,
    on_done: &(impl Fn(u64, Result<String, String>) + Send + 'static),
) {
    match cmd {
        EntityCmd::LoadCreationKit => {
            load(store, wake, "entity creation kit", store.entity_kit, || {
                let c = require_client(client)?;
                let (templates, template_warnings) = templates_from_payload(&c.entity_templates()?);
                let mut kit = CreationKit {
                    templates,
                    template_warnings,
                    ..CreationKit::default()
                };
                let defaults = c.entity_creation_defaults();
                kit.apply_defaults(defaults.as_ref().map_err(err_text));
                match c.discovery_providers() {
                    Ok(v) => {
                        kit.providers = provider_names(&v);
                        if kit.providers.is_empty() {
                            kit.notes.push(
                                "#FALLBACK provider discovery returned no providers — leave on Gateway default, or configure a provider on the Providers screen".into(),
                            );
                        }
                    }
                    Err(e) => kit.notes.push(format!(
                        "#FALLBACK provider discovery unavailable: {e} — leave on Gateway default, or configure a provider on the Providers screen"
                    )),
                }
                if let Some((ep, _)) = kit.default_embedding.clone() {
                    // The default option still works; free choice
                    // degrades quietly (the web's rule).
                    if let Ok(v) = c.provider_models_of_type(&ep, "embeddings") {
                        kit.embedding_models = model_names(&v);
                    }
                }
                match c.entity_capability_matrix() {
                    Ok(v) => kit.matrix = Some(matrix_from_spec(&v)),
                    Err(e) => {
                        kit.matrix_error = Some(format!("capability matrix unavailable: {e}"))
                    }
                }
                Ok(kit)
            });
        }

        EntityCmd::LoadCreateModels { provider } => {
            let result = with_busy(store, wake, &format!("models: {provider}"), || {
                require_client(client)
                    .and_then(|c| c.provider_models_of_type(&provider, "text"))
                    .map(|v| model_names(&v))
                    .map_err(|e| e.to_string())
            });
            let s = *store;
            wake.post(move || {
                s.entity_kit.update(|k| {
                    if let Loadable::Ready(k) = k {
                        k.models = Some((provider, result));
                    }
                })
            });
        }

        EntityCmd::LoadTemplateVersions { id } => {
            let result = require_client(client)
                .and_then(|c| c.entity_template_versions(&id))
                .map(|v| versions_line(&v))
                .map_err(|e| e.to_string());
            let s = *store;
            wake.post(move || {
                s.entity_kit.update(|k| {
                    if let Loadable::Ready(k) = k {
                        k.versions = Some((id, result));
                    }
                })
            });
        }

        EntityCmd::SaveTemplate {
            id,
            create,
            body,
            form_id,
        } => {
            let action = if create {
                format!("POST template '{id}' (new)")
            } else {
                format!("PUT template '{id}' (new version)")
            };
            let (write, verify) = with_busy(store, wake, &format!("template {id}: save"), || {
                let c = require_client(client);
                let write = c.clone().and_then(|c| {
                    if create {
                        c.create_entity_template(&body)
                    } else {
                        c.put_entity_template(&id, &body)
                    }
                });
                let verify = match &write {
                    Ok(_) => Some(c.and_then(|c| c.entity_template(&id))),
                    Err(_) => None,
                };
                (write, verify)
            });
            let saved_version = write
                .as_ref()
                .ok()
                .and_then(|v| v.get("version").and_then(Value::as_u64));
            let verified = verify.map(|r| match r {
                Ok(v) => {
                    let now = v.get("version").and_then(Value::as_u64);
                    match (now, saved_version) {
                        (Some(n), Some(w)) if n == w => Ok(format!("GET shows {id} v{n}")),
                        (n, w) => Err(format!(
                            "GET shows {id} v{} (the save reported v{})",
                            n.map(|x| x.to_string()).unwrap_or_else(|| "?".into()),
                            w.map(|x| x.to_string()).unwrap_or_else(|| "?".into())
                        )),
                    }
                }
                Err(e) => Err(format!("GET failed: {e}")),
            });
            let lint = write
                .as_ref()
                .ok()
                .map(|v| {
                    v.get("lint_warnings")
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter_map(|x| x.as_str().map(str::to_string))
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default()
                })
                .unwrap_or_default();
            let ok = write.is_ok();
            finish_write(store, wake, action, write, verified, form_id, on_done);
            if ok {
                // Refresh the gallery so BOTH pickers show the new version.
                if let Ok(g) = require_client(client).and_then(|c| c.entity_templates()) {
                    let (rows, warnings) = templates_from_payload(&g);
                    let s = *store;
                    wake.post(move || {
                        s.entity_kit.update(|k| {
                            if let Loadable::Ready(k) = k {
                                k.templates = rows;
                                k.template_warnings = warnings;
                                k.versions = None;
                            }
                        })
                    });
                }
                if !lint.is_empty() {
                    let s = *store;
                    let note = format!(
                        "Saved {id} v{}. Warnings: {}",
                        saved_version
                            .map(|n| n.to_string())
                            .unwrap_or_else(|| "?".into()),
                        lint.join(" | ")
                    );
                    wake.post(move || s.notice.set(Some(note)));
                }
            }
        }

        EntityCmd::ValidateEntity { name, body } => {
            let label = format!("validating '{name}' (dry-run)");
            load(store, wake, &label, store.entity_check, || {
                require_client(client)?
                    .validate_entity(&name, &body)
                    .map(|v| CreateCheck::from_value(&name, &v))
            });
        }

        EntityCmd::CreateEntity {
            name,
            body,
            substrate,
            policy,
            warnings,
            extra_note,
            form_id,
        } => {
            let action = format!("POST entity '{name}' (summon)");
            let (write, verify) = with_busy(store, wake, &format!("summoning {name}"), || {
                let c = require_client(client);
                let write = c.clone().and_then(|c| c.create_entity(&body));
                let verify = match &write {
                    Ok(_) => Some(c.and_then(|c| c.entity_card(&name))),
                    Err(_) => None,
                };
                (write, verify)
            });
            let verified = verify.map(|r| match r {
                Ok(card) => Ok(format!(
                    "GET card shows {}",
                    card.get("handle")
                        .and_then(Value::as_str)
                        .or_else(|| card.get("entity_id").and_then(Value::as_str))
                        .unwrap_or(name.as_str())
                )),
                Err(e) => Err(format!("GET card failed: {e}")),
            });
            let created_flag = write
                .as_ref()
                .ok()
                .and_then(|v| v.get("created").and_then(Value::as_bool));
            let ok = write.is_ok();
            finish_write(
                store,
                wake,
                action,
                write,
                verified.clone(),
                form_id,
                on_done,
            );
            if !ok {
                return;
            }
            let mut note = if created_flag == Some(false) {
                format!("{name} already existed (identical spark).")
            } else {
                format!("Summoned {name}.")
            };
            if !warnings.is_empty() {
                note.push_str(&format!(" Warnings: {}", warnings.join(" | ")));
            }
            // Optional substrate (admin) — its own write + verify; its
            // failure is surfaced, never loses the create.
            if let Some(sub) = substrate {
                let action = format!("PUT entity '{name}' substrate (at summon)");
                let (w, v) = with_busy(store, wake, &format!("{name}: substrate"), || {
                    let c = require_client(client);
                    let w = c.clone().and_then(|c| c.put_entity_substrate(&name, &sub));
                    let v = c.and_then(|c| c.entity_substrate(&name));
                    (w, v)
                });
                match &w {
                    Ok(_) => note.push_str(" Substrate set."),
                    Err(e) => note.push_str(&format!(" (substrate not set: {e})")),
                }
                let verified = v.as_ref().ok().map(|v| {
                    Ok(format!(
                        "GET shows mind: {} / {}",
                        v.get("provider").and_then(Value::as_str).unwrap_or("—"),
                        v.get("model").and_then(Value::as_str).unwrap_or("—")
                    ))
                });
                finish_write(store, wake, action, w, verified, None, on_done);
            }
            if let Some(pol) = policy {
                let action = format!("PUT entity '{name}' tool policy (at summon)");
                let (w, v) = with_busy(store, wake, &format!("{name}: capabilities"), || {
                    let c = require_client(client);
                    let w = c
                        .clone()
                        .and_then(|c| c.put_entity_tool_policy(&name, &pol));
                    let v = c.and_then(|c| c.entity_tool_policy(&name));
                    (w, v)
                });
                match &w {
                    Ok(_) => note.push_str(" Capabilities set."),
                    Err(e) => note.push_str(&format!(" (capabilities not set: {e})")),
                }
                let verified = v.as_ref().ok().map(|v| {
                    let custom = v
                        .get("phases")
                        .and_then(Value::as_object)
                        .map(|o| {
                            o.iter()
                                .filter(|(_, p)| {
                                    p.get("source").and_then(Value::as_str) == Some("custom")
                                })
                                .map(|(k, _)| k.clone())
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default();
                    Ok(format!(
                        "GET shows custom phases: {}",
                        if custom.is_empty() {
                            "none".to_string()
                        } else {
                            custom.join(", ")
                        }
                    ))
                });
                finish_write(store, wake, action, w, verified, None, on_done);
            }
            note.push_str(&extra_note);
            if let Some(Ok(v)) = &verified {
                note.push_str(&format!(" — verified: {v}"));
            }
            refresh_entities(client, store, wake);
            let s = *store;
            wake.post(move || s.notice.set(Some(note)));
        }

        EntityCmd::LoadCard { name } => {
            let label = format!("{name}: card");
            load(store, wake, &label, store.entity_card, || {
                require_client(client)?
                    .entity_card(&name)
                    .map(|v| EntityCard::from_value(&name, &v))
            });
        }

        EntityCmd::ChatOpen { name } => {
            let s = *store;
            let n = name.clone();
            wake.post(move || {
                s.entity_chat.update(|c| {
                    if c.entity != n {
                        *c = ChatState::fresh(&n);
                    }
                    c.busy = true;
                    c.status = "opening the visit (prelude + memory)…".into();
                })
            });
            let action = format!("POST entity '{name}' chat/open");
            let (write, verify) = with_busy(store, wake, &format!("{name}: opening visit"), || {
                let c = require_client(client);
                let w = c.clone().and_then(|c| c.entity_chat_open(&name));
                let v = match &w {
                    Ok(_) => Some(c.and_then(|c| c.entity_chat_status(&name))),
                    Err(_) => None,
                };
                (w, v)
            });
            let opened = write
                .as_ref()
                .ok()
                .and_then(|v| v.get("chat_id").and_then(Value::as_str))
                .map(str::to_string);
            let verified = verify.map(|r| match r {
                Ok(v) => {
                    let open = v.get("open").and_then(Value::as_bool) == Some(true);
                    let id = v.get("chat_id").and_then(Value::as_str).map(str::to_string);
                    if open && id == opened {
                        Ok(format!("GET shows visit {} open", id.unwrap_or_default()))
                    } else {
                        Err(format!("GET shows open={open} chat_id={id:?}"))
                    }
                }
                Err(e) => Err(format!("GET failed: {e}")),
            });
            let outcome: Result<Value, String> = write.clone().map_err(|e| e.to_string());
            finish_write(store, wake, action, write, verified, None, on_done);
            let s = *store;
            let n = name.clone();
            wake.post(move || {
                s.entity_chat.update(|c| {
                    if c.entity != n {
                        return;
                    }
                    c.busy = false;
                    match &outcome {
                        Ok(v) => c.apply_open(v),
                        Err(e) => c.status = e.clone(),
                    }
                })
            });
            refresh_entity_detail(client, store, wake, &name);
        }

        EntityCmd::ChatTurn {
            name,
            chat_id,
            text,
        } => {
            let result = with_busy(store, wake, &format!("{name}: thinking…"), || {
                require_client(client).and_then(|c| c.entity_chat_turn(&name, &chat_id, &text))
            });
            let s = *store;
            wake.post(move || {
                s.entity_chat.update(|c| {
                    // A turn for a visit this panel no longer holds is
                    // dropped, never painted under another header.
                    if c.entity != name || c.chat_id.as_deref() != Some(chat_id.as_str()) {
                        return;
                    }
                    c.busy = false;
                    match &result {
                        Ok(v) => c.apply_turn(v),
                        Err(e) => c.status = e.to_string(),
                    }
                })
            });
        }

        EntityCmd::ChatClose { name, chat_id } => {
            let action = format!("POST entity '{name}' chat/close (reflect)");
            let (write, verify) = with_busy(
                store,
                wake,
                &format!("{name}: closing visit (reflection)"),
                || {
                    let c = require_client(client);
                    let w = c.clone().and_then(|c| c.entity_chat_close(&name, &chat_id));
                    let v = match &w {
                        Ok(_) => Some(c.and_then(|c| c.entity_chat_status(&name))),
                        Err(_) => None,
                    };
                    (w, v)
                },
            );
            let verified = verify.map(|r| match r {
                Ok(v) => {
                    if v.get("open").and_then(Value::as_bool) == Some(true) {
                        Err("GET still shows a visit open".to_string())
                    } else {
                        Ok("GET shows no visit open".to_string())
                    }
                }
                Err(e) => Err(format!("GET failed: {e}")),
            });
            let outcome: Result<(), crate::api::ApiError> =
                write.as_ref().map(|_| ()).map_err(Clone::clone);
            finish_write(store, wake, action, write, verified, None, on_done);
            let s = *store;
            let n = name.clone();
            wake.post(move || {
                s.entity_chat.update(|c| {
                    if c.entity != n {
                        return;
                    }
                    c.busy = false;
                    match &outcome {
                        Ok(()) => {
                            c.chat_id = None;
                            c.status =
                                "visit closed — reflection ran, the loop (if yielded) wakes."
                                    .into();
                        }
                        Err(e) => c.apply_close_error(e),
                    }
                })
            });
            refresh_entity_detail(client, store, wake, &name);
        }

        EntityCmd::VoiceAudition {
            name,
            provider,
            model,
            voice,
        } => {
            let s = *store;
            wake.post(move || s.entity_audition.set(Loadable::Loading));
            let started = std::time::Instant::now();
            let mut body = json!({
                "text": format!("Hello — I am {name}, and this is how I would sound."),
                "provider": provider,
                "model": model,
                "timeout_s": 25,
            });
            if let Some(v) = &voice {
                body["voice"] = Value::String(v.clone());
            }
            let result = with_busy(
                store,
                wake,
                &format!("{name}: synthesizing (up to 25s)"),
                || {
                    let c = require_client(client)?;
                    let res = c.entity_voice_tts(&name, &body)?;
                    let secs = started.elapsed().as_secs_f32();
                    let mut out = AuditionOutcome {
                        entity: name.clone(),
                        summary: format!(
                            "Synthesized in {secs:.1}s with {provider}/{model}{} — this is the unsaved selection; Save makes it his.",
                            match &voice {
                                Some(v) => format!("/{v}"),
                                None => " (provider default voice)".into(),
                            }
                        ),
                        player: std::env::var("PATH").ok().and_then(|p| find_player_in(&p)),
                        ..AuditionOutcome::default()
                    };
                    let artifact = res.get("audio_artifact").cloned().unwrap_or(Value::Null);
                    let run_id = res.get("run_id").and_then(Value::as_str).unwrap_or("");
                    match artifact_id(&artifact) {
                        None => out.error = Some("the response carried no audio artifact".into()),
                        Some(aid) => match c.run_artifact_content(run_id, &aid) {
                            Err(e) => out.error = Some(format!("audio download failed: {e}")),
                            Ok(bytes) => match save_audio(&name, &aid, &artifact, &bytes) {
                                Ok(path) => {
                                    out.bytes = bytes.len();
                                    out.path = Some(path);
                                }
                                Err(e) => out.error = Some(e),
                            },
                        },
                    }
                    Ok(out)
                },
            );
            let s = *store;
            wake.post(move || {
                s.entity_audition.set(match result {
                    Ok(o) => Loadable::Ready(o),
                    Err(e) => Loadable::Failed(e),
                })
            });
        }
    }
}

/// Write the audition audio under the OS temp dir
/// (`<tmp>/abstractgateway-console/<entity>-audition-<artifact>.<ext>`)
/// and return the path. The name is sanitized to a safe file stem.
fn save_audio(name: &str, aid: &str, artifact: &Value, bytes: &[u8]) -> Result<String, String> {
    let safe = |s: &str| -> String {
        s.chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect()
    };
    let dir = std::env::temp_dir().join("abstractgateway-console");
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let path = dir.join(format!(
        "{}-audition-{}.{}",
        safe(name),
        safe(aid),
        audio_extension(artifact)
    ));
    std::fs::write(&path, bytes).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    Ok(path.display().to_string())
}
