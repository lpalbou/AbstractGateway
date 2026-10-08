//! Per-entity configuration + state controls — the TUI half of the web
//! console's Manage drawer (parity audit items a-1..a-8).
//!
//! What lives here is what an operator does to an existing entity:
//! its identity card, Talk (the hosted visit — `entity_chat`), state
//! (wake/sleep/pause), mind substrate, voice triple (+ audition), work
//! order, own-time grant + loop, re-embed, verify. Summoning a NEW
//! entity and its spark templates live in `entity_create` (web parity:
//! the web console offers both).

use abstracttui::prelude::*;
use abstracttui::widgets::{ColWidth, Column, SubmitPolicy, Table};
use serde_json::{json, Value};

use super::util::{field, line, span, span_bold};
use super::w::Action;
use super::Ctx;
use crate::store::{EntityDetail, EntityRow, Loadable};
use crate::worker::Cmd;

/// The web Manage modal's tabs (console.py `#entity-manage-section`).
pub const MANAGE_TABS: [&str; 6] = [
    "Overview",
    "Talk",
    "Lifecycle",
    "Mind & voice",
    "Work & tools",
    "Prompt",
];

/// One card of a Manage tab: the web card's title and description, and
/// the form whose fields live inside it (inline).
#[derive(Clone, Debug)]
pub struct Section {
    pub tab: usize,
    pub title: &'static str,
    pub desc: &'static str,
    pub form: Option<SubForm>,
}

/// The web's sentence for Freeze now (the inline confirm).
pub const FREEZE_QUESTION: &str = "Freeze it now? Its process is killed, an open visit closes without reflection, and it refuses everything until Active is turned back on.";

/// Every Manage card in tab order: the web's titles and descriptions,
/// each with the form whose fields it holds.
pub fn manage_sections(_non_admin: bool) -> Vec<Section> {
    let sec = |tab, title, desc, form| Section {
        tab,
        title,
        desc,
        form,
    };
    vec![
        sec(0, "Right now", "What it is doing, read live every few seconds.", None),
        sec(0, "Identity", "Verify memory checks that its memory history and birth record were never altered. It only reads.", Some(SubForm::Card)),
        sec(0, "Memories from sleep waiting for your review", "Sleep proposes new long-term memories. Keep one with two independent records that back it up, or reject it with a reason.", Some(SubForm::Candidates)),
        sec(1, "Visit", "Talk with it in a hosted visit: the conversation becomes its memories, and closing the visit runs its reflection. A visit pauses its personal time until you close it.", Some(SubForm::Talk)),
        sec(2, "Awake or asleep", "On: it serves visits and work. Off: it sleeps, its memory consolidates, and the next visit wakes it.", Some(SubForm::State)),
        sec(2, "Personal time", "On: it explores on its own schedule and spends tokens without anyone watching. Off: it acts only when visited or given work.", Some(SubForm::OwnTime)),
        sec(2, "Emergency freeze", "Kills its personal-time process now and stops it: work in progress ends without reflection. For hard failures only; turn Active on in its Accounts row to bring it back.", Some(SubForm::Freeze)),
        sec(3, "Mind", "The model it thinks with. Changes save by themselves.", Some(SubForm::Mind)),
        sec(3, "Voice", "How it sounds when it speaks. Changes save by themselves.", Some(SubForm::Voice)),
        sec(3, "Danger zone: rebuild its memory index", "Only when the status below says MISMATCH: re-computes every memory's search vector with the gateway's embedding model. Type that model's name to confirm you mean it.", Some(SubForm::Reembed)),
        sec(4, "Work order", "A task it works on instead of its personal time, from its next day on, with the Work tools below. It says itself when the task is done or blocked.", Some(SubForm::Work)),
        sec(4, "Tools per phase", "Which tools it may use in each phase of its day. Each box saves when you tick it.", Some(SubForm::Tools)),
        sec(5, "Instructions", "Layers you may rewrite in its system prompt; an empty layer uses the built-in text. Its identity is never editable. Each layer saves when you leave it.", Some(SubForm::Prompt)),
    ]
}

/// A card's description as shown: the web's words, minus the web's
/// save-on-change sentence on the cards that keep a Save button here
/// (free text: one write per keystroke otherwise) — one rule per card.
pub fn shown_desc(title: &str, desc: &'static str) -> &'static str {
    let rule = match title {
        "Mind" | "Voice" => " Changes save by themselves.",
        "Tools per phase" => " Each box saves when you tick it.",
        "Instructions" => " Each layer saves when you leave it.",
        _ => return desc,
    };
    desc.strip_suffix(rule).unwrap_or(desc)
}

/// What the inline cards share with the Manage modal: the "unsaved
/// edit" predicates of the tab on screen (the modal's Close / Esc / ✕
/// and a tab switch ask "Discard changes?" when one holds).
type DirtyList = std::rc::Rc<std::cell::RefCell<Vec<std::rc::Rc<dyn Fn() -> bool>>>>;

#[derive(Clone, Default)]
pub(crate) struct Inline {
    dirty: DirtyList,
}

impl Inline {
    pub(crate) fn any_dirty(&self) -> bool {
        self.dirty.borrow().iter().any(|f| f())
    }
    fn clear(&self) {
        self.dirty.borrow_mut().clear();
    }
    /// Watch a card's own predicate (loaded data vs the fields).
    fn watch_fn(&self, f: impl Fn() -> bool + 'static) {
        self.dirty.borrow_mut().push(std::rc::Rc::new(f));
    }
    /// A Save card's typed fields against their saved values; returns the
    /// save's success callback (the saved values move to what was saved,
    /// the state line says "Saved").
    fn card_saved(
        &self,
        mcx: Scope,
        pairs: Vec<(Signal<String>, String)>,
        st: Signal<super::w::FieldState>,
    ) -> super::CloserFn {
        let base: Vec<(Signal<String>, Signal<String>)> =
            pairs.into_iter().map(|(f, v)| (f, mcx.signal(v))).collect();
        let b2 = base.clone();
        self.watch_fn(move || {
            b2.iter()
                .any(|(f, b)| f.get_untracked() != b.get_untracked())
        });
        std::rc::Rc::new(move || {
            for (f, b) in &base {
                b.set(f.get_untracked());
            }
            st.set(super::w::FieldState::Saved("Saved".into()));
        })
    }
    /// A Save card's success callback (no typed baseline to move).
    fn saved_fn(&self, st: Signal<super::w::FieldState>) -> super::CloserFn {
        std::rc::Rc::new(move || st.set(super::w::FieldState::Saved("Saved".into())))
    }
}

/// The forms whose fields live inside Manage's cards (each one's
/// buttons come from [`subform_actions`] — the single source for the
/// cards and the tests).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SubForm {
    State,
    Mind,
    Voice,
    Work,
    OwnTime,
    Freeze,
    Reembed,
    Tools,
    Prompt,
    Candidates,
    Card,
    Talk,
}

impl SubForm {
    pub const ALL: [SubForm; 12] = [
        SubForm::State,
        SubForm::Mind,
        SubForm::Voice,
        SubForm::Work,
        SubForm::OwnTime,
        SubForm::Freeze,
        SubForm::Reembed,
        SubForm::Tools,
        SubForm::Prompt,
        SubForm::Candidates,
        SubForm::Card,
        SubForm::Talk,
    ];
}

/// The buttons of form `f`, in order. (Manage has ONE Close; the Talk
/// panel's Close is for the standalone panel, `c` on Accounts.)
pub fn subform_actions(f: SubForm) -> Vec<Action> {
    let save = |tip: &str| Action::label("save", "Save").tooltip(tip.to_string());
    match f {
        SubForm::State => vec![],
        SubForm::Mind => vec![save("Save the provider and model it thinks with")],
        SubForm::Voice => vec![
            Action::label("audition", "Hear a sample")
                .tooltip("Speak a sample with the selection above (not saved)"),
            Action::label("play", "Play").tooltip("Play the sample on this computer"),
            save("Save its voice"),
        ],
        SubForm::Work => vec![
            Action::label("save", "Give this task").tooltip("Give it this task as its work order"),
            Action::label("end", "End the work order").tooltip("End the current work order"),
        ],
        SubForm::OwnTime => vec![
            Action::label("grant", "Grant (timer)")
                .tooltip("Allow personal time for the hours typed above"),
            Action::label("revoke", "Revoke grant").tooltip("Withdraw its personal-time grant"),
        ],
        SubForm::Freeze => vec![Action::label("freeze", "Freeze now")
            .tooltip("Kill its personal-time process now (no reflection)")
            .danger()],
        SubForm::Reembed => vec![Action::label("rebuild", "Rebuild index")
            .tooltip("Rebuild every memory vector (repair only)")
            .danger()],
        SubForm::Tools => vec![save("Save the changed phases")],
        SubForm::Prompt => vec![save("Save every layer of the overlay")],
        SubForm::Candidates => vec![
            Action::label("promote", "Promote (accept)")
                .tooltip("Keep it as a long-term memory (two independent records back it up)"),
            Action::label("reject", "Reject").tooltip("Reject it, with the reason typed above"),
            Action::label("reload", "Reload").tooltip("Read the candidates again"),
        ],
        SubForm::Card => vec![
            Action::label("verify", "Verify memory")
                .tooltip("Check its memory history and birth record (read only)"),
            Action::label("reload", "Reload").tooltip("Read the card again"),
        ],
        SubForm::Talk => vec![
            Action::label("open", "Open visit").tooltip("Open a visit and talk with it"),
            Action::label("send", "Send").tooltip("Send the message"),
            Action::label("close_visit", "Close visit")
                .tooltip("Close the visit: its reflection runs"),
            Action::label("close", "Close").tooltip("Close"),
        ],
    }
}

/// The button `id` of form `f` (from [`subform_actions`]; an id the list
/// does not have is a defect and fails loudly).
pub(crate) fn wb(
    cx: Scope,
    t: &TokenSet,
    f: SubForm,
    id: &str,
    on_press: impl FnMut() + 'static,
) -> View {
    let a = subform_actions(f)
        .into_iter()
        .find(|a| a.id == id)
        .unwrap_or_else(|| panic!("{f:?} has no {id} action"));
    super::w::action::button(cx, t, &a, super::w::action::On::Raised, true, on_press)
}

/// A Close that asks the form's guard first ("Discard changes?" on
/// unsaved edits), like Esc and the title ✕ (R15 F2).
pub(crate) fn guarded_close(close: &super::CloserFn, guard: &super::GuardSlot) -> super::CloserFn {
    let (close, guard) = (close.clone(), guard.clone());
    std::rc::Rc::new(move || {
        let handled = guard.borrow().as_ref().map(|g| g()).unwrap_or(false);
        if !handled {
            close();
        }
    })
}

/// The fields of form `f` for `entity`, inline in its card.
fn form_body(mcx: Scope, ctx: &Ctx, entity: &EntityRow, f: SubForm, inl: &Inline, w: i32) -> View {
    let n = entity.name.clone();
    match f {
        SubForm::State => state_card(mcx, ctx, entity.clone(), w),
        SubForm::Mind => substrate_body(mcx, ctx, n, inl, w),
        SubForm::Voice => voice_body(mcx, ctx, n, inl, w),
        SubForm::Work => work_body(mcx, ctx, n, inl, w),
        SubForm::OwnTime => own_time_body(mcx, ctx, n, w),
        SubForm::Freeze => freeze_body(mcx, ctx, n),
        SubForm::Reembed => reembed_body(mcx, ctx, n, inl, w),
        SubForm::Tools => tools_body(mcx, ctx, n, inl, w),
        SubForm::Prompt => prompt_body(mcx, ctx, n, inl, w),
        SubForm::Candidates => candidates_body(mcx, ctx, n, inl, w),
        SubForm::Card => {
            let t = use_theme(mcx).get().tokens;
            let c = ctx.clone();
            let n2 = n.clone();
            Element::new()
                .style(LayoutStyle::column().shrink(0.0))
                .child(super::entity_chat::card_body(mcx, ctx, n))
                .child(super::w::form::button_row(vec![wb(
                    mcx,
                    &t,
                    SubForm::Card,
                    "verify",
                    move || c.send(Cmd::EntityVerify { name: n2.clone() }),
                )]))
                .build()
        }
        SubForm::Talk => match super::entity_chat::talk_prepare(ctx, &n) {
            None => super::entity_chat::talk_body(mcx, ctx, n, None),
            Some(why) => {
                let t = use_theme(mcx).get().tokens;
                super::w::form::sentence(&t, &why, w, t.warn)
            }
        },
    }
}

/// Manage — <name> (the web's Manage modal): ONE FormModal — the web's
/// six tabs as a Segmented, and the selected tab's cards with their
/// fields INLINE (each card's own apply-on-change state line or Save).
/// The tab body scrolls when taller than the modal. Unsaved typed edits:
/// Close / Esc / ✕ and a tab switch ask "Discard changes?". Confirms
/// (sleep, pause, freeze, rebuild) stack over Manage and return to the
/// same tab.
pub fn open_manage_menu(cx: Scope, ctx: &Ctx, entity: EntityRow) {
    let store = ctx.store;
    // (Re)load the snapshot for THIS entity unless it is already warm.
    let warm = store
        .entity_detail
        .with_untracked(|d| d.ready().map(|d| d.name == entity.name).unwrap_or(false));
    if !warm {
        store.entity_detail.set(Loadable::Loading);
        ctx.send(Cmd::LoadEntityDetail {
            name: entity.name.clone(),
        });
    }
    let non_admin = store
        .conn
        .with_untracked(crate::store::ConnPhase::is_known_non_admin);
    let name = entity.name.clone();
    let ctx2 = ctx.clone();
    let lead = if non_admin {
        format!(
            "Currently {}. You are not an admin: changes to its state, mind, voice, work, tools, prompt and memories need an admin session.",
            entity.state
        )
    } else {
        format!("Currently {}.", entity.state)
    };
    super::w::FormModal::new(format!("Manage — {name}"))
        .lead(lead)
        .size(100, 60)
        .open(ctx, cx, move |mcx, close, guard, w| {
            let t0 = use_theme(mcx).get().tokens;
            let inl = Inline::default();
            {
                let inl = inl.clone();
                let esc_armed = mcx.signal(false);
                let form_error = mcx.signal(Option::<String>::None);
                super::install_dirty_guard_with(
                    mcx,
                    &guard,
                    move || inl.any_dirty(),
                    || {},
                    esc_armed,
                    form_error,
                );
            }
            // `tab` = the tab on screen; `pick` = the Segmented's choice (a
            // pick with unsaved edits asks first, and snaps back on Keep).
            let tab = mcx.signal(0usize);
            let pick = mcx.signal(0usize);
            let asking = mcx.signal(Option::<usize>::None);
            {
                let (inl, ui) = (inl.clone(), ctx2.ui);
                mcx.effect(move || {
                    let Some(i) = asking.get() else { return };
                    asking.set(None);
                    if !inl.any_dirty() {
                        tab.set(i);
                        return;
                    }
                    super::w::Confirm::danger(super::DISCARD_QUESTION, "Discard", "Keep editing")
                        .open_with(
                            mcx,
                            ui,
                            move || tab.set(i),
                            move || pick.set(tab.get_untracked()),
                        );
                });
            }
            let tabs = super::w::Segmented::new(MANAGE_TABS, Some(0))
                .bind(pick)
                .autofocus_chosen(true)
                .on_pick(move |i| {
                    if i == tab.get_untracked() {
                        return;
                    }
                    // The Segmented releases the pointer on the press that
                    // picks (6f84a4d): a "Discard changes?" may open at once.
                    asking.set(Some(i));
                })
                .view(mcx, &t0);
            let body = {
                let (ctx, entity, inl) = (ctx2.clone(), entity.clone(), inl.clone());
                dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |bcx| {
                    let t = use_theme(bcx).get().tokens;
                    let k = tab.get();
                    if pick.get_untracked() != k {
                        pick.set(k);
                    }
                    inl.clear();
                    let cw = (w - 2).max(20);
                    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                    for sec in manage_sections(non_admin)
                        .into_iter()
                        .filter(|s| s.tab == k)
                    {
                        col = col
                            .child(line(vec![span(String::new(), t.text)]))
                            .child(super::w::form::section(&t, sec.title))
                            .child(super::w::form::sentence(
                                &t,
                                shown_desc(sec.title, sec.desc),
                                cw,
                                t.text_muted,
                            ));
                        if sec.title == "Right now" {
                            let theme = abstracttui::reactive::untrack(|| use_theme(bcx));
                            col = col.child(
                                Element::new()
                                    .style(LayoutStyle::column().h(9).shrink(0.0))
                                    .child(inspector_view(bcx, &ctx, theme))
                                    .build(),
                            );
                        }
                        if let Some(f) = sec.form {
                            // Untracked: only a tab switch rebuilds the body
                            // (a data answer must never wipe typed edits).
                            let card = abstracttui::reactive::untrack(|| {
                                form_body(bcx, &ctx, &entity, f, &inl, cw)
                            });
                            col = col.child(card);
                        }
                    }
                    col.build()
                })
            };
            let body = Scroll::new(body)
                .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                .scrollbar_auto_hide(true)
                .view(mcx);
            let close_b = guarded_close(&close, &guard);
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(tabs)
                .child(body)
                .child(super::w::form::button_row(vec![super::w::action::button(
                    mcx,
                    &t0,
                    &Action::label("close", "Close").tooltip("Close"),
                    super::w::action::On::Raised,
                    true,
                    move || close_b(),
                )]))
                .build()
        });
}

/// Seconds since the epoch (the timer grant's clock).
fn now_epoch() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// The own-time timer grant, exactly as the web builds it
/// (console.py entityOwntimeToggle): `{"mode": "timer", "expires_at":
/// now + hours}` when "grant hours" holds a number > 0, None when it is
/// blank or not positive (no timed window), Err when it is not a number.
/// The gateway refuses a timer without `expires_at` (routes/entities.py).
pub fn timer_grant_body(hours: &str, now_epoch: u64) -> Result<Option<Value>, String> {
    let raw = hours.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    let h: f64 = raw
        .parse()
        .map_err(|_| format!("grant hours is not a number: '{raw}'"))?;
    if !h.is_finite() || h <= 0.0 {
        return Ok(None);
    }
    let expires = now_epoch + (h * 3600.0).round() as u64;
    Ok(Some(json!({
        "mode": "timer",
        "expires_at": super::sandbox::utc_iso(expires),
    })))
}

/// The `POST /entities/{name}/loop/start` body, as the web builds it
/// (console.py `entityOwntimeToggle`): a blank field is OMITTED so the
/// gateway applies its own default (the placeholders 20 / 8 / 30 only
/// describe that default); `ticks_per_day` is an integer. A field that
/// is not a number is refused with the reason rather than sent or
/// dropped (what you typed is what applies).
pub fn loop_start_body(tick_s: &str, ticks_day: &str, rest_min: &str) -> Result<Value, String> {
    let mut body = serde_json::Map::new();
    let tick = tick_s.trim();
    if !tick.is_empty() {
        let v: f64 = tick
            .parse()
            .map_err(|_| format!("tick seconds is not a number: '{tick}'"))?;
        body.insert("tick_seconds".into(), json!(v));
    }
    let ticks = ticks_day.trim();
    if !ticks.is_empty() {
        let v: u64 = ticks
            .parse()
            .map_err(|_| format!("ticks per day is not a whole number: '{ticks}'"))?;
        body.insert("ticks_per_day".into(), json!(v));
    }
    let rest = rest_min.trim();
    if !rest.is_empty() {
        let v: f64 = rest
            .parse()
            .map_err(|_| format!("rest minutes is not a number: '{rest}'"))?;
        body.insert("rest_minutes".into(), json!(v));
    }
    Ok(Value::Object(body))
}

/// Why an entity WRITE is refused before it is sent (None = send it).
/// Every write below is an admin route on the gateway
/// (`security/authorization.py`: the entities mutation pattern); forms
/// stay open to read, and their save lands the reason in the form.
fn write_refusal(ctx: &Ctx, what: &str) -> Option<String> {
    ctx.store.conn.with_untracked(|c| c.admin_refusal(what))
}

/// The selected entity's manage snapshot when it matches `name`.
fn detail_for(store: &crate::store::Store, name: &str) -> Option<EntityDetail> {
    store
        .entity_detail
        .with_untracked(|d| d.ready().filter(|d| d.name == name).cloned())
}

/// The right-drawer entity inspector: the full manage snapshot with
/// ROOM (the inline strip this replaces had three rows) — glanceable
/// beside the live roster (DrawerFocus::Passive keeps the keyboard on
/// the tables). Pure read; actions stay on `m`'s manage menu.
pub fn inspector_view(
    _cx: Scope,
    ctx: &Ctx,
    theme: Signal<&'static abstracttui::theme::Theme>,
) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    dyn_view_scoped(LayoutStyle::default().grow(1.0), move |gcx| {
        let t = theme.get().tokens;
        let idx = ui.entity_sel.get();
        let row = store
            .entities
            .with(|d| d.ready().and_then(|d| d.get(idx).cloned()));
        let Some(row) = row else {
            return line(vec![span("no entity selected", t.text_muted)]);
        };
        let mut col = Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(row.name.clone(), t.accent)]))
            .child(line(vec![
                span(format!("state: {}", row.state), t.text),
                span(
                    format!(
                        "  ·  mode: {}",
                        row.mode.clone().unwrap_or_else(|| "—".into())
                    ),
                    t.text_muted,
                ),
            ]));
        if let Some(h) = &row.handle {
            col = col.child(line(vec![span(format!("handle: {h}"), t.text_muted)]));
        }
        col = col.child(line(vec![span(
            format!(
                "drives — questions: {}  problems: {}  interests: {}",
                row.open_questions
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "—".into()),
                row.open_problems
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "—".into()),
                row.open_interests
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "—".into()),
            ),
            t.text_muted,
        )]));
        col = col.child(line(vec![span(String::new(), t.text)]));
        let name = row.name.clone();
        match store.entity_detail.get() {
            Loadable::Ready(d) if d.name == name => {
                col = col
                    .child(line(vec![
                        span_bold("mind      ".to_string(), t.text),
                        span(
                            d.substrate
                                .as_ref()
                                .map(|(p, m)| format!("{p} / {m}"))
                                .unwrap_or_else(|| "unset".into()),
                            t.text,
                        ),
                        span(
                            if d.substrate_source.is_empty() {
                                String::new()
                            } else {
                                format!("  ({})", d.substrate_source)
                            },
                            t.text_faint,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("voice     ".to_string(), t.text),
                        span(
                            d.voice
                                .as_ref()
                                .map(|(p, m, v)| format!("{p} / {m} / {v}"))
                                .unwrap_or_else(|| "unset".into()),
                            t.text,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("effective ".to_string(), t.text),
                        span(
                            d.voice_effective.clone().unwrap_or_else(|| "—".into()),
                            t.text_muted,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("work order".to_string(), t.text),
                        span(
                            format!(" {}", d.work_order.clone().unwrap_or_else(|| "none".into())),
                            t.text,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("own time  ".to_string(), t.text),
                        span(
                            format!(
                                "loop {}{}",
                                match d.loop_running {
                                    Some(true) => "running",
                                    Some(false) => "stopped",
                                    None => "unreported",
                                },
                                d.loop_phase
                                    .as_ref()
                                    .map(|p| format!(" (phase {p})"))
                                    .unwrap_or_default()
                            ),
                            t.text,
                        ),
                    ]))
                    .child(line(vec![
                        span_bold("grant     ".to_string(), t.text),
                        span(
                            format!(" {}", d.grant.clone().unwrap_or_else(|| "none".into())),
                            t.text_muted,
                        ),
                    ]));
            }
            Loadable::Failed(e) => {
                col = col.child(line(vec![span(
                    format!("detail read failed — {}", e.message),
                    t.error,
                )]));
            }
            _ => {
                col = col.child(line(vec![span(
                    format!("⟳ reading {name}'s configuration…"),
                    t.info,
                )]));
            }
        }
        col = col
            .child(line(vec![span(String::new(), t.text)]))
            .child(line(vec![span(
                "m = manage actions · c = talk · i closes this panel · n summons a new entity",
                t.text_faint,
            )]));
        Scroll::new(col.build()).view(gcx)
    })
}

// ---------------------------------------------------------------------
// State: awake / asleep (+dream) / paused
// ---------------------------------------------------------------------

/// The states the web's Lifecycle tab can ask for, plus the kill switch.
pub const STATE_CHOICES: [&str; 4] = ["awake", "asleep", "asleep + dream pass", "paused"];
/// The web's sleep confirmation (Lifecycle → Awake off).
pub const SLEEP_QUESTION: &str =
    "Put it to sleep? An open visit closes and its reflection runs first.";
/// The web's Reason field help (Lifecycle).
pub const STATE_REASON_HELP: &str = "Optional: recorded in its history with the next change you make on this tab, and given to it when it wakes.";

/// The state write's body for choice `pick` (STATE_CHOICES) and a reason.
pub fn state_body(pick: usize, reason: &str) -> Value {
    let state = match pick {
        1 | 2 => "asleep",
        3 => "paused",
        _ => "awake",
    };
    let mut body = json!({ "state": state });
    if pick == 2 {
        body["dream"] = Value::Bool(true);
    }
    let r = reason.trim();
    if !r.is_empty() {
        body["reason"] = Value::String(r.to_string());
    }
    body
}

/// Arm a field's state line on the journal: after `arm()` the line says
/// "Saving…", then the verified outcome of the next journal entry whose
/// action starts with `prefix` (the worker's write + GET verification).
fn journal_state(
    mcx: Scope,
    store: crate::store::Store,
    prefix: String,
    state: Signal<super::w::FieldState>,
) -> std::rc::Rc<dyn Fn()> {
    let armed = mcx.signal(Option::<usize>::None);
    mcx.effect(move || {
        let Some(from) = armed.get() else { return };
        let hit = store.journal.with(|j| {
            j.iter()
                .skip(from)
                .find(|e| e.action.starts_with(&prefix))
                .cloned()
        });
        if let Some(e) = hit {
            armed.set(None);
            state.set(match (&e.outcome, &e.verified) {
                (Err(err), _) => super::w::FieldState::Refused(err.clone()),
                (Ok(_), Some(Err(v))) => super::w::FieldState::Refused(v.clone()),
                (Ok(_), Some(Ok(v))) => super::w::FieldState::Saved(format!("Saved — {v}")),
                (Ok(_), None) => super::w::FieldState::Saved("Saved".into()),
            });
        }
    });
    std::rc::Rc::new(move || {
        state.set(super::w::FieldState::Saving);
        armed.set(Some(store.journal.with_untracked(Vec::len)));
    })
}

/// Awake or asleep (the web's Lifecycle card): the state applies at once
/// (a Segmented), sleep asks the web's question, the kill switch asks a
/// danger confirm; the Reason rides with the next change.
fn state_card(mcx: Scope, ctx: &Ctx, entity: EntityRow, w: i32) -> View {
    let ctx2 = ctx.clone();
    {
        let t0 = use_theme(mcx).get().tokens;
        let current = match entity.state.as_str() {
            "asleep" => 1usize,
            "paused" => 3usize,
            _ => 0usize,
        };
        let pick = mcx.signal(current);
        let applied = mcx.signal(current);
        let reason = mcx.signal(String::new());
        // A non-admin reads the state; the segments say why they are off.
        let refused = write_refusal(&ctx2, "changing an entity's state");
        let st = mcx.signal(match &refused {
            Some(why) => super::w::FieldState::Refused(why.clone()),
            None => super::w::FieldState::Idle,
        });
        let arm = journal_state(
            mcx,
            ctx2.store,
            format!("POST entity '{}' state", entity.name),
            st,
        );
        let send = {
            let (ctx, n, arm) = (ctx2.clone(), entity.name.clone(), arm.clone());
            std::rc::Rc::new(move |p: usize| {
                if let Some(why) = write_refusal(&ctx, "changing an entity's state") {
                    st.set(super::w::FieldState::Refused(why));
                    pick.set(applied.get_untracked());
                    return;
                }
                applied.set(p);
                arm();
                ctx.send(Cmd::EntityState {
                    name: n.clone(),
                    body: state_body(p, &reason.get_untracked()).into(),
                });
            })
        };
        let asking = mcx.signal(Option::<usize>::None);
        {
            let (ctx, n, send) = (ctx2.clone(), entity.name.clone(), send.clone());
            mcx.effect(move || {
                    let Some(p) = asking.get() else { return };
                    asking.set(None);
                    let send = send.clone();
                    let undo = move || pick.set(applied.get_untracked());
                    match p {
                        1 | 2 => super::w::Confirm::plain(SLEEP_QUESTION, "Sleep", "Cancel")
                            .open_with(mcx, ctx.ui, move || send(p), undo),
                        _ => super::w::Confirm::danger(
                            format!("Pause {n}? The kill switch never clears by itself: it stays paused until you change its state here."),
                            "Pause",
                            "Cancel",
                        )
                        .open_with(mcx, ctx.ui, move || send(p), undo),
                    }
                });
        }
        let seg = {
            let send = send.clone();
            let mut sg = super::w::Segmented::new(STATE_CHOICES, Some(current))
                .bind(pick)
                .tip(2, "Sleep, then run a dream pass")
                .tip(3, "The kill switch: never clears by itself")
                .on_pick(move |p| {
                    if p == applied.get_untracked() {
                        return;
                    }
                    if p == 0 {
                        send(p);
                        return;
                    }
                    // The question opens at once: the Segmented releases
                    // the pointer on the press that picks (shared layer
                    // 6f84a4d), so the dialog takes the release.
                    asking.set(Some(p));
                });
            if let Some(why) = &refused {
                for i in 0..STATE_CHOICES.len() {
                    sg = sg.disable(i, why.clone());
                }
            }
            sg.view(mcx, &t0)
        };
        Element::new()
                .style(LayoutStyle::column().gap(0).shrink(0.0))
                .child(super::w::form::sentence(
                    &t0,
                    &format!("Now: {}. State is the operator's intent; the loop and visits settle behind it.", entity.state),
                    w,
                    t0.text_faint,
                ))
                .child(field(&t0, "State", seg))
                .child(super::w::state_line(st, w))
                .child(field(
                    &t0,
                    "Reason",
                    TextInput::new()
                        .value(reason)
                        .layout(LayoutStyle::default().w(52).h(1))
                        .element(mcx, &t0)
                        .build(),
                ))
                .child(super::w::form::sentence(&t0, STATE_REASON_HELP, w, t0.text_muted))
                .build()
    }
}

// ---------------------------------------------------------------------
// Substrate: provider / model
// ---------------------------------------------------------------------

fn substrate_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    let st = mcx.signal(super::w::FieldState::Idle);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let detail = detail_for(&store, &name);
    let (p0, m0) = detail
        .as_ref()
        .and_then(|d| d.substrate.clone())
        .unwrap_or_default();
    let provider = mcx.signal(p0.clone());
    let model = mcx.signal(m0.clone());
    let form_error = mcx.signal(Option::<String>::None);
    let in_flight = mcx.signal(false);
    let form_id = crate::worker::next_form_id();
    let close = inl.card_saved(mcx, vec![(provider, p0.clone()), (model, m0.clone())], st);
    super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close);

    let ctx_save = ctx2.clone();
    let name_save = name.clone();
    let source = detail
        .as_ref()
        .map(|d| d.substrate_source.clone())
        .unwrap_or_default();
    Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span(
                match &detail {
                    Some(_) => format!(
                        "current: {} (source: {})",
                        if p0.is_empty() {
                            "unset".to_string()
                        } else {
                            format!("{p0} / {m0}")
                        },
                        if source.is_empty() { "?" } else { &source }
                    ),
                    None => "current values still loading — what you type wins".to_string(),
                },
                t0.text_muted,
            )]))
            .child(line(vec![span(
                "one substrate per entity: visits AND the own-time loop resolve this pair",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "provider",
                TextInput::new()
                    .value(provider)
                    .placeholder("e.g. lmstudio, endpoint:ovh-provider")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(44).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "model",
                TextInput::new()
                    .value(model)
                    .placeholder("e.g. ornith-1.0-35b, gpt-oss-120b")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(44).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(super::w::state_line(st, w))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            wb(bcx, &t, SubForm::Mind, "save", move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let p = provider.get_untracked().trim().to_string();
                                    let m = model.get_untracked().trim().to_string();
                                    if p.is_empty() || m.is_empty() {
                                        form_error.set(Some(
                                            "both provider and model are required — the gateway refuses substrate-less entities".into(),
                                        ));
                                        return;
                                    }
                                    if let Some(why) = write_refusal(&ctx_s, "saving the mind substrate") {
                                        form_error.set(Some(why));
                                        return;
                                    }
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::SaveEntitySubstrate {
                                        name: n.clone(),
                                        body: json!({ "provider": p, "model": m }).into(),
                                        form_id: Some(form_id),
                                    });
                                }),
                        )
                        .build()
                },
            ))
            .build()
}

// ---------------------------------------------------------------------
// Voice: provider / model / voice (+ clear)
// ---------------------------------------------------------------------

fn voice_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let store = ctx.store;
    // A previous audition (maybe of another entity) never dresses this form.
    store.entity_audition.set(Loadable::NotAsked);
    let ctx2 = ctx.clone();
    let st = mcx.signal(super::w::FieldState::Idle);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let detail = detail_for(&store, &name);
    let (p0, m0, v0) = detail
        .as_ref()
        .and_then(|d| d.voice.clone())
        .unwrap_or_default();
    let provider = mcx.signal(p0.clone());
    let model = mcx.signal(m0.clone());
    let voice = mcx.signal(v0.clone());
    let clear = mcx.signal(false);
    let form_error = mcx.signal(Option::<String>::None);
    let in_flight = mcx.signal(false);
    let form_id = crate::worker::next_form_id();
    let close = inl.card_saved(
        mcx,
        vec![
            (provider, p0.clone()),
            (model, m0.clone()),
            (voice, v0.clone()),
        ],
        st,
    );
    super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close);

    let effective = detail
        .as_ref()
        .and_then(|d| d.voice_effective.clone())
        .unwrap_or_else(|| "unknown".into());
    let ctx_save = ctx2.clone();
    let name_save = name.clone();
    let name_aud = name.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![span(
            format!("applies now: {effective}"),
            t0.text_muted,
        )]))
        .child(line(vec![span(
            "the FULL triple is required together — a bare voice id leaks across providers",
            t0.text_faint,
        )]))
        .child(field(
            &t0,
            "provider",
            TextInput::new()
                .value(provider)
                .placeholder("e.g. supertonic, openai")
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(40).h(1))
                .element(mcx, &t0)
                .build(),
        ))
        .child(field(
            &t0,
            "model",
            TextInput::new()
                .value(model)
                .placeholder("e.g. supertonic-3, gpt-4o-mini-tts")
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(40).h(1))
                .element(mcx, &t0)
                .build(),
        ))
        .child(field(
            &t0,
            "voice",
            TextInput::new()
                .value(voice)
                .placeholder("e.g. M3, alloy — pickable in Multimodal → output.voice")
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(40).h(1))
                .element(mcx, &t0)
                .build(),
        ))
        .child(field(
            &t0,
            "",
            super::w::Toggle::switch("clear the set voice (fall back to gateway default)", clear)
                .view(mcx, &t0),
        ))
        .child(super::message_slot(theme, form_error, in_flight))
        .child(super::w::state_line(st, w))
        .child(audition_view(store, theme, name_aud))
        .child(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |bcx| {
                let t = theme.get().tokens;
                let ctx_s = ctx_save.clone();
                let ctx_a = ctx_save.clone();
                let n = name_save.clone();
                let n_a = name_save.clone();
                Element::new()
                    .style(LayoutStyle::row().gap(2))
                    .child(wb(bcx, &t, SubForm::Voice, "audition", move || {
                        // The UNSAVED selection, spoken as the
                        // entity (web parity: Save makes it his).
                        // Untracked guard, not a disabled flag: a
                        // tracked flag would rebuild this row and
                        // drop the keyboard focus mid-form.
                        if store.entity_audition.with_untracked(Loadable::is_loading) {
                            return;
                        }
                        let p = provider.get_untracked().trim().to_string();
                        let m = model.get_untracked().trim().to_string();
                        let v = voice.get_untracked().trim().to_string();
                        if p.is_empty() || m.is_empty() {
                            form_error.set(Some(
                                "select at least a provider and model to audition.".into(),
                            ));
                            return;
                        }
                        form_error.set(None);
                        ctx_a.store.entity_audition.set(Loadable::Loading);
                        ctx_a.send(Cmd::Entity(
                            crate::worker::entities::EntityCmd::VoiceAudition {
                                name: n_a.clone(),
                                provider: p,
                                model: m,
                                voice: (!v.is_empty()).then_some(v),
                            },
                        ));
                    }))
                    .child(wb(bcx, &t, SubForm::Voice, "save", move || {
                        if in_flight.get_untracked() {
                            return;
                        }
                        let body = if clear.get_untracked() {
                            json!({ "clear": true })
                        } else {
                            let p = provider.get_untracked().trim().to_string();
                            let m = model.get_untracked().trim().to_string();
                            let v = voice.get_untracked().trim().to_string();
                            if p.is_empty() || m.is_empty() {
                                form_error.set(Some(
                                    "provider and model are required (or check clear)".into(),
                                ));
                                return;
                            }
                            let mut b = json!({ "provider": p, "model": m });
                            if !v.is_empty() {
                                b["voice"] = Value::String(v);
                            }
                            b
                        };
                        if let Some(why) = write_refusal(&ctx_s, "saving the voice") {
                            form_error.set(Some(why));
                            return;
                        }
                        form_error.set(None);
                        in_flight.set(true);
                        ctx_s.send(Cmd::SaveEntityVoice {
                            name: n.clone(),
                            body: body.into(),
                            form_id: Some(form_id),
                        });
                    }))
                    .build()
            },
        ))
        .build()
}

/// The audition's outcome inside the voice form — the terminal's twin
/// of the web's inline audio player: the audio file's path (and a Play
/// button only when a local command-line player exists).
fn audition_view(
    store: crate::store::Store,
    theme: Signal<&'static abstracttui::theme::Theme>,
    name: String,
) -> View {
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |acx| {
        let t = theme.get().tokens;
        match store.entity_audition.get() {
            Loadable::NotAsked => Element::new().style(LayoutStyle::default().h(0)).build(),
            Loadable::Loading => line(vec![span("⟳ Synthesizing… (up to 25s)", t.info)]),
            Loadable::Failed(e) => line(vec![span(format!("Audition failed: {e}"), t.error)]),
            Loadable::Ready(o) if o.entity != name => {
                Element::new().style(LayoutStyle::default().h(0)).build()
            }
            Loadable::Ready(o) => {
                let mut col = Element::new().style(LayoutStyle::column().gap(0));
                for l in super::util::wrap_text(&o.summary, 76) {
                    col = col.child(line(vec![span(l, t.ok)]));
                }
                if let Some(err) = &o.error {
                    col = col.child(line(vec![span(err.clone(), t.error)]));
                }
                if let Some(path) = &o.path {
                    col = col.child(line(vec![
                        span("audio saved: ", t.text_muted),
                        span(
                            format!("{path} ({})", crate::store::human_bytes(o.bytes as u64)),
                            t.text,
                        ),
                    ]));
                    match &o.player {
                        Some(player) => {
                            let (pl, pa) = (player.clone(), path.clone());
                            col = col.child(
                                Element::new()
                                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                                    .child(wb(acx, &t, SubForm::Voice, "play", move || {
                                        let note =
                                            match crate::api::entities::spawn_player(&pl, &pa) {
                                                Ok(()) => format!("playing {pa} with {pl}"),
                                                Err(e) => format!("{pl} failed to start: {e}"),
                                            };
                                        store.notice.set(Some(note));
                                    }))
                                    .build(),
                            );
                        }
                        None => {
                            col = col.child(line(vec![span(
                                "no command-line audio player found on PATH (afplay, paplay, aplay, ffplay) — open the file yourself",
                                t.text_faint,
                            )]));
                        }
                    }
                }
                col.build()
            }
        }
    })
}

// ---------------------------------------------------------------------
// Work order: set / clear
// ---------------------------------------------------------------------

fn work_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    let st = mcx.signal(super::w::FieldState::Idle);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let detail = detail_for(&store, &name);
    let o0 = detail
        .as_ref()
        .and_then(|d| d.work_order.clone())
        .unwrap_or_default();
    let order = mcx.signal(o0.clone());
    let clear = mcx.signal(false);
    let form_error = mcx.signal(Option::<String>::None);
    let in_flight = mcx.signal(false);
    let form_id = crate::worker::next_form_id();
    let close = inl.card_saved(mcx, vec![(order, o0.clone())], st);
    super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close);

    let ctx_save = ctx2.clone();
    let name_save = name.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![span(
            "what the entity should work on during its own time",
            t0.text_faint,
        )]))
        .child(field(
            &t0,
            "order",
            TextInput::new()
                .value(order)
                .placeholder("the task it works on")
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(56).h(1))
                .element(mcx, &t0)
                .build(),
        ))
        .child(super::message_slot(theme, form_error, in_flight))
        .child(super::w::state_line(st, w))
        .child(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |bcx| {
                let t = theme.get().tokens;
                let ctx_s = ctx_save.clone();
                let n = name_save.clone();
                Element::new()
                    .style(LayoutStyle::row().gap(2))
                    .child(wb(bcx, &t, SubForm::Work, "save", move || {
                        if in_flight.get_untracked() {
                            return;
                        }
                        let body = if clear.get_untracked() {
                            json!({ "clear": true })
                        } else {
                            let o = order.get_untracked().trim().to_string();
                            if o.is_empty() {
                                form_error.set(Some(
                                    "type the task first — End the work order removes it".into(),
                                ));
                                return;
                            }
                            json!({ "order": o })
                        };
                        if let Some(why) = write_refusal(&ctx_s, "saving the work order") {
                            form_error.set(Some(why));
                            return;
                        }
                        form_error.set(None);
                        in_flight.set(true);
                        ctx_s.send(Cmd::SaveEntityWorkOrder {
                            name: n.clone(),
                            body: body.into(),
                            form_id: Some(form_id),
                        });
                    }))
                    .child(wb(bcx, &t, SubForm::Work, "end", {
                        let (ctx_e, n_e) = (ctx_save.clone(), name_save.clone());
                        move || {
                            if in_flight.get_untracked() {
                                return;
                            }
                            if let Some(why) = write_refusal(&ctx_e, "ending the work order") {
                                form_error.set(Some(why));
                                return;
                            }
                            form_error.set(None);
                            in_flight.set(true);
                            ctx_e.send(Cmd::SaveEntityWorkOrder {
                                name: n_e.clone(),
                                body: json!({ "clear": true }).into(),
                                form_id: Some(form_id),
                            });
                        }
                    }))
                    .build()
            },
        ))
        .build()
}

// ---------------------------------------------------------------------
// Own time: personal grant + loop start/stop/freeze
// ---------------------------------------------------------------------

/// The web's Schedule help (Lifecycle → Personal time).
pub const SCHEDULE_HELP: &str =
    "Used the next time Personal time is switched on. Blank fields keep the defaults.";

/// The web's Personal time switch description (Lifecycle).
pub const PERSONAL_TIME_DESC: &str = "On: it explores on its own schedule and spends tokens without anyone watching. Off: it acts only when visited or given work.";

/// Personal time (the web's Lifecycle card): the switch applies at once
/// (on = start the loop with the schedule below, off = a graceful stop);
/// the grant buttons; Freeze now on THE danger confirm.
fn own_time_body(mcx: Scope, ctx: &Ctx, name: String, w: i32) -> View {
    let store = ctx.store;
    let ctx2 = ctx.clone();
    {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        // The web's "grant hours" (entity-grant-hours): blank = the start
        // itself arms an until-revoked grant; N > 0 = a timed window.
        let grant_hours = mcx.signal(String::new());
        // Empty = the gateway's default (the placeholders say which),
        // as on the web: a blank field is omitted from the start body.
        let tick_s = mcx.signal(String::new());
        let ticks_day = mcx.signal(String::new());
        let rest_min = mcx.signal(String::new());
        let st = mcx.signal(super::w::FieldState::Idle);
        let arm = journal_state(mcx, store, format!("POST entity '{name}' loop"), st);
        let switch = {
            let (n, ctx, arm) = (name.clone(), ctx2.clone(), arm.clone());
            dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |scx| {
                let t = use_theme(scx).get().tokens;
                let on = store.entity_detail.with(|d| match d {
                    Loadable::Ready(d) if d.name == n => d.loop_running == Some(true),
                    _ => false,
                });
                let (n, ctx, arm) = (n.clone(), ctx.clone(), arm.clone());
                super::w::Toggle::new(on)
                    .label("Personal time")
                    .tip(PERSONAL_TIME_DESC)
                    .refused(write_refusal(
                        &ctx,
                        "starting or stopping an entity's own time",
                    ))
                    .on_change(move |want| {
                        if want {
                            // Blank fields are omitted (the gateway's
                            // defaults apply, as on the web); garbage
                            // is refused with the reason.
                            let loop_body = match loop_start_body(
                                &tick_s.get_untracked(),
                                &ticks_day.get_untracked(),
                                &rest_min.get_untracked(),
                            ) {
                                Ok(b) => b,
                                Err(e) => {
                                    st.set(super::w::FieldState::Refused(e));
                                    return;
                                }
                            };
                            // A timed window is the one case the start
                            // cannot express: the timer grant goes FIRST
                            // (the worker lane is serial).
                            let timer =
                                match timer_grant_body(&grant_hours.get_untracked(), now_epoch()) {
                                    Ok(t) => t,
                                    Err(e) => {
                                        st.set(super::w::FieldState::Refused(e));
                                        return;
                                    }
                                };
                            if let Some(body) = timer {
                                ctx.send(Cmd::SavePersonalGrant {
                                    name: n.clone(),
                                    body: body.into(),
                                });
                            }
                            arm();
                            ctx.send(Cmd::EntityLoop {
                                name: n.clone(),
                                start: true,
                                body: loop_body.into(),
                            });
                        } else {
                            arm();
                            ctx.send(Cmd::EntityLoop {
                                name: n.clone(),
                                start: false,
                                body: json!({ "mode": "graceful",
                                                  "reason": "operator stop via console-tui" })
                                .into(),
                            });
                        }
                    })
                    .view(scx, &t)
            })
        };
        let status = {
            let n = name.clone();
            dyn_view(LayoutStyle::column().shrink(0.0), move || {
                let t = theme.get().tokens;
                match store.entity_detail.get() {
                    Loadable::Ready(d) if d.name == n => Element::new()
                        .style(LayoutStyle::column())
                        .child(line(vec![span(
                            format!(
                                "loop: {}{}",
                                match d.loop_running {
                                    Some(true) => "running",
                                    Some(false) => "stopped",
                                    None => "unreported",
                                },
                                d.loop_phase
                                    .as_ref()
                                    .map(|p| format!(" (phase {p})"))
                                    .unwrap_or_default()
                            ),
                            t.text,
                        )]))
                        .child(line(vec![span(
                            format!(
                                "grant: {}",
                                d.grant.clone().unwrap_or_else(|| "none reported".into())
                            ),
                            t.text_muted,
                        )]))
                        .build(),
                    Loadable::Failed(e) => line(vec![span(
                        format!("status read failed: {}", e.message),
                        t.error,
                    )]),
                    _ => line(vec![span("⟳ reading own-time status…", t.info)]),
                }
            })
        };
        let input = |sig: Signal<String>, ph: &str, wd: i32| {
            TextInput::new()
                .value(sig)
                .placeholder(ph.to_string())
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(wd).h(1))
                .element(mcx, &t0)
                .build()
        };
        let grant = {
            let (n, ctx) = (name.clone(), ctx2.clone());
            move || {
                if !super::util::admin_gate(&ctx.store, "granting own time") {
                    return;
                }
                // The gateway refuses a timer without its expiry
                // (routes/entities.py): the window is "Hours allowed".
                match timer_grant_body(&grant_hours.get_untracked(), now_epoch()) {
                    Ok(Some(body)) => ctx.send(Cmd::SavePersonalGrant {
                        name: n.clone(),
                        body: body.into(),
                    }),
                    Ok(None) => ctx.store.notice.set(Some(
                        "type the hours allowed first — a timer grant needs its window".into(),
                    )),
                    Err(e) => ctx.store.notice.set(Some(e)),
                }
            }
        };
        let revoke = {
            let (n, ctx) = (name.clone(), ctx2.clone());
            move || {
                if !super::util::admin_gate(&ctx.store, "revoking own time") {
                    return;
                }
                ctx.send(Cmd::SavePersonalGrant {
                    name: n.clone(),
                    body: json!({ "mode": "disabled" }).into(),
                });
            }
        };
        Element::new()
            .style(LayoutStyle::column().gap(0).shrink(0.0))
            .child(status)
            .child(switch)
            .child(super::w::state_line(st, w))
            .child(super::w::form::section(&t0, "Schedule"))
            .child(super::w::form::sentence(
                &t0,
                SCHEDULE_HELP,
                w,
                t0.text_muted,
            ))
            .child(field(&t0, "Seconds between steps", input(tick_s, "20", 10)))
            .child(field(&t0, "Steps per day", input(ticks_day, "8", 10)))
            .child(field(&t0, "Rest minutes", input(rest_min, "30", 10)))
            .child(field(
                &t0,
                "Hours allowed",
                input(grant_hours, "blank = until revoked", 24),
            ))
            .child(super::w::form::button_row(vec![
                wb(mcx, &t0, SubForm::OwnTime, "grant", grant),
                wb(mcx, &t0, SubForm::OwnTime, "revoke", revoke),
            ]))
            .build()
    }
}

/// The web's Emergency freeze card: Freeze now on THE danger confirm
/// (it stacks over Manage; Cancel returns to the same tab).
fn freeze_body(mcx: Scope, ctx: &Ctx, name: String) -> View {
    let t0 = use_theme(mcx).get().tokens;
    let c = ctx.clone();
    super::w::form::button_row(vec![wb(mcx, &t0, SubForm::Freeze, "freeze", move || {
        if !super::util::admin_gate(&c.store, "freezing an entity's own time") {
            return;
        }
        let (n, c2) = (name.clone(), c.clone());
        let c3 = c2.clone();
        super::w::Confirm::danger(FREEZE_QUESTION, "Freeze", "Cancel").open(
            mcx,
            c2.ui,
            move || {
                c3.send(Cmd::EntityLoop {
                    name: n,
                    start: false,
                    body: json!({ "mode": "freeze",
                    "reason": "operator emergency freeze via console-tui" })
                    .into(),
                });
            },
        );
    })])
}

// ---------------------------------------------------------------------
// Re-embed (operator repair — danger-gated)
// ---------------------------------------------------------------------

/// The web's rebuild confirmation (Mind & voice → Danger zone).
pub const REEMBED_QUESTION: &str =
    "Rebuild every memory vector now? The swap is atomic and recorded in its history.";

fn reembed_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let ctx2 = ctx.clone();
    let st = mcx.signal(super::w::FieldState::Idle);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let model = mcx.signal(String::new());
    let reason = mcx.signal(String::new());
    let name2 = name.clone();
    let ctx3 = ctx2.clone();
    let close = inl.card_saved(
        mcx,
        vec![(model, String::new()), (reason, String::new())],
        st,
    );
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![span(
            "vectors are a derived index over engraved text; re-embedding rewrites it",
            t0.text_muted,
        )]))
        .child(line(vec![span(
            "all-or-nothing, takes the home lease, shifts semantic neighborhoods — repair only",
            t0.warn,
        )]))
        .child(field(
            &t0,
            "embedding model",
            TextInput::new()
                .value(model)
                .placeholder("e.g. text-embedding-qwen3-embedding-0.6b")
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(48).h(1))
                .element(mcx, &t0)
                .build(),
        ))
        .child(field(
            &t0,
            "reason",
            TextInput::new()
                .value(reason)
                .placeholder("why this repair is needed (journaled)")
                .placeholder_while_focused(true)
                .layout(LayoutStyle::default().w(48).h(1))
                .element(mcx, &t0)
                .build(),
        ))
        .child(line(vec![span(String::new(), t0.text)]))
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(wb(mcx, &t0, SubForm::Reembed, "rebuild", move || {
                    let m = model.get_untracked().trim().to_string();
                    let r = reason.get_untracked().trim().to_string();
                    if m.is_empty() {
                        ctx3.store
                            .notice
                            .set(Some("type the embedding model id first".into()));
                        return;
                    }
                    let n = name2.clone();
                    let c = ctx3.clone();
                    let close_done = close.clone();
                    // THE confirm widget over the form (the web's
                    // sentence); the form closes once it is sent.
                    super::w::Confirm::danger(REEMBED_QUESTION, "Rebuild", "Cancel").open(
                        mcx,
                        c.ui,
                        {
                            let c2 = c.clone();
                            move || {
                                close_done();
                                let mut body = json!({ "embedding_model": m });
                                if !r.is_empty() {
                                    body["reason"] = Value::String(r);
                                }
                                c2.send(Cmd::EntityReembed {
                                    name: n,
                                    body: body.into(),
                                    form_id: None,
                                });
                            }
                        },
                    );
                }))
                .build(),
        )
        .child(super::w::state_line(st, w))
        .build()
}

// ---------------------------------------------------------------------
// Tool policy: per-phase grants (the web's capability matrix)
// ---------------------------------------------------------------------

/// Fixed slot budget for phase MultiSelects: signals must live in the
/// MODAL scope (region scopes die on re-render), but the phase list
/// arrives async — so a stable slot array is filled once on Ready.
const PHASE_SLOTS: usize = 6;

fn tools_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let store = ctx.store;
    store.entity_policy.set(Loadable::Loading);
    ctx.send(Cmd::LoadToolPolicy { name: name.clone() });

    let ctx2 = ctx.clone();
    let st = mcx.signal(super::w::FieldState::Idle);
    let close = inl.saved_fn(st);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let phase_sigs: std::rc::Rc<Vec<Signal<Vec<String>>>> =
        std::rc::Rc::new((0..PHASE_SLOTS).map(|_| mcx.signal(Vec::new())).collect());
    let filled = mcx.signal(false);
    let form_error = mcx.signal(Option::<String>::None);
    let in_flight = mcx.signal(false);
    let form_id = crate::worker::next_form_id();

    // One-shot fill: grants land in the slots when the read arrives.
    {
        let phase_sigs = phase_sigs.clone();
        let n = name.clone();
        mcx.effect(move || {
            if filled.get() {
                return;
            }
            if let Loadable::Ready(d) = store.entity_policy.get() {
                if d.entity != n {
                    return;
                }
                for (i, (_, tools, _)) in d.phases.iter().take(PHASE_SLOTS).enumerate() {
                    phase_sigs[i].set(tools.clone());
                }
                filled.set(true);
            }
        });
    }

    super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close);

    let ctx_save = ctx2.clone();
    let name_save = name.clone();
    // The web's "Empty phase means no tools" switch: on = a cleared
    // phase is an explicit deny-all ([]); off = it returns to the
    // framework default (null).
    let deny_all = mcx.signal(false);
    {
        let sigs = phase_sigs.clone();
        let n = name.clone();
        let dirty = move || {
            store.entity_policy.with_untracked(|p| {
                p.ready().filter(|d| d.entity == n).is_some_and(|d| {
                    d.phases
                        .iter()
                        .take(PHASE_SLOTS)
                        .enumerate()
                        .any(|(i, (_, orig, _))| sigs[i].get_untracked() != *orig)
                })
            })
        };
        inl.watch_fn(dirty);
    }
    let phase_sigs_save = phase_sigs.clone();
    let phase_sigs_render = phase_sigs.clone();

    // (Inline in Manage: the tab bar holds the focus.)
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(
            super::w::Toggle::switch(EMPTY_PHASE_LABEL, deny_all)
                .tip(EMPTY_PHASE_DESC)
                .view(mcx, &t0),
        )
        .child(super::w::form::sentence(
            &t0,
            EMPTY_PHASE_DESC,
            80,
            t0.text_faint,
        ))
        .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
            let n = name.clone();
            move |gcx| {
                let t = theme.get().tokens;
                match store.entity_policy.get() {
                    Loadable::Ready(d) if d.entity == n => {
                        if !filled.get() {
                            // The fill effect runs this same frame.
                            return line(vec![span("⟳ preparing grants…", t.info)]);
                        }
                        let opts: Vec<SelectOption> = d
                            .all_tools
                            .iter()
                            .map(|tname| SelectOption::keyed(tname.clone(), tname.clone()))
                            .collect();
                        let mut col = Element::new().style(LayoutStyle::column().gap(0));
                        for (i, (phase, _, source)) in d.phases.iter().take(PHASE_SLOTS).enumerate()
                        {
                            col = col.child(field(
                                &t,
                                phase,
                                Element::new()
                                    .style(LayoutStyle::row().gap(1))
                                    .child(
                                        MultiSelect::new(opts.clone())
                                            .values(phase_sigs_render[i])
                                            .placeholder("no tools (empty grant on save)")
                                            .layout(LayoutStyle::default().w(44).h(1).shrink(0.0))
                                            .element(gcx, &t)
                                            .build(),
                                    )
                                    .child(line(vec![span(format!("({source})"), t.text_faint)]))
                                    .build(),
                            ));
                        }
                        if d.phases.len() > PHASE_SLOTS {
                            col = col.child(line(vec![span(
                                format!(
                                    "{} more phase(s) not editable here — use the web console",
                                    d.phases.len() - PHASE_SLOTS
                                ),
                                t.warn,
                            )]));
                        }
                        col.build()
                    }
                    Loadable::Failed(e) => super::util::error_panel_hint(
                        &t,
                        &e,
                        Some("close and reopen this dialog to retry (opening re-reads)"),
                    ),
                    _ => line(vec![span("⟳ loading tool policy…", t.info)]),
                }
            }
        }))
        .child(super::message_slot(theme, form_error, in_flight))
        .child(super::w::state_line(st, w))
        .child(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |bcx| {
                let t = theme.get().tokens;
                let ctx_s = ctx_save.clone();
                let n = name_save.clone();
                let sigs = phase_sigs_save.clone();
                Element::new()
                    .style(LayoutStyle::row().gap(2))
                    .child(wb(bcx, &t, SubForm::Tools, "save", move || {
                        if in_flight.get_untracked() {
                            return;
                        }
                        if let Some(why) = write_refusal(&ctx_s, "saving tool grants") {
                            form_error.set(Some(why));
                            return;
                        }
                        let Some(d) = ctx_s
                            .store
                            .entity_policy
                            .with_untracked(|p| p.ready().filter(|d| d.entity == n).cloned())
                        else {
                            form_error.set(Some("policy not loaded yet".into()));
                            return;
                        };
                        // Only CHANGED phases ride the write
                        // (web parity: readMatrix sends deltas).
                        let mut policy = serde_json::Map::new();
                        let mut emptied: Vec<String> = Vec::new();
                        for (i, (phase, orig, _)) in d.phases.iter().take(PHASE_SLOTS).enumerate() {
                            let now = sigs[i].get_untracked();
                            if &now == orig {
                                continue;
                            }
                            if now.is_empty() {
                                emptied.push(phase.clone());
                            }
                            policy.insert(
                                phase.clone(),
                                Value::Array(
                                    now.iter().map(|s| Value::String(s.clone())).collect(),
                                ),
                            );
                        }
                        if policy.is_empty() {
                            form_error.set(Some("no changes to save".into()));
                            return;
                        }
                        if emptied.is_empty() {
                            form_error.set(None);
                            in_flight.set(true);
                            ctx_s.send(Cmd::SaveToolPolicy {
                                name: n.clone(),
                                body: json!({ "policy": Value::Object(policy) }).into(),
                                form_id: Some(form_id),
                            });
                            return;
                        }
                        // Emptied phases follow the switch.
                        if !deny_all.get_untracked() {
                            for p in &emptied {
                                policy.insert(p.clone(), Value::Null);
                            }
                        }
                        form_error.set(None);
                        in_flight.set(true);
                        ctx_s.send(Cmd::SaveToolPolicy {
                            name: n.clone(),
                            body: json!({ "policy": Value::Object(policy) }).into(),
                            form_id: Some(form_id),
                        });
                    }))
                    .build()
            },
        ))
        .build()
}

/// The web's "Tools per phase" card words.
pub const TOOLS_DESC: &str =
    "Which tools it may use in each phase of its day. Each box saves when you tick it.";
pub const EMPTY_PHASE_LABEL: &str = "Empty phase means no tools";
pub const EMPTY_PHASE_DESC: &str = "On: a phase with every box cleared has no tools at all. Off: it goes back to the default tools.";

// ---------------------------------------------------------------------
// Prompt overlay editor (per-layer TextAreas; Enter inserts newlines)
// ---------------------------------------------------------------------

/// Fixed layer-slot budget (same rationale as PHASE_SLOTS: TextArea
/// states must live in the modal scope, the layer list arrives async).
const LAYER_SLOTS: usize = 4;

fn prompt_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let store = ctx.store;
    store.entity_prompt.set(Loadable::Loading);
    ctx.send(Cmd::LoadEntityPrompt { name: name.clone() });
    let ctx2 = ctx.clone();
    let st = mcx.signal(super::w::FieldState::Idle);
    let close = inl.saved_fn(st);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let states: std::rc::Rc<Vec<TextAreaState>> =
        std::rc::Rc::new((0..LAYER_SLOTS).map(|_| TextAreaState::new(mcx)).collect());
    let filled = mcx.signal(false);
    let form_error = mcx.signal(Option::<String>::None);
    let in_flight = mcx.signal(false);
    let form_id = crate::worker::next_form_id();

    // One-shot fill from the read.
    {
        let states = states.clone();
        let n = name.clone();
        mcx.effect(move || {
            if filled.get() {
                return;
            }
            if let Loadable::Ready(d) = store.entity_prompt.get() {
                if d.entity != n {
                    return;
                }
                for (i, (_, text)) in d.layers.iter().take(LAYER_SLOTS).enumerate() {
                    states[i].set_text(text.clone());
                }
                filled.set(true);
            }
        });
    }

    super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close);

    let ctx_save = ctx2.clone();
    let name_save = name.clone();
    let n_render = name.clone();
    {
        let states = states.clone();
        let n = name.clone();
        let dirty = move || {
            store.entity_prompt.with_untracked(|p| {
                p.ready().filter(|d| d.entity == n).is_some_and(|d| {
                    d.layers
                        .iter()
                        .take(LAYER_SLOTS)
                        .enumerate()
                        .any(|(i, (_, text))| states[i].text() != *text)
                })
            })
        };
        inl.watch_fn(dirty);
    }
    let states_render = states.clone();
    let states_save = states.clone();

    // (Inline in Manage: the tab bar holds the focus.)
    Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span(
                "per-layer overlay text — Enter inserts a newline; Tab moves between layers; Save writes ALL layers",
                t0.text_faint,
            )]))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_prompt.get() {
                        Loadable::Ready(d) if d.entity == n_render => {
                            if !filled.get() {
                                return line(vec![span("⟳ preparing layers…", t.info)]);
                            }
                            if d.layers.is_empty() {
                                return line(vec![span(
                                    "no overlay layers reported — the framework prelude applies unmodified",
                                    t.text_muted,
                                )]);
                            }
                            let mut col = Element::new().style(LayoutStyle::column().gap(0));
                            for (i, (layer, _)) in
                                d.layers.iter().take(LAYER_SLOTS).enumerate()
                            {
                                col = col
                                    .child(line(vec![span_bold(
                                        format!("── {layer} ──"),
                                        t.accent,
                                    )]))
                                    .child(
                                        TextArea::new()
                                            .state(&states_render[i])
                                            .submit_policy(SubmitPolicy::EnterInserts)
                                            .rows(3, 8)
                                            // Inline in Manage's scrolling body:
                                            // a fixed height (no grow in a scroll).
                                            .layout(LayoutStyle::default().h(6).shrink(0.0))
                                            .element(gcx, &t)
                                            .build(),
                                    );
                            }
                            if d.layers.len() > LAYER_SLOTS {
                                col = col.child(line(vec![span(
                                    format!(
                                        "{} more layer(s) not shown — use the web console",
                                        d.layers.len() - LAYER_SLOTS
                                    ),
                                    t.warn,
                                )]));
                            }
                            col.build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(&t, &e, Some("close and reopen this dialog to retry (opening re-reads)")),
                        _ => line(vec![span("⟳ loading overlay…", t.info)]),
                    }
                }
            }))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(super::w::state_line(st, w))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let ctx_s = ctx_save.clone();
                    let n = name_save.clone();
                    let states_s = states_save.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            wb(bcx, &t, SubForm::Prompt, "save", move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let Some(d) = ctx_s.store.entity_prompt.with_untracked(|p| {
                                        p.ready().filter(|d| d.entity == n).cloned()
                                    }) else {
                                        form_error.set(Some("overlay not loaded yet".into()));
                                        return;
                                    };
                                    // ALL layers ride (web parity — the
                                    // form is the whole overlay truth).
                                    let mut overlay = serde_json::Map::new();
                                    for (i, (layer, _)) in
                                        d.layers.iter().take(LAYER_SLOTS).enumerate()
                                    {
                                        overlay.insert(
                                            layer.clone(),
                                            Value::String(states_s[i].text()),
                                        );
                                    }
                                    if let Some(why) = write_refusal(&ctx_s, "saving the prompt overlay") {
                                        form_error.set(Some(why));
                                        return;
                                    }
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::SaveEntityPrompt {
                                        name: n.clone(),
                                        body: json!({ "overlay": Value::Object(overlay) }).into(),
                                        form_id: Some(form_id),
                                    });
                                }),
                        )
                        .build()
                },
            ))
            .build()
}

// ---------------------------------------------------------------------
// Candidates review (sleep consolidation → waking evidence disposes)
// ---------------------------------------------------------------------

fn candidates_body(mcx: Scope, ctx: &Ctx, name: String, inl: &Inline, w: i32) -> View {
    let store = ctx.store;
    store.entity_candidates.set(Loadable::Loading);
    ctx.send(Cmd::LoadCandidates { name: name.clone() });
    let ctx2 = ctx.clone();
    let _ = (inl, w);
    let theme = use_theme(mcx);
    let t0 = theme.get().tokens;
    let sel = mcx.signal(0usize);
    let reason = mcx.signal(String::new());
    let corroborating = mcx.signal(String::new());
    let n = name.clone();
    let n_act = name.clone();
    let ctx3 = ctx2.clone();
    super::util::clamp_selection(mcx, sel, move || {
        store
            .entity_candidates
            .with(|d| d.ready().map(|(_, rows)| rows.len()).unwrap_or(0))
    });
    // (Inline in Manage: the tab bar holds the focus.)
    Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                let n = n.clone();
                move |gcx| {
                    let t = theme.get().tokens;
                    match store.entity_candidates.get() {
                        Loadable::Ready((e, rows)) if e == n => {
                            if rows.is_empty() {
                                return line(vec![span(
                                    "no candidates awaiting review",
                                    t.text_muted,
                                )]);
                            }
                            let table_rows: Vec<Vec<String>> = rows
                                .iter()
                                // Uncapped: the title column is Flex, so
                                // it already grows with the drawer; a
                                // 60-char pre-cut only hid the end of a
                                // title the drawer had room for.
                                .map(|r| vec![r.kind.clone(), r.title.clone()])
                                .collect();
                            Element::new()
                                .style(LayoutStyle::column().gap(0))
                                .child(
                                    Table::new(vec![
                                        Column::new("kind", ColWidth::Cells(10)),
                                        Column::new("title", ColWidth::Flex(1.0)),
                                    ])
                                    .rows(table_rows)
                                    .selection(sel)
                                    .layout(LayoutStyle::default().h(6).shrink(0.0))
                                    .element(gcx, &t)
                                    .build(),
                                )
                                .child(dyn_view(LayoutStyle::default().h(3).shrink(0.0), {
                                    let n2 = n.clone();
                                    move || {
                                        let t = theme.get().tokens;
                                        let digest = store.entity_candidates.with(|d| {
                                            d.ready()
                                                .filter(|(e, _)| *e == n2)
                                                .and_then(|(_, rows)| {
                                                    rows.get(sel.get()).map(|r| r.digest.clone())
                                                })
                                                .unwrap_or_default()
                                        });
                                        line(vec![span(
                                            super::util::ellipsize(&digest, 250),
                                            t.text_muted,
                                        )])
                                    }
                                }))
                                .build()
                        }
                        Loadable::Failed(e) => super::util::error_panel_hint(
                            &t,
                            &e,
                            Some("press the Reload button below to retry"),
                        ),
                        _ => line(vec![span("⟳ loading candidates…", t.info)]),
                    }
                }
            }))
            .child(field(
                &t0,
                "corroborating",
                TextInput::new()
                    .value(corroborating)
                    .placeholder("promote: record ids, comma-separated (>= 2 independent origins)")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(66).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "reason",
                TextInput::new()
                    .value(reason)
                    .placeholder("required — recorded with the act")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(56).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(dyn_view_scoped(
                LayoutStyle::default().h(1).shrink(0.0),
                move |bcx| {
                    let t = theme.get().tokens;
                    let act = |promote: bool| {
                        let ctx_a = ctx3.clone();
                        let n2 = n_act.clone();
                        move || {
                            if !super::util::admin_gate(&ctx_a.store, "promoting or rejecting a candidate") {
                                return;
                            }
                            let r = reason.get_untracked().trim().to_string();
                            if r.is_empty() {
                                ctx_a.store.notice.set(Some(
                                    "type the reason first — candidate acts are journaled".into(),
                                ));
                                return;
                            }
                            let row = ctx_a.store.entity_candidates.with_untracked(|d| {
                                d.ready()
                                    .filter(|(e, _)| *e == n2)
                                    .and_then(|(_, rows)| rows.get(sel.get_untracked()).cloned())
                            });
                            let Some(row) = row else {
                                ctx_a.store.notice.set(Some("no candidate selected".into()));
                                return;
                            };
                            let ids = crate::store::parse_corroborating_ids(
                                &corroborating.get_untracked(),
                            );
                            if promote && ids.is_empty() {
                                ctx_a.store.notice.set(Some(
                                    "type the corroborating record ids first — a promote needs >= 2 independent origins".into(),
                                ));
                                return;
                            }
                            ctx_a.send(Cmd::CandidateAct {
                                name: n2.clone(),
                                record_id: row.record_id.clone(),
                                promote,
                                corroborating_ids: if promote { ids } else { Vec::new() },
                                reason: r,
                            });
                            reason.set(String::new());
                            corroborating.set(String::new());
                        }
                    };
                    let ctx_r = ctx3.clone();
                    let n_r = n_act.clone();
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            wb(bcx, &t, SubForm::Candidates, "promote", act(true)),
                        )
                        .child(
                            wb(bcx, &t, SubForm::Candidates, "reject", act(false)),
                        )
                        .child(
                            wb(bcx, &t, SubForm::Candidates, "reload", move || {
                                    ctx_r.store.entity_candidates.set(Loadable::Loading);
                                    ctx_r.send(Cmd::LoadCandidates { name: n_r.clone() });
                                }),
                        )
                        .build()
                },
            ))
            .build()
}

// The shared form plumbing (dirty-Esc guard, write_done routing, the
// message slot) lives in `super` (ui/mod.rs) — one implementation of
// the contract for every write form in the app (F4).

#[cfg(test)]
mod tests {
    use super::{loop_start_body, timer_grant_body};
    use serde_json::json;

    /// Own-time Start mirrors the web body: blank fields are omitted
    /// (the gateway's defaults apply), typed values are sent as typed.
    #[test]
    fn loop_start_body_omits_blank_fields_like_the_web() {
        assert_eq!(loop_start_body("", "", "").unwrap(), json!({}));
        assert_eq!(
            loop_start_body(" 45 ", "", "").unwrap(),
            json!({"tick_seconds": 45.0})
        );
        assert_eq!(
            loop_start_body("", "12", "2.5").unwrap(),
            json!({"ticks_per_day": 12, "rest_minutes": 2.5})
        );
        assert!(loop_start_body("20abc", "", "")
            .unwrap_err()
            .contains("tick seconds is not a number"));
        assert!(loop_start_body("", "8.5", "")
            .unwrap_err()
            .contains("ticks per day is not a whole number"));
    }

    /// The own-time timer carries the expiry the web computes (now +
    /// hours) — without it the gateway answers 400.
    #[test]
    fn timer_grant_body_carries_expires_at() {
        // 2026-09-27T10:04:05Z + 2h.
        assert_eq!(
            timer_grant_body("2", 1_790_503_445).unwrap(),
            Some(json!({"mode": "timer", "expires_at": "2026-09-27T12:04:05Z"}))
        );
        assert_eq!(
            timer_grant_body("0.5", 1_790_503_445).unwrap().unwrap()["expires_at"],
            "2026-09-27T10:34:05Z"
        );
        assert_eq!(
            timer_grant_body("", 1).unwrap(),
            None,
            "blank = no timed window"
        );
        assert_eq!(
            timer_grant_body("0", 1).unwrap(),
            None,
            "not positive = no window"
        );
        assert!(
            timer_grant_body("2h", 1).is_err(),
            "garbage is refused, not guessed"
        );
    }
}
