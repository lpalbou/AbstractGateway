//! OpenAI API page (round 7 skeleton — the page body lands with its own
//! commit). Contract `gateway_openai_api_v1` (routes/core_endpoint.py).

use abstracttui::prelude::*;

use super::util::{line, span};
use super::Ctx;

/// The page's key hints (footer).
pub fn hints(_non_admin: bool) -> Vec<(&'static str, &'static str)> {
    vec![("r", "refresh")]
}

/// `r` on this page.
pub fn refresh(_ctx: &Ctx) {}

/// The page.
pub fn view(_cx: Scope, _ctx: &Ctx, t: &TokenSet) -> View {
    line(vec![span("OpenAI API", t.text)])
}
