//! Setup — the first-run guide's welcome step, and the first-run
//! lifecycle that the web console's setup guide owns (`console.py`,
//! `FIRST_RUN_STEPS`, `maybeOpenFirstRun`, `completeFirstRun`).
//!
//! * At connect the console reads `GET /host/first-run`; without an
//!   explicit `--wizard`/`--browse`, the guide stays open only for an
//!   admin whose gateway has not completed its first run (the web
//!   `firstRunShouldAutoOpen` rule), browse mode otherwise.
//! * The welcome step summarises this computer from `GET /host/state`
//!   (the web's six tiles, in its words) and lists what the guide sets up.
//! * Finish (Review) and Skip setup (Review, or Ctrl+G here and on every
//!   step) `POST /host/first-run {"outcome"}` exactly like the web guide,
//!   verified by the follow-up GET and journaled; the guide closes only
//!   on a verified write. Leaving with Ctrl+G WITHOUT recording is the
//!   web guide's Escape: it opens again next start.
//!
//! R15: the page is the web guide's welcome step with real controls —
//! the head's Setup guide / Go to a step / Skip setup / Next / ↻, the six
//! tiles, the three "Go to …" cards, the recommended set's Use
//! recommended defaults / Download all (and the second pass, Replace mine
//! too). The guide menu (Ctrl+G) is a dialog of buttons (the web
//! stepper); the finish row's Finish / Skip setup / Start at login are
//! buttons and a Toggle.

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

use super::util::{line, span, span_bold, wrap_text};
use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Toggle};
use super::{Ctx, WIZARD_STEPS};
use crate::api::firstrun::{
    can_download_all, first_run_auto_wizard, route_title, route_what, PlanRow, WelcomeSummary,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The web guide's per-step copy, keyed by the TUI screen that carries
/// the step (title, one-line lede).
pub fn step_copy(screen: usize) -> (&'static str, &'static str) {
    match screen {
        super::SCREEN_CONNECTION => ("Connection", "Sign in to the gateway (the terminal needs a token)."),
        super::SCREEN_WELCOME => ("Welcome", "Check this computer."),
        // Round 7: the guide's engines step is the Providers page (its
        // local engines, then the cloud providers' keys) — the web
        // guide's engines lede, word for word.
        super::SCREEN_PROVIDERS => (
            "Local engines",
            "Engines run AI models on this computer. Install one if you want local models; cloud providers only need an API key (Providers tab).",
        ),
        super::SCREEN_ROUTES => (
            "Default model",
            "The recommended set is sized for this computer: set it up in one step.",
        ),
        super::SCREEN_CATALOG => ("Models", "Or pick any model that fits."),
        super::SCREEN_APPS => (
            "Apps",
            "Apps that work with this gateway: build workflows, code with an agent, watch runs, talk to your entities.",
        ),
        super::SCREEN_REVIEW => ("Done", "Test a model if you like, then Finish."),
        _ => ("", ""),
    }
}

/// The first-run effects, installed once at root.
pub fn install(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let ui = ctx.ui;

    // Read the first-run state whenever a connection lands on a gateway
    // whose state is not known (a reconnect resets it to NotAsked).
    {
        let ctx = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected && matches!(store.first_run.get(), Loadable::NotAsked) {
                store.first_run.set(Loadable::Loading);
                ctx.send(Cmd::LoadFirstRun);
            }
        });
    }

    // The boot decision, ONCE per session (the web decides once per page
    // load). An explicit --wizard / --browse wins; so does an operator
    // who already chose a mode (Finish, Ctrl+G) before the read landed.
    cx.effect(move || {
        let fr = store.first_run.get();
        if ui.first_run_decided.get_untracked() {
            return;
        }
        let admin = store.conn.with_untracked(|c| match c {
            ConnPhase::Connected(id) => Some(id.admin),
            _ => None,
        });
        let Some(admin) = admin else {
            return;
        };
        match fr {
            Loadable::Ready(st) => {
                ui.first_run_decided.set(true);
                if ui.mode_forced.get_untracked() {
                    return;
                }
                let wizard = first_run_auto_wizard(&st, admin);
                if ui.wizard.get_untracked() != wizard {
                    ui.wizard.set(wizard);
                }
                // The web guide opens on its welcome step; the terminal
                // had to sign in first, so it continues from there.
                if wizard && ui.screen.get_untracked() == super::SCREEN_CONNECTION {
                    ui.screen.set(super::SCREEN_WELCOME);
                }
            }
            Loadable::Failed(e) => {
                ui.first_run_decided.set(true);
                if ui.mode_forced.get_untracked() {
                    return;
                }
                // The web console does not open the guide when this read
                // fails; neither does the terminal — and it says why.
                ui.wizard.set(false);
                store.notice.set(Some(format!(
                    "first-run state unreadable ({e}) — browse mode; Ctrl+G opens the setup guide"
                )));
            }
            _ => {}
        }
    });

    // Finish / Skip outcomes come back through the write-done lane; the
    // guide closes only when the verifying GET agreed.
    cx.effect(move || {
        let Some((fid, outcome)) = ui.write_done.get() else {
            return;
        };
        let Some((pending, asked)) = ui.first_run_pending.get_untracked() else {
            return;
        };
        if fid != pending {
            return;
        }
        ui.write_done.set(None);
        ui.first_run_pending.set(None);
        match outcome {
            Ok(_) => {
                ui.first_run_error.set(None);
                ui.first_run_decided.set(true);
                ui.wizard.set(false);
                store.notice.set(Some(format!(
                    "setup {} — recorded on the gateway; browse with 1-9, 0 and A, Ctrl+G reopens the guide",
                    if asked == "skipped" { "skipped" } else { "finished" }
                )));
            }
            Err(e) => ui.first_run_error.set(Some(e)),
        }
    });
}

fn admin_now(ctx: &Ctx) -> bool {
    ctx.store.conn.with_untracked(|c| match c {
        ConnPhase::Connected(id) => id.admin,
        _ => false,
    })
}

/// Finish / Skip setup: `POST /host/first-run {"outcome"}` (admin). A
/// non-admin cannot record it — the guide just closes, and says so.
pub fn record_outcome(ctx: &Ctx, outcome: &str) {
    if ctx.ui.first_run_pending.get_untracked().is_some() {
        ctx.store.notice.set(Some(
            "already recording the first-run outcome — one moment".into(),
        ));
        return;
    }
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        ctx.store.notice.set(Some(
            "not connected — the first-run outcome is recorded on the gateway; connect first"
                .into(),
        ));
        return;
    }
    if !admin_now(ctx) {
        ctx.ui.first_run_decided.set(true);
        ctx.ui.wizard.set(false);
        ctx.store.notice.set(Some(
            "guide closed — recording the first-run outcome is admin-only, so nothing was recorded"
                .into(),
        ));
        return;
    }
    let fid = crate::worker::next_form_id();
    ctx.ui.first_run_error.set(None);
    ctx.ui
        .first_run_pending
        .set(Some((fid, outcome.to_string())));
    ctx.send(Cmd::CompleteFirstRun {
        outcome: outcome.to_string(),
        form_id: Some(fid),
    });
}

// ------------------------------------------------------------ the web's words

/// The step's title, hint and admin lede (`FIRST_RUN_STEP_COPY.welcome`).
pub const TITLE: &str = "Welcome to your gateway";
pub const HINT: &str = "Check this computer";
pub const ADMIN_LEDE: &str = "Your gateway is running on this computer and you are signed in as its admin. The next steps get you to a working model. Every step is optional.";
/// The footer's Skip setup tooltip (the web's).
pub const SKIP_TIP: &str = "Close the guide and do not open it automatically again";
/// The browse-mode "Setup guide" button (the nav's sentence).
pub const GUIDE_TIP: &str = "Run the setup guide again: engines, default models, apps, network. Keeps your current choices unless you replace them.";
pub const SETS_UP: &str = "What this guide sets up";
pub const SETS_UP_SUB: &str = "Each step takes a minute; skip any of them.";
pub const APPLY: &str = "Use recommended defaults";
pub const DOWNLOAD_ALL: &str = "Download all";
/// The guide's three middle steps as the welcome cards show them:
/// (TUI screen, card title, lede, "Go to …" label).
pub fn step_cards() -> [(usize, &'static str, &'static str, &'static str); 3] {
    [
        (
            super::SCREEN_PROVIDERS,
            "Local engines",
            "Engines run AI models on this computer. Install one if you want local models; cloud providers only need an API key (Providers tab).",
            "Go to local engines",
        ),
        (
            super::SCREEN_ROUTES,
            "Choose your default model",
            "The recommended set is sized for this computer: set it up in one click, or pick any model that fits.",
            "Go to default model",
        ),
        (
            super::SCREEN_APPS,
            "Apps",
            "Apps that work with this gateway: build workflows, code with an agent, watch runs, talk to your entities.",
            "Go to apps",
        ),
    ]
}

/// The page head's buttons: browse = Setup guide (admins); the guide =
/// Go to a step, Skip setup, Next; ↻ always.
pub fn head_actions(wizard: bool, admin: bool) -> Vec<Action> {
    let mut out = Vec::new();
    if wizard {
        out.push(
            Action::label("steps", "Go to a step")
                .tooltip("Go to a step, leave the guide, or skip setup  (Ctrl+G)"),
        );
        if admin {
            out.push(Action::label("skip", "Skip setup").tooltip(SKIP_TIP));
        }
        out.push(Action::label("next", "Next").tooltip("The next step  (Ctrl+N)"));
    } else if admin {
        out.push(Action::label("guide", "Setup guide").tooltip(format!("{GUIDE_TIP}  (Ctrl+G)")));
    }
    out.push(
        Action::label("reload", "↻")
            .key('r')
            .tooltip("Read this computer and the recommended set again"),
    );
    out
}

/// The recommended set's buttons (admins; Download all while a model
/// of the set is absent and no Download all runs).
pub fn recommended_actions(admin: bool, can_download: bool) -> Vec<Action> {
    if !admin {
        return Vec::new();
    }
    let mut out = vec![Action::label("apply", APPLY).key('a').tooltip(
        "Set the recommended models for every capability this computer can run; choices you made are kept",
    )];
    if can_download {
        out.push(
            Action::label("download_all", DOWNLOAD_ALL)
                .key('D')
                .tooltip("Download every recommended model this computer does not have yet"),
        );
    }
    out
}

/// The footer's verbs on the Setup page.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let admin = ctx.store.conn.with(|c| match c {
        ConnPhase::Connected(id) => id.admin,
        _ => false,
    });
    let can = ctx.store.availability.with(|a| {
        a.ready()
            .map(|a| can_download_all(&a.plan, ctx.store.download_group.get().as_ref()))
            .unwrap_or(false)
    });
    let mut out: Vec<(&'static str, &'static str)> = Vec::new();
    for a in recommended_actions(admin, can) {
        if let Some(k) = a.key {
            out.push((
                if k == 'a' { "a" } else { "D" },
                if k == 'a' { APPLY } else { DOWNLOAD_ALL },
            ));
        }
    }
    out.push(("r", "refresh"));
    out
}

/// Ctrl+G, anywhere: browse → reopen the guide at its welcome step (the
/// web "Setup guide" button); the guide → the stepper dialog: every step
/// is a button, then Leave for now / Skip setup.
pub fn guide_key(ctx: &Ctx, cx: Scope) {
    let ui = ctx.ui;
    if !ui.wizard.get_untracked() {
        open_guide(ctx);
        return;
    }
    open_steps(ctx, cx);
}

/// Browse → the guide, at its welcome step (admins: the web hides the
/// "Setup guide" button for a non-admin).
fn open_guide(ctx: &Ctx) {
    let ui = ctx.ui;
    if ctx.store.conn.with_untracked(ConnPhase::is_known_non_admin) {
        super::util::admin_gate(&ctx.store, "the setup guide");
        return;
    }
    ui.first_run_decided.set(true);
    ui.wizard.set(true);
    ui.screen.set(super::SCREEN_WELCOME);
    ctx.store.notice.set(Some(
        "setup guide — Ctrl+N walks it, Ctrl+G jumps to a step, leaves or skips it".into(),
    ));
}

/// Leave the guide without recording anything (the web guide's Escape).
fn leave_guide(ctx: &Ctx) {
    ctx.ui.first_run_decided.set(true);
    ctx.ui.wizard.set(false);
    ctx.store.notice.set(Some(
        "left the setup guide (not recorded) — Ctrl+G reopens it".into(),
    ));
}

/// The stepper dialog's buttons (the web's sidebar steps + footer).
pub fn steps_actions(current: usize, admin: bool) -> Vec<Action> {
    let mut out: Vec<Action> = WIZARD_STEPS
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let here = if *s == current {
                "  (you are here)"
            } else {
                ""
            };
            Action::label(
                STEP_IDS[i.min(STEP_IDS.len() - 1)],
                format!("{}. {}{here}", i + 1, step_copy(*s).0),
            )
        })
        .collect();
    out.push(
        Action::label("leave", "Leave for now")
            .tooltip("Nothing is recorded: the guide opens again at the next start"),
    );
    if admin {
        out.push(Action::label("skip", "Skip setup").tooltip(SKIP_TIP));
    }
    out
}

const STEP_IDS: [&str; 8] = [
    "step0", "step1", "step2", "step3", "step4", "step5", "step6", "step7",
];

/// The guide's stepper as a dialog of buttons.
fn open_steps(ctx: &Ctx, cx: Scope) {
    let admin = admin_now(ctx);
    let current = ctx.ui.screen.get_untracked();
    let c = ctx.clone();
    super::w::FormModal::new("Setup guide")
        .lead("Go to a step, or leave the guide.")
        .size(64, 8 + WIZARD_STEPS.len() as i32 + 3)
        .open(ctx, cx, move |mcx, close, _guard, _inner_w| {
            let t = use_theme(mcx).get().tokens;
            let mut col = Element::new().style(LayoutStyle::column().gap(0));
            let mut foot = Vec::new();
            for (i, a) in steps_actions(current, admin).into_iter().enumerate() {
                let (c2, close2) = (c.clone(), close.clone());
                let id = a.id;
                let b = button(mcx, &t, &a, On::Raised, true, move || {
                    close2();
                    match id {
                        "leave" => leave_guide(&c2),
                        "skip" => record_outcome(&c2, "skipped"),
                        _ => {
                            if let Some(step) = WIZARD_STEPS.get(i).copied() {
                                goto_step(&c2, step);
                            }
                        }
                    }
                });
                if i < WIZARD_STEPS.len() {
                    col = col.child(
                        Element::new()
                            .style(LayoutStyle::row().h(1).shrink(0.0))
                            .child(b)
                            .build(),
                    );
                } else {
                    foot.push(b);
                }
            }
            foot.push(button(
                mcx,
                &t,
                &Action::label("close", "Close"),
                On::Raised,
                true,
                move || close(),
            ));
            col.child(super::w::fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![],
                None,
            ))
            .child(super::w::form::button_row(foot))
            .build()
        });
}

/// Open guide step `step` directly (the web stepper's `firstRunGoto`).
/// The one gate is the terminal's own: leaving Connection needs a
/// sign-in (or the explicit offline choice), as with Ctrl+N.
pub fn goto_step(ctx: &Ctx, step: usize) {
    let signed_in = ctx.store.conn.with_untracked(ConnPhase::is_connected);
    if step != super::SCREEN_CONNECTION && !signed_in && !ctx.ui.offline_ok.get_untracked() {
        ctx.store.notice.set(Some(
            "sign in on the Connection step first (or choose Continue offline there)".into(),
        ));
        return;
    }
    ctx.ui.screen.set(step);
}

/// `r` on the Setup step: the summary, the first-run state, and the
/// recommended set (the routes + the weights' availability) again.
pub fn refresh(ctx: &Ctx) {
    ctx.store.welcome.set(Loadable::Loading);
    ctx.store.first_run.set(Loadable::Loading);
    ctx.send(Cmd::LoadWelcome);
    ctx.send(Cmd::LoadFirstRun);
    ctx.store.routes.set(Loadable::Loading);
    ctx.store.availability.set(Loadable::Loading);
    ctx.send(Cmd::LoadRoutes);
    ctx.send(Cmd::LoadAvailability);
}

/// The recommended set's sentence under its keys — the web guide's model
/// step, word for word.
pub const RECOMMENDED_NOTE: &str = "Sets the recommended models for text, voice, transcription, images and video, where this computer can run them. Choices you already made are kept.";

/// "Text model now: <provider> · <model>" or "No text model is set yet."
/// (the web guide's section subtitle): `output.text`, else `input.text`.
pub fn text_model_now(routes: Option<&crate::store::RoutesData>) -> String {
    let rows = routes.map(|r| r.rows.as_slice()).unwrap_or(&[]);
    let pick = rows
        .iter()
        .find(|r| r.key == "output.text")
        .or_else(|| rows.iter().find(|r| r.key == "input.text"));
    match pick.and_then(|r| Some((r.provider.clone()?, r.model.clone()?))) {
        Some((p, m)) if !p.is_empty() && !m.is_empty() => format!("Text model now: {p} · {m}"),
        _ => "No text model is set yet.".to_string(),
    }
}

/// One recommended route as the web card says it, in lines:
/// `<title> · <status>`, `<engine> · <model>`, the blurb, then the card's
/// alerts (GPU limit, AbstractCore's warning, the missing engine).
pub fn plan_card_lines(r: &PlanRow) -> Vec<(bool, String, &'static str)> {
    let engine = r
        .route_provider
        .clone()
        .unwrap_or_else(|| r.provider.clone());
    let model = r.route_model.clone().unwrap_or_else(|| r.artifact.clone());
    let mut out = vec![
        (
            false,
            format!("{} · {}", route_title(&r.route), r.status_label()),
            match r.status.as_str() {
                "installed" => "ok",
                "absent" => "warn",
                _ => "muted",
            },
        ),
        (true, format!("{engine} · {model}"), "text"),
    ];
    let what = route_what(&r.route);
    if !what.is_empty() {
        out.push((true, what.to_string(), "muted"));
    }
    if let Some(g) = r.gpu_limit_text() {
        let mut c = g.chars();
        let g = match c.next() {
            Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
            None => g,
        };
        out.push((true, g, "info"));
    }
    if let Some(w) = &r.warning {
        out.push((true, w.clone(), "warn"));
    }
    if let Some(m) = &r.engine_missing {
        // The web's engineMissingMarkup sentence.
        let mut text = format!("Engine missing: {}", m.reason);
        if let Some(cmd) = &m.install {
            text.push_str(&format!(" — install: {cmd}"));
        }
        if m.engine_row.is_some() {
            text.push_str(" (Providers tab, Local providers: Install)");
        }
        out.push((true, text, "warn"));
    }
    out
}

/// The page width the content lays out in.
fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// Use recommended defaults: the web button applies at once (never over
/// a route you configured); the second pass is offered after.
fn apply_recommended(ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "applying the recommended defaults") {
        return;
    }
    ctx.send(Cmd::ApplyRecommendedRoutes { force: false });
}

/// Download all: one parent job for the whole recommended set (the web
/// starts it at once).
fn download_all(ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "downloading the recommended models") {
        return;
    }
    let plan = ctx
        .store
        .availability
        .with_untracked(|a| a.ready().map(|a| a.plan.clone()))
        .unwrap_or_default();
    let group = ctx.store.download_group.get_untracked();
    if !can_download_all(&plan, group.as_ref()) {
        super::w::tip::say(
            "nothing to download — no recommended model is reported absent on this host",
        );
        return;
    }
    ctx.send(Cmd::DownloadRecommended);
}

/// The "Recommended for this computer" block (the web guide's model
/// step): every recommended route, the current text model, the buttons,
/// and the second pass when the first kept something.
fn recommended(cx: Scope, ctx: &Ctx, t: &TokenSet, width: i32) -> View {
    let store = ctx.store;
    let admin = admin_now(ctx);
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    col = col.child(super::w::section(t, "Recommended for this computer"));
    col = col.child(sentence(
        t,
        &text_model_now(store.routes.get().ready()),
        width,
        t.text_muted,
    ));
    match store.availability.get() {
        Loadable::Ready(a) if a.plan.is_empty() => {
            col = col.child(sentence(
                t,
                "This gateway reported no recommended downloads.",
                width,
                t.text_muted,
            ));
        }
        Loadable::Ready(a) => {
            for r in &a.plan {
                for (indent, text, tone) in plan_card_lines(r) {
                    let ink = match tone {
                        "ok" => t.ok,
                        "warn" => t.warn,
                        "info" => t.info,
                        "muted" => t.text_muted,
                        _ => t.text,
                    };
                    if indent {
                        for l in wrap_text(&text, (width - 2).max(10) as usize) {
                            col = col.child(line(vec![span(format!("  {l}"), ink)]));
                        }
                    } else {
                        for l in wrap_text(&text, width.max(10) as usize) {
                            col = col.child(line(vec![span_bold(l, ink)]));
                        }
                    }
                }
            }
            let group = store.download_group.get();
            if let Some(g) = group.as_ref() {
                col = col.child(sentence(t, &g.message, width, t.info));
            }
            let mut buttons = Vec::new();
            for a in recommended_actions(admin, can_download_all(&a.plan, group.as_ref())) {
                let c = ctx.clone();
                let id = a.id;
                buttons.push(button(cx, t, &a, On::Page, true, move || match id {
                    "apply" => apply_recommended(&c),
                    _ => download_all(&c),
                }));
            }
            if !buttons.is_empty() {
                col = col.child(super::w::form::button_row(buttons));
            }
            col = col.child(sentence(t, RECOMMENDED_NOTE, width, t.text_muted));
        }
        Loadable::Loading | Loadable::NotAsked => {
            col = col.child(sentence(
                t,
                "Checking the recommended starter models...",
                width,
                t.info,
            ));
        }
        Loadable::Failed(e) => {
            col = col.child(sentence(
                t,
                &format!("The recommended models could not be read: {e}"),
                width,
                t.warn,
            ));
        }
    }
    col.build()
}

/// The second pass after Use recommended defaults (the web's button under
/// the outcome): pinned under the head so it is seen without scrolling.
fn followup_view(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    width: i32,
    followup: Signal<Option<String>>,
) -> View {
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    if let Some(label) = followup.get() {
        let what = if label == "Replace mine too" {
            "The recommended routes were applied; routes you configured were kept."
        } else {
            "The recommended routes were applied; a configured route this computer cannot run was left in place."
        };
        col = col.child(sentence(t, what, width, t.text_muted));
        let c = ctx.clone();
        let a = Action::label("again", format!("♻ {label}"));
        col = col.child(super::w::form::button_row(vec![button(
            cx,
            t,
            &a,
            On::Page,
            true,
            move || {
                followup.set(None);
                c.send(Cmd::ApplyRecommendedRoutes { force: true });
            },
        )]));
    }
    col.build()
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    // The recommended set reads the routes and the weights' availability
    // (the Multimodal page's reads) once per connection.
    {
        let ctx_l = ctx.clone();
        cx.effect(move || {
            if !store.conn.with(ConnPhase::is_connected) {
                return;
            }
            if store
                .routes
                .with_untracked(|r| matches!(r, Loadable::NotAsked))
            {
                store.routes.set(Loadable::Loading);
                ctx_l.send(Cmd::LoadRoutes);
            }
            if store
                .availability
                .with_untracked(|r| matches!(r, Loadable::NotAsked))
            {
                store.availability.set(Loadable::Loading);
                ctx_l.send(Cmd::LoadAvailability);
            }
        });
    }
    // The web's second pass after "Use recommended defaults": the forced
    // apply under its own label, as a button under the outcome.
    let followup = cx.signal(Option::<String>::None);
    cx.effect(move || {
        let Some(label) = store.apply_followup.get() else {
            return;
        };
        if ui.screen.get_untracked() != super::SCREEN_WELCOME {
            return;
        }
        store.apply_followup.set(None);
        followup.set(Some(label));
    });
    let keys_ctx = ctx.clone();
    let head_ctx = ctx.clone();
    let body_ctx = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .focusable()
        .autofocus()
        .on(Phase::Bubble, move |ectx, ev| {
            let UiEvent::Key(k) = ev else { return };
            if k.mods.0 != 0 && !matches!(k.key, Key::Char('D')) {
                return;
            }
            match k.key {
                Key::Char('a') => apply_recommended(&keys_ctx),
                Key::Char('D') => download_all(&keys_ctx),
                _ => return,
            }
            ectx.stop_propagation();
        })
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |hcx| {
                let t = tt;
                let w = page_w(hcx);
                let wizard = ui.wizard.get();
                let admin = head_ctx.store.conn.with(|c| match c {
                    ConnPhase::Connected(id) => id.admin,
                    _ => false,
                });
                let mut buttons = Vec::new();
                for a in head_actions(wizard, admin) {
                    let c = head_ctx.clone();
                    let wd = a.width();
                    let id = a.id;
                    buttons.push((
                        button(hcx, &t, &a, On::Page, true, move || match id {
                            "guide" => open_guide(&c),
                            "steps" => open_steps(&c, cx),
                            "skip" => record_outcome(&c, "skipped"),
                            "next" => {
                                if let Some(s) = super::wizard_step_after(super::SCREEN_WELCOME) {
                                    goto_step(&c, s);
                                }
                            }
                            _ => refresh(&c),
                        }),
                        wd,
                    ));
                }
                super::workflows::page_head(&t, TITLE, HINT, w, buttons)
            },
        ))
        .child({
            let fctx = ctx.clone();
            dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |fcx| {
                followup_view(fcx, &fctx, &tt, page_w(fcx), followup)
            })
        })
        .child(dyn_view_scoped(
            LayoutStyle::default().grow(1.0),
            move |gcx| {
                let t = tt;
                let store = body_ctx.store;
                let width = page_w(gcx) - 1;
                let mut rows: Vec<View> = Vec::new();
                let who = store.conn.with(|c| match c {
                    ConnPhase::Connected(id) => Some((id.user_id.clone(), id.admin)),
                    _ => None,
                });
                let lede = match &who {
                    Some((_, true)) => ADMIN_LEDE.to_string(),
                    Some((user, false)) => format!(
                        "Signed in as {user}, not an admin: the guide's writes (engines, \
                         downloads, routes, the first-run record) are admin-only."
                    ),
                    None => "Not connected — the summary below needs a live gateway \
                             (Connection screen)."
                        .to_string(),
                };
                rows.push(sentence(&t, &lede, width, t.text));
                let fr = match store.first_run.get() {
                    Loadable::Ready(st) => (st.line(), if st.completed { t.ok } else { t.warn }),
                    Loadable::Loading => ("reading…".to_string(), t.info),
                    Loadable::Failed(e) => (format!("unreadable: {e}"), t.error),
                    Loadable::NotAsked => ("not read yet".to_string(), t.text_muted),
                };
                rows.push(line(vec![
                    span_bold("First run: ", t.text_muted),
                    span(fr.0, fr.1),
                ]));
                rows.push(line(vec![span(String::new(), t.text)]));
                match store.welcome.get() {
                    Loadable::Ready(w) => rows.extend(summary_rows(&t, &w, width as usize)),
                    Loadable::Loading => {
                        rows.push(sentence(&t, "Looking at this computer...", width, t.info))
                    }
                    Loadable::Failed(e) => {
                        rows.push(line(vec![span_bold(
                            "This computer's summary is not available right now.",
                            t.warn,
                        )]));
                        rows.push(sentence(&t, &e.to_string(), width, t.text_muted));
                    }
                    Loadable::NotAsked => rows.push(sentence(
                        &t,
                        "— not loaded yet (connect first, or press r to refresh)",
                        width,
                        t.text_muted,
                    )),
                }
                rows.push(line(vec![span(String::new(), t.text)]));
                // What this guide sets up: the web's three cards.
                rows.push(super::w::section(&t, SETS_UP));
                rows.push(sentence(&t, SETS_UP_SUB, width, t.text_muted));
                for (i, (screen, title, lede, go)) in step_cards().into_iter().enumerate() {
                    let n = WIZARD_STEPS
                        .iter()
                        .position(|s| *s == screen)
                        .map(|p| p + 1)
                        .unwrap_or(i + 3);
                    rows.push(line(vec![span_bold(format!("{n}  {title}"), t.text)]));
                    rows.push(sentence(&t, &format!("   {lede}"), width, t.text_muted));
                    let c = body_ctx.clone();
                    let a = Action::label(GO_IDS[i], go);
                    rows.push(
                        Element::new()
                            .style(LayoutStyle::row().h(1).shrink(0.0).padding(Edges {
                                left: 3,
                                right: 0,
                                top: 0,
                                bottom: 0,
                            }))
                            .child(button(gcx, &t, &a, On::Page, true, move || {
                                goto_step(&c, screen)
                            }))
                            .build(),
                    );
                }
                rows.push(line(vec![span(String::new(), t.text)]));
                rows.push(recommended(gcx, &body_ctx, &t, width));
                Scroll::new(
                    Element::new()
                        .style(LayoutStyle::column())
                        .children(rows)
                        .build(),
                )
                .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                .scrollbar_auto_hide(true)
                .view(gcx)
            },
        ))
        .build()
}

/// The three cards' "Go to …" button ids (engines, model, apps).
pub const GO_IDS: [&str; 3] = ["go_engines", "go_model", "go_apps"];

fn summary_rows(t: &TokenSet, w: &WelcomeSummary, width: usize) -> Vec<View> {
    // label (16) · value (bold) · note — the note WRAPS under the value
    // column instead of being cut at the edge.
    let mut out = Vec::new();
    for (label, value, note) in w.rows() {
        let text = if note.is_empty() {
            value.clone()
        } else {
            format!("{value}  {note}")
        };
        let body_w = width.saturating_sub(16).max(10);
        for (i, l) in wrap_text(&text, body_w).into_iter().enumerate() {
            let head = if i == 0 {
                format!("{label:<16}")
            } else {
                " ".repeat(16)
            };
            if i == 0 && l.starts_with(&value) {
                let rest = l[value.len()..].to_string();
                out.push(line(vec![
                    span(head, t.text_muted),
                    span_bold(value.clone(), t.text),
                    span(rest, t.text_faint),
                ]));
            } else {
                out.push(line(vec![span(head, t.text_muted), span(l, t.text_faint)]));
            }
        }
    }
    out
}

/// The finish row's controls: Finish (or Leave the guide for a
/// non-admin) and Skip setup (admins).
pub fn finish_actions(admin: bool) -> Vec<Action> {
    let mut out = vec![if admin {
        Action::label("finish", "Finish")
            .tooltip("Record that setup is done and switch to browse mode")
    } else {
        Action::label("finish", "Leave the guide")
            .tooltip("Close the guide (recording the first-run outcome is admin-only)")
    }];
    if admin {
        out.push(Action::label("skip", "Skip setup").tooltip(SKIP_TIP));
    }
    out
}

/// The finish row on the last step (Review), wizard mode: the web
/// guide's "done" facts, then Finish / Skip setup and the Start at login
/// switch. `screen` is the Review screen's scope (stable while this row
/// rebuilds): the start-at-login confirm opens there.
pub fn finish_row(gcx: Scope, screen: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let admin = admin_now(ctx);
    let model = store.routes.with(|r| {
        r.ready().and_then(|d| {
            d.rows
                .iter()
                .find(|row| row.key == "output.text" && row.model.is_some())
                .or_else(|| {
                    d.rows
                        .iter()
                        .find(|row| row.key == "input.text" && row.model.is_some())
                })
                .map(|row| row.pair_text())
        })
    });
    // Start at login: the gateway's own verdict (GET /host/start-at-login),
    // with the switch below — read once when this step shows.
    if admin
        && store
            .op
            .start_at_login
            .with_untracked(|r| matches!(r, Loadable::NotAsked))
    {
        super::host::load_start_at_login(ctx);
    }
    let login_state = store.op.start_at_login.get();
    let login = super::host::start_at_login_text(&login_state, admin);
    let model = model.unwrap_or_else(|| "not set yet".into());
    let facts = line(vec![
        span("Console ", t.text_muted),
        span(format!("{}/console", ui.conn_url.get()), t.text),
        span("  ·  text model ", t.text_muted),
        span(model.clone(), t.text),
        span("  ·  starts at login ", t.text_muted),
        span(login.clone(), t.text),
    ]);
    // The web guide's "From the command line" block, on one line.
    let service = store.welcome.with(|w| {
        w.ready()
            .map(|w| (w.service_installed, w.service_mechanism.clone()))
    });
    let cli = line(vec![
        span("CLI ", t.text_muted),
        span(done_cli_hints(service), t.text_faint),
    ]);
    let tall = crate::ui::page_viewport(gcx).get().h >= 30;
    let status: View = if ui.first_run_pending.get().is_some() {
        line(vec![span("⟳ recording… (POST + verify via GET)", t.info)])
    } else if let Some(e) = ui.first_run_error.get() {
        line(vec![span_bold(format!("✗ {e}"), t.error)])
    } else if !admin {
        line(vec![span(
            "recording the first-run outcome is admin-only",
            t.text_faint,
        )])
    } else if !tall {
        // Under 30 rows the Review step has ONE row for the finish
        // controls: the "done" facts ride it.
        line(vec![
            span("text model ", t.text_muted),
            span(model, t.text),
            span(" · starts at login ", t.text_muted),
            span(login.clone(), t.text),
            span(format!(" · {}/console", ui.conn_url.get()), t.text_muted),
        ])
    } else {
        line(vec![span(
            "Ctrl+G also jumps to a step, leaves or skips",
            t.text_faint,
        )])
    };
    let mut buttons = Element::new().style(LayoutStyle::row().gap(2).h(1));
    for a in finish_actions(admin) {
        let c = ctx.clone();
        let id = a.id;
        buttons = buttons.child(button(gcx, t, &a, On::Page, true, move || {
            record_outcome(&c, if id == "skip" { "skipped" } else { "finished" })
        }));
    }
    if admin {
        // The "Start at login" switch (a persistent state, confirmed then
        // verified), plus a one-shot repair when the registration is broken.
        if let Some(st) = login_state.ready() {
            let c_login = ctx.clone();
            buttons = buttons.child(
                Toggle::new(st.enabled)
                    .label("Start at login")
                    .tip("Start the gateway when you log in to this computer")
                    .refused(st.switch_unavailable())
                    .on_change(move |_| {
                        super::host::toggle_start_at_login(screen, &c_login, &|| {})
                    })
                    .view(gcx, t),
            );
            if let Some(label) = st.repair_label() {
                let c_fix = ctx.clone();
                buttons = buttons.child(button(
                    gcx,
                    t,
                    &Action::label("repair", label),
                    On::Page,
                    true,
                    move || super::host::toggle_start_at_login(screen, &c_fix, &|| {}),
                ));
            }
        }
    }
    // 30+ rows: the facts and the web's command-line block get their own
    // lines above the controls.
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    if tall {
        col = col.child(facts).child(cli);
    }
    col.child(buttons.child(status).build()).build()
}

/// The web guide's done-step commands (`renderFirstRunDone`, "From the
/// command line"): sign in again, start at login (or how it is already
/// installed), status. `service` = (installed, mechanism) from
/// `GET /host/state`, `None` while unread.
pub fn done_cli_hints(service: Option<(Option<bool>, Option<String>)>) -> String {
    let login = match service {
        Some((Some(true), mech)) => format!(
            "starts at login ({}; remove: abstractgateway service uninstall)",
            mech.as_deref().unwrap_or("service")
        ),
        _ => "abstractgateway service install (start at login)".to_string(),
    };
    format!(
        "abstractgateway claim --open (sign in again) · {login} · abstractgateway-config status"
    )
}
