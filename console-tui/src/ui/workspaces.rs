//! Workspaces (ACCOUNTS, right after Accounts).
//!
//! The gateway moved workspaces into Accounts in round 9 (a gateway
//! posture modal and a per-account chooser on the web console, API
//! `/workspace/policy`, `/workspace/policy/{account|me}`,
//! `/workspace/effective/{account|me}`). Until the terminal console
//! follows (R9.4), the page shows ONE sentence for the policy and sends no
//! write and offers no verb.
//!
//! Round 12 (R12.1): the page also shows the host's COMMAND SANDBOX state,
//! the same state line as the web console (Accounts, next to "Eligible
//! workspaces"), read from `GET /workspace/policy` → `command_sandbox`:
//! "Commands sandboxed: macOS sandbox-exec" / "Commands refused: no sandbox
//! on this host" / "Unsandboxed commands allowed (flag)", with the web
//! tooltip's sentence below it (a terminal has no hover).

use abstracttui::prelude::*;
use serde_json::Value;

use super::kit;
use super::Ctx;
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The page's title (the sidebar entry's name).
pub const TITLE: &str = "Workspaces";

/// The one sentence the page shows for the policy.
pub const PARKED: &str = "Workspaces are managed from Accounts in the web console; the terminal console follows in the next update.";

/// The footer verbs: none — the page has no action (`r` re-reads, as everywhere).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let _ = ctx;
    Vec::new()
}

/// (state line, its sentence, warn?) for the command sandbox, from the
/// `GET /workspace/policy` slot. The line is the gateway's, verbatim.
pub fn sandbox_line(slot: &Loadable<Value>) -> (String, String, bool) {
    match slot {
        Loadable::Ready(v) => {
            let cs = v.get("command_sandbox");
            let line = cs.and_then(|c| c.get("line")).and_then(Value::as_str);
            let sentence = cs
                .and_then(|c| c.get("sentence"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let state = cs
                .and_then(|c| c.get("state"))
                .and_then(Value::as_str)
                .unwrap_or("");
            match line {
                Some(l) => (l.to_string(), sentence.to_string(), state != "sandboxed"),
                None => (
                    "Commands: this gateway does not report a command sandbox (an older gateway)"
                        .into(),
                    String::new(),
                    true,
                ),
            }
        }
        Loadable::Failed(e) => (format!("Commands: unavailable ({e})"), String::new(), true),
        Loadable::Loading | Loadable::NotAsked => (
            "Commands: reading GET /api/gateway/workspace/policy…".into(),
            String::new(),
            false,
        ),
    }
}

fn load(ctx: &Ctx) {
    let store = ctx.store;
    if store.conn.get_untracked().is_connected() {
        store.workspace_policy.set(Loadable::Loading);
        ctx.send(Cmd::LoadWorkspacePolicy);
    } else {
        store.workspace_policy.set(Loadable::NotAsked);
    }
}

/// `r` on the Workspaces page.
pub fn refresh(ctx: &Ctx) {
    load(ctx);
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    {
        let ctx_load = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected
                && store
                    .workspace_policy
                    .with_untracked(|a| matches!(a, Loadable::NotAsked))
            {
                load(&ctx_load);
            }
        });
    }
    let theme = use_theme(cx);
    let vp = abstracttui::app::use_viewport(cx);
    let width = (vp.get().w - 4).max(20);
    let state = dyn_view(LayoutStyle::column().gap(0), move || {
        let t = theme.get().tokens;
        let w = (vp.get().w - 4).max(20);
        let (line, sentence, warn) = sandbox_line(&store.workspace_policy.get());
        let mut views = vec![kit::sentence(
            &t,
            &line,
            w,
            if warn { t.warn } else { t.text },
        )];
        if !sentence.is_empty() {
            views.push(kit::sentence(&t, &sentence, w, t.text_muted));
        }
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .children(views)
            .build()
    });
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
                        .gap(1)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                .child(state)
                .child(kit::sentence(t, PARKED, width, t.text))
                .element(t)
                .build(),
        )
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn the_state_line_is_the_gateways_verbatim() {
        let slot = Loadable::Ready(json!({"policy": {}, "command_sandbox": {
            "state": "sandboxed", "line": "Commands sandboxed: macOS sandbox-exec",
            "sentence": "Every command a run starts is confined by the operating system to that run's workspaces."}}));
        let (line, sentence, warn) = sandbox_line(&slot);
        assert_eq!(line, "Commands sandboxed: macOS sandbox-exec");
        assert!(sentence.starts_with("Every command"));
        assert!(!warn);
        let refused = Loadable::Ready(
            json!({"command_sandbox": {"state": "refused", "line": "Commands refused: no sandbox on this host", "sentence": "x"}}),
        );
        let (line, _, warn) = sandbox_line(&refused);
        assert_eq!(line, "Commands refused: no sandbox on this host");
        assert!(warn);
        let flag = Loadable::Ready(
            json!({"command_sandbox": {"state": "unsandboxed", "line": "Unsandboxed commands allowed (flag)", "sentence": "y"}}),
        );
        assert_eq!(sandbox_line(&flag).0, "Unsandboxed commands allowed (flag)");
    }

    #[test]
    fn an_older_gateway_is_said_never_blank() {
        let (line, _, warn) = sandbox_line(&Loadable::Ready(json!({"policy": {}})));
        assert!(line.contains("does not report a command sandbox"));
        assert!(warn);
    }
}
