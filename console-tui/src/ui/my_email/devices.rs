//! My email → Notifications → **Devices** (framework backlog 1049; web:
//! `console_push.py`): the Apple Watches and iPhones that get alerts from this
//! gateway while their app is closed. Read-only, nothing to configure (an app
//! registers itself at sign-in; the system's notification permission is the
//! switch). One line per device from `GET /me/push/devices`:
//! "name · app", then "Registered … · last alert …", then the last error in
//! words. Your own page only (an entity's page has no Devices).

use abstracttui::prelude::*;
use serde_json::Value;

use super::{gap, helper, line, span, span_bold, Page};
use crate::store::email::email_error_text;
use crate::store::Loadable;
use crate::ui::Ctx;
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

/// The JSON lane's read of `GET /me/push/devices` (this page's own key).
pub const K_DEVICES: &str = "my_email.push_devices";
const DEVICES_PATH: &str = "/me/push/devices";
pub const DEVICES: &str = "Devices";
pub const DEVICES_HELP: &str = "Your Apple Watch and iPhone get alerts here while the app is closed. Nothing to set up: sign in on the device and allow notifications.";
pub const NO_DEVICES: &str =
    "No watch or iPhone yet. Sign in to the AbstractAssistant app on one and allow notifications.";

/// One registered device (a `GET /me/push/devices` row; the gateway never sends the token).
#[derive(Clone, Debug, PartialEq)]
pub struct PushDevice {
    pub device_id: String,
    pub app: String,
    pub name: String,
    pub refreshed_at: String,
    pub last_sent_at: String,
    pub last_error: String,
}

fn st(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

/// The rows, in the gateway's order (oldest registration first).
pub fn push_devices(v: &Value) -> Vec<PushDevice> {
    v.as_array()
        .map(|rows| {
            rows.iter()
                .map(|d| PushDevice {
                    device_id: st(d, "device_id"),
                    app: st(d, "app"),
                    name: st(d, "name"),
                    refreshed_at: st(d, "refreshed_at"),
                    last_sent_at: st(d, "last_sent_at"),
                    last_error: st(d, "last_error"),
                })
                .collect()
        })
        .unwrap_or_default()
}

impl PushDevice {
    /// "Apple Watch" | "iPhone" (the web's words).
    pub fn app_words(&self) -> &str {
        match self.app.as_str() {
            "watch" => "Apple Watch",
            "phone" => "iPhone",
            other => other,
        }
    }

    /// "name · app", or the app alone.
    pub fn title(&self) -> String {
        if self.name.is_empty() {
            self.app_words().to_string()
        } else {
            format!("{} \u{b7} {}", self.name, self.app_words())
        }
    }

    /// "Registered 5 min ago · last alert 2 min ago" (or "no alert yet").
    pub fn meta(&self, now_s: i64) -> String {
        let ago =
            |iso: &str| crate::ui::triggers::ago(iso, now_s).unwrap_or_else(|| iso.to_string());
        let mut parts = Vec::new();
        if !self.refreshed_at.is_empty() {
            parts.push(format!("Registered {}", ago(&self.refreshed_at)));
        }
        parts.push(if self.last_sent_at.is_empty() {
            "no alert yet".to_string()
        } else {
            format!("last alert {}", ago(&self.last_sent_at))
        });
        parts.join(" \u{b7} ")
    }
}

fn now_s() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Read the devices when the page opens (your own page only).
pub(super) fn install(ctx: &Ctx) {
    ctx.send(Cmd::Json(JsonCmd::get(K_DEVICES, DEVICES_PATH)));
}

/// The Devices card: its sentence, then one block per device (or why there is none).
pub(super) fn section(_cx: Scope, ctx: &Ctx, t: &TokenSet, p: Page) -> View {
    let t0 = *t;
    let store = ctx.store;
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(vec![span_bold(DEVICES.to_string(), t0.text)]))
        .child(helper(&t0, DEVICES_HELP, p.wrap_w))
        .child(dyn_view(
            LayoutStyle::column().gap(0).shrink(0.0),
            move || {
                let t = t0;
                match store.json.get(K_DEVICES) {
                    Loadable::Ready(v) => {
                        let rows = push_devices(&v);
                        if rows.is_empty() {
                            return helper(&t, &format!("  {NO_DEVICES}"), p.wrap_w);
                        }
                        let now = now_s();
                        let mut col =
                            Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                        for d in rows {
                            col = col
                                .child(gap())
                                .child(line(vec![span(format!("  {}", d.title()), t.text)]))
                                .child(line(vec![span(
                                    format!("    {}", d.meta(now)),
                                    t.text_muted,
                                )]));
                            if !d.last_error.is_empty() {
                                for l in crate::ui::util::wrap_text(
                                    &d.last_error,
                                    p.wrap_w.saturating_sub(4).max(10),
                                ) {
                                    col = col.child(line(vec![span(format!("    {l}"), t.warn)]));
                                }
                            }
                        }
                        col.build()
                    }
                    Loadable::Failed(e) => helper(
                        &t,
                        &format!("  Could not list your devices: {}", email_error_text(&e)),
                        p.wrap_w,
                    ),
                    _ => line(vec![span("  \u{25cc} Reading your devices...", t.info)]),
                }
            },
        ))
        .build()
}
