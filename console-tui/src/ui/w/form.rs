//! Form modals (DESIGN-TUI.md §4.7): ONE centred modal editing several
//! parameters, with the web's apply model per modal (apply-on-change
//! controls show `Saving… / Saved / <sentence> Not saved.` under
//! themselves; a Save modal has exactly one primary button). Title row
//! with a mouse ✕, a one-sentence purpose line, label/control rows.
//! At 80x24 the modal takes the whole page area.

use abstracttui::prelude::*;

use super::super::{open_form_guarded, CloserFn, Ctx, GuardSlot};
use super::paint::{fill_line, wrap, Ink};

/// The state line under an apply-on-change control (or a whole form).
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub enum FieldState {
    #[default]
    Idle,
    Saving,
    /// The success sentence ("Saved", "Saved: alice can use the OpenAI API.").
    Saved(String),
    /// The gateway's refusal, shown with "Not saved." appended unless the
    /// sentence already says it.
    Refused(String),
}

impl FieldState {
    pub fn text(&self) -> String {
        match self {
            FieldState::Idle => String::new(),
            FieldState::Saving => "Saving…".into(),
            FieldState::Saved(s) => s.clone(),
            FieldState::Refused(s) => {
                if s.contains("Not saved") {
                    s.clone()
                } else {
                    format!("{s} Not saved.")
                }
            }
        }
    }
}

pub struct FormModal {
    title: String,
    lead: Option<String>,
    want: Size,
}

impl FormModal {
    pub fn new(title: impl Into<String>) -> FormModal {
        FormModal {
            title: title.into(),
            lead: None,
            want: Size::new(84, 24),
        }
    }
    /// The one-sentence purpose line under the title.
    pub fn lead(mut self, s: impl Into<String>) -> FormModal {
        self.lead = Some(s.into());
        self
    }
    /// Requested panel size (clamped to the viewport; full page area
    /// on an 80x24 terminal).
    pub fn size(mut self, w: i32, h: i32) -> FormModal {
        self.want = Size::new(w, h);
        self
    }

    /// The panel size for viewport `vp`.
    pub fn panel_size(want: Size, vp: Size) -> Size {
        if vp.w <= 90 || vp.h <= 26 {
            // Small terminal: everything but the header and status rows.
            return Size::new(vp.w, (vp.h - 2).max(8));
        }
        Size::new(want.w.min(vp.w - 4), want.h.min(vp.h - 4))
    }

    /// Open. `build(mcx, close, guard, inner_width)` returns the body
    /// (fields); the title row, ✕ and lead are drawn here.
    pub fn open(
        self,
        ctx: &Ctx,
        cx: Scope,
        build: impl FnOnce(Scope, CloserFn, GuardSlot, i32) -> View + 'static,
    ) {
        let vp = abstracttui::app::use_viewport(cx).get_untracked();
        let size = Self::panel_size(self.want, vp);
        let inner_w = (size.w - 6).max(20);
        let title = self.title;
        let lead = self.lead;
        open_form_guarded(ctx, cx, size, move |mcx, close, guard| {
            let t = use_theme(mcx).get().tokens;
            let close_x = close.clone();
            let x = super::action::Action::label("close", "✕").tooltip("Close");
            let title_w = abstracttui::text::width(&title);
            let mut col = Element::new().style(LayoutStyle::column().grow(1.0));
            col = col.child(
                Element::new()
                    .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
                    .child(fill_line(
                        LayoutStyle::default()
                            .width(Dimension::Cells(title_w.min(inner_w - 4)))
                            .height(Dimension::Cells(1)),
                        vec![Ink::new(title.clone(), t.accent).bold()],
                        None,
                    ))
                    .child(
                        Element::new()
                            .style(LayoutStyle::default().grow(1.0))
                            .build(),
                    )
                    .child(super::action::button(
                        mcx,
                        &t,
                        &x,
                        super::action::On::Raised,
                        false,
                        move || close_x(),
                    ))
                    .build(),
            );
            if let Some(l) = &lead {
                for line in wrap(l, inner_w) {
                    col = col.child(fill_line(
                        LayoutStyle::line(1).shrink(0.0),
                        vec![Ink::new(line, t.text_muted)],
                        None,
                    ));
                }
            }
            col = col.child(
                Element::new()
                    .style(LayoutStyle::line(1).shrink(0.0))
                    .build(),
            );
            col.child(build(mcx, close, guard, inner_w)).build()
        });
    }
}

/// A `label  control` row; the label column is `label_w` wide.
pub fn field_row(t: &TokenSet, label: &str, label_w: i32, control: View) -> View {
    Element::new()
        .style(LayoutStyle::row().shrink(0.0).min_h(1))
        .child(fill_line(
            LayoutStyle::default()
                .width(Dimension::Cells(label_w))
                .height(Dimension::Cells(1))
                .shrink(0.0),
            vec![Ink::new(label, t.text)],
            None,
        ))
        .child(
            Element::new()
                .style(LayoutStyle::column().grow(1.0).shrink(1.0))
                .child(control)
                .build(),
        )
        .build()
}

/// A section heading inside a form or page.
pub fn section(t: &TokenSet, title: &str) -> View {
    fill_line(
        LayoutStyle::line(1).shrink(0.0),
        vec![Ink::new(title, t.text).bold()],
        None,
    )
}

/// A wrapped sentence in `ink` (help lines, leads, notes).
pub fn sentence(t: &TokenSet, text: &str, width: i32, ink: Rgba) -> View {
    let _ = t;
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    for l in wrap(text, width) {
        col = col.child(fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(l, ink)],
            None,
        ));
    }
    col.build()
}

/// The live state line of an apply-on-change control.
pub fn state_line(state: Signal<FieldState>, width: i32) -> View {
    dyn_view(LayoutStyle::column().shrink(0.0), move || {
        let t = abstracttui::app::current_theme().tokens;
        let st = state.get();
        let ink = match &st {
            FieldState::Refused(_) => t.error,
            FieldState::Saved(_) => t.ok,
            _ => t.text_muted,
        };
        let text = st.text();
        if text.is_empty() {
            return Element::new().style(LayoutStyle::default().h(0)).build();
        }
        sentence(&t, &text, width, ink)
    })
}

/// A row of buttons (right-aligned footer of a form).
pub fn button_row(views: Vec<View>) -> View {
    let mut row = Element::new()
        .style(
            LayoutStyle::row()
                .height(Dimension::Cells(1))
                .shrink(0.0)
                .gap(1),
        )
        .child(
            Element::new()
                .style(LayoutStyle::default().grow(1.0))
                .build(),
        );
    for v in views {
        row = row.child(v);
    }
    row.build()
}
