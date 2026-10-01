//! Round-2 buffer snapshots: the Workflows screen (§4) and the Multimodal
//! screen's transcription row (item 1/5).
//! `ROUND2_SHOTS_DIR=<dir> cargo test --test round2_shots_workflows -- --ignored`.

mod r2shots;

use abstractgateway_console::store::{
    workflows_from_payload, Loadable, RoutesData, RuntimeConfigData,
};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2shots::{harness, Harness, SIZES};
use serde_json::{json, Value};

pub fn bundles_payload() -> Value {
    json!({
        "default_bundle_id": "basic-agent",
        "items": [
            {"bundle_id": "basic-agent", "bundle_version": "0.3.2", "version_channel": "published",
             "is_draft": false, "latest_published_version": "0.3.2", "created_at": "2026-09-20T10:00:00Z",
             "registry_scope": "private", "default_entrypoint": "agent",
             "source": "shipped",
             "description": "A general chat agent with tools: answers, plans and runs tasks step by step.",
             "entrypoints": [
                {"flow_id": "agent", "name": "Basic agent",
                 "description": "A general chat agent with tools: answers, plans and runs tasks step by step.",
                 "interfaces": ["abstractcode.agent.v1", "abstractassistant.agent.v1"],
                 "workflow_id": "basic-agent@0.3.2:agent", "deprecated": false}]},
            {"bundle_id": "basic-agent", "bundle_version": "0.3.1", "version_channel": "published",
             "is_draft": false, "latest_published_version": "0.3.2", "created_at": "2026-09-01T10:00:00Z",
             "registry_scope": "private", "default_entrypoint": "agent", "source": "shipped",
             "description": "A general chat agent with tools: answers, plans and runs tasks step by step.",
             "entrypoints": [
                {"flow_id": "agent", "name": "Basic agent", "description": "",
                 "interfaces": ["abstractcode.agent.v1"], "workflow_id": "basic-agent@0.3.1:agent"}]},
            {"bundle_id": "deep-research", "bundle_version": "1.0.0", "version_channel": "published",
             "is_draft": false, "latest_published_version": "1.0.0", "created_at": "2026-09-22T10:00:00Z",
             "registry_scope": "private", "default_entrypoint": "research", "source": "imported",
             "description": "Researches a question across many sources and writes a cited report.",
             "entrypoints": [
                {"flow_id": "research", "name": "Deep research",
                 "description": "Researches a question across many sources and writes a cited report.",
                 "interfaces": ["abstractresearch.deep.v1"],
                 "workflow_id": "deep-research@1.0.0:research"}]},
            {"bundle_id": "my-flow", "bundle_version": "0.1.0", "version_channel": "published",
             "is_draft": false, "latest_published_version": "0.1.0", "created_at": "2026-09-30T10:00:00Z",
             "registry_scope": "private", "default_entrypoint": "main", "source": "published",
             "description": "Summarises my morning inbox.",
             "entrypoints": [
                {"flow_id": "main", "name": "Morning digest", "description": "Summarises my morning inbox.",
                 "interfaces": [], "workflow_id": "my-flow@0.1.0:main", "deprecated": true}]}
        ],
        "skipped": [
            {"bundle_id": "coding-agent", "bundle_version": "0.2.8",
             "reason": "needs abstractruntime >= 0.9 (this gateway has 0.8.1)", "path": "/x/coding-agent@0.2.8.flow"}
        ],
        "default_agent_workflows": {"abstractcode.agent.v1": {"workflow_id": "basic-agent@0.3.2:agent"}}
    })
}

pub fn runtime_config_payload() -> Value {
    let elig_agent = json!([{"value": "basic-agent:agent", "workflow_id": "basic-agent@0.3.2:agent",
        "bundle_id": "basic-agent", "bundle_version": "0.3.2", "flow_id": "agent", "name": "Basic agent",
        "registry_scope": "private"}]);
    json!({
        "writable": true,
        "agents": {
            "label": "Default agent workflow",
            "default_workflow": {
                "abstractcode.agent.v1": {
                    "key": "agents.default_workflow.abstractcode.agent.v1", "interface": "abstractcode.agent.v1",
                    "label": "AbstractCode — chat agent", "app": "AbstractCode",
                    "help": "The agent AbstractCode (and apps that borrow it) runs for a conversation.",
                    "group": "apps", "state": "builtin", "value": "", "source": "default",
                    "available": true, "reason": null, "default": "basic-agent:agent",
                    "resolved": {"workflow_id": "basic-agent@0.3.2:agent", "name": "Basic agent"},
                    "eligible": elig_agent},
                "abstractassistant.agent.v1": {
                    "key": "agents.default_workflow.abstractassistant.agent.v1",
                    "interface": "abstractassistant.agent.v1",
                    "label": "Assistant", "app": "Assistant",
                    "help": "The agent the menu-bar Assistant runs.",
                    "group": "apps", "state": "set", "value": "basic-agent:agent", "source": "stored",
                    "available": true, "reason": null, "default": null,
                    "resolved": {"workflow_id": "basic-agent@0.3.2:agent", "name": "Basic agent"},
                    "eligible": elig_agent},
                "abstractcode.coding.v1": {
                    "key": "agents.default_workflow.abstractcode.coding.v1", "interface": "abstractcode.coding.v1",
                    "label": "AbstractCode — coding agent", "app": "AbstractCode",
                    "help": "The agent AbstractCode runs for coding tasks.",
                    "group": "apps", "state": "broken", "value": "coding-agent:main", "source": "stored",
                    "available": false,
                    "reason": "Broken: coding-agent 0.2.8 is no longer installed — pick another or choose Clients choose",
                    "default": null, "resolved": null, "eligible": []},
                "abstractresearch.deep.v1": {
                    "key": "agents.default_workflow.abstractresearch.deep.v1", "interface": "abstractresearch.deep.v1",
                    "label": "Deep research", "app": "AbstractResearch",
                    "help": "The research agent AbstractResearch runs for a deep-research question.",
                    "group": "apps", "state": "clients_choose", "value": "", "source": "default",
                    "available": false, "reason": null, "default": null, "resolved": null,
                    "eligible": [{"value": "deep-research:research", "workflow_id": "deep-research@1.0.0:research",
                        "bundle_id": "deep-research", "bundle_version": "1.0.0", "flow_id": "research",
                        "name": "Deep research", "registry_scope": "private"}]},
                "abstractbatch.mapreduce.v1": {
                    "key": "agents.default_workflow.abstractbatch.mapreduce.v1",
                    "interface": "abstractbatch.mapreduce.v1",
                    "label": "Batch map-reduce", "app": null,
                    "help": "Declared by the batch workflow; no app asks for it by default.",
                    "group": "other", "state": "clients_choose", "value": "", "source": "default",
                    "available": false, "reason": null, "default": null, "resolved": null, "eligible": []}
            }
        }
    })
}

pub fn voice_routes(voice: Value) -> RoutesData {
    RoutesData::from_value(&json!({
        "ok": true, "writable": true, "authority": "abstractcore.gateway_runtime",
        "source": "abstractcore.gateway_runtime", "errors": [],
        "routes": [
            {"key": "input.text", "kind": "input", "modality": "text", "label": "Text Input",
             "provider": "lmstudio", "model": "test-model-a", "source": "abstractcore.gateway_runtime",
             "configured": true},
            voice,
            {"key": "output.voice", "kind": "output", "modality": "voice", "label": "Voice Output",
             "provider": "supertonic", "model": "supertonic-3", "source": "abstractcore.gateway_runtime",
             "configured": true}
        ]
    }))
}

pub fn voice_ok() -> Value {
    json!({"key": "input.voice", "kind": "input", "modality": "voice", "label": "Voice Input",
           "provider": "faster-whisper", "model": "base", "source": "abstractcore.gateway_runtime",
           "configured": true})
}

pub fn voice_missing() -> Value {
    json!({"key": "input.voice", "kind": "input", "modality": "voice", "label": "Voice Input",
           "provider": "huggingface", "model": "Systran/faster-whisper-base",
           "source": "abstractcore.gateway_runtime", "configured": true,
           "engine_missing": {"engine": "huggingface", "name": "huggingface",
                              "reason": "unknown AbstractVoice engine 'huggingface'", "install": null}})
}

pub fn workflows_screen(size: (i32, i32)) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(ui::SCREEN_WORKFLOWS);
    h.turns(2);
    h.store
        .workflows
        .set(Loadable::Ready(workflows_from_payload(&bundles_payload())));
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(
            &runtime_config_payload(),
        )));
    h.turns(3);
    h
}

pub fn routes_screen(size: (i32, i32), voice: Value) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.turns(2);
    h.store.routes.set(Loadable::Ready(voice_routes(voice)));
    h.turns(3);
    h
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_workflows() {
    for size in SIZES {
        let mut h = workflows_screen(size);
        h.shoot("workflows");
    }
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_multimodal_transcription() {
    for size in SIZES {
        let mut h = routes_screen(size, voice_ok());
        h.shoot("multimodal-transcription-ok");
        let mut h = routes_screen(size, voice_missing());
        h.shoot("multimodal-transcription-engine-missing");
    }
}
