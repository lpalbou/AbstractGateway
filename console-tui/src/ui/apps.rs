//! Apps: the browser apps on this gateway (Flow Editor, Code, Observer,
//! Continuum, Entity), the desktop Assistant, and Node.js — the web
//! console's Apps tab (console_ui.py, `appCardMarkup` and friends) in the
//! terminal.
//!
//! Same routes, same states, same guards: every verb the web card offers
//! is a key here, and a verb that is not available says WHY (admin-only,
//! installs off, started outside the gateway, another computer…). The
//! web opens a signed-in tab; a terminal cannot, so Open shows the
//! one-time link — copy it, or open it in a browser on this machine —
//! with the SSH-forward line when the console reaches the gateway on a
//! loopback address (a headless server).
//!
//! The rules themselves live in `store::apps` (unit-tested there); this
//! file only renders them and sends commands.

use abstracttui::app::{ChoiceOutcome, ChoicePrompt};
use abstracttui::prelude::*;

use super::util::{line, span, span_bold, wrap_text};
use super::w::action::{button, On};
use super::w::{Action, Cell, Col, ColW, DataTable};
use super::widths;
use super::{open_form, open_prompt, Ctx};
use crate::store::apps::{
    app_blurb, app_key, badge_tip, badge_verb, copyables, part_word, primary_verb, secondary_verbs,
    status_label, tui_key, AppJob, AppNote, AppOpenLink, AppRow, AppVerb, AppsOverview, Tone,
    VerbState, NODE_KEY,
};
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The footer's verbs for this screen (ui/mod.rs footer arm).
pub const HINTS: &[(&str, &str)] = &[
    ("↑↓", "rows"),
    ("Enter", "Open"),
    ("Tab", "actions"),
    ("s", "status badge"),
    ("i/u", "install/update"),
    ("l", "log"),
    ("c", "cancel"),
    ("t/T", "terminal"),
    ("n", "Node.js"),
    ("y", "copy"),
    ("a", "Apps settings"),
    ("g", "app settings (Continuum)"),
    ("r", "check again"),
];

/// The text column inside the Apps block (border + padding, both sides).
fn text_width(cx: Scope) -> usize {
    (crate::ui::page_viewport(cx).get().w - widths::BLOCK_CHROME - 2).max(20) as usize
}

#[allow(clippy::too_many_arguments)]
/// `text` wrapped to `width` (never cut at the edge — R7.2), each line
/// indented by `indent`; the first line may carry a muted `label`.
fn wrapped(
    mut col: Element,
    t: &TokenSet,
    label: Option<&str>,
    text: &str,
    ink: abstracttui::base::Rgba,
    bold: bool,
    width: usize,
    indent: usize,
) -> Element {
    let pad = " ".repeat(indent);
    let head = label.map(|l| format!("{l} ")).unwrap_or_default();
    let hw = abstracttui::text::width(&head) as usize;
    for (i, l) in wrap_text(text, width.saturating_sub(indent + hw).max(10))
        .into_iter()
        .enumerate()
    {
        let lead = if i == 0 {
            format!("{pad}{head}")
        } else {
            format!("{pad}{}", " ".repeat(hw))
        };
        let body = if bold {
            span_bold(l, ink)
        } else {
            span(l, ink)
        };
        col = col.child(line(vec![span(lead, t.text_faint), body]));
    }
    col
}

const LOG_TAIL: u32 = 200;

fn is_admin(conn: &ConnPhase) -> bool {
    matches!(conn, ConnPhase::Connected(id) | ConnPhase::Verifying(id) if id.admin)
}

fn ink(t: &TokenSet, tone: Tone) -> abstracttui::base::Rgba {
    match tone {
        Tone::Ok => t.ok,
        Tone::Info => t.info,
        Tone::Warn => t.warn,
        Tone::Err => t.error,
        Tone::Muted => t.text_muted,
    }
}

fn selected(ctx: &Ctx) -> Option<AppRow> {
    let sel = ctx.store.apps.sel.get_untracked();
    ctx.store
        .apps
        .overview
        .with_untracked(|o| o.ready().and_then(|d| d.apps.get(sel).cloned()))
}

// ---------------------------------------------------------------------
// R15 Apps (DESIGN-TUI.md §3.2): head (title, subtitle, Check again, the
// Apps settings gear), ONE table (App · Status badge control · Version ·
// labelled action buttons), the selected app's details below, Node.js.
// ---------------------------------------------------------------------

/// The Apps page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let apps = store.apps;
    let ui = ctx.ui;
    let tt = *t;

    super::util::clamp_selection(cx, apps.sel, move || {
        apps.overview
            .with(|o| o.ready().map(|d| d.apps.len()).unwrap_or(0))
    });
    // The table's keyed selection ⇄ the store's index (the verbs read it).
    cx.effect(move || {
        let key = ui.apps_key.get();
        let i = apps.overview.with(|o| {
            o.ready()
                .and_then(|d| key.and_then(|k| d.apps.iter().position(|a| a.id == k)))
        });
        if let Some(i) = i {
            if apps.sel.get_untracked() != i {
                apps.sel.set(i);
            }
        }
    });
    cx.effect(move || {
        let i = apps.sel.get();
        let id = apps
            .overview
            .with(|o| o.ready().and_then(|d| d.apps.get(i).map(|a| a.id.clone())));
        if let Some(id) = id {
            if ui
                .apps_key
                .with_untracked(|k| k.as_deref() != Some(id.as_str()))
            {
                ui.apps_key.set(Some(id));
            }
        }
    });

    // A minted sign-in link opens its modal (here, where the operator is:
    // a link minted while away waits for the screen).
    {
        let ctx = ctx.clone();
        cx.effect(move || {
            if let Some(link) = apps.open_link.get() {
                if ctx.ui.prompt_open.get() > 0 {
                    return; // never stack over a live prompt; its close re-wakes us
                }
                apps.open_link.set(None);
                open_link_modal(cx, &ctx, link);
            }
        });
    }
    // The settings' values come from `GET /admin/runtime-config` (admin).
    {
        let c = ctx.clone();
        cx.effect(move || {
            let conn = c.store.conn.get();
            if is_admin(&conn)
                && c.store
                    .runtime_config
                    .with_untracked(|r| matches!(r, Loadable::NotAsked))
            {
                c.store.runtime_config.set(Loadable::Loading);
                c.send(Cmd::LoadRuntimeConfig);
            }
        });
    }

    let mut root = Element::new().style(LayoutStyle::column().grow(1.0).padding(Edges {
        left: 1,
        right: 1,
        top: 0,
        bottom: 0,
    }));
    let key = |c: char| KeyChord::plain(Key::Char(c));
    for (ch, verb) in [
        ('o', None),
        ('i', Some(AppVerb::Install)),
        ('u', Some(AppVerb::Update)),
        ('l', Some(AppVerb::Log)),
        ('c', Some(AppVerb::Cancel)),
        ('t', Some(AppVerb::OpenTerminal)),
        ('T', Some(AppVerb::InstallTerminal)),
        ('C', Some(AppVerb::CancelTerminal)),
    ] {
        let c = ctx.clone();
        root = root.shortcut(key(ch), move |_| run_key(cx, &c, verb));
    }
    {
        // R11.3: `s` = the status badge (Running → stop, Stopped → start).
        let c = ctx.clone();
        root = root.shortcut(key('s'), move |_| run_badge(cx, &c));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('n'), move |_| node_key(cx, &c));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('y'), move |_| copy_menu(cx, &c));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('a'), move |_| {
            super::app_settings::open(cx, &c, super::app_settings::Which::Apps)
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('g'), move |_| card_settings_key(cx, &c));
    }
    {
        let c = ctx.clone();
        root = root.shortcut(key('r'), move |_| check_again(&c));
    }
    let ctx_head = ctx.clone();
    let ctx_body = ctx.clone();
    root.child(dyn_view_scoped(
        LayoutStyle::column().shrink(0.0),
        move |hcx| apps_head(hcx, cx, &ctx_head, &tt),
    ))
    .child(dyn_view_scoped(
        LayoutStyle::default().grow(1.0),
        move |gcx| {
            let conn = store.conn.get();
            let data = apps.overview.get();
            let admin = is_admin(&conn);
            let anchor = |v: View| -> View {
                Element::new()
                    .style(LayoutStyle::column().shrink(0.0))
                    .focusable()
                    .autofocus()
                    .child(v)
                    .build()
            };
            match &data {
                Loadable::Ready(d) => ready_view(gcx, cx, &ctx_body, &tt, d, admin),
                // The web's "This gateway cannot manage apps right now":
                // the honest failure kind, never a guessed list.
                Loadable::Failed(e) => anchor(
                    Element::new()
                        .style(LayoutStyle::column())
                        .child(line(vec![span_bold(
                            "This gateway cannot manage apps right now.",
                            tt.warn,
                        )]))
                        .child(super::util::error_panel_conn(
                            &tt,
                            e,
                            &conn,
                            Some("r checks again"),
                        ))
                        .build(),
                ),
                Loadable::NotAsked => anchor(line(vec![span(
                    "— not loaded yet (connect first, or press r to check)",
                    tt.text_muted,
                )])),
                Loadable::Loading => anchor(
                    abstracttui::widgets::Spinner::new()
                        .frame(store.tick.get())
                        .label("looking for the apps…")
                        .element(&tt)
                        .build(),
                ),
            }
        },
    ))
    .build()
}

/// "Check again" (the web's toolbar button, `r`).
fn check_again(ctx: &Ctx) {
    ctx.store.apps.notes.set(Vec::new());
    ctx.send(Cmd::LoadApps { latest: true });
}

/// The web subtitle of the Apps page.
pub const APPS_SUBTITLE: &str = "Install and open the apps that work with this gateway";

/// Title + subtitle, then [Check again] and the Apps settings gear (admins).
fn apps_head(cx: Scope, pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let admin = ctx.store.conn.with(is_admin);
    let w = (crate::ui::page_viewport(cx).get().w - 2).max(20);
    let c = ctx.clone();
    let check = Action::label("check", "Check again").key('r');
    let mut buttons = vec![button(cx, t, &check, On::Page, true, move || {
        check_again(&c)
    })];
    let mut bw = check.width() + 1;
    if admin {
        let gear = Action::glyph("settings", "Apps settings")
            .key('a')
            .tooltip("Settings shared by every app (Node.js, ports, registries)");
        bw += gear.width() + 1;
        let c = ctx.clone();
        buttons.push(button(cx, t, &gear, On::Page, true, move || {
            super::app_settings::open(pcx, &c, super::app_settings::Which::Apps)
        }));
    }
    let mut btn_row = Element::new().style(
        LayoutStyle::row()
            .height(Dimension::Cells(1))
            .gap(1)
            .shrink(0.0),
    );
    for b in buttons {
        btn_row = btn_row.child(b);
    }
    let title_w = abstracttui::text::width(APPS_SUBTITLE);
    let side = title_w + bw + 2 <= w;
    let titles = Element::new()
        .style(if side {
            LayoutStyle::column()
                .width(Dimension::Cells(w - bw - 1))
                .shrink(0.0)
        } else {
            LayoutStyle::column().shrink(0.0)
        })
        .child(super::w::paint::fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![super::w::Ink::new("Apps", t.text).bold()],
            None,
        ))
        .child(super::w::form::sentence(t, APPS_SUBTITLE, w, t.text_muted))
        .build();
    if side {
        Element::new()
            .style(LayoutStyle::row().shrink(0.0))
            .child(titles)
            .child(btn_row.build())
            .build()
    } else {
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .child(titles)
            .child(btn_row.build())
            .build()
    }
}

/// `pcx`: the page scope — actions open their modals/prompts there (this
/// region re-renders on every selection move and job tick).
fn ready_view(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: &AppsOverview,
    admin: bool,
) -> View {
    let apps = ctx.store.apps;
    // Tracked: job progress and notes re-render the panel.
    let jobs = apps.jobs.get();
    let _ = apps.notes.get();
    let _ = apps.pending.get();
    let job_of = |key: &str, fallback: Option<&AppJob>| -> Option<AppJob> {
        jobs.iter()
            .find(|(k, _)| k == key)
            .map(|(_, j)| j.clone())
            .or_else(|| fallback.cloned())
    };
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    let width = text_width(cx);
    let mut intro = d.intro();
    if let Some(h) = &d.apps_host {
        if h.starts_with("127.") || h == "localhost" {
            intro.push_str(&format!(" They listen on {h}: this machine only."));
        } else {
            intro.push_str(&format!(" They listen on {h}."));
        }
    }
    col = wrapped(col, t, None, &intro, t.text_muted, false, width, 0);
    if d.registry_reachable == Some(false) {
        col = wrapped(
            col,
            t,
            None,
            "The app store (npm) is not reachable. Installed apps keep working; installing needs the internet.",
            t.warn,
            false,
            width,
            0,
        );
    }
    let node = node_view(
        cx,
        pcx,
        ctx,
        t,
        width,
        d,
        job_of(NODE_KEY, d.node.active_job.as_ref()),
        apps.note_for(NODE_KEY),
        apps.is_pending(NODE_KEY),
        admin,
    );
    if d.apps.is_empty() {
        return Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .focusable()
            .autofocus()
            .child(
                col.child(node)
                    .child(line(vec![span(
                        "∅ this gateway lists no apps",
                        t.text_muted,
                    )]))
                    .build(),
            )
            .build();
    }
    col = col.child(apps_table(cx, pcx, ctx, t, d, admin, &job_of));
    let mut below = Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![span(String::new(), t.text)]));
    if let Some(row) = d.apps.get(apps.sel.get()) {
        let job = job_of(&app_key(&row.id), row.active_job.as_ref());
        let tjob = job_of(
            &tui_key(&row.id),
            row.tui.as_ref().and_then(|x| x.active_job.as_ref()),
        );
        below = below.child(detail_view(
            cx,
            t,
            d,
            row,
            job.as_ref(),
            tjob.as_ref(),
            apps.note_for(&app_key(&row.id)),
            apps.note_for(&tui_key(&row.id)),
            apps.is_pending(&app_key(&row.id)) || apps.is_pending(&tui_key(&row.id)),
            admin,
        ));
    }
    below = below
        .child(line(vec![span(String::new(), t.text)]))
        .child(node);
    col.child(
        Scroll::new(below.build())
            .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
            .scrollbar_auto_hide(true)
            .view(cx),
    )
    .build()
}

/// The web's tooltip of an app action (the card buttons' data-af-tip).
fn verb_tip(row: &AppRow, v: &VerbState) -> String {
    // The web card's data-af-tip texts (console_ui.py appCardMarkup).
    let n = &row.name;
    let first_entity = row.id == "entity" && row.entities_count == Some(0);
    let entity_title = format!("Open {n} on the form that creates your first entity, signed in");
    match v.verb {
        AppVerb::Install if row.is_desktop() => {
            format!("Install the {n} on the gateway's computer (into the gateway's own Python)")
        }
        AppVerb::Install => format!(
            "Install {n}{}{}",
            if row.install_parts.iter().any(|p| p == "tui") {
                " for the browser and the terminal"
            } else {
                ""
            },
            if row.needs_node_install {
                " (Node.js is installed for you first: about 56 MB, no password needed)"
            } else {
                ""
            }
        ),
        AppVerb::Open if first_entity && row.running => entity_title,
        AppVerb::Open if first_entity => {
            let mut t = entity_title;
            if let Some(c) = t.get_mut(0..1) {
                c.make_ascii_lowercase();
            }
            format!("Start {n}, then {t}")
        }
        AppVerb::Open if row.running => format!("Open {n} in a new tab, signed in"),
        AppVerb::Open => format!("Start {n} and open it in a new tab, signed in"),
        AppVerb::DesktopOpen if row.running => format!("Bring the {n} to the front on this computer"),
        AppVerb::DesktopOpen => format!("Start the {n} on this computer"),
        AppVerb::Update => row.update_tip.clone().unwrap_or_else(|| {
            format!(
                "Install the newest {n} ({}); a running app restarts on it",
                row.latest_version.clone().unwrap_or_else(|| "latest".into())
            )
        }),
        AppVerb::OpenTerminal => {
            format!("The same {n}, in a terminal window on this computer, signed in")
        }
        AppVerb::InstallTerminal if row.tui.as_ref().is_some_and(|t| t.installed) => {
            format!("Install the newest {n} terminal app")
        }
        AppVerb::InstallTerminal => format!(
            "Install {n}'s terminal app: a ready-made download from {n}'s release, checked against its published checksums"
        ),
        AppVerb::Cancel => format!("Stop installing {n}"),
        AppVerb::CancelTerminal => format!("Stop installing {n}'s terminal app"),
        _ => v.label.clone(),
    }
}

/// The verb behind an action id (the table's buttons).
fn verb_of(id: &str) -> Option<AppVerb> {
    Some(match id {
        "open" => AppVerb::Open,
        "desktop_open" => AppVerb::DesktopOpen,
        "install" => AppVerb::Install,
        "update" => AppVerb::Update,
        "log" => AppVerb::Log,
        "cancel" => AppVerb::Cancel,
        "terminal" => AppVerb::OpenTerminal,
        "install_terminal" => AppVerb::InstallTerminal,
        "cancel_terminal" => AppVerb::CancelTerminal,
        _ => return None,
    })
}

fn verb_id(v: AppVerb) -> &'static str {
    match v {
        AppVerb::Open => "open",
        AppVerb::DesktopOpen => "desktop_open",
        AppVerb::Install => "install",
        AppVerb::Update => "update",
        AppVerb::Log => "log",
        AppVerb::Cancel => "cancel",
        AppVerb::OpenTerminal => "terminal",
        AppVerb::InstallTerminal => "install_terminal",
        AppVerb::CancelTerminal => "cancel_terminal",
        AppVerb::Start | AppVerb::Stop => "badge",
    }
}

/// A row's actions: the primary one, the secondary ones (Update, the
/// terminal version, Show log), the card's gear (Continuum) — labelled
/// buttons in the web's words; a refused one faint with its reason.
pub fn app_actions(
    row: &AppRow,
    job: Option<&AppJob>,
    tjob: Option<&AppJob>,
    admin: bool,
) -> Vec<Action> {
    let mut verbs: Vec<VerbState> = Vec::new();
    if let Some(p) = primary_verb(row, job, admin) {
        verbs.push(p);
    }
    verbs.extend(secondary_verbs(row, job, tjob, admin));
    let mut out: Vec<Action> = verbs
        .iter()
        .map(|v| {
            let label = match v.verb {
                AppVerb::OpenTerminal => ">_ Open in Terminal".to_string(),
                _ => v.label.clone(),
            };
            let key = match v.verb {
                AppVerb::Cancel => 'c',
                other => other.key().chars().next().unwrap_or('o'),
            };
            Action::label(verb_id(v.verb), label)
                .key(key)
                .tooltip(verb_tip(row, v))
                .refused(v.available.clone().err())
        })
        .collect();
    if admin && has_card_settings(&row.id) {
        out.push(
            Action::glyph("settings", "Settings")
                .key('g')
                .tooltip(format!("{} settings", row.name)),
        );
    }
    out
}

#[allow(clippy::too_many_arguments)]
fn apps_table(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: &AppsOverview,
    admin: bool,
    job_of: &dyn Fn(&str, Option<&AppJob>) -> Option<AppJob>,
) -> View {
    let vp = crate::ui::page_viewport(cx).get();
    let rows: Vec<super::w::Row> = d
        .apps
        .iter()
        .map(|a| {
            let job = job_of(&app_key(&a.id), a.active_job.as_ref());
            let tjob = job_of(
                &tui_key(&a.id),
                a.tui.as_ref().and_then(|x| x.active_job.as_ref()),
            );
            let active = job.as_ref().map(AppJob::is_active).unwrap_or(false);
            let (label, tone) = status_label(a, active);
            // R11.3 / A3: the badge's LABEL is the state only; the action
            // sentence ("Running — click to stop") is its tooltip.
            let badge = match badge_verb(a, job.as_ref()) {
                Some(bv) if bv.available.is_ok() => Cell::Badge {
                    label: label.clone(),
                    ink: ink(t, tone),
                    action: Some("badge"),
                    tip: Some(format!(
                        "{}  (s)",
                        badge_tip(a).unwrap_or_else(|| bv.label.clone())
                    )),
                },
                _ => Cell::Badge {
                    label,
                    ink: ink(t, tone),
                    action: None,
                    tip: badge_tip(a),
                },
            };
            let mut version = vec![super::w::Ink::new(
                a.version.clone().unwrap_or_else(|| "—".into()),
                t.text_muted,
            )];
            if a.update_available {
                if let Some(l) = &a.latest_version {
                    version.push(super::w::Ink::new(format!(" · {l} ⇡"), t.warn));
                }
            }
            super::w::Row::new(
                a.id.clone(),
                vec![
                    Cell::text(a.name.clone(), t.text),
                    badge,
                    Cell::Text(version),
                    Cell::Actions(app_actions(a, job.as_ref(), tjob.as_ref(), admin)),
                ],
            )
        })
        .collect();
    let ctx_a = ctx.clone();
    let ctx_e = ctx.clone();
    DataTable::new(
        vec![
            Col::new("App", ColW::Fit { min: 6, max: 14 }),
            Col::new("Status", ColW::Fit { min: 8, max: 22 }),
            Col::new("Version", ColW::Fit { min: 7, max: 22 }),
            Col::new("Actions", ColW::Flex { weight: 1, min: 16 }),
        ],
        rows,
        ctx.ui.apps_key,
    )
    .width((vp.w - 2).max(20))
    .max_rows((vp.h - 10).clamp(4, 20))
    .top(ctx.ui.apps_top)
    .autofocus()
    .on_action(move |key, id| app_action(pcx, &ctx_a, key, id))
    .on_activate(move |key| {
        select_app(&ctx_e, key);
        run_key(pcx, &ctx_e, None)
    })
    .view(cx, t)
}

/// Select the app `id` (the verbs read the store's index at once).
fn select_app(ctx: &Ctx, id: &str) {
    ctx.ui.apps_key.set(Some(id.to_string()));
    let i = ctx.store.apps.overview.with_untracked(|o| {
        o.ready()
            .and_then(|d| d.apps.iter().position(|a| a.id == id))
    });
    if let Some(i) = i {
        if ctx.store.apps.sel.get_untracked() != i {
            ctx.store.apps.sel.set(i);
        }
    }
}

/// A row action (a click or its key).
fn app_action(cx: Scope, ctx: &Ctx, key: &str, id: &str) {
    select_app(ctx, key);
    match id {
        "badge" => run_badge(cx, ctx),
        "settings" => card_settings_key(cx, ctx),
        other => {
            if let Some(v) = verb_of(other) {
                run_key(cx, ctx, Some(v))
            }
        }
    }
}

/// Does this app have its own settings (a gear on its card)? Only
/// Continuum today (backlog folder, exec runner, process manager).
pub fn has_card_settings(app_id: &str) -> bool {
    app_id == "continuum"
}

/// `g`: the selected card's gear.
fn card_settings_key(cx: Scope, ctx: &Ctx) {
    match selected(ctx) {
        Some(row) if has_card_settings(&row.id) => {
            super::app_settings::open(cx, ctx, super::app_settings::Which::Continuum)
        }
        Some(row) => ctx.store.notice.set(Some(format!(
            "{} has no settings of its own — a opens the Apps settings",
            row.name
        ))),
        None => ctx
            .store
            .notice
            .set(Some("no app selected — nothing to set".into())),
    }
}

#[allow(clippy::too_many_arguments)]
fn node_view(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    width: usize,
    d: &AppsOverview,
    job: Option<AppJob>,
    note: Option<AppNote>,
    pending: bool,
    admin: bool,
) -> View {
    let n = &d.node;
    let mut spans = vec![span_bold("Node.js  ", t.text)];
    let active = job.as_ref().map(AppJob::is_active).unwrap_or(false);
    if active {
        spans.push(span("Installing", t.info));
    } else if n.available {
        spans.push(span("Ready", t.ok));
    } else {
        spans.push(span("Not installed", t.text_muted));
    }
    if let Some(v) = &n.version {
        spans.push(span(format!(" · {v}"), t.text_muted));
    }
    match n.source.as_str() {
        "system" => spans.push(span(" · found on this computer", t.text_muted)),
        "managed" => spans.push(span(" · installed by the gateway", t.text_muted)),
        _ => {}
    }
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(spans));
    col = wrapped(
        col,
        t,
        None,
        "The engine the browser apps run on.",
        t.text_faint,
        false,
        width,
        2,
    );
    if let Some(j) = job.as_ref().filter(|j| j.is_active()) {
        col = col.child(line(vec![span(
            format!("  ⟳ {}", j.progress_line("Installing Node.js")),
            t.info,
        )]));
        let c = ctx.clone();
        let a = Action::label("node_cancel", "Cancel")
            .key('n')
            .tooltip("Cancel the Node.js install")
            .refused((!admin).then(|| "Only an admin can cancel an install".to_string()));
        col = col.child(
            Element::new()
                .style(
                    LayoutStyle::row()
                        .height(Dimension::Cells(1))
                        .shrink(0.0)
                        .padding(Edges {
                            left: 2,
                            right: 0,
                            top: 0,
                            bottom: 0,
                        }),
                )
                .child(button(cx, t, &a, On::Page, true, move || node_key(pcx, &c)))
                .build(),
        );
    } else if !n.available {
        col = wrapped(
            col,
            t,
            None,
            "Node.js will be installed for you: the gateway puts it in its own folder (about 56 MB, no password, no terminal) the first time you install an app, or now.",
            t.text_muted,
            false,
            width,
            2,
        );
        // The web's [Install Node.js] button (non-admin: disabled, with
        // the web's tooltip).
        let why = if pending {
            Some("starting…".to_string())
        } else if !n.install_available {
            Some(format!(
                "Install is not available here{}",
                n.message
                    .as_ref()
                    .map(|m| format!(": {m}"))
                    .unwrap_or_default()
            ))
        } else if !admin {
            Some("Only an admin can install Node.js".to_string())
        } else {
            None
        };
        let c = ctx.clone();
        let a = Action::label("node_install", "Install Node.js")
            .key('n')
            .tooltip("Install Node.js into the gateway's own folder (about 56 MB, no password, no terminal)")
            .refused(why.clone());
        col = col.child(
            Element::new()
                .style(
                    LayoutStyle::row()
                        .height(Dimension::Cells(1))
                        .shrink(0.0)
                        .padding(Edges {
                            left: 2,
                            right: 0,
                            top: 0,
                            bottom: 0,
                        }),
                )
                .child(button(cx, t, &a, On::Page, true, move || node_key(pcx, &c)))
                .build(),
        );
        if let Some(w) = why {
            col = wrapped(
                col,
                t,
                Some("Install Node.js:"),
                &w,
                t.text_faint,
                false,
                width,
                2,
            );
        }
    }
    if let Some(note) = note {
        col = note_lines(col, t, &note, 2, width);
    }
    col.build()
}

fn note_lines(
    mut col: Element,
    t: &TokenSet,
    note: &AppNote,
    indent: usize,
    width: usize,
) -> Element {
    let tone = note.tone.unwrap_or(Tone::Info);
    col = wrapped(col, t, None, &note.text, ink(t, tone), true, width, indent);
    if let Some(h) = &note.hint {
        col = wrapped(col, t, None, h, t.text_muted, false, width, indent);
    }
    for (label, cmd) in &note.commands {
        col = wrapped(
            col,
            t,
            Some(&format!("{label}:")),
            cmd,
            t.text,
            false,
            width,
            indent,
        );
    }
    if note.details.is_some() {
        col = wrapped(
            col,
            t,
            None,
            "(y copies the details)",
            t.text_faint,
            false,
            width,
            indent,
        );
    }
    col
}

#[allow(clippy::too_many_arguments)]
fn detail_view(
    cx: Scope,
    t: &TokenSet,
    d: &AppsOverview,
    row: &AppRow,
    job: Option<&AppJob>,
    tjob: Option<&AppJob>,
    note: Option<AppNote>,
    tnote: Option<AppNote>,
    pending: bool,
    admin: bool,
) -> View {
    let width = text_width(cx);
    let active = job.map(AppJob::is_active).unwrap_or(false);
    let (label, tone) = status_label(row, active);
    let mut col = Element::new().style(LayoutStyle::column().gap(0));
    // The card head: name + pill, then the web's one-line blurb.
    col = col.child(line(vec![
        span_bold(row.name.clone(), t.accent),
        span("  ", t.text),
        span_bold(label, ink(t, tone)),
    ]));
    col = wrapped(col, t, None, &app_blurb(row), t.text_muted, false, width, 0);
    // The body: only what the operator must see now (web card body).
    if let Some(j) = job.filter(|j| j.is_active()) {
        col = wrapped(
            col,
            t,
            None,
            &format!("⟳ {}", j.progress_line(&format!("Installing {}", row.name))),
            t.info,
            false,
            width,
            0,
        );
        for p in &j.parts {
            col = wrapped(
                col,
                t,
                None,
                &format!("{} · {}", p.label, part_word(&p.state)),
                t.text_muted,
                false,
                width,
                2,
            );
        }
    } else if let Some(j) = job.filter(|j| j.state == "failed") {
        let err = j.error.clone().unwrap_or_default();
        let head = if err.message.is_empty() {
            "The install did not finish.".to_string()
        } else {
            err.message
        };
        col = wrapped(col, t, None, &head, t.error, true, width, 0);
        if let Some(h) = err.hint {
            col = wrapped(col, t, None, &h, t.text_muted, false, width, 0);
        }
        for p in &j.parts {
            col = wrapped(
                col,
                t,
                None,
                &format!("{} · {}", p.label, part_word(&p.state)),
                t.text_muted,
                false,
                width,
                2,
            );
        }
        col = col.child(line(vec![span("(y copies the install log)", t.text_faint)]));
    } else if matches!(row.status.as_str(), "crashed" | "crash_loop") {
        col = wrapped(
            col,
            t,
            None,
            &format!("{} stopped unexpectedly. Open starts it again.", row.name),
            t.error,
            true,
            width,
            0,
        );
        if let Some(e) = &row.last_error {
            // Show details: the whole error, wrapped (never cut).
            col = wrapped(col, t, None, e, t.text_muted, false, width, 2);
        }
    }
    if !row.installed && !active {
        if let Some(r) = &row.install_blocked_reason {
            col = wrapped(col, t, None, r, t.warn, false, width, 0);
        }
    }
    if let Some(desk) = &row.desktop {
        if row.installed && desk.launch_blocked.as_deref() == Some("other_computer") {
            col = wrapped(
                col,
                t,
                None,
                desk.launch_blocked_reason.as_deref().unwrap_or_default(),
                t.text_muted,
                false,
                width,
                0,
            );
        }
        // R10.5 / R10.6: the source checkout's version sentence (only for a
        // source checkout, as the web card); another Assistant runs; an
        // update left one alone.
        let version_reason = desk.version_reason.clone().filter(|_| desk.source_checkout);
        for note in [&version_reason, &desk.other_running, &desk.restart_note]
            .into_iter()
            .flatten()
            .filter(|_| row.installed)
        {
            col = wrapped(col, t, None, note, t.text_muted, false, width, 0);
        }
    }
    // R10.6: a newer version of an app started outside the gateway is shown
    // with where to update it (the web card's note; no action here).
    if row.updates_elsewhere() && row.update_available {
        if let Some(l) = &row.latest_version {
            col = wrapped(
                col,
                t,
                None,
                &format!(
                    "Latest {l} · {}",
                    row.update_tip.clone().unwrap_or_default()
                ),
                t.text_muted,
                false,
                width,
                0,
            );
        }
    }
    if let Some(j) = tjob.filter(|j| j.is_active()) {
        col = wrapped(
            col,
            t,
            None,
            &format!(
                "⟳ {}",
                j.progress_line(&format!("Installing {} for the terminal", row.name))
            ),
            t.info,
            false,
            width,
            0,
        );
    } else if let Some(j) = tjob.filter(|j| j.state == "failed") {
        let err = j.error.clone().unwrap_or_default();
        let head = if err.message.is_empty() {
            format!("{}'s terminal app did not install.", row.name)
        } else {
            err.message
        };
        col = wrapped(col, t, None, &head, t.error, true, width, 0);
    }
    if let Some(n) = &note {
        col = note_lines(col, t, n, 0, width);
    }
    if let Some(n) = &tnote {
        col = note_lines(col, t, n, 0, width);
    }

    // The verbs: the primary one, then everything under the web's
    // "Technical details", each with its key or the reason it is off.
    let mut verbs: Vec<VerbState> = Vec::new();
    if let Some(p) = primary_verb(row, job, admin) {
        verbs.push(p);
    }
    verbs.extend(secondary_verbs(row, job, tjob, admin));
    let mut on: Vec<(&str, String)> = Vec::new();
    let mut off: Vec<(String, String)> = Vec::new();
    // R11.3: the status badge first, in the web's tooltip words: "s Running —
    // click to stop" (Enter / Space while the cursor is on the badge cell),
    // or "Running: Started outside the gateway — stop it where it was started".
    if let Some(bv) = badge_verb(row, job) {
        if let Err(why) = &bv.available {
            off.push((bv.label.clone(), why.clone()));
        }
    }
    for v in &verbs {
        match &v.available {
            Ok(()) => {
                let k = if v.verb == AppVerb::Cancel
                    && primary_verb(row, job, admin).map(|p| p.verb) == Some(AppVerb::Cancel)
                {
                    "c"
                } else {
                    v.verb.key()
                };
                on.push((k, v.label.clone()));
            }
            Err(why) => off.push((v.label.clone(), why.clone())),
        }
    }
    // R8.1: the card's gear, next to Open (admins; Continuum's settings).
    if admin && has_card_settings(&row.id) {
        on.push(("g", "Settings".to_string()));
    }
    // R15: the actions are the row's buttons (the table); here only what
    // is refused, with its reason (visible, never tooltip-only).
    let _ = &on;
    if pending {
        col = col.child(line(vec![span("⟳ working…", t.info)]));
    }
    for (label, why) in &off {
        col = wrapped(
            col,
            t,
            Some(&format!("{label}:")),
            why,
            t.text_faint,
            false,
            width,
            0,
        );
    }

    // The web's technical line and rows, in its words.
    let mut facts: Vec<String> = Vec::new();
    if let Some(port) = row.external_port {
        facts.push(format!("Started outside the gateway on port {port}"));
    }
    if let Some(v) = &row.version {
        facts.push(format!("Version {v}"));
    } else if let Some(l) = row.latest_version.as_ref().filter(|_| row.update_available) {
        facts.push(format!("Latest {l}"));
    }
    if row.is_desktop() {
        if let Some(l) = row.latest_version.as_ref().filter(|_| row.update_available) {
            facts.push(format!("Latest {l}"));
        }
        if let Some(e) = row.desktop.as_ref().and_then(|d| d.latest_error.clone()) {
            facts.push(e);
        }
    }
    if row.is_desktop() && row.running {
        if let Some(pid) = row.pid {
            facts.push(format!("Running (process {pid})"));
        }
    }
    if let Some(t2) = &row.tui {
        if t2.installed {
            if let Some(v) = &t2.version {
                facts.push(format!("Terminal {v}"));
            }
        }
    }
    if !facts.is_empty() {
        col = wrapped(
            col,
            t,
            None,
            &facts.join(" · "),
            t.text_faint,
            false,
            width,
            0,
        );
    }
    let address = d.address_of(row);
    if let Some(a) = &address {
        col = wrapped(col, t, Some("Address"), a, t.text_muted, false, width, 0);
    }
    if let Some(u) = &row.url {
        let label = if address.is_some() {
            "On this machine"
        } else {
            "Address"
        };
        col = wrapped(col, t, Some(label), u, t.text_muted, false, width, 0);
    }
    if !row.is_desktop() && !row.package.is_empty() {
        col = wrapped(
            col,
            t,
            Some("npm"),
            &format!("npx {}", row.package),
            t.text_muted,
            false,
            width,
            0,
        );
    }
    if let Some(t2) = &row.tui {
        if !t2.installed && !t2.install_available && t2.install_method == "cargo" {
            if let Some(c) = &t2.install_command {
                col = wrapped(
                    col,
                    t,
                    Some("Terminal version: needs the Rust toolchain"),
                    c,
                    t.text_muted,
                    false,
                    width,
                    0,
                );
            }
        } else if !t2.installed && !t2.install_available {
            let why = t2
                .install_blocked_reason
                .clone()
                .unwrap_or_else(|| "installing is not available right now".into());
            col = wrapped(
                col,
                t,
                None,
                &format!("Terminal version: {why}"),
                t.text_faint,
                false,
                width,
                0,
            );
        } else if t2.installed && !t2.launch_available {
            if let Some(c) = &t2.command {
                col = wrapped(
                    col,
                    t,
                    Some("Terminal version, on the other computer"),
                    c,
                    t.text_muted,
                    false,
                    width,
                    0,
                );
            }
            if let Some(c) = &t2.signin_command {
                col = wrapped(
                    col,
                    t,
                    Some("First time there, sign in once"),
                    c,
                    t.text_muted,
                    false,
                    width,
                    0,
                );
            }
        } else if t2.installed {
            if let Some(c) = &t2.command {
                col = wrapped(col, t, Some("Terminal"), c, t.text_muted, false, width, 0);
            }
        }
    }
    if let Some(dk) = &row.desktop {
        if let Some(l) = &dk.location {
            col = wrapped(col, t, Some("Location"), l, t.text_muted, false, width, 0);
        }
        if let Some(c) = &dk.launch_command {
            col = wrapped(
                col,
                t,
                Some("Launch command"),
                c,
                t.text_muted,
                false,
                width,
                0,
            );
        }
        if !row.installed {
            if let Some(c) = &dk.install_command {
                col = wrapped(
                    col,
                    t,
                    Some("Install command"),
                    c,
                    t.text_muted,
                    false,
                    width,
                    0,
                );
            }
        }
    }
    col.build()
}

// ---------------------------------------------------------------------
// Verbs
// ---------------------------------------------------------------------

fn run_key(cx: Scope, ctx: &Ctx, verb: Option<AppVerb>) {
    let store = ctx.store;
    let Some(row) = selected(ctx) else {
        store
            .notice
            .set(Some(match store.apps.overview.get_untracked() {
                Loadable::Ready(_) => "no app selected".into(),
                _ => "the apps are not loaded — r checks".into(),
            }));
        return;
    };
    let admin = store.conn.with_untracked(is_admin);
    let job = store
        .apps
        .job_for(&app_key(&row.id), row.active_job.as_ref());
    let tjob = store.apps.job_for(
        &tui_key(&row.id),
        row.tui.as_ref().and_then(|t| t.active_job.as_ref()),
    );
    let state = match verb {
        None => primary_verb(&row, job.as_ref(), admin),
        // The badge's verbs (Stop / Start / the Assistant's Open) come
        // first: `s` acts on the badge even where the row's primary is Open.
        Some(v) => badge_verb(&row, job.as_ref())
            .filter(|b| b.verb == v)
            .into_iter()
            .chain(primary_verb(&row, job.as_ref(), admin))
            .chain(secondary_verbs(&row, job.as_ref(), tjob.as_ref(), admin))
            .find(|s| s.verb == v || (v == AppVerb::Open && s.verb == AppVerb::DesktopOpen)),
    };
    let Some(state) = state else {
        store.notice.set(Some(match verb {
            Some(AppVerb::Cancel) => format!("{}: no install is running", row.name),
            Some(AppVerb::CancelTerminal) => {
                format!("{}: no terminal install is running", row.name)
            }
            Some(AppVerb::OpenTerminal | AppVerb::InstallTerminal) => {
                format!("{} has no terminal version", row.name)
            }
            Some(AppVerb::Install) => format!("{} is already installed", row.name),
            Some(v) => format!("{}: {v:?} is not offered for this app", row.name),
            None => format!("{}: nothing to do", row.name),
        }));
        return;
    };
    if let Err(why) = &state.available {
        store
            .notice
            .set(Some(format!("{} — {}: {why}", row.name, state.label)));
        return;
    }
    let pending_key = match state.verb {
        AppVerb::OpenTerminal | AppVerb::InstallTerminal | AppVerb::CancelTerminal => {
            tui_key(&row.id)
        }
        _ => app_key(&row.id),
    };
    if store.apps.is_pending(&pending_key) {
        store
            .notice
            .set(Some(format!("{}: already working on it", row.name)));
        return;
    }
    let act = |c: &Ctx, verb: AppVerb, path: Option<String>, start_first: bool| {
        c.send(Cmd::AppAct {
            app_id: row.id.clone(),
            name: row.name.clone(),
            verb,
            path,
            start_first,
        })
    };
    match state.verb {
        AppVerb::Open => act(
            ctx,
            AppVerb::Open,
            state.path.clone(),
            row.installed && !row.running,
        ),
        AppVerb::DesktopOpen | AppVerb::Start | AppVerb::OpenTerminal => {
            act(ctx, state.verb, None, false)
        }
        AppVerb::Log => open_log_modal(cx, ctx, &row),
        AppVerb::Cancel | AppVerb::CancelTerminal => {
            let (key, j) = if state.verb == AppVerb::Cancel {
                (app_key(&row.id), job)
            } else {
                (tui_key(&row.id), tjob)
            };
            match j.filter(AppJob::is_active) {
                Some(j) => ctx.send(Cmd::CancelAppJob {
                    key,
                    name: row.name.clone(),
                    job_id: j.id,
                }),
                None => store
                    .notice
                    .set(Some(format!("{}: no install is running", row.name))),
            }
        }
        // R11.3: the badge stops in one action, as the web badge's one click.
        AppVerb::Stop => act(ctx, AppVerb::Stop, None, false),
        AppVerb::Install | AppVerb::Update | AppVerb::InstallTerminal => {
            let what = match state.verb {
                AppVerb::Install if row.is_desktop() => format!(
                    "Install the {} on the gateway's computer (into the gateway's own Python)?",
                    row.name
                ),
                AppVerb::Install => format!(
                    "Install {}{}{}? It downloads from npm onto the gateway host and starts nothing.",
                    row.name,
                    if row.install_parts.iter().any(|p| p == "tui") { " for the browser and the terminal" } else { "" },
                    if row.needs_node_install { " (Node.js is installed for you first: about 56 MB, no password needed)" } else { "" }
                ),
                // The gateway's tooltip (the web button's), word for word.
                AppVerb::Update => match &row.update_tip {
                    Some(tip) => format!("{tip}."),
                    None => format!(
                        "Install the newest {} ({})?{}",
                        row.name,
                        row.latest_version.clone().unwrap_or_else(|| "latest".into()),
                        if row.running { " It is running: it restarts on the new version." } else { "" }
                    ),
                },
                _ => format!(
                    "{} {}'s terminal app? A ready-made download from its release, checked against its published checksums, into the gateway's own folder.",
                    if row.tui.as_ref().map(|x| x.installed).unwrap_or(false) { "Update" } else { "Install" },
                    row.name
                ),
            };
            let c = ctx.clone();
            let r = row.clone();
            let verb = state.verb;
            open_prompt(
                cx,
                ctx.ui,
                ChoicePrompt::new(what)
                    .option("go", state.label.clone())
                    .option("keep", "Not now")
                    .initial("go"),
                move |outcome| {
                    if let ChoiceOutcome::Answered(a) = outcome {
                        if a.selected.iter().any(|s| s == "go") {
                            c.send(Cmd::AppAct {
                                app_id: r.id.clone(),
                                name: r.name.clone(),
                                verb,
                                path: None,
                                start_first: false,
                            });
                        }
                    }
                },
            );
        }
    }
}

/// R11.3: the status badge's action (`s`, or Enter / Space on the badge
/// cell): Running → stop, Stopped → start (the Assistant: its Open). The
/// request is the one the web badge sends (POST /apps/{id}/stop | /launch).
fn run_badge(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let Some(row) = selected(ctx) else {
        store
            .notice
            .set(Some(match store.apps.overview.get_untracked() {
                Loadable::Ready(_) => "no app selected".into(),
                _ => "the apps are not loaded — r checks".into(),
            }));
        return;
    };
    let job = store
        .apps
        .job_for(&app_key(&row.id), row.active_job.as_ref());
    let Some(v) = badge_verb(&row, job.as_ref()) else {
        let (label, _) = status_label(&row, job.as_ref().map(AppJob::is_active).unwrap_or(false));
        store.notice.set(Some(format!(
            "{} — {label}: nothing to start or stop now",
            row.name
        )));
        return;
    };
    run_key(cx, ctx, Some(v.verb));
}

/// `n`: install Node.js (confirmed), or cancel its running install.
fn node_key(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let Some(d) = store.apps.overview.with_untracked(|o| o.ready().cloned()) else {
        store
            .notice
            .set(Some("the apps are not loaded — r checks".into()));
        return;
    };
    let admin = store.conn.with_untracked(is_admin);
    let job = store.apps.job_for(NODE_KEY, d.node.active_job.as_ref());
    if let Some(j) = job.filter(AppJob::is_active) {
        if !admin {
            store
                .notice
                .set(Some("Node.js install: only an admin can cancel".into()));
            return;
        }
        ctx.send(Cmd::CancelAppJob {
            key: NODE_KEY.into(),
            name: "Node.js".into(),
            job_id: j.id,
        });
        return;
    }
    if d.node.available {
        store.notice.set(Some(format!(
            "Node.js is ready{} — nothing to install",
            d.node
                .version
                .map(|v| format!(" ({v})"))
                .unwrap_or_default()
        )));
        return;
    }
    if !d.node.install_available {
        store.notice.set(Some(format!(
            "Install Node.js is not available here{}",
            d.node.message.map(|m| format!(": {m}")).unwrap_or_default()
        )));
        return;
    }
    if !admin {
        store.notice.set(Some(
            "Install Node.js: only an admin can install Node.js".into(),
        ));
        return;
    }
    if store.apps.is_pending(NODE_KEY) {
        store
            .notice
            .set(Some("Node.js: already working on it".into()));
        return;
    }
    let c = ctx.clone();
    open_prompt(
        cx,
        ctx.ui,
        ChoicePrompt::new(
            "Install Node.js into the gateway's own folder? About 56 MB, no password, no terminal.",
        )
        .option("go", "Install Node.js")
        .option("keep", "Not now")
        .initial("go"),
        move |outcome| {
            if let ChoiceOutcome::Answered(a) = outcome {
                if a.selected.iter().any(|s| s == "go") {
                    c.send(Cmd::InstallAppsNode);
                }
            }
        },
    );
}

/// `y`: pick one of the app's copyable lines (address, commands, the
/// failure's details) and put it on the clipboard.
fn copy_menu(cx: Scope, ctx: &Ctx) {
    let store = ctx.store;
    let Some(row) = selected(ctx) else {
        store
            .notice
            .set(Some("no app selected — nothing to copy".into()));
        return;
    };
    let note = store.apps.note_for(&app_key(&row.id));
    let tnote = store.apps.note_for(&tui_key(&row.id));
    let mut items = copyables(&row, note.as_ref());
    if let Some(n) = &tnote {
        for (l, c) in &n.commands {
            if !items.iter().any(|(_, x)| x == c) {
                items.push((l.clone(), c.clone()));
            }
        }
    }
    let job = store
        .apps
        .job_for(&app_key(&row.id), row.active_job.as_ref());
    if let Some(j) = job.filter(|j| !j.is_active()) {
        let log = j.log_text();
        if !log.is_empty() {
            items.push(("Install log".into(), log));
        }
    }
    for n in [note, tnote].into_iter().flatten() {
        if let Some(d) = n.details {
            items.push(("Failure details".into(), d));
        }
    }
    if items.is_empty() {
        store
            .notice
            .set(Some(format!("{}: nothing to copy", row.name)));
        return;
    }
    let mut prompt = ChoicePrompt::new(format!("Copy from {}", row.name));
    for (i, (label, value)) in items.iter().enumerate() {
        let first = value.lines().next().unwrap_or("");
        prompt = prompt.option_detail(
            i.to_string(),
            label.clone(),
            super::util::ellipsize(first, 90),
        );
    }
    prompt = prompt.option("keep", "Nothing");
    open_prompt(cx, ctx.ui, prompt, move |outcome| {
        if let ChoiceOutcome::Answered(a) = outcome {
            if let Some(i) = a.selected.first().and_then(|s| s.parse::<usize>().ok()) {
                if let Some((label, value)) = items.get(i) {
                    copy_to_clipboard(value.clone());
                    store.notice.set(Some(format!("copied: {label}")));
                }
            }
        }
    });
}

// ---------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------

/// The one-time sign-in link modal (also opened by Workflows' "Open in
/// AbstractFlow").
pub(crate) fn open_link_modal(cx: Scope, ctx: &Ctx, link: AppOpenLink) {
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(96, 16), move |mcx, close| {
        let theme = use_theme(mcx);
        let t = theme.get().tokens;
        let wrap_w = 88usize;
        let mut col = Element::new().style(LayoutStyle::column().gap(0));
        col = col.child(line(vec![span_bold(
            format!(
                "{}{} — signed-in link",
                link.name,
                if link.started { " started" } else { "" }
            ),
            t.accent,
        )]));
        col = col.child(line(vec![span(link.link.clone(), t.text)]));
        col = col.child(line(vec![span(
            format!(
                "Works once{}, only on this address; opening it signs this browser in to {} as you.",
                link.expires_in_s.map(|n| format!(", within {n} seconds")).unwrap_or_default(),
                link.name
            ),
            t.text_muted,
        )]));
        if let Some(u) = &link.app_url {
            col = col.child(line(vec![
                span("The app itself: ", t.text_faint),
                span(u.clone(), t.text_muted),
            ]));
        }
        // No screen in front of the person (SSH, or Linux without a
        // display): NEVER run a URL opener — say why, and the link to copy
        // is the way (with the tunnel when it names a loopback address).
        let no_display = ctx2.no_display.clone();
        if let Some(why) = &no_display {
            for l in wrap_text(
                &format!(
                    "No browser here: {why}. Copy the link (y) and open it on your own computer."
                ),
                wrap_w,
            ) {
                col = col.child(line(vec![span(l, t.warn)]));
            }
        }
        if let Some(h) = &link.tunnel_hint {
            for l in wrap_text(h, wrap_w) {
                col = col.child(line(vec![span(l, t.warn)]));
            }
        }
        let l_copy = link.link.clone();
        let l_open = link.link.clone();
        let store = ctx2.store;
        let ctx_open = ctx2.clone();
        let close_btn = close.clone();
        let close_open = close.clone();
        col = col.child(line(vec![span(String::new(), t.text)]));
        let mut buttons = Element::new()
            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
            .child(
                Button::new("Copy link (y)")
                    .on_click(move || {
                        copy_to_clipboard(l_copy.clone());
                        store.notice.set(Some("copied the one-time link".into()));
                    })
                    .element(mcx, &t)
                    .autofocus()
                    .build(),
            );
        if no_display.is_none() {
            buttons = buttons.child(
                Button::new("Open in a browser here (o)")
                    .on_click(move || {
                        // Not opened (e.g. NoDisplay): the modal stays, the
                        // notice says why and the link is right here.
                        if ctx_open.screens.open_url(&l_open).is_ok() {
                            close_open();
                        }
                    })
                    .element(mcx, &t)
                    .build(),
            );
        }
        col = col.child(
            buttons
                .child(
                    Button::new("Close (Esc)")
                        .on_click(move || close_btn())
                        .element(mcx, &t)
                        .build(),
                )
                .build(),
        );
        let l_y = link.link.clone();
        let l_o = link.link.clone();
        let ctx_o = ctx2.clone();
        let close_o = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .shortcut(KeyChord::plain(Key::Char('y')), move |_| {
                copy_to_clipboard(l_y.clone());
                store.notice.set(Some("copied the one-time link".into()));
            })
            .shortcut(KeyChord::plain(Key::Char('o')), move |_| {
                match &ctx_o.no_display {
                    Some(why) => store.notice.set(Some(format!(
                        "not opening a browser: {why} — copy the link (y)"
                    ))),
                    None => {
                        if ctx_o.screens.open_url(&l_o).is_ok() {
                            close_o();
                        }
                    }
                }
            })
            .child(col.build())
            .build()
    });
}

fn open_log_modal(cx: Scope, ctx: &Ctx, row: &AppRow) {
    let apps = ctx.store.apps;
    apps.log.set(Loadable::Loading);
    ctx.send(Cmd::LoadAppLog {
        app_id: row.id.clone(),
        tail: LOG_TAIL,
    });
    let size = super::preview_size(cx);
    // The pane's rows: the modal minus its title, head, footer, keys and
    // button lines and the border — so the newest lines fill the pane.
    let page = (size.h - 8).max(3);
    let ctx2 = ctx.clone();
    let id = row.id.clone();
    let name = row.name.clone();
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let top = mcx.signal(i32::MAX);
        let (c_ref, c_more, c_copy) = (ctx2.clone(), ctx2.clone(), ctx2.clone());
        let (id_ref, id_more) = (id.clone(), id.clone());
        let reload = move |c: &Ctx, app_id: &str, tail: u32| {
            c.store.apps.log.set(Loadable::Loading);
            c.send(Cmd::LoadAppLog {
                app_id: app_id.to_string(),
                tail,
            });
        };
        let close_btn = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .shortcut(KeyChord::plain(Key::Char('r')), move |_| {
                let tail = c_ref
                    .store
                    .apps
                    .log
                    .with_untracked(|l| l.ready().map(|x| x.tail))
                    .unwrap_or(LOG_TAIL);
                reload(&c_ref, &id_ref, tail);
            })
            .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
                let cur = c_more.store.apps.log.with_untracked(|l| l.ready().cloned());
                match cur {
                    Some(l) if l.can_show_more() => reload(
                        &c_more,
                        &id_more,
                        (l.tail * 4).min(crate::api::apps::APP_LOG_MAX),
                    ),
                    Some(_) => c_more.store.notice.set(Some(
                        "the whole log is shown (or the 5000-line ceiling is reached)".into(),
                    )),
                    None => {}
                }
            })
            .shortcut(KeyChord::plain(Key::Char('y')), move |_| {
                let text = c_copy
                    .store
                    .apps
                    .log
                    .with_untracked(|l| l.ready().map(|x| x.lines.join("\n")));
                match text {
                    Some(t) if !t.is_empty() => {
                        copy_to_clipboard(t);
                        c_copy.store.notice.set(Some("copied the log lines".into()));
                    }
                    _ => c_copy
                        .store
                        .notice
                        .set(Some("the log is empty — nothing to copy".into())),
                }
            })
            .child(line(vec![span_bold(format!("{name} — log"), t0.accent)]))
            .child(dyn_view_scoped(
                LayoutStyle::default().grow(1.0).min_h(3),
                move |pcx| {
                    let log = apps.log.get();
                    match &log {
                        Loadable::Ready(l) => {
                            let mut col =
                                Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
                            col = col.child(line(vec![span_bold(l.head(), t0.text)]));
                            if !l.lines.is_empty() {
                                col = col.child(scroll_lines(
                                    pcx,
                                    &t0,
                                    l.lines.join("\n"),
                                    top,
                                    page,
                                ));
                            }
                            let mut foot = Vec::new();
                            if l.capped() {
                                foot.push(span(
                                    "Older lines are in the log file.  ",
                                    t0.text_muted,
                                ));
                            }
                            if let Some(p) = &l.path {
                                foot.push(span("Log file ", t0.text_faint));
                                foot.push(span(p.clone(), t0.text_muted));
                            }
                            if !foot.is_empty() {
                                col = col.child(line(foot));
                            }
                            let mut keys = vec![span("r refresh", t0.text_faint)];
                            if l.can_show_more() {
                                keys.push(span("  ·  m show more", t0.text_faint));
                            }
                            if !l.lines.is_empty() {
                                keys.push(span("  ·  y copy", t0.text_faint));
                            }
                            col.child(line(keys)).build()
                        }
                        Loadable::Failed(e) => Element::new()
                            .style(LayoutStyle::column())
                            .child(line(vec![span_bold("Could not read the log", t0.error)]))
                            .child(super::util::error_panel(&t0, e))
                            .build(),
                        _ => line(vec![span("Reading the log...", t0.text_muted)]),
                    }
                },
            ))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Close (Esc)")
                            .on_click(move || close_btn())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

/// A read-only scrolling text pane that opens at the END (newest lines
/// in view, like the web panel). ↑/↓ line, PgUp/PgDn page, Home/End.
fn scroll_lines(_cx: Scope, t: &TokenSet, text: String, top: Signal<i32>, page: i32) -> View {
    let t0 = *t;
    let total = abstracttui::widgets::CodeView::line_count(&text) as i32;
    let max_top = (total - page).max(0);
    let cur = top.get().clamp(0, max_top);
    if cur != top.get_untracked() {
        top.set(cur);
    }
    Element::new()
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().grow(1.0))
        .shortcut(KeyChord::plain(Key::Down), move |_| {
            top.set((top.get_untracked() + 1).min(max_top))
        })
        .shortcut(KeyChord::plain(Key::Up), move |_| {
            top.set((top.get_untracked() - 1).max(0))
        })
        .shortcut(KeyChord::plain(Key::PageDown), move |_| {
            top.set((top.get_untracked() + page).min(max_top))
        })
        .shortcut(KeyChord::plain(Key::PageUp), move |_| {
            top.set((top.get_untracked() - page).max(0))
        })
        .shortcut(KeyChord::plain(Key::Home), move |_| top.set(0))
        .shortcut(KeyChord::plain(Key::End), move |_| top.set(max_top))
        .child(
            abstracttui::widgets::CodeView::new(text)
                .scroll_offset(cur)
                .layout(LayoutStyle::default().grow(1.0))
                .element(&t0)
                .build(),
        )
        .build()
}
