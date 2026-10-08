//! The round-15 app shell (DESIGN-TUI.md §2.1): header with clickable
//! items (resources widget → Resources, identity → Connection, ✦ Docs,
//! ☾/☼ theme), the navigation — a grouped left rail like the web sidebar
//! on wide terminals (≥ 120 x 32), a one-row clickable strip otherwise —
//! the page region, and the `?` keys panel. Navigation keys (←/→,
//! accelerators) live in `ui::root` (Capture phase).

use std::rc::Rc;

use abstracttui::prelude::*;
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

use super::w::action::{self, Action, On};
use super::w::paint::{fill_line, Ink};
use super::{
    nav_pos, screen_key, Ctx, NAV_GROUPS, NAV_ORDER, SCREENS, SCREEN_ABOUT, SCREEN_CONNECTION,
    SCREEN_KEYS_HINT, SCREEN_MODELS, SCREEN_RUNTIMES, SCREEN_SKILLS, SCREEN_WELCOME,
};
use crate::store::ConnPhase;

/// Rail width (items) — the separator column is extra.
pub const RAIL_W: i32 = 20;

/// Does the viewport get the left rail (else the top strip)?
pub fn wide(vp: Size) -> bool {
    vp.w >= 120 && vp.h >= 32
}

/// The page region's size for viewport `vp` (`banner` = pause banner rows).
pub fn page_size(vp: Size, banner: i32) -> Size {
    if wide(vp) {
        Size::new(vp.w - RAIL_W - 1, vp.h - 2 - banner)
    } else {
        Size::new(vp.w, vp.h - 3 - banner)
    }
}

/// The web sidebar tooltip of each screen (DESIGN-TUI §8.0).
pub fn nav_tip(i: usize) -> &'static str {
    match i {
        super::SCREEN_CONNECTION => "The gateway this console talks to.",
        super::SCREEN_USERS => "People who use this gateway and the entities that act on it",
        super::SCREEN_WORKFLOWS => "Bundles, versions, import and export",
        super::SCREEN_SKILLS => "Skills agents can load, and the MCP tool servers this gateway knows",
        super::SCREEN_RUNTIMES => "Each user's data plane: runs, flows, sessions and memory",
        super::SCREEN_APPS => "Browser apps (Flow, Code, Observer...): install, start, open",
        super::SCREEN_PROVIDERS => {
            "Local engines (Ollama, LM Studio, MLX...) and remote provider connections"
        }
        super::SCREEN_OPENAI => "Let apps use your models through one OpenAI-compatible address",
        super::SCREEN_CATALOG => "Browse, download and delete models that fit this machine",
        super::SCREEN_ROUTES => {
            "Which provider/model serves each capability (vision, audio, image...)"
        }
        super::SCREEN_MODELS => "Host resources: loaded models, memory and GPU, session caches",
        super::SCREEN_REVIEW => "Try any provider/model directly — text, image, audio, video",
        super::SCREEN_NETWORK => {
            "Who can reach this gateway (this computer, local network, internet) and its addresses"
        }
        super::SCREEN_WELCOME => {
            "Run the setup guide again: engines, default models, apps, network. Keeps your current choices unless you replace them."
        }
        super::SCREEN_ABOUT => "About AbstractGateway",
        _ => "",
    }
}

/// Is screen `i` shown in the navigation for this principal (the web hides
/// Runtimes and Skills & MCP from non-admins)?
pub fn nav_visible(ctx: &Ctx, i: usize) -> bool {
    let non_admin = ctx.store.conn.with(ConnPhase::is_known_non_admin);
    !(non_admin && (i == SCREEN_RUNTIMES || i == SCREEN_SKILLS))
}

/// Go to screen `i` (a nav click / accelerator): refused with the
/// sentence the digit keys use while the setup guide runs.
pub fn go(ctx: &Ctx, i: usize) {
    if ctx.ui.wizard.get_untracked() {
        ctx.store.notice.set(Some(format!(
            "screen jumps ({SCREEN_KEYS_HINT}) work in browse mode — in the guide Ctrl+N walks, Ctrl+G jumps to a step or leaves"
        )));
    } else if ctx.ui.screen.get_untracked() != i {
        ctx.ui.screen.set(i);
    }
}

/// One clickable nav row (rail) — a mouse target, not a Tab stop (the
/// keyboard has ←/→ and the accelerators).
fn rail_item(cx: Scope, ctx: &Ctx, t: &TokenSet, i: usize, active: bool, prefix: &str) -> View {
    let label = SCREENS[i];
    let key = screen_key(i).map(|k| k.to_string()).unwrap_or_default();
    let hovered = cx.signal(false);
    let c = ctx.clone();
    let (sel_fg, sel_bg, text, accent, faint) = (
        t.selection_fg,
        t.selection_bg,
        t.text,
        t.accent,
        t.text_faint,
    );
    let prefix = prefix.to_string();
    let el = Element::new()
        .style(LayoutStyle::line(1).shrink(0.0))
        .role(abstracttui::ui::Role::Button)
        .access_label(label)
        .hover_signal(hovered)
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Mouse(m) = ev {
                if matches!(m.kind, MouseKind::Down(MouseButton::Left)) {
                    ectx.stop_propagation();
                    go(&c, i);
                }
            }
        })
        .child(dyn_view(LayoutStyle::fill(), move || {
            let h = hovered.get();
            let (fg, bg) = if active {
                (sel_fg, Some(sel_bg))
            } else if h {
                (accent, None)
            } else {
                (text, None)
            };
            let pad = (RAIL_W
                - 1
                - abstracttui::text::width(&prefix)
                - abstracttui::text::width(label)
                - abstracttui::text::width(&key))
            .max(1);
            fill_line(
                LayoutStyle::fill(),
                vec![
                    Ink::new(format!("{prefix}{label}"), fg).bold_if(active),
                    Ink::new(" ".repeat(pad as usize), fg),
                    Ink::new(key.clone(), if active { fg } else { faint }),
                ],
                bg,
            )
        }));
    super::w::tip::with_tip(cx, el, nav_tip(i).to_string()).build()
}

trait BoldIf {
    fn bold_if(self, b: bool) -> Self;
}
impl BoldIf for Ink {
    fn bold_if(self, b: bool) -> Ink {
        if b {
            self.bold()
        } else {
            self
        }
    }
}

/// The left rail: Connection-less groups, then Connection / Setup / About
/// at the foot.
pub fn rail(cx: Scope, ctx: &Ctx, t: &TokenSet, height: i32) -> View {
    let screen = ctx.ui.screen.get();
    let wizard = ctx.ui.wizard.get();
    let mut col = Element::new().style(
        LayoutStyle::column()
            .width(Dimension::Cells(RAIL_W))
            .shrink(0.0),
    );
    let mut used = 0;
    for (name, members) in NAV_GROUPS.iter() {
        let shown: Vec<usize> = members
            .iter()
            .copied()
            .filter(|i| nav_visible(ctx, *i))
            .collect();
        if shown.is_empty() {
            continue;
        }
        col = col.child(fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(format!(" {name}"), t.text_muted).bold()],
            None,
        ));
        used += 1;
        for i in shown {
            col = col.child(rail_item(cx, ctx, t, i, i == screen, "  "));
            used += 1;
        }
    }
    let foot = [SCREEN_CONNECTION, SCREEN_WELCOME, SCREEN_ABOUT];
    let gap = (height - used - foot.len() as i32 - if wizard { 1 } else { 0 }).max(1);
    col = col.child(
        Element::new()
            .style(
                LayoutStyle::default()
                    .height(Dimension::Cells(gap))
                    .shrink(1.0),
            )
            .build(),
    );
    if wizard {
        col = col.child(fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(" setup guide running", t.warn)],
            None,
        ));
    }
    for i in foot {
        let prefix = if i == SCREEN_WELCOME { " ⚑ " } else { "   " };
        col = col.child(rail_item(cx, ctx, t, i, i == screen, prefix));
    }
    col.build()
}

/// The narrow top strip: the active screen's group caption, then the
/// titles windowed around the active one, `‹ ›` as click targets.
pub fn strip(cx: Scope, ctx: &Ctx, t: &TokenSet, width: i32) -> View {
    let screen = ctx.ui.screen.get();
    let order: Vec<usize> = NAV_ORDER
        .iter()
        .copied()
        .filter(|i| nav_visible(ctx, *i))
        .collect();
    let pos = order.iter().position(|i| *i == screen).unwrap_or(0);
    let group = super::nav_group(screen).unwrap_or("");
    let cap = if group.is_empty() {
        String::new()
    } else {
        format!(" {group} ")
    };
    let cap_w = abstracttui::text::width(&cap);
    let w_of = |i: usize| abstracttui::text::width(SCREENS[i]) + 2;
    let budget = width - cap_w - 4;
    // Window: start at the active one, grow left while it fits, then right.
    let (mut lo, mut hi) = (pos, pos);
    let mut used = w_of(order[pos]);
    loop {
        let mut grew = false;
        if hi + 1 < order.len() && used + w_of(order[hi + 1]) <= budget {
            hi += 1;
            used += w_of(order[hi]);
            grew = true;
        }
        if lo > 0 && used + w_of(order[lo - 1]) <= budget {
            lo -= 1;
            used += w_of(order[lo]);
            grew = true;
        }
        if !grew {
            break;
        }
    }
    let mut row = Element::new().style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0));
    row = row.child(fill_line(
        LayoutStyle::default()
            .width(Dimension::Cells(cap_w))
            .height(Dimension::Cells(1)),
        vec![Ink::new(cap, t.text_muted).bold()],
        None,
    ));
    let arrow = |cx: Scope, glyph: &str, target: Option<usize>| -> View {
        let c = ctx.clone();
        let mut a = Action::label("nav", glyph.to_string());
        if target.is_none() {
            a = a.refused(Some("no more screens this way".into()));
        }
        let tip_target = target.map(|i| SCREENS[i]).unwrap_or("");
        a = a.tooltip(tip_target.to_string());
        let mut tt = *t;
        tt.surface_raised = t.bg;
        action::button(cx, &tt, &a, On::Page, false, move || {
            if let Some(i) = target {
                go(&c, i);
            }
        })
    };
    row = row.child(arrow(cx, "‹", lo.checked_sub(1).map(|k| order[k])));
    for &i in &order[lo..=hi] {
        let active = i == screen;
        let a = Action::label("nav", SCREENS[i]).tooltip(nav_tip(i).to_string());
        let c = ctx.clone();
        let mut tt = *t;
        if active {
            tt.surface_raised = t.selection_bg;
            tt.text = t.selection_fg;
        } else {
            tt.surface_raised = t.bg;
        }
        row = row.child(action::button(cx, &tt, &a, On::Page, false, move || {
            go(&c, i)
        }));
    }
    row = row.child(arrow(cx, "›", order.get(hi + 1).copied()));
    row.build()
}

/// The header row (clickable items).
pub fn header(cx: Scope, ctx: &Ctx, t: &TokenSet, vp: Size) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let conn = store.conn.get();
    let mode = if ui.wizard.get() { "wizard" } else { "browse" };
    let short = vp.w < 120;
    let mut row = Element::new().style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0));
    let brand = format!(" AbstractGateway · {mode}  ");
    row = row.child(fill_line(
        LayoutStyle::default()
            .width(Dimension::Cells(abstracttui::text::width(&brand)))
            .height(Dimension::Cells(1))
            .shrink(0.0),
        vec![
            Ink::new(" AbstractGateway", t.accent).bold(),
            Ink::new(format!(" · {mode}  "), t.text_muted),
        ],
        None,
    ));
    let mut tt = *t;
    tt.surface_raised = t.bg;
    if conn.is_connected() {
        let data = store.host_state.with(|h| h.ready().cloned());
        let err = store.host_widget_error.get();
        let wv = super::resources_widget::view(data.as_ref(), err.as_deref());
        let mut tip = wv.tip.join(" · ");
        tip.push_str(" · Click to open Resources.");
        let a = Action::label("resources", wv.line(short)).tooltip(tip);
        let c = ctx.clone();
        let mut t2 = tt;
        t2.text = if wv.stale { t.text_faint } else { t.text_muted };
        row = row.child(action::button(cx, &t2, &a, On::Page, false, move || {
            go(&c, SCREEN_MODELS)
        }));
    } else {
        let u = ui.conn_url.get();
        row = row.child(fill_line(
            LayoutStyle::default()
                .width(Dimension::Cells(abstracttui::text::width(&u).min(44) + 1))
                .height(Dimension::Cells(1)),
            vec![Ink::new(u, t.text_muted)],
            None,
        ));
    }
    row = row.child(
        Element::new()
            .style(LayoutStyle::default().grow(1.0))
            .build(),
    );
    let (dot, dot_ink, ident) = match &conn {
        ConnPhase::NotConnected => ("○", t.text_muted, "not connected".to_string()),
        ConnPhase::Probing => ("◌", t.info, "probing…".to_string()),
        ConnPhase::Verifying(id) => (
            "◌",
            t.warn,
            format!("{}@{} — verifying…", id.user_id, id.tenant_id),
        ),
        ConnPhase::Connected(id) => (
            "●",
            t.ok,
            format!(
                "{}@{}{}",
                id.user_id,
                id.tenant_id,
                if id.admin { " (admin)" } else { "" }
            ),
        ),
        ConnPhase::Unauthorized(_) => ("●", t.error, "unauthorized".to_string()),
        ConnPhase::Forbidden(_) => ("●", t.error, "forbidden".to_string()),
        ConnPhase::NotGateway(_, _) => ("●", t.warn, "not a gateway?".to_string()),
        ConnPhase::Unreachable(_) => ("○", t.error, "unreachable".to_string()),
    };
    row = row.child(fill_line(
        LayoutStyle::default()
            .width(Dimension::Cells(2))
            .height(Dimension::Cells(1))
            .shrink(0.0),
        vec![Ink::new(dot, dot_ink)],
        None,
    ));
    let c = ctx.clone();
    let a = Action::label("identity", ident).tooltip("Signed in as — open Connection");
    row = row.child(action::button(cx, &tt, &a, On::Page, false, move || {
        go(&c, SCREEN_CONNECTION)
    }));
    if conn.is_connected() {
        let c = ctx.clone();
        let a = Action::label("docs", if vp.w < 100 { "✦" } else { "✦ Docs" })
            .tooltip("Docs assistant  (F2)");
        row = row.child(action::button(cx, &tt, &a, On::Page, false, move || {
            super::docs::open(&c, cx);
        }));
    }
    row = row.child(theme_button(cx, &tt));
    row.build()
}

/// The ☾/☼ appearance switch (header; the status bar under 90 columns).
pub fn theme_button(cx: Scope, t: &TokenSet) -> View {
    let mut tt = *t;
    tt.surface_raised = t.bg;
    let dark = abstracttui::app::current_theme().dark;
    let a = Action::label("theme", if dark { "☾" } else { "☼" })
        .tooltip("Appearance: switch the light / dark theme  (Ctrl+T)");
    action::button(cx, &tt, &a, On::Page, false, super::w::theme::flip)
}

/// The `?` keys panel: every key of the current screen and the globals.
pub fn open_keys(ctx: &Ctx, cx: Scope) {
    let pairs = super::screen_hint_pairs(ctx);
    let title = format!("Keys — {}", SCREENS[ctx.ui.screen.get_untracked()]);
    let n = pairs.len() as i32;
    super::w::FormModal::new(title)
        .lead("Everything here also works with the mouse: click a button, a toggle or a row.")
        .size(72, (n + 10).min(40))
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let t = use_theme(mcx).get().tokens;
            let kw = pairs
                .iter()
                .map(|(k, _)| abstracttui::text::width(k))
                .max()
                .unwrap_or(4)
                .min(w / 3)
                + 2;
            let mut col = Element::new().style(LayoutStyle::column().grow(1.0));
            for (k, v) in &pairs {
                col = col.child(fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![
                        Ink::new(format!("{k:<width$}", width = kw as usize), t.accent),
                        Ink::new(v.clone(), t.text),
                    ],
                    None,
                ));
            }
            let close2 = close.clone();
            let a = Action::label("close", "Close");
            col.child(super::w::form::button_row(vec![action::button(
                mcx,
                &t,
                &a,
                On::Raised,
                true,
                move || close2(),
            )]))
            .build()
        });
}

/// `Rc` alias used by screens wiring row actions.
pub type ActionCb = Rc<dyn Fn(&str, &'static str)>;

/// Position helper re-export (tests).
pub fn position(i: usize) -> usize {
    nav_pos(i)
}
