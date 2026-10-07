//! DESIGN-v2 §2.3: another user's email as the admin sees it (the Accounts
//! screen opens `my_email::open_other`).

mod r2email;

use abstractgateway_console::ui::{self, my_email};
use abstractgateway_console::worker::Cmd;
use abstracttui::prelude::*;
use r2email::harness;

#[test]
fn other_user_email_is_address_only_with_an_inline_save() {
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    let (cx, ctx) = h.root.borrow().clone().expect("root");
    my_email::open_other(
        cx,
        &ctx,
        "alice".into(),
        "default".into(),
        Some("alice@example.test".into()),
        String::new(),
    );
    let s = h.turns(3);
    assert!(s.contains("Email — alice"), "{s}");
    assert!(
        s.contains("Where alice's sign-in codes and notifications go."),
        "{s}"
    );
    // The web's sentence (wrapped inside the modal).
    assert!(
        s.contains("Mailbox: not connected — only alice can connect a mailbox.")
            && s.contains("anyone's mail."),
        "{s}"
    );
    let inner = r2email::inside_modal(&s);
    for banned in ["Password", "Incoming mail", " Connect "] {
        assert!(
            !inner.contains(banned),
            "{banned:?} (no mailbox form):\n{s}"
        );
    }
    let _ = h.sent();
    h.key(b"\x1b[F");
    h.key(b"x");
    let s = h.turns(2);
    assert!(
        s.contains("Save"),
        "Save appears once the field differs:\n{s}"
    );
    h.key(b"\r");
    let sent = h.sent();
    let patch = sent
        .iter()
        .find_map(|c| match c {
            Cmd::PatchUser { user_id, body, .. } => Some((user_id.clone(), body.0.clone())),
            _ => None,
        })
        .expect("PATCH /admin/users/alice");
    assert_eq!(patch.0, "alice");
    assert_eq!(patch.1["email"], serde_json::json!("alice@example.testx"));
    // A connected mailbox: the Accounts row's words, read-only.
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.turns(2);
    let (cx, ctx) = h.root.borrow().clone().expect("root");
    my_email::open_other(
        cx,
        &ctx,
        "bob".into(),
        "default".into(),
        None,
        "Connected as bob@x.test".into(),
    );
    let s = h.turns(3);
    assert!(s.contains("Connected as bob@x.test"), "{s}");
}
