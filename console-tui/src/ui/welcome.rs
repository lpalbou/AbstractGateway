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

use abstracttui::app::{ChoiceOutcome, ChoicePrompt};
use abstracttui::prelude::*;

use super::util::{line, span, span_bold, wrap_text};
use super::widths::BLOCK_CHROME;
use super::{Ctx, WIZARD_STEPS};
use crate::api::firstrun::{first_run_auto_wizard, WelcomeSummary};
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The web guide's per-step copy, keyed by the TUI screen that carries
/// the step (title, one-line lede).
pub fn step_copy(screen: usize) -> (&'static str, &'static str) {
    match screen {
        super::SCREEN_CONNECTION => ("Connection", "Sign in to the gateway (the terminal needs a token)."),
        super::SCREEN_WELCOME => ("Welcome", "Check this computer."),
        super::SCREEN_ENGINES => (
            "Local engines",
            "Engines run AI models on this computer. Install one if you want local models.",
        ),
        super::SCREEN_PROVIDERS => (
            "Cloud providers",
            "Cloud providers only need an API key.",
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
        ctx.store
            .notice
            .set(Some("already recording the first-run outcome — one moment".into()));
        return;
    }
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        ctx.store.notice.set(Some(
            "not connected — the first-run outcome is recorded on the gateway; connect first".into(),
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
    ctx.ui.first_run_pending.set(Some((fid, outcome.to_string())));
    ctx.send(Cmd::CompleteFirstRun {
        outcome: outcome.to_string(),
        form_id: Some(fid),
    });
}

/// Ctrl+G, anywhere: browse → reopen the guide at its welcome step (the
/// web "Setup guide" button); wizard → leave it, for now (the web
/// Escape) or for good (Skip setup, recorded).
pub fn guide_key(ctx: &Ctx, cx: Scope) {
    let ui = ctx.ui;
    if !ui.wizard.get_untracked() {
        // The guide is an admin surface (its writes — downloads, routes,
        // the first-run record — are admin routes); the web hides its
        // "Setup guide" button for a non-admin.
        if ctx.store.conn.with_untracked(ConnPhase::is_known_non_admin) {
            super::util::admin_gate(&ctx.store, "the setup guide");
            return;
        }
        ui.first_run_decided.set(true);
        ui.wizard.set(true);
        ui.screen.set(super::SCREEN_WELCOME);
        ctx.store.notice.set(Some(
            "setup guide — Ctrl+N walks it, Ctrl+G leaves or skips it".into(),
        ));
        return;
    }
    let admin = admin_now(ctx);
    let mut prompt = ChoicePrompt::new("Leave the setup guide?")
        .option_detail(
            "leave",
            "Leave for now",
            "nothing is recorded: the guide opens again at the next start",
        );
    prompt = if admin {
        prompt.option_detail(
            "skip",
            "Skip setup",
            "records it on the gateway (POST /host/first-run): the guide stops opening by itself",
        )
    } else {
        prompt
    };
    let prompt = prompt.option("stay", "Stay in the guide").initial("stay");
    let ctx2 = ctx.clone();
    super::open_prompt(cx, ui, prompt, move |outcome| {
        if let ChoiceOutcome::Answered(a) = outcome {
            match a.selected.first().map(String::as_str) {
                Some("leave") => {
                    ctx2.ui.first_run_decided.set(true);
                    ctx2.ui.wizard.set(false);
                    ctx2.store.notice.set(Some(
                        "left the setup guide (not recorded) — Ctrl+G reopens it".into(),
                    ));
                }
                Some("skip") => record_outcome(&ctx2, "skipped"),
                _ => {}
            }
        }
    });
}

/// `r` on the Setup step: the summary and the first-run state again.
pub fn refresh(ctx: &Ctx) {
    ctx.store.welcome.set(Loadable::Loading);
    ctx.store.first_run.set(Loadable::Loading);
    ctx.send(Cmd::LoadWelcome);
    ctx.send(Cmd::LoadFirstRun);
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let viewport = abstracttui::app::use_viewport(cx);
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Setup — welcome to your gateway")
                .fill(t.surface)
                .layout(LayoutStyle::column().gap(0).grow(1.0).padding(Edges::all(1)))
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), move |gcx| {
                    let t = tt;
                    let width = (viewport.get().w - BLOCK_CHROME).max(20) as usize;
                    let mut rows: Vec<View> = Vec::new();
                    let who = store.conn.with(|c| match c {
                        ConnPhase::Connected(id) => Some((id.user_id.clone(), id.admin)),
                        _ => None,
                    });
                    let lede = match &who {
                        Some((_, true)) => "Your gateway is running and you are signed in as its admin. \
                             The next steps get you to a working model. Every step is optional."
                            .to_string(),
                        Some((user, false)) => format!(
                            "Signed in as {user}, not an admin: the guide's writes (engines, \
                             downloads, routes, the first-run record) are admin-only."
                        ),
                        None => "Not connected — the summary below needs a live gateway \
                                 (Connection screen)."
                            .to_string(),
                    };
                    for l in wrap_text(&lede, width) {
                        rows.push(line(vec![span(l, t.text)]));
                    }
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
                        Loadable::Ready(w) => rows.extend(summary_rows(&t, &w)),
                        Loadable::Loading => rows.push(line(vec![span(
                            "Looking at this computer…",
                            t.info,
                        )])),
                        Loadable::Failed(e) => {
                            rows.push(line(vec![span_bold(
                                "This computer's summary is not available right now.",
                                t.warn,
                            )]));
                            rows.push(line(vec![span(e.to_string(), t.text_muted)]));
                        }
                        Loadable::NotAsked => rows.push(line(vec![span(
                            "— not loaded yet (connect first, or press r to refresh)",
                            t.text_muted,
                        )])),
                    }
                    rows.push(line(vec![span(String::new(), t.text)]));
                    rows.push(line(vec![span_bold(
                        "What this guide sets up — each step is optional:",
                        t.text_muted,
                    )]));
                    let steps = WIZARD_STEPS
                        .iter()
                        .enumerate()
                        .map(|(i, s)| format!("{} {}", i + 1, step_copy(*s).0))
                        .collect::<Vec<_>>()
                        .join(" · ");
                    for l in wrap_text(&steps, width) {
                        rows.push(line(vec![span(l, t.text)]));
                    }
                    let keys = if ui.wizard.get() {
                        "Ctrl+N next step · Ctrl+G leave or skip setup · r refresh"
                    } else {
                        "Ctrl+G opens the setup guide · r refresh"
                    };
                    rows.push(line(vec![span(keys, t.text_faint)]));
                    Scroll::new(Element::new().style(LayoutStyle::column()).children(rows).build())
                        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                        .scrollbar_auto_hide(true)
                        .view(gcx)
                }))
                .element(t)
                .build(),
        )
        .build()
}

fn summary_rows(t: &TokenSet, w: &WelcomeSummary) -> Vec<View> {
    w.rows()
        .into_iter()
        .map(|(label, value, note)| {
            let mut spans = vec![
                span(format!("{label:<16}"), t.text_muted),
                span_bold(value, t.text),
            ];
            if !note.is_empty() {
                spans.push(span(format!("  {note}"), t.text_faint));
            }
            line(spans)
        })
        .collect()
}

/// The finish row on the last step (Review), wizard mode: the web
/// guide's "done" facts in one line, then Finish / Skip setup.
pub fn finish_row(gcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let admin = admin_now(ctx);
    let model = store.routes.with(|r| {
        r.ready().and_then(|d| {
            d.rows
                .iter()
                .find(|row| row.key == "output.text" && row.model.is_some())
                .or_else(|| d.rows.iter().find(|row| row.key == "input.text" && row.model.is_some()))
                .map(|row| row.pair_text())
        })
    });
    let login = store.welcome.with(|w| match w.ready().and_then(|w| w.service_installed) {
        Some(true) => "yes",
        Some(false) => "not yet",
        None => "unknown",
    });
    let facts = line(vec![
        span("Console ", t.text_muted),
        span(format!("{}/console", ui.conn_url.get()), t.text),
        span("  ·  text model ", t.text_muted),
        span(model.unwrap_or_else(|| "not set yet".into()), t.text),
        span("  ·  starts at login ", t.text_muted),
        span(login, t.text),
    ]);
    let status: View = if ui.first_run_pending.get().is_some() {
        line(vec![span("⟳ recording… (POST + verify via GET)", t.info)])
    } else if let Some(e) = ui.first_run_error.get() {
        line(vec![span_bold(format!("✗ {e}"), t.error)])
    } else if !admin {
        line(vec![span(
            "recording the first-run outcome is admin-only",
            t.text_faint,
        )])
    } else {
        line(vec![span("Ctrl+G also leaves or skips", t.text_faint)])
    };
    let c_finish = ctx.clone();
    let c_skip = ctx.clone();
    let mut buttons = Element::new().style(LayoutStyle::row().gap(2).h(1)).child(
        Button::new(if admin { "Finish" } else { "Leave the guide" })
            .on_click(move || record_outcome(&c_finish, "finished"))
            .element(gcx, t)
            .build(),
    );
    if admin {
        buttons = buttons.child(
            Button::new("Skip setup")
                .on_click(move || record_outcome(&c_skip, "skipped"))
                .element(gcx, t)
                .build(),
        );
    }
    // The facts line only where the rows exist: the Review step's 80x24
    // arithmetic has exactly one row for the finish controls.
    let tall = abstracttui::app::use_viewport(gcx).get().h >= 30;
    let mut col = Element::new().style(LayoutStyle::column());
    if tall {
        col = col.child(facts);
    }
    col.child(buttons.child(status).build()).build()
}
