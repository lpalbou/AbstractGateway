//! Workspaces (ACCOUNTS, right after Accounts) — PARKED (round 10, Y1).
//!
//! The gateway moved workspaces into Accounts in round 9 (a gateway
//! posture modal and a per-account chooser on the web console, API
//! `/workspace/policy`, `/workspace/policy/{account|me}`,
//! `/workspace/effective/{account|me}`) and removed the routes this page
//! used to call (`/admin/user-workspace-policy`, `/workspace/policy/self`,
//! the runtime-config `workspace_*` keys). Until the terminal console
//! follows (R9.4), the page shows ONE sentence and sends NOTHING: no read,
//! no write, no verb. The sidebar entry stays.

use abstracttui::prelude::*;

use super::kit;
use super::Ctx;

/// The page's title (the sidebar entry's name).
pub const TITLE: &str = "Workspaces";

/// The one sentence the page shows.
pub const PARKED: &str = "Workspaces are managed from Accounts in the web console; the terminal console follows in the next update.";

/// The footer verbs: none — the page has no action.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let _ = ctx;
    Vec::new()
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let _ = ctx;
    let width = (abstracttui::app::use_viewport(cx).get().w - 4).max(20);
    Element::new()
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title(TITLE)
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                .child(kit::sentence(t, PARKED, width, t.text))
                .element(t)
                .build(),
        )
        .build()
}
