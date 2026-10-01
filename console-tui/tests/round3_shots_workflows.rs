//! Round-3 buffer snapshot: the Workflows screen's archive confirm (`d`; DESIGN-v3 §5.3:
//! workflows are archived, never deleted).
//! `ROUND2_SHOTS_DIR=<dir> cargo test --test round3_shots_workflows -- --ignored`.

mod r2shots;

use abstractgateway_console::store::{workflows_from_payload, Loadable};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2shots::harness;
use serde_json::json;

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_workflows_archive_confirm() {
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.ui.screen.set(ui::SCREEN_WORKFLOWS);
    h.turns(2);
    h.store.workflows.set(Loadable::Ready(workflows_from_payload(&json!({
        "items": [
            {"bundle_id": "team-report", "bundle_version": "1.0.0", "source": "imported",
             "owner": {"kind": "gateway", "user_id": null}, "shipped": false, "available": true, "archived": false,
             "description": "Turns the week's notes into a one-page team report.",
             "entrypoints": [{"flow_id": "main", "name": "Team report", "interfaces": []}]}
        ]
    }))));
    h.turns(3);
    h.shoot("workflows");
    h.key(b"d");
    h.turns(2);
    h.shoot("workflows-archive-confirm");
}
