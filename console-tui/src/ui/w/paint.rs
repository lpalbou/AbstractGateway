//! Small paint helpers: a line of styled spans on an optional ground
//! (the selection pair / zebra need the ground filled, `util::line`
//! leaves it transparent).

use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::Style;

/// One span: text, ink, bold, underline.
#[derive(Clone, Debug)]
pub struct Ink {
    pub text: String,
    pub fg: Rgba,
    pub bold: bool,
    pub underline: bool,
}

impl Ink {
    pub fn new(text: impl Into<String>, fg: Rgba) -> Ink {
        Ink {
            text: text.into(),
            fg,
            bold: false,
            underline: false,
        }
    }
    pub fn bold(mut self) -> Ink {
        self.bold = true;
        self
    }
    pub fn underline(mut self) -> Ink {
        self.underline = true;
        self
    }
}

/// A one-row line of spans on `bg` (filled across the whole rect when
/// `Some`), clipped to its rect with an honest `…` when it does not fit.
pub fn fill_line(style: LayoutStyle, spans: Vec<Ink>, bg: Option<Rgba>) -> View {
    Element::new()
        .style(style)
        .draw(move |canvas, rect| {
            if rect.is_empty() {
                return;
            }
            if let Some(bg) = bg {
                canvas.fill_styled(rect, ' ', &Style::new().bg(bg));
            }
            let mut x = rect.x;
            let right = rect.x + rect.w;
            for s in &spans {
                if x >= right {
                    break;
                }
                let mut st = Style::new().fg(s.fg);
                if let Some(bg) = bg {
                    st = st.bg(bg);
                }
                if s.bold {
                    st = st.bold();
                }
                if s.underline {
                    st = st.attrs(abstracttui::render::Attrs::UNDERLINE);
                }
                let budget = (right - x).max(0);
                let fitted = fit(&s.text, budget);
                canvas.print_styled(Point::new(x, rect.y), &fitted, &st);
                x += abstracttui::text::width(&fitted);
            }
        })
        .build()
}

/// Cell-width truncation with `…`.
pub fn fit(text: &str, max: i32) -> String {
    if abstracttui::text::width(text) <= max {
        return text.to_string();
    }
    if max <= 0 {
        return String::new();
    }
    let mut out = String::new();
    let mut used = 0;
    for ch in text.chars() {
        let w = abstracttui::text::width(ch.encode_utf8(&mut [0; 4]));
        if used + w > max - 1 {
            break;
        }
        out.push(ch);
        used += w;
    }
    out.push('…');
    out
}

/// Word wrap to `width` cells (explicit newlines kept; long words hard-break).
pub fn wrap(text: &str, width: i32) -> Vec<String> {
    super::super::util::wrap_text(text, width.max(1) as usize)
}
