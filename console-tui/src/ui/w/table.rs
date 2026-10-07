//! DataTable (DESIGN-TUI.md §4.3): a table whose cells can hold real
//! widgets (action buttons, toggles, clickable badges, links) — the
//! engine's `Table` holds strings only.
//!
//! - Cells WRAP within their column (never cut silently, never a
//!   horizontal scroll); columns are solved from the given width.
//! - Selection by row key (sticky across reloads), the selection pair;
//!   ↑/↓ PgUp/PgDn Home/End move it; a click selects; Enter (or a
//!   double-click) = `on_activate` (the row's first action); Space =
//!   `on_space` when bound (Accounts: Active).
//! - Tab from the table enters the SELECTED row's widgets (only that
//!   row's buttons/toggles are tab stops; every row's stay clickable).
//! - Sortable headers: click a title (or `s` / `S`) → `sort` signal.
//! - The body windows itself to `max_rows` lines (wheel / keys scroll);
//!   no engine `Scroll` (it swallows ←/→, see DESIGN-TUI §7.2).

use std::rc::Rc;

use abstracttui::prelude::*;
use abstracttui::theme::derive;
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

use super::action::{Action, On, RowActions};
use super::paint::{fill_line, wrap, Ink};

/// Column width rule.
#[derive(Clone, Copy, Debug)]
pub enum ColW {
    /// Exactly n cells.
    Cells(i32),
    /// The widest cell (first line) up to `max`, at least `min`.
    Fit { min: i32, max: i32 },
    /// A share of what is left (weight), at least `min`.
    Flex { weight: i32, min: i32 },
}

#[derive(Clone, Debug)]
pub struct Col {
    pub title: String,
    pub w: ColW,
    pub right: bool,
    pub sortable: bool,
}

impl Col {
    pub fn new(title: impl Into<String>, w: ColW) -> Col {
        Col {
            title: title.into(),
            w,
            right: false,
            sortable: false,
        }
    }
    pub fn sortable(mut self) -> Col {
        self.sortable = true;
        self
    }
    pub fn right(mut self) -> Col {
        self.right = true;
        self
    }
}

/// A cell.
#[derive(Clone, Debug)]
pub enum Cell {
    /// Wrapping text (one paragraph of spans).
    Text(Vec<Ink>),
    /// Several lines, each wrapping.
    Lines(Vec<Vec<Ink>>),
    /// A state toggle (`id` names it for `on_toggle`).
    Toggle {
        id: &'static str,
        on: bool,
        refused: Option<String>,
        tip: Option<String>,
    },
    /// A state badge; clickable when `action` is Some (Apps status).
    Badge {
        label: String,
        ink: Rgba,
        action: Option<&'static str>,
        tip: Option<String>,
    },
    /// A link (underlined, `link` ink) firing `action`.
    Link {
        label: String,
        action: &'static str,
        tip: Option<String>,
    },
    /// Action buttons (glyph or labelled).
    Actions(Vec<Action>),
}

impl Cell {
    pub fn text(s: impl Into<String>, fg: Rgba) -> Cell {
        Cell::Text(vec![Ink::new(s, fg)])
    }
    fn natural(&self) -> i32 {
        match self {
            Cell::Text(sp) => sp.iter().map(|s| abstracttui::text::width(&s.text)).sum(),
            Cell::Lines(ls) => ls
                .iter()
                .map(|l| {
                    l.iter()
                        .map(|s| abstracttui::text::width(&s.text))
                        .sum::<i32>()
                })
                .max()
                .unwrap_or(0),
            Cell::Toggle { .. } => 2,
            Cell::Badge { label, .. } => abstracttui::text::width(label),
            Cell::Link { label, .. } => abstracttui::text::width(label),
            Cell::Actions(a) => RowActions::new(a.clone()).width(),
        }
    }
    /// Lines the cell takes at width `w`.
    fn height(&self, w: i32) -> i32 {
        match self {
            Cell::Text(sp) => wrap_spans(sp, w).len() as i32,
            Cell::Lines(ls) => ls
                .iter()
                .map(|l| wrap_spans(l, w).len() as i32)
                .sum::<i32>()
                .max(1),
            Cell::Badge { label, .. } | Cell::Link { label, .. } => {
                wrap(label, w).len().max(1) as i32
            }
            Cell::Toggle { .. } => 1,
            Cell::Actions(a) => actions_lines(a, w),
        }
    }
}

/// A table row.
#[derive(Clone, Debug)]
pub struct Row {
    pub key: String,
    pub cells: Vec<Cell>,
    /// Muted text (archived / inactive rows).
    pub dim: bool,
    /// An inline sentence under the row (a refusal, a receive-only reason).
    pub note: Option<(String, Rgba)>,
    /// A group separator drawn BEFORE this row ("Shared with everyone").
    pub group: Option<String>,
}

impl Row {
    pub fn new(key: impl Into<String>, cells: Vec<Cell>) -> Row {
        Row {
            key: key.into(),
            cells,
            dim: false,
            note: None,
            group: None,
        }
    }
    pub fn dim(mut self, d: bool) -> Row {
        self.dim = d;
        self
    }
    pub fn note(mut self, n: Option<(String, Rgba)>) -> Row {
        self.note = n;
        self
    }
    pub fn group(mut self, g: impl Into<String>) -> Row {
        self.group = Some(g.into());
        self
    }
}

type ActionFn = Rc<dyn Fn(&str, &'static str)>;
type ToggleFn = Rc<dyn Fn(&str, &'static str, bool)>;
type KeyFn = Rc<dyn Fn(&str)>;

pub struct DataTable {
    cols: Vec<Col>,
    rows: Vec<Row>,
    selection: Signal<Option<String>>,
    sort: Option<Signal<(usize, bool)>>,
    on_action: Option<ActionFn>,
    on_toggle: Option<ToggleFn>,
    on_activate: Option<KeyFn>,
    on_space: Option<KeyFn>,
    on_focus: Option<Rc<dyn Fn()>>,
    empty: String,
    width: i32,
    max_rows: i32,
    top: Option<Signal<usize>>,
    autofocus: bool,
}

const GAP: i32 = 2;

impl DataTable {
    pub fn new(cols: Vec<Col>, rows: Vec<Row>, selection: Signal<Option<String>>) -> DataTable {
        DataTable {
            cols,
            rows,
            selection,
            sort: None,
            on_action: None,
            on_toggle: None,
            on_activate: None,
            on_space: None,
            on_focus: None,
            empty: String::new(),
            width: 80,
            max_rows: 1000,
            top: None,
            autofocus: false,
        }
    }
    pub fn sort(mut self, s: Signal<(usize, bool)>) -> DataTable {
        self.sort = Some(s);
        self
    }
    pub fn on_action(mut self, f: impl Fn(&str, &'static str) + 'static) -> DataTable {
        self.on_action = Some(Rc::new(f));
        self
    }
    pub fn on_toggle(mut self, f: impl Fn(&str, &'static str, bool) + 'static) -> DataTable {
        self.on_toggle = Some(Rc::new(f));
        self
    }
    pub fn on_activate(mut self, f: impl Fn(&str) + 'static) -> DataTable {
        self.on_activate = Some(Rc::new(f));
        self
    }
    pub fn on_space(mut self, f: impl Fn(&str) + 'static) -> DataTable {
        self.on_space = Some(Rc::new(f));
        self
    }
    /// Called when the table gains focus (the status bar's hint context).
    pub fn on_focus(mut self, f: impl Fn() + 'static) -> DataTable {
        self.on_focus = Some(Rc::new(f));
        self
    }
    pub fn empty(mut self, s: impl Into<String>) -> DataTable {
        self.empty = s.into();
        self
    }
    /// The width the table lays out in (cells).
    pub fn width(mut self, w: i32) -> DataTable {
        self.width = w.max(10);
        self
    }
    /// Body lines shown before the body windows (scrolls).
    pub fn max_rows(mut self, n: i32) -> DataTable {
        self.max_rows = n.max(1);
        self
    }
    /// Take the keyboard when mounted (the page's main table: its keys work
    /// from the first frame).
    pub fn autofocus(mut self) -> DataTable {
        self.autofocus = true;
        self
    }
    /// A durable scroll position (survives rebuilds of the table).
    pub fn top(mut self, s: Signal<usize>) -> DataTable {
        self.top = Some(s);
        self
    }

    /// Solved column widths for `width`.
    pub fn solve(cols: &[Col], rows: &[Row], width: i32) -> Vec<i32> {
        let n = cols.len() as i32;
        let avail = (width - GAP * (n - 1).max(0)).max(n);
        let natural: Vec<i32> = cols
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let head = abstracttui::text::width(&c.title) + if c.sortable { 1 } else { 0 };
                rows.iter()
                    .filter_map(|r| r.cells.get(i))
                    .map(Cell::natural)
                    .max()
                    .unwrap_or(0)
                    .max(head)
            })
            .collect();
        let mut w: Vec<i32> = cols
            .iter()
            .zip(&natural)
            .map(|(c, nat)| match c.w {
                ColW::Cells(k) => k,
                ColW::Fit { min, max } => (*nat).clamp(min, max.max(min)),
                ColW::Flex { min, .. } => min,
            })
            .collect();
        let used: i32 = w.iter().sum();
        let weights: i32 = cols
            .iter()
            .map(|c| match c.w {
                ColW::Flex { weight, .. } => weight,
                _ => 0,
            })
            .sum();
        if used < avail && weights > 0 {
            let spare = avail - used;
            let mut given = 0;
            let flex: Vec<usize> = (0..cols.len())
                .filter(|i| matches!(cols[*i].w, ColW::Flex { .. }))
                .collect();
            for (k, i) in flex.iter().enumerate() {
                let ColW::Flex { weight, .. } = cols[*i].w else {
                    continue;
                };
                let add = if k + 1 == flex.len() {
                    spare - given
                } else {
                    spare * weight / weights
                };
                w[*i] += add;
                given += add;
            }
        } else if used > avail {
            // Shrink Fit columns toward their minimum, widest first.
            let mut over = used - avail;
            while over > 0 {
                let Some((i, _)) = cols
                    .iter()
                    .enumerate()
                    .filter(|(i, c)| match c.w {
                        ColW::Fit { min, .. } => w[*i] > min,
                        ColW::Flex { min, .. } => w[*i] > min,
                        ColW::Cells(_) => false,
                    })
                    .max_by_key(|(i, _)| w[*i])
                else {
                    break;
                };
                w[i] -= 1;
                over -= 1;
            }
        }
        w
    }

    pub fn view(self, cx: Scope, t: &TokenSet) -> View {
        let DataTable {
            cols,
            rows,
            selection,
            sort,
            on_action,
            on_toggle,
            on_activate,
            on_space,
            on_focus,
            empty,
            width,
            max_rows,
            top,
            autofocus,
        } = self;
        let widths = Self::solve(&cols, &rows, width);
        let t = *t;
        let rows = Rc::new(rows);
        let keys: Rc<Vec<String>> = Rc::new(rows.iter().map(|r| r.key.clone()).collect());
        let top = top.unwrap_or_else(|| cx.signal(0usize));

        // The effective selected index (the signal by key; first row when unset/missing).
        let sel_index = {
            let keys = keys.clone();
            move || -> Option<usize> {
                if keys.is_empty() {
                    return None;
                }
                let k = selection.get();
                Some(
                    k.and_then(|k| keys.iter().position(|x| *x == k))
                        .unwrap_or(0),
                )
            }
        };
        let heights: Rc<Vec<i32>> = Rc::new(
            rows.iter()
                .map(|r| {
                    let h = r
                        .cells
                        .iter()
                        .zip(&widths)
                        .map(|(c, w)| c.height(*w))
                        .max()
                        .unwrap_or(1)
                        .max(1);
                    let note = r
                        .note
                        .as_ref()
                        .map(|(s, _)| wrap(s, width - 2).len() as i32)
                        .unwrap_or(0);
                    let group = if r.group.is_some() { 1 } else { 0 };
                    h + note + group
                })
                .collect(),
        );

        let header = header_view(cx, &t, &cols, &widths, sort);
        let rule = fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new("─".repeat(width.max(1) as usize), t.border)],
            None,
        );

        // Keys on the table root (focusable: one tab stop for the table).
        let n = rows.len();
        let keys_k = keys.clone();
        let heights_k = heights.clone();
        let act_k = on_activate.clone();
        let space_k = on_space.clone();
        let sel_k = sel_index.clone();
        let sortable_cols: Vec<usize> = cols
            .iter()
            .enumerate()
            .filter(|(_, c)| c.sortable)
            .map(|(i, _)| i)
            .collect();
        let mut root = Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .focusable()
            .role(abstracttui::ui::Role::Table)
            .on(Phase::Bubble, move |ectx, ev| {
                if let UiEvent::FocusIn = ev {
                    if let Some(f) = &on_focus {
                        f();
                    }
                }
                let UiEvent::Key(k) = ev else { return };
                if k.mods.0 != 0 && !matches!(k.key, Key::Char('S')) {
                    return;
                }
                if n == 0 {
                    return;
                }
                let cur = untrack(&sel_k).unwrap_or(0);
                let page = (max_rows / 2).max(1) as usize;
                let next = match k.key {
                    Key::Up => Some(cur.saturating_sub(1)),
                    Key::Down => Some((cur + 1).min(n - 1)),
                    Key::PageUp => Some(cur.saturating_sub(page)),
                    Key::PageDown => Some((cur + page).min(n - 1)),
                    Key::Home => Some(0),
                    Key::End => Some(n - 1),
                    Key::Enter => {
                        if let Some(f) = &act_k {
                            ectx.stop_propagation();
                            f(&keys_k[cur]);
                        }
                        None
                    }
                    Key::Char(' ') => {
                        if let Some(f) = &space_k {
                            ectx.stop_propagation();
                            f(&keys_k[cur]);
                        }
                        None
                    }
                    Key::Char('s') | Key::Char('S') => {
                        if let (Some(s), false) = (sort, sortable_cols.is_empty()) {
                            let (c, asc) = s.get_untracked();
                            if k.key == Key::Char('S') {
                                s.set((c, !asc));
                            } else {
                                let pos = sortable_cols.iter().position(|x| *x == c);
                                let next = match pos {
                                    Some(p) => sortable_cols[(p + 1) % sortable_cols.len()],
                                    None => sortable_cols[0],
                                };
                                s.set((next, true));
                            }
                            ectx.stop_propagation();
                        }
                        None
                    }
                    _ => None,
                };
                if let Some(i) = next {
                    ectx.stop_propagation();
                    selection.set(Some(keys_k[i].clone()));
                    keep_visible(top, &heights_k, i, max_rows);
                }
            });
        if autofocus {
            root = root.autofocus();
        }
        root = root.child(header).child(rule);

        if rows.is_empty() {
            let e = empty.clone();
            return root
                .child(fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![Ink::new(e, t.text_muted)],
                    None,
                ))
                .build();
        }

        // Wheel over the body scrolls the window.
        let heights_w = heights.clone();
        let body_rows = rows.clone();
        let body = dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |bcx| {
            let sel = sel_index();
            let first = top.get().min(body_rows.len().saturating_sub(1));
            let mut used = 0;
            let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
            for (i, r) in body_rows.iter().enumerate().skip(first) {
                let h = heights_w[i];
                if used > 0 && used + h > max_rows {
                    break;
                }
                used += h;
                let selected = sel == Some(i);
                col = col.child(row_view(
                    bcx,
                    &t,
                    r,
                    &widths,
                    width,
                    i,
                    selected,
                    selection,
                    on_action.clone(),
                    on_toggle.clone(),
                    on_activate.clone(),
                ));
            }
            col.build()
        });
        let heights_s = heights.clone();
        let total = rows.len();
        let body = Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .on(Phase::Bubble, move |ectx, ev| {
                if let UiEvent::Mouse(m) = ev {
                    match m.kind {
                        MouseKind::ScrollDown => {
                            let cur = top.get_untracked();
                            if fits_from(&heights_s, cur + 1, max_rows)
                                || cur + 1 < total && !fits_from(&heights_s, cur, max_rows)
                            {
                                top.set((cur + 1).min(total.saturating_sub(1)));
                            }
                            ectx.stop_propagation();
                        }
                        MouseKind::ScrollUp => {
                            top.update(|v| *v = v.saturating_sub(1));
                            ectx.stop_propagation();
                        }
                        _ => {}
                    }
                }
            })
            .child(body)
            .build();
        root.child(body).build()
    }
}

/// Does the window starting at `from` still have rows hidden below?
fn fits_from(heights: &[i32], from: usize, max_rows: i32) -> bool {
    if from >= heights.len() {
        return false;
    }
    let rest: i32 = heights[from..].iter().sum();
    rest > max_rows || from == 0
}

/// Move the window so row `i` is visible.
fn keep_visible(top: Signal<usize>, heights: &[i32], i: usize, max_rows: i32) {
    let cur = top.get_untracked();
    if i < cur {
        top.set(i);
        return;
    }
    let mut first = cur;
    loop {
        let used: i32 = heights[first..=i].iter().sum();
        if used <= max_rows || first == i {
            break;
        }
        first += 1;
    }
    if first != cur {
        top.set(first);
    }
}

/// Lines a set of actions takes in `w` cells.
pub fn actions_lines(a: &[Action], w: i32) -> i32 {
    let mut lines = 1;
    let mut used = 0;
    for x in a {
        let add = x.width()
            + if x.display == super::action::Display::Label {
                1
            } else {
                0
            };
        if used > 0 && used + x.width() > w {
            lines += 1;
            used = 0;
        }
        used += add;
    }
    lines
}

/// Wrap a paragraph of spans to `w` cells, keeping each span's ink.
pub fn wrap_spans(spans: &[Ink], w: i32) -> Vec<Vec<Ink>> {
    let w = w.max(1);
    let mut lines: Vec<Vec<Ink>> = vec![Vec::new()];
    let mut used = 0;
    for s in spans {
        for (pi, para) in s.text.split('\n').enumerate() {
            if pi > 0 {
                lines.push(Vec::new());
                used = 0;
            }
            for (wi, word) in para.split(' ').enumerate() {
                let piece = if wi > 0 && used > 0 {
                    format!(" {word}")
                } else {
                    word.to_string()
                };
                let pw = abstracttui::text::width(&piece);
                if used > 0 && used + pw > w {
                    lines.push(Vec::new());
                    used = 0;
                    let pw2 = abstracttui::text::width(word);
                    push_hard(&mut lines, &mut used, word, pw2, w, s);
                } else {
                    push_hard(&mut lines, &mut used, &piece, pw, w, s);
                }
            }
        }
    }
    if lines.len() > 1 && lines.last().map(|l| l.is_empty()).unwrap_or(false) {
        lines.pop();
    }
    lines
}

fn push_hard(lines: &mut Vec<Vec<Ink>>, used: &mut i32, piece: &str, pw: i32, w: i32, s: &Ink) {
    if pw <= w - *used {
        lines.last_mut().expect("line").push(Ink {
            text: piece.to_string(),
            ..s.clone()
        });
        *used += pw;
        return;
    }
    // A word longer than the column: hard-break it.
    let mut cur = String::new();
    let mut cw = 0;
    for ch in piece.chars() {
        let chw = abstracttui::text::width(ch.encode_utf8(&mut [0; 4]));
        if *used + cw + chw > w && (cw > 0 || *used > 0) {
            lines.last_mut().expect("line").push(Ink {
                text: std::mem::take(&mut cur),
                ..s.clone()
            });
            lines.push(Vec::new());
            *used = 0;
            cw = 0;
        }
        cur.push(ch);
        cw += chw;
    }
    lines.last_mut().expect("line").push(Ink {
        text: cur,
        ..s.clone()
    });
    *used += cw;
}

fn header_view(
    cx: Scope,
    t: &TokenSet,
    cols: &[Col],
    widths: &[i32],
    sort: Option<Signal<(usize, bool)>>,
) -> View {
    let t = *t;
    let cols = cols.to_vec();
    let widths = widths.to_vec();
    let _ = cx;
    dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
        let cur = sort.map(|s| s.get());
        let mut row = Element::new().style(LayoutStyle::row().height(Dimension::Cells(1)).gap(GAP));
        for (i, c) in cols.iter().enumerate() {
            let mark = match cur {
                Some((sc, asc)) if sc == i && c.sortable => {
                    if asc {
                        " ▲"
                    } else {
                        " ▼"
                    }
                }
                _ => "",
            };
            let label = format!("{}{}", c.title, mark);
            let ink = if c.sortable { t.text } else { t.text_muted };
            let mut cell = Element::new().style(
                LayoutStyle::default()
                    .width(Dimension::Cells(widths[i]))
                    .height(Dimension::Cells(1))
                    .shrink(0.0),
            );
            if c.sortable {
                if let Some(s) = sort {
                    cell = cell.on(Phase::Bubble, move |ectx, ev| {
                        if let UiEvent::Mouse(m) = ev {
                            if matches!(m.kind, MouseKind::Down(MouseButton::Left)) {
                                let (sc, asc) = s.get_untracked();
                                s.set(if sc == i { (i, !asc) } else { (i, true) });
                                ectx.stop_propagation();
                            }
                        }
                    });
                }
            }
            row = row.child(
                cell.child(fill_line(
                    LayoutStyle::fill(),
                    vec![Ink::new(label, ink).bold()],
                    None,
                ))
                .build(),
            );
        }
        row.build()
    })
}

#[allow(clippy::too_many_arguments)]
fn row_view(
    cx: Scope,
    t: &TokenSet,
    r: &Row,
    widths: &[i32],
    width: i32,
    index: usize,
    selected: bool,
    selection: Signal<Option<String>>,
    on_action: Option<ActionFn>,
    on_toggle: Option<ToggleFn>,
    on_activate: Option<KeyFn>,
) -> View {
    let zebra = derive::mix(t.surface, t.surface_raised, 0.35);
    let bg = if selected {
        t.selection_bg
    } else if index % 2 == 1 {
        zebra
    } else {
        t.surface
    };
    let fg_override = if selected { Some(t.selection_fg) } else { None };
    let dim = r.dim;
    let ink = |c: Rgba| -> Rgba {
        if let Some(f) = fg_override {
            f
        } else if dim {
            t.text_muted
        } else {
            c
        }
    };
    let key = r.key.clone();
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    if let Some(g) = &r.group {
        let rest = (width - abstracttui::text::width(g) - 4).max(0) as usize;
        col = col.child(fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(format!("── {g} {}", "─".repeat(rest)), t.text_muted).bold()],
            None,
        ));
    }
    let h = r
        .cells
        .iter()
        .zip(widths)
        .map(|(c, w)| c.height(*w))
        .max()
        .unwrap_or(1)
        .max(1);
    let mut line = Element::new().style(
        LayoutStyle::row()
            .height(Dimension::Cells(h))
            .gap(GAP)
            .shrink(0.0),
    );
    let on = if selected { On::Selected } else { On::Page };
    for (ci, (c, w)) in r.cells.iter().zip(widths).enumerate() {
        let w = *w;
        let cell_el = Element::new().style(
            LayoutStyle::column()
                .width(Dimension::Cells(w))
                .height(Dimension::Cells(h))
                .shrink(0.0),
        );
        let _ = ci;
        let inner: View = match c {
            Cell::Text(sp) => lines_view(wrap_spans(sp, w), &ink, bg),
            Cell::Lines(ls) => {
                let mut all = Vec::new();
                for l in ls {
                    all.extend(wrap_spans(l, w));
                }
                lines_view(all, &ink, bg)
            }
            Cell::Toggle {
                id,
                on,
                refused,
                tip,
            } => {
                let id = *id;
                let k = key.clone();
                let cb = on_toggle.clone();
                let mut tg = super::toggle::Toggle::new(*on)
                    .refused(refused.clone())
                    .tab_stop(selected)
                    .on_change(move |v| {
                        selection.set(Some(k.clone()));
                        if let Some(f) = &cb {
                            f(&k, id, v);
                        }
                    });
                if let Some(tp) = tip {
                    tg = tg.tip(tp.clone());
                }
                tg.view(cx, t)
            }
            Cell::Badge {
                label,
                ink: bink,
                action,
                tip,
            } => {
                let a = Action::plain(action.unwrap_or("badge"), label.clone())
                    .tooltip(tip.clone().unwrap_or_default());
                match action {
                    Some(id) => {
                        let id = *id;
                        let k = key.clone();
                        let cb = on_action.clone();
                        badge_button(cx, t, &a, *bink, selected, move || {
                            selection.set(Some(k.clone()));
                            if let Some(f) = &cb {
                                f(&k, id);
                            }
                        })
                    }
                    None => {
                        let tipped = Element::new()
                            .style(LayoutStyle::line(1).shrink(0.0))
                            .child(fill_line(
                                LayoutStyle::fill(),
                                vec![Ink::new(label.clone(), ink(*bink))],
                                Some(bg),
                            ));
                        super::tip::with_tip(cx, tipped, tip.clone().unwrap_or_default()).build()
                    }
                }
            }
            Cell::Link { label, action, tip } => {
                let id = *action;
                let k = key.clone();
                let cb = on_action.clone();
                let a = Action::link(id, label.clone())
                    .tooltip(tip.clone().unwrap_or_else(|| label.clone()));
                link_button(cx, t, &a, selected, move || {
                    selection.set(Some(k.clone()));
                    if let Some(f) = &cb {
                        f(&k, id);
                    }
                })
            }
            Cell::Actions(acts) => {
                let k = key.clone();
                let cb = on_action.clone();
                let handler: Rc<dyn Fn(&'static str)> = Rc::new(move |id| {
                    selection.set(Some(k.clone()));
                    if let Some(f) = &cb {
                        f(&k, id);
                    }
                });
                RowActions::new(acts.clone())
                    .view(cx, t, on, selected, w, handler)
                    .0
            }
        };
        line = line.child(cell_el.child(inner).build());
    }
    // Row ground + click-to-select + double-click activate.
    let k2 = key.clone();
    let act = on_activate.clone();
    let line = line
        .draw(move |canvas, rect| {
            if !rect.is_empty() {
                canvas.fill_styled(rect, ' ', &abstracttui::render::Style::new().bg(bg));
            }
        })
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Mouse(m) = ev {
                if matches!(m.kind, MouseKind::Down(MouseButton::Left)) {
                    let was = selection.get_untracked().as_deref() == Some(k2.as_str())
                        || (selected && selection.get_untracked().is_none());
                    selection.set(Some(k2.clone()));
                    if ectx.click_count() >= 2 && was {
                        if let Some(f) = &act {
                            f(&k2);
                        }
                    }
                }
            }
        });
    col = col.child(line.build());
    if let Some((note, nink)) = &r.note {
        for l in wrap(note, width - 2) {
            col = col.child(fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![Ink::new(format!("  {l}"), *nink)],
                Some(bg),
            ));
        }
    }
    col.build()
}

fn lines_view(lines: Vec<Vec<Ink>>, ink: &dyn Fn(Rgba) -> Rgba, bg: Rgba) -> View {
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    for l in lines {
        let spans: Vec<Ink> = l.into_iter().map(|s| Ink { fg: ink(s.fg), ..s }).collect();
        col = col.child(fill_line(LayoutStyle::line(1).shrink(0.0), spans, Some(bg)));
    }
    col.build()
}

/// A clickable state badge: the label is the STATE only (A3); the action
/// sentence is in the tooltip.
fn badge_button(
    cx: Scope,
    t: &TokenSet,
    a: &Action,
    ink: Rgba,
    selected: bool,
    f: impl FnMut() + 'static,
) -> View {
    let mut tt = *t;
    tt.text = ink;
    tt.surface_raised = if selected {
        t.surface
    } else {
        t.surface_raised
    };
    super::action::button(
        cx,
        &tt,
        a,
        if selected { On::Selected } else { On::Page },
        selected,
        f,
    )
}

fn link_button(
    cx: Scope,
    t: &TokenSet,
    a: &Action,
    selected: bool,
    f: impl FnMut() + 'static,
) -> View {
    let mut tt = *t;
    tt.text = t.link;
    super::action::button(
        cx,
        &tt,
        a,
        if selected { On::Selected } else { On::Page },
        selected,
        f,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ink(s: &str) -> Ink {
        Ink::new(s, Rgba::rgb(1, 2, 3))
    }

    #[test]
    fn wrap_spans_never_exceeds_width() {
        let l = wrap_spans(
            &[ink("alice@example.com · connected and more words here")],
            12,
        );
        for line in &l {
            let w: i32 = line.iter().map(|s| abstracttui::text::width(&s.text)).sum();
            assert!(w <= 12, "{w}: {line:?}");
        }
        assert!(l.len() >= 4);
        let joined: String = l.iter().flatten().map(|s| s.text.clone()).collect();
        assert!(joined.contains("alice@example"));
    }

    #[test]
    fn solve_fills_width_and_respects_fixed() {
        let cols = vec![
            Col::new("Name", ColW::Fit { min: 6, max: 20 }),
            Col::new("Email", ColW::Flex { weight: 1, min: 10 }),
            Col::new("Active", ColW::Cells(6)),
        ];
        let rows = vec![Row::new(
            "a",
            vec![
                Cell::Text(vec![ink("alice")]),
                Cell::Text(vec![ink("x")]),
                Cell::Toggle {
                    id: "active",
                    on: true,
                    refused: None,
                    tip: None,
                },
            ],
        )];
        let w = DataTable::solve(&cols, &rows, 60);
        assert_eq!(w[2], 6);
        assert_eq!(w[0], 6);
        assert_eq!(w.iter().sum::<i32>() + 2 * 2, 60);
    }
}
