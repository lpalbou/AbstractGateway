//! "My workspace policy" — the caller's OWN policy (web: the Users tab's
//! "My workspace policy" section, open to every signed-in principal, not
//! only admins). `GET/PUT /workspace/policy/self`: mode, launch-folder
//! trust, extra allowed folders, refused folders; Reset sends `{}` back
//! to inherited. The admin-classed scope-override grant is not offered
//! here (the route refuses it from self-service).
//!
//! Opened with `w` on the Users screen (one shortcut line in users.rs).

use abstracttui::prelude::*;
use abstracttui::widgets::{SubmitPolicy, TextArea, TextAreaState};

use super::util::{ellipsize, field, line, line_styled, span, span_bold};
use super::{open_form, Ctx};
use crate::store::operator::{my_policy_body, MyPolicy};
use crate::store::{ConnPhase, Loadable};
use crate::worker::operator::OpCmd;
use crate::worker::Cmd;

/// Open the form; it reads the policy first and fills in when it lands.
pub fn open(cx: Scope, ctx: &Ctx) {
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        ctx.store
            .notice
            .set(Some("not connected — probe on the Connection screen first".into()));
        return;
    }
    ctx.store.op.my_policy.set(Loadable::Loading);
    ctx.send(Cmd::Operator(OpCmd::LoadMyPolicy));
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(96, 30), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let store = ctx2.store;
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        let ctx_body = ctx2.clone();
        let close_cancel = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("My workspace policy", t0.accent)]))
            .child(line(vec![span(
                "Whitelist (default): deny everything, allow your folders (+ the launch folder while trust is on). \
                 Blacklist: allow everything except your refused folders. The gateway-wide deny list always applies.",
                t0.text_faint,
            )]))
            .child(dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |bcx| {
                let t = theme.get().tokens;
                match store.op.my_policy.get() {
                    Loadable::Ready(p) => form_body(bcx, &ctx_body, &t, p, form_id, in_flight, form_error),
                    Loadable::Failed(e) => line(vec![span(format!("✗ {e}"), t.error)]),
                    _ => line(vec![span("◌ reading my workspace policy…", t.info)]),
                }
            }))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_cancel())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

fn form_body(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    p: MyPolicy,
    form_id: u64,
    in_flight: Signal<bool>,
    form_error: Signal<Option<String>>,
) -> View {
    let t0 = *t;
    let mode = cx.signal(p.mode.clone());
    let trust = cx.signal(p.trust.clone());
    let allowed0 = p.allowed.join("\n");
    let blocked0 = p.blocked.join("\n");
    let allowed = cx.signal(allowed0.clone());
    let blocked = cx.signal(blocked0.clone());
    let allowed_state = TextAreaState::new(cx);
    allowed_state.set_text(allowed0);
    let blocked_state = TextAreaState::new(cx);
    blocked_state.set_text(blocked0);
    let ctx_save = ctx.clone();
    let ctx_reset = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        // Pinned rows: who this is and what applies must survive height
        // pressure from the two text areas below.
        .child(line_styled(LayoutStyle::line(1).shrink(0.0), vec![
            span(format!("{}:{} · ", p.tenant_id, p.user_id), t0.text_muted),
            span(
                if p.customized { "customized" } else { "inherits the gateway defaults" },
                t0.text,
            ),
        ]))
        .child(line_styled(
            LayoutStyle::line(1).shrink(0.0),
            vec![span(ellipsize(&p.effective_text(), 92), t0.text_faint)],
        ))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let label = match trust.get().as_str() {
                "on" => "on — agents may write where they start",
                "off" => "off — launch folders are not trusted",
                _ => "inherit gateway default",
            };
            line(vec![span("launch-folder trust: ", t0.text_muted), span(label, t0.text)])
        }))
        .child(
            Button::new("change launch-folder trust")
                .on_click(move || {
                    trust.update(|v| {
                        *v = match v.as_str() {
                            "" => "on".to_string(),
                            "on" => "off".to_string(),
                            _ => String::new(),
                        }
                    })
                })
                .element(cx, &t0)
                .build(),
        )
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let label = match mode.get().as_str() {
                "whitelist" => "whitelist — deny everything, allow my list",
                "blacklist" => "blacklist — allow everything, refuse my list",
                _ => "whitelist (default) — deny everything, allow my list",
            };
            line(vec![span("access mode: ", t0.text_muted), span(label, t0.text)])
        }))
        .child(
            Button::new("change access mode")
                .on_click(move || {
                    mode.update(|m| {
                        *m = match m.as_str() {
                            "" => "whitelist".to_string(),
                            "whitelist" => "blacklist".to_string(),
                            _ => String::new(),
                        }
                    })
                })
                .element(cx, &t0)
                .build(),
        )
        .child(field(
            &t0,
            "extra allowed",
            TextArea::new()
                .state(&allowed_state)
                .placeholder("/abs/path/to/project — one per line, added to the gateway-wide roots")
                .on_change(move |s: &str| {
                    if allowed.with_untracked(|cur| cur != s) {
                        allowed.set(s.to_string());
                    }
                })
                .submit_policy(SubmitPolicy::EnterInserts)
                .rows(3, 4)
                .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                .element(cx, &t0)
                .build(),
        ))
        .child(field(
            &t0,
            "refused folders",
            TextArea::new()
                .state(&blocked_state)
                .placeholder("/abs/path/to/private — always denied, in every posture")
                .on_change(move |s: &str| {
                    if blocked.with_untracked(|cur| cur != s) {
                        blocked.set(s.to_string());
                    }
                })
                .submit_policy(SubmitPolicy::EnterInserts)
                .rows(3, 4)
                .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                .element(cx, &t0)
                .build(),
        ))
        .child(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Save my workspace policy")
                        .on_click(move || {
                            if in_flight.get_untracked() {
                                return;
                            }
                            let body = my_policy_body(
                                &mode.get_untracked(),
                                &trust.get_untracked(),
                                &allowed.get_untracked(),
                                &blocked.get_untracked(),
                            );
                            form_error.set(None);
                            in_flight.set(true);
                            ctx_save.send(Cmd::Operator(OpCmd::SaveMyPolicy {
                                body: body.into(),
                                clear: false,
                                form_id: Some(form_id),
                            }));
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .child(
                    Button::new("Reset to inherited")
                        .on_click(move || {
                            if in_flight.get_untracked() {
                                return;
                            }
                            form_error.set(None);
                            in_flight.set(true);
                            ctx_reset.send(Cmd::Operator(OpCmd::SaveMyPolicy {
                                body: serde_json::json!({}).into(),
                                clear: true,
                                form_id: Some(form_id),
                            }));
                        })
                        .element(cx, &t0)
                        .build(),
                )
                .build(),
        )
        .build()
}
