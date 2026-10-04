//! Round-7 shared widgets (DESIGN.md R7.2 "Conventions"): the terminal's
//! counterparts of the web console's modal, table, inline confirm and
//! footer, used by every page brought to parity with the WUI.
//!
//! - [`open_overlay`]: a modal becomes a FULL-WIDTH overlay — title row,
//!   body, key-hint bar; Esc closes (a dirty form's guard still wins).
//! - [`WrapTable`]: a table whose cells WRAP onto continuation lines
//!   instead of being cut — nothing is ever truncated silently; Enter
//!   expands the selected row's detail lines (the web row's secondary
//!   text, its actions and their reasons).
//! - [`InlineConfirm`]: the web console's inline confirmation — one line
//!   in place, `<sentence>  [y] <Danger>  [n] Keep`; y confirms, n or Esc
//!   keeps. Nothing is done before `y`.
//! - [`key_hint_bar`]: the key-hint footer, wrapping onto a second line on
//!   a narrow terminal instead of dropping verbs.
//!
//! - [`inline_input`] (round 8): the web's "edit in place" field — a
//!   one-line input shown where the value was; Enter saves, Esc keeps the
//!   old value. Nothing opens a dialog.
//!
//! Switch rows are [`super::switch::Switch`] (`[x]`/`[ ]` + the feature
//! label) — unchanged.

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use abstracttui::base::{Point, Rgba};
use abstracttui::prelude::*;
use abstracttui::render::{Attrs, Style};
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

use super::util::{line, span, span_bold, wrap_text};
use super::widths::{self, ColRule};
use super::{CloserFn, Ctx, GuardSlot};

// ---------------------------------------------------------------------------
// Key-hint bar
// ---------------------------------------------------------------------------

/// The hint pairs laid out into lines no wider than `width` cells: a pair
/// is never split, a pair that does not fit starts the next line.
pub fn hint_lines(pairs: &[(&str, &str)], width: i32) -> Vec<String> {
    let width = width.max(10);
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    for (k, v) in pairs {
        let item = format!("{k} {v}");
        let sep = if cur.is_empty() { "" } else { " · " };
        let next_w = abstracttui::text::width(&cur)
            + abstracttui::text::width(sep)
            + abstracttui::text::width(&item);
        if !cur.is_empty() && next_w > width {
            out.push(std::mem::take(&mut cur));
            cur.push_str(&item);
        } else {
            cur.push_str(sep);
            cur.push_str(&item);
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// The key-hint footer: `key action · key action`, keys in accent ink,
/// wrapped onto as many lines as `width` needs (no verb is dropped).
pub fn key_hint_bar(t: &TokenSet, pairs: &[(&str, &str)], width: i32) -> View {
    let width = width.max(10);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    let mut spans = Vec::new();
    let mut used = 0i32;
    for (k, v) in pairs {
        let item_w = abstracttui::text::width(k) + 1 + abstracttui::text::width(v);
        if used > 0 && used + 3 + item_w > width {
            col = col.child(line(std::mem::take(&mut spans)));
            used = 0;
        }
        if used > 0 {
            spans.push(span(" · ", t.text_faint));
            used += 3;
        }
        spans.push(span_bold((*k).to_string(), t.accent));
        spans.push(span(format!(" {v}"), t.text_muted));
        used += item_w;
    }
    if !spans.is_empty() {
        col = col.child(line(spans));
    }
    col.build()
}

/// The app footer's key-hint bar: [`key_hint_bar`] capped at `max_lines`
/// lines (the page keeps the rows). Pairs come in priority order — the
/// screen's own verbs first, the universal keys last — and when they do
/// not all fit, the last line ends with a visible `…` (never a silent
/// cut); the dropped pairs are universal keys the About page lists.
pub fn footer_hint_bar(t: &TokenSet, pairs: &[(&str, &str)], width: i32, max_lines: usize) -> View {
    let width = width.max(10);
    let mut kept: Vec<(&str, &str)> = pairs.to_vec();
    let mut cut = false;
    while hint_lines(&kept, width).len() > max_lines.max(1)
        || (cut
            && hint_lines(&[kept.as_slice(), &[("…", "")]].concat(), width).len()
                > max_lines.max(1))
    {
        if kept.pop().is_none() {
            break;
        }
        cut = true;
    }
    if cut {
        kept.push(("…", ""));
    }
    key_hint_bar(t, &kept, width)
}

// ---------------------------------------------------------------------------
// Overlay
// ---------------------------------------------------------------------------

/// A full-width overlay over the page (the WUI's modal): the title row,
/// the body `build` returns, and the key-hint bar. Esc closes it (unless
/// the body's dirty guard blocks that Esc); the builder also gets the
/// closer for its own buttons. One overlay at a time (the shared slot).
pub fn open_overlay(
    ctx: &Ctx,
    cx: Scope,
    title: impl Into<String>,
    hints: &[(&str, &str)],
    build: impl FnOnce(Scope, CloserFn, GuardSlot) -> View + 'static,
) {
    let title = title.into();
    let mut pairs: Vec<(String, String)> = hints
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect();
    if !pairs.iter().any(|(k, _)| k == "esc") {
        pairs.push(("esc".into(), "close".into()));
    }
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    let notice = ctx.store.notice;
    super::open_form_guarded(ctx, cx, vp, move |mcx, close, guard| {
        let t = use_theme(mcx).get().tokens;
        let vw = abstracttui::app::use_viewport(mcx).get_untracked().w;
        let refs: Vec<(&str, &str)> = pairs
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        Element::new()
            .style(LayoutStyle::column().gap(0).grow(1.0))
            .child(line(vec![span_bold(title.clone(), t.text)]))
            .child(
                Element::new()
                    .style(LayoutStyle::column().gap(0).grow(1.0).min_h(1))
                    .child(build(mcx, close, guard))
                    .build(),
            )
            // The notice lane: a write's outcome sentence stays visible
            // while the overlay covers the page's footer.
            .child(dyn_view(
                LayoutStyle::column().gap(0).shrink(0.0),
                move || {
                    let t = use_theme(mcx).get().tokens;
                    match notice.get() {
                        Some(n) if !n.is_empty() => sentence(&t, &n, vw - 6, t.text_muted),
                        _ => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                },
            ))
            .child(key_hint_bar(&t, &refs, vw - 6))
            .build()
    });
}

// ---------------------------------------------------------------------------
// Wrapping table
// ---------------------------------------------------------------------------

/// One row of a [`WrapTable`]: its cells (one per column), the detail
/// lines Enter reveals, and whether it reads dimmed (archived, inactive).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Row {
    pub cells: Vec<String>,
    pub detail: Vec<String>,
    pub dim: bool,
    /// A group caption ("Shared with everyone"): one full-width line, never
    /// selected (the selection steps over it).
    pub group: bool,
}

impl Row {
    pub fn new(cells: Vec<String>) -> Row {
        Row {
            cells,
            ..Row::default()
        }
    }
    pub fn detail(mut self, lines: Vec<String>) -> Row {
        self.detail = lines;
        self
    }
    pub fn dim(mut self, dim: bool) -> Row {
        self.dim = dim;
        self
    }
    /// A group caption row (the web's full-width group header row).
    pub fn group(title: impl Into<String>) -> Row {
        Row {
            cells: vec![title.into()],
            group: true,
            ..Row::default()
        }
    }
}

/// The nearest selectable (non-group) row from `i`, stepping `down` first
/// and then the other way; `None` when every row is a caption.
pub fn selectable(rows: &[Row], i: usize, down: bool) -> Option<usize> {
    if rows.is_empty() {
        return None;
    }
    let i = i.min(rows.len() - 1);
    let fwd = (i..rows.len()).find(|&j| !rows[j].group);
    let back = (0..=i).rev().find(|&j| !rows[j].group);
    if down {
        fwd.or(back)
    } else {
        back.or(fwd)
    }
}

/// What one painted line of a [`WrapTable`] is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LineKind {
    Header,
    Group,
    Cell,
    Detail,
}

/// One painted line: its text, which row it belongs to (None: header).
#[derive(Clone, Debug, PartialEq)]
pub struct WrapLine {
    pub text: String,
    pub row: Option<usize>,
    pub kind: LineKind,
}

const COL_GAP: i32 = 2;
/// Columns whose widest cell is at most this many cells never wrap
/// (a version with "+N older", a state, two short words).
const SHORT_CELL: i32 = 16;

/// Lay a table out at `width` cells: column widths are solved by the
/// shared width policy ([`widths::solve`]); a cell wider than its column
/// WRAPS onto continuation lines (word wrap, hard break for over-wide
/// words) — no cell is ever cut. An expanded row adds its detail lines,
/// indented, wrapped to the full width.
pub fn wrap_layout(
    rules: &[ColRule],
    rows: &[Row],
    width: i32,
    expanded: Option<usize>,
) -> Vec<WrapLine> {
    let width = width.max(8);
    let cells: Vec<Vec<String>> = rows
        .iter()
        .filter(|r| !r.group)
        .map(|r| r.cells.clone())
        .collect();
    // solve() reserves a scrollbar cell and one-cell gaps; ours are two
    // cells wide, so hand it the width minus the extra gap cells.
    let mut ws = wrap_widths(rules, &cells, width);
    for w in ws.iter_mut() {
        *w = (*w).max(1);
    }
    // Never wider than the table: the solver's floors can exceed a narrow
    // budget, so take the excess from the widest columns (their cells
    // wrap, nothing is cut). One cell stays free for the scrollbar.
    let gaps = COL_GAP * (ws.len() as i32 - 1).max(0);
    while ws.iter().sum::<i32>() + gaps > width - 1 {
        let Some((i, w)) = ws.iter().enumerate().max_by_key(|(_, w)| **w) else {
            break;
        };
        if *w <= 1 {
            break;
        }
        ws[i] -= 1;
    }
    let mut out = Vec::new();
    let header: Vec<Vec<String>> = rules.iter().map(|r| vec![r.title.to_string()]).collect();
    for l in join_columns(&header, &ws) {
        out.push(WrapLine {
            text: l,
            row: None,
            kind: LineKind::Header,
        });
    }
    for (i, r) in rows.iter().enumerate() {
        if r.group {
            for l in wrap_text(
                r.cells.first().map(String::as_str).unwrap_or(""),
                width as usize,
            ) {
                out.push(WrapLine {
                    text: l,
                    row: Some(i),
                    kind: LineKind::Group,
                });
            }
            continue;
        }
        let wrapped: Vec<Vec<String>> = ws
            .iter()
            .enumerate()
            .map(|(c, w)| {
                let cell = r.cells.get(c).cloned().unwrap_or_default();
                wrap_text(&cell, *w as usize)
            })
            .collect();
        for l in join_columns(&wrapped, &ws) {
            out.push(WrapLine {
                text: l,
                row: Some(i),
                kind: LineKind::Cell,
            });
        }
        if expanded == Some(i) {
            for d in &r.detail {
                for l in wrap_text(d, (width - 4).max(4) as usize) {
                    out.push(WrapLine {
                        text: format!("    {l}"),
                        row: Some(i),
                        kind: LineKind::Detail,
                    });
                }
            }
        }
    }
    out
}

/// Column widths for wrapping rows: every column first gets its LONGEST
/// WORD (so a name or a one-word state is never broken mid-word), then the
/// cells left over go to the columns that still have text to show, in
/// proportion to what they lack — prose columns absorb the wrapping.
/// When even the longest words do not fit, the shared width policy
/// ([`widths::solve`]) decides and words hard-break.
pub fn wrap_widths(rules: &[ColRule], cells: &[Vec<String>], width: i32) -> Vec<i32> {
    let n = rules.len();
    if n == 0 {
        return Vec::new();
    }
    let usable = (width - COL_GAP * (n as i32 - 1)).max(n as i32);
    let mut natural: Vec<i32> = rules
        .iter()
        .map(|r| abstracttui::text::width(r.title))
        .collect();
    let mut floor: Vec<i32> = natural.clone();
    for row in cells {
        for (i, cell) in row.iter().enumerate().take(n) {
            natural[i] = natural[i].max(abstracttui::text::width(cell));
            for word in cell.split_whitespace() {
                floor[i] = floor[i].max(abstracttui::text::width(word));
            }
        }
    }
    for (i, r) in rules.iter().enumerate() {
        floor[i] = floor[i].max(r.min.min(natural[i])).min(natural[i]);
        // A short cell (a state, a version, two words) stays on one line.
        if natural[i] <= SHORT_CELL {
            floor[i] = natural[i];
        }
        // One very long word (a bundle id) may not take more than a
        // quarter of the row: it hard-breaks instead of starving the prose.
        floor[i] = floor[i].min((usable / 4).max(r.min).max(SHORT_CELL));
    }
    if natural.iter().sum::<i32>() <= usable {
        return natural;
    }
    let base: i32 = floor.iter().sum();
    if base > usable {
        let extra = (COL_GAP - 1) * (n as i32 - 1);
        return widths::solve(rules, cells, width - extra);
    }
    let mut out = floor.clone();
    let mut left = usable - base;
    let want: Vec<i32> = (0..n).map(|i| natural[i] - floor[i]).collect();
    let total: i32 = want.iter().sum();
    if total > 0 {
        let mut given = 0;
        for i in 0..n {
            let share = (i64::from(left) * i64::from(want[i]) / i64::from(total)) as i32;
            out[i] += share.min(want[i]);
            given += share.min(want[i]);
        }
        left -= given;
        // Rounding crumbs to the column that still lacks the most.
        while left > 0 {
            let Some(i) = (0..n)
                .filter(|&i| out[i] < natural[i])
                .max_by_key(|&i| natural[i] - out[i])
            else {
                break;
            };
            out[i] += 1;
            left -= 1;
        }
    }
    out
}

fn join_columns(cols: &[Vec<String>], ws: &[i32]) -> Vec<String> {
    let height = cols.iter().map(Vec::len).max().unwrap_or(1).max(1);
    let mut out = Vec::with_capacity(height);
    for li in 0..height {
        let mut s = String::new();
        for (c, w) in ws.iter().enumerate() {
            let piece = cols
                .get(c)
                .and_then(|v| v.get(li))
                .cloned()
                .unwrap_or_default();
            let pw = abstracttui::text::width(&piece);
            s.push_str(&piece);
            if c + 1 < ws.len() {
                let pad = (*w - pw).max(0) + COL_GAP;
                s.push_str(&" ".repeat(pad as usize));
            }
        }
        out.push(s.trim_end().to_string());
    }
    out
}

/// The whole table as plain text at `width` (tests and captures).
pub fn wrap_text_table(
    rules: &[ColRule],
    rows: &[Row],
    width: i32,
    expanded: Option<usize>,
) -> String {
    wrap_layout(rules, rows, width, expanded)
        .into_iter()
        .map(|l| l.text)
        .collect::<Vec<_>>()
        .join("\n")
}

type ActivateFn = Rc<RefCell<Option<Box<dyn FnMut(usize)>>>>;

/// A focusable table of wrapping rows. ↑/↓ (k/j), PgUp/PgDn, Home/End
/// move the selection; Enter expands/collapses the selected row's detail
/// (or calls `on_activate` when set and the row has no detail); a click
/// selects; the wheel scrolls. The selected row is always kept in view.
pub struct WrapTable {
    rules: Vec<ColRule>,
    rows: Rc<Vec<Row>>,
    sel: Signal<usize>,
    expanded: Option<Signal<Option<usize>>>,
    on_activate: ActivateFn,
    empty: String,
    layout: LayoutStyle,
}

impl WrapTable {
    pub fn new(rules: Vec<ColRule>, rows: Vec<Row>, sel: Signal<usize>) -> WrapTable {
        WrapTable {
            rules,
            rows: Rc::new(rows),
            sel,
            expanded: None,
            on_activate: Rc::new(RefCell::new(None)),
            empty: String::new(),
            layout: LayoutStyle::default().grow(1.0).min_h(2),
        }
    }

    /// Which row is expanded (Enter toggles it).
    pub fn expanded(mut self, expanded: Signal<Option<usize>>) -> WrapTable {
        self.expanded = Some(expanded);
        self
    }

    /// Enter on a row WITHOUT detail lines (or with no expansion signal).
    pub fn on_activate(self, f: impl FnMut(usize) + 'static) -> WrapTable {
        *self.on_activate.borrow_mut() = Some(Box::new(f));
        self
    }

    /// The sentence shown when there are no rows (the WUI's empty text).
    pub fn empty(mut self, text: impl Into<String>) -> WrapTable {
        self.empty = text.into();
        self
    }

    pub fn layout(mut self, layout: LayoutStyle) -> WrapTable {
        self.layout = layout;
        self
    }

    pub fn element(self, cx: Scope, t: &TokenSet) -> Element {
        let tokens = *t;
        let WrapTable {
            rules,
            rows,
            sel,
            expanded,
            on_activate,
            empty,
            layout,
        } = self;
        let n = rows.len();
        let focused = cx.signal(false);
        let top = Rc::new(Cell::new(0i32));
        // The last painted mapping line → row (for clicks) and the page
        // height (PgUp/PgDn), written by the draw closure.
        let painted: Rc<RefCell<(Vec<Option<usize>>, i32)>> = Rc::new(RefCell::new((vec![], 0)));
        // An opened row taller than the pane: PgDn/PgUp scroll INSIDE it
        // (`dscroll` lines past its first line, at most `dmax`, which the
        // draw closure writes); ↑/↓ still move between rows.
        let dscroll = Rc::new(Cell::new(0i32));
        let dmax = Rc::new(Cell::new(0i32));
        let (dscroll_ev, dmax_ev) = (dscroll.clone(), dmax.clone());
        let dtick = cx.signal(0u32);
        let rules = Rc::new(rules);
        let painted_ev = painted.clone();
        let top_ev = top.clone();
        let rows_ev = rows.clone();
        let dscroll_mv = dscroll.clone();
        let last_click: Rc<Cell<Option<(usize, std::time::Instant)>>> = Rc::new(Cell::new(None));
        let on_activate_click = on_activate.clone();
        let rows_mv = rows.clone();
        let move_to = move |i: usize| {
            if n == 0 {
                return;
            }
            let down = i >= sel.get_untracked();
            let Some(i) = selectable(&rows_mv, i.min(n - 1), down) else {
                return;
            };
            if sel.get_untracked() != i {
                dscroll_mv.set(0);
                sel.set(i);
            }
        };
        // A selection resting on a caption moves to the first row below it.
        if let Some(i) = selectable(&rows, sel.get_untracked(), true) {
            if i != sel.get_untracked() {
                sel.set(i);
            }
        }
        let el = Element::new()
            .style(layout)
            .focusable()
            .focus_signal(focused)
            .on(Phase::Bubble, move |ectx, ev| match ev {
                UiEvent::Key(k) if k.mods.0 == 0 => {
                    let cur = sel.get_untracked();
                    let page = painted_ev.borrow().1.max(1) as usize;
                    let handled = match k.key {
                        Key::Up | Key::Char('k') => {
                            move_to(cur.saturating_sub(1));
                            true
                        }
                        Key::Down | Key::Char('j') => {
                            move_to(cur + 1);
                            true
                        }
                        Key::PageUp if dscroll_ev.get() > 0 => {
                            dscroll_ev.set((dscroll_ev.get() - (page as i32 / 2).max(1)).max(0));
                            dtick.update(|t| *t += 1);
                            true
                        }
                        Key::PageDown if dscroll_ev.get() < dmax_ev.get() => {
                            dscroll_ev.set(
                                (dscroll_ev.get() + (page as i32 / 2).max(1)).min(dmax_ev.get()),
                            );
                            dtick.update(|t| *t += 1);
                            true
                        }
                        Key::PageUp => {
                            move_to(cur.saturating_sub(page / 2));
                            true
                        }
                        Key::PageDown => {
                            move_to(cur + page / 2);
                            true
                        }
                        Key::Home => {
                            move_to(0);
                            true
                        }
                        Key::End => {
                            move_to(n.saturating_sub(1));
                            true
                        }
                        Key::Enter if n > 0 => {
                            let has_detail = rows_ev.get(cur).is_some_and(|r| !r.detail.is_empty());
                            match expanded {
                                Some(e) if has_detail => {
                                    let now = e.get_untracked();
                                    e.set(if now == Some(cur) { None } else { Some(cur) });
                                }
                                _ => {
                                    if let Some(f) = on_activate.borrow_mut().as_mut() {
                                        f(cur);
                                    }
                                }
                            }
                            true
                        }
                        _ => false,
                    };
                    if handled {
                        ectx.stop_propagation();
                    }
                }
                UiEvent::Mouse(m) => match m.kind {
                    MouseKind::Down(MouseButton::Left) => {
                        let rect = ectx.current_rect();
                        let y = m.pos.y - rect.y;
                        let hit = {
                            let p = painted_ev.borrow();
                            if y >= 0 {
                                p.0.get(y as usize).copied().flatten()
                            } else {
                                None
                            }
                        };
                        if let Some(r) = hit {
                            // A second press on the same row within the
                            // double-click window activates it.
                            let now = std::time::Instant::now();
                            let double = last_click.get().is_some_and(|(row, at)| {
                                row == r && now.duration_since(at).as_millis() < 500
                            });
                            last_click.set(Some((r, now)));
                            move_to(r);
                            if double && sel.get_untracked() == r {
                                last_click.set(None);
                                if let Some(f) = on_activate_click.borrow_mut().as_mut() {
                                    f(r);
                                }
                            }
                        }
                        ectx.stop_propagation();
                    }
                    MouseKind::ScrollDown => {
                        move_to(sel.get_untracked() + 1);
                        ectx.stop_propagation();
                    }
                    MouseKind::ScrollUp => {
                        move_to(sel.get_untracked().saturating_sub(1));
                        ectx.stop_propagation();
                    }
                    _ => {}
                },
                _ => {}
            });
        let _ = top_ev;
        el.child(dyn_view(
            LayoutStyle::default().grow(1.0).min_h(1),
            move || {
                let cur = sel.get();
                let exp = expanded.map(|e| e.get());
                let focus = focused.get();
                let rows = rows.clone();
                let rules = rules.clone();
                let top = top.clone();
                let painted = painted.clone();
                let empty = empty.clone();
                let (dscroll, dmax) = (dscroll.clone(), dmax.clone());
                let _ = dtick.get();
                Element::new()
                    .style(LayoutStyle::default().grow(1.0).min_h(1))
                    .draw(move |canvas, rect| {
                        if rect.is_empty() {
                            return;
                        }
                        let ground = Style::new().fg(tokens.text).bg(tokens.surface);
                        canvas.fill_styled(rect, ' ', &ground);
                        if rows.is_empty() {
                            for (i, l) in
                                wrap_text(&empty, rect.w.max(1) as usize).iter().enumerate()
                            {
                                if i as i32 >= rect.h {
                                    break;
                                }
                                canvas.print_styled(
                                    Point::new(rect.x, rect.y + i as i32),
                                    l,
                                    &Style::new().fg(tokens.text_muted).bg(tokens.surface),
                                );
                            }
                            *painted.borrow_mut() = (vec![], rect.h);
                            return;
                        }
                        let lines = wrap_layout(&rules, &rows, rect.w, exp.flatten());
                        // Header is pinned; the body scrolls under it.
                        let header_n = lines
                            .iter()
                            .take_while(|l| l.kind == LineKind::Header)
                            .count();
                        let body_h = (rect.h - header_n as i32).max(1);
                        let first = lines
                            .iter()
                            .position(|l| l.row == Some(cur))
                            .unwrap_or(header_n);
                        let last = lines
                            .iter()
                            .rposition(|l| l.row == Some(cur))
                            .unwrap_or(first);
                        let (first, last) = ((first - header_n) as i32, (last - header_n) as i32);
                        let mut tp = top.get();
                        let block = last - first + 1;
                        if block > body_h {
                            // Taller than the pane: anchored at its first
                            // line plus the inner scroll.
                            dmax.set(block - body_h);
                            let d = dscroll.get().min(block - body_h);
                            dscroll.set(d);
                            tp = first + d;
                        } else {
                            dmax.set(0);
                            dscroll.set(0);
                            if first < tp {
                                tp = first;
                            }
                            if last >= tp + body_h {
                                tp = (last - body_h + 1).min(first);
                            }
                        }
                        let body_n = (lines.len() - header_n) as i32;
                        tp = tp.clamp(0, (body_n - body_h).max(0));
                        top.set(tp);
                        let mut map: Vec<Option<usize>> = Vec::new();
                        let mut y = rect.y;
                        for l in lines.iter().take(header_n) {
                            if y >= rect.y + rect.h {
                                break;
                            }
                            let st = Style::new()
                                .fg(tokens.text_muted)
                                .bg(tokens.surface)
                                .attrs(Attrs::BOLD);
                            canvas.print_styled(Point::new(rect.x, y), &clip(&l.text, rect.w), &st);
                            map.push(None);
                            y += 1;
                        }
                        for l in lines.iter().skip(header_n + tp as usize) {
                            if y >= rect.y + rect.h {
                                break;
                            }
                            let row = l.row.unwrap_or(usize::MAX);
                            let dim = rows.get(row).is_some_and(|r| r.dim);
                            let selected = row == cur;
                            let st = if selected && l.kind == LineKind::Cell {
                                if focus {
                                    Style::new().fg(tokens.selection_fg).bg(tokens.selection_bg)
                                } else {
                                    Style::new()
                                        .fg(tokens.text)
                                        .bg(tokens.surface_raised)
                                        .attrs(Attrs::BOLD)
                                }
                            } else if l.kind == LineKind::Detail {
                                Style::new().fg(tokens.text_muted).bg(tokens.surface)
                            } else if l.kind == LineKind::Group {
                                Style::new()
                                    .fg(tokens.accent)
                                    .bg(tokens.surface)
                                    .attrs(Attrs::BOLD)
                            } else if dim {
                                Style::new().fg(tokens.text_faint).bg(tokens.surface)
                            } else {
                                ground
                            };
                            let line_rect = abstracttui::base::Rect::new(rect.x, y, rect.w, 1);
                            if selected && l.kind == LineKind::Cell {
                                canvas.fill_styled(line_rect, ' ', &st);
                            }
                            canvas.print_styled(Point::new(rect.x, y), &clip(&l.text, rect.w), &st);
                            map.push(l.row);
                            y += 1;
                        }
                        *painted.borrow_mut() = (map, body_h);
                    })
                    .build()
            },
        ))
    }
}

/// Clip to `w` cells (the layout already fits every line; this is the
/// guard against a one-cell rounding at the right border, never a cut of
/// content the layout produced).
fn clip(s: &str, w: i32) -> String {
    if abstracttui::text::width(s) <= w {
        return s.to_string();
    }
    let mut out = String::new();
    let mut used = 0;
    for ch in s.chars() {
        let cw = abstracttui::text::width(&ch.to_string());
        if used + cw > w {
            break;
        }
        out.push(ch);
        used += cw;
    }
    out
}

// ---------------------------------------------------------------------------
// Inline confirm
// ---------------------------------------------------------------------------

/// A pending inline confirmation.
#[derive(Clone)]
pub struct Pending {
    /// The question, one sentence saying what happens.
    pub sentence: String,
    /// The confirming verb (short: "Archive", "Delete").
    pub confirm: String,
    pub action: Rc<dyn Fn()>,
}

/// The WUI's inline confirm, in the terminal: [`InlineConfirm::ask`] puts
/// the sentence on the page (in place, no dialog); `y` runs the action,
/// `n` or Esc keeps. While a confirmation is pending every other key of
/// the page is held (the answer comes first).
#[derive(Clone, Copy)]
pub struct InlineConfirm {
    pub pending: Signal<Option<Pending>>,
}

impl InlineConfirm {
    pub fn new(cx: Scope) -> InlineConfirm {
        InlineConfirm {
            pending: cx.signal(None),
        }
    }

    /// Ask: show `sentence` with `[y] <confirm>  [n] Keep`.
    pub fn ask(
        &self,
        sentence: impl Into<String>,
        confirm: impl Into<String>,
        action: impl Fn() + 'static,
    ) {
        self.pending.set(Some(Pending {
            sentence: sentence.into(),
            confirm: confirm.into(),
            action: Rc::new(action),
        }));
    }

    /// The pending question, if any.
    pub fn current(&self) -> Option<Pending> {
        self.pending.get_untracked()
    }

    pub fn is_open(&self) -> bool {
        self.pending.with_untracked(Option::is_some)
    }

    /// Keep (no action).
    pub fn cancel(&self) {
        self.pending.set(None);
    }

    /// Confirm: run the action once and close.
    pub fn confirm(&self) {
        let p = self.current();
        self.cancel();
        if let Some(p) = p {
            (p.action)();
        }
    }

    /// The confirmation line text (tests and captures).
    pub fn text(p: &Pending) -> String {
        format!("{}  [y] {}  [n] Keep", p.sentence, p.confirm)
    }

    /// Wire the keys onto the page root (capture phase, so the answer is
    /// taken before the focused widget sees the key).
    pub fn keys(self, el: Element) -> Element {
        el.on(Phase::Capture, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if !self.is_open() {
                    return;
                }
                match k.key {
                    Key::Char('y') | Key::Char('Y') => self.confirm(),
                    Key::Char('n') | Key::Char('N') | Key::Escape => self.cancel(),
                    _ => {}
                }
                ectx.stop_propagation();
            }
        })
    }

    /// The line itself, shown in place (zero height when nothing is asked).
    pub fn view(self, t: &TokenSet, width: i32) -> View {
        let tokens = *t;
        // `width` under 20 (e.g. 0, or a viewport read before the first
        // layout) means: the terminal's width minus the page's border.
        dyn_view_scoped(LayoutStyle::column().gap(0).shrink(0.0), move |gcx| {
            let Some(p) = self.pending.get() else {
                return Element::new().style(LayoutStyle::default().h(0)).build();
            };
            let width = if width < 20 {
                abstracttui::app::use_viewport(gcx).get().w - 4
            } else {
                width
            };
            let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
            let words = wrap_text(&p.sentence, (width - 2).max(10) as usize);
            for w in words {
                col = col.child(line(vec![span_bold(w, tokens.warn)]));
            }
            col = col.child(line(vec![
                span_bold("[y] ", tokens.accent),
                span_bold(p.confirm.clone(), tokens.error),
                span("  ", tokens.text),
                span_bold("[n] ", tokens.accent),
                span("Keep", tokens.text),
            ]));
            col.build()
        })
    }
}

/// The web's in-place edit (round 8: the skills shelf folder, a
/// workflow's description, a workspace folder row): `label` then a
/// one-line input that takes the keyboard at once. Enter calls
/// `on_submit` with the text; Esc calls `on_cancel` (the page restores
/// the old value). The page shows its own "Saved" / refusal line.
pub fn inline_input(
    cx: Scope,
    t: &TokenSet,
    label: &str,
    value: Signal<String>,
    placeholder: impl Into<String>,
    on_submit: impl Fn(String) + 'static,
    on_cancel: impl Fn() + 'static,
) -> View {
    let lw = abstracttui::text::width(label) as i32 + 1;
    let cancel = Rc::new(on_cancel);
    let cancel_key = cancel.clone();
    Element::new()
        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
        .on(Phase::Capture, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.key == Key::Escape {
                    cancel_key();
                    ectx.stop_propagation();
                }
            }
        })
        .child(super::util::line_styled(
            LayoutStyle::line(1).w(lw).shrink(0.0),
            vec![span_bold(label.to_string(), t.text)],
        ))
        .child(
            TextInput::new()
                .value(value)
                .placeholder(placeholder)
                .on_submit(move |s: &str| on_submit(s.to_string()))
                .layout(LayoutStyle::default().grow(1.0).h(1))
                .element(cx, t)
                .autofocus()
                .build(),
        )
        .build()
}

/// [`sentence`] indented by `indent` cells on every line (help under a
/// field keeps the field's indent when it wraps).
pub fn sentence_indent(t: &TokenSet, text: &str, width: i32, indent: usize, ink: Rgba) -> View {
    let pad = " ".repeat(indent);
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for l in wrap_text(text.trim_start(), (width - indent as i32).max(10) as usize) {
        col = col.child(line(vec![span(format!("{pad}{l}"), ink)]));
    }
    let _ = t;
    col.build()
}

/// A one-line muted sentence (empty/error text helpers keep the WUI words).
pub fn sentence(t: &TokenSet, text: &str, width: i32, ink: Rgba) -> View {
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for l in wrap_text(text, width.max(10) as usize) {
        col = col.child(line(vec![span(l, ink)]));
    }
    let _ = t;
    col.build()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules() -> Vec<ColRule> {
        vec![ColRule::tail("name", 6), ColRule::head("what it does", 10)]
    }

    #[test]
    fn wrapped_lines_never_exceed_the_table_width() {
        let rules = vec![
            ColRule::tail("Run", 3),
            ColRule::tail("Model", 12),
            ColRule::tail("Status", 6),
        ];
        let rows = vec![Row::new(vec![
            "run-0123456789".into(),
            "lmstudio/a-very-long-model-identifier".into(),
            "completed".into(),
        ])];
        for width in [20, 30, 40, 78] {
            for l in wrap_layout(&rules, &rows, width, None) {
                assert!(
                    abstracttui::text::width(&l.text) < width,
                    "{width}: {:?}",
                    l.text
                );
            }
            let header = &wrap_layout(&rules, &rows, width, None)[0].text;
            assert!(header.starts_with("Run"), "{width}: {header:?}");
        }
    }

    #[test]
    fn footer_bar_caps_its_lines_and_marks_the_cut() {
        let pairs = [
            ("a", "alpha"),
            ("b", "bravo"),
            ("c", "charlie"),
            ("d", "delta"),
            ("e", "echo"),
        ];
        let lines = hint_lines(&pairs, 20);
        assert!(lines.len() > 2, "{lines:?}");
        let mut kept = pairs.to_vec();
        while hint_lines(&[kept.as_slice(), &[("…", "")]].concat(), 20).len() > 2 {
            kept.pop();
        }
        assert!(kept.len() < pairs.len());
    }

    #[test]
    fn a_long_cell_wraps_and_is_never_cut() {
        let rows = vec![Row::new(vec![
            "alice".into(),
            "Writes a short daily summary of everything that happened in the inbox".into(),
        ])];
        let text = wrap_text_table(&rules(), &rows, 40, None);
        assert!(!text.contains('…'), "{text}");
        let joined: String = text
            .lines()
            .skip(1)
            .map(str::trim)
            .collect::<Vec<_>>()
            .join(" ");
        for w in ["Writes", "summary", "everything", "inbox"] {
            assert!(joined.contains(w), "{w} lost: {text}");
        }
        assert!(text.lines().count() > 2, "the cell wrapped: {text}");
        for l in text.lines() {
            assert!(abstracttui::text::width(l) <= 40, "overflow: {l:?}");
        }
    }

    #[test]
    fn enter_reveals_the_detail_lines_of_the_expanded_row_only() {
        let rows = vec![
            Row::new(vec!["a".into(), "x".into()]).detail(vec!["detail of a".into()]),
            Row::new(vec!["b".into(), "y".into()]).detail(vec!["detail of b".into()]),
        ];
        let closed = wrap_text_table(&rules(), &rows, 40, None);
        assert!(!closed.contains("detail of"));
        let open = wrap_text_table(&rules(), &rows, 40, Some(1));
        assert!(open.contains("detail of b") && !open.contains("detail of a"));
    }

    #[test]
    fn group_captions_are_lines_and_never_selected() {
        let rows = vec![
            Row::group("Shared with everyone"),
            Row::new(vec!["a".into(), "x".into()]),
            Row::group("Mine"),
            Row::new(vec!["b".into(), "y".into()]),
        ];
        let text = wrap_text_table(&rules(), &rows, 40, None);
        assert!(
            text.contains("Shared with everyone") && text.contains("Mine"),
            "{text}"
        );
        assert_eq!(selectable(&rows, 0, true), Some(1));
        assert_eq!(selectable(&rows, 2, true), Some(3));
        assert_eq!(selectable(&rows, 2, false), Some(1));
    }

    #[test]
    fn hints_wrap_whole_pairs() {
        let lines = hint_lines(&[("a", "add"), ("e", "edit"), ("d", "delete")], 14);
        assert_eq!(lines, vec!["a add · e edit", "d delete"]);
    }

    #[test]
    fn confirm_text_names_the_verb_and_keep() {
        let p = Pending {
            sentence: "Archive alice?".into(),
            confirm: "Archive".into(),
            action: Rc::new(|| {}),
        };
        assert_eq!(
            InlineConfirm::text(&p),
            "Archive alice?  [y] Archive  [n] Keep"
        );
    }
}
