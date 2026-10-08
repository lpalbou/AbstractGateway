//! Round 15 shared widget layer (DESIGN-TUI.md §4): the mouse-first,
//! keyboard-complete building blocks every screen composes — action
//! buttons with tooltips (hover AND keyboard focus), state toggles,
//! segmented choices, the data table with widgets in its cells, form
//! modals, toasts/confirmations, the console themes and the caret
//! registry that lets the shell own ←/→.
//!
//! Screens USE these; changes go through the lead (COORD), never forks.

pub mod action;
pub mod caret;
pub mod confirm;
pub mod form;
pub mod glyphs;
pub mod notify;
pub mod paint;
pub mod segmented;
pub mod table;
pub mod theme;
pub mod tip;
pub mod toggle;

pub use action::{Action, Display, RowActions, Tone};
pub use caret::{caret_tracked, Caret};
pub use confirm::Confirm;
pub use form::{field_row, section, state_line, FieldState, FormModal};
pub use notify::{confirm, toast};
pub use paint::{fill_line, Ink};
pub use segmented::Segmented;
pub use table::{Cell, Col, ColW, DataTable, Row};
pub use toggle::Toggle;
