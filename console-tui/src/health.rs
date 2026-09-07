//! The centralized connection authority (round-4 architecture review;
//! operator directive: "one centralized state instead of retesting on
//! every page").
//!
//! ONE rule: a transport-class failure anywhere no longer speaks for
//! itself — it triggers ONE background health probe whose settle
//! decides the story every surface tells. `ConnPhase` is the single
//! connection truth: `Verifying` while the probe runs (header +
//! panels + footer all render it), then either `Connected` (the
//! failures were transient — each failed domain is retried ONCE) or
//! the settled failure phase (every panel defers to the one
//! connection story).
//!
//! Bounds, each structural:
//! - the probe fires only on the `Connected -> Verifying` edge, so a
//!   failing probe can never re-trigger itself;
//! - one probe in flight by definition (the phase IS the guard);
//! - each slot's failure funds at most ONE verification+retry until a
//!   user action re-arms it (`conn_retry_spent`, cleared by r /
//!   refresh / reset) — an endpoint that keeps failing while /ping
//!   answers cannot loop;
//! - stale settles are discarded by generation (`probe_gen`).
//!
//! The probe runs on its OWN short-lived thread with a FRESH client
//! built from the same credential resolution the Probe button uses —
//! never through the worker queue (it must not wait behind a 300s
//! route test), never sharing the worker's client.

use std::sync::mpsc::Sender;

use abstracttui::prelude::*;

use crate::api::{ApiError, ApiErrorKind};
use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::Cmd;

/// The ten reload-safe domains: fieldless Load* commands whose retry
/// is a pure read. Action slots (sandbox, route_test, discover,
/// models, voices, entity_*) deliberately have `cmd: None` — their
/// failures trigger verification (and are budget-marked so they
/// cannot re-trigger), but a retry would repeat a real action (a
/// sandbox retry is a paid generation; entity_detail reloads through
/// its own selection effect).
struct Slot {
    name: &'static str,
    failed_unreachable: fn(&Store) -> bool,
    set_loading: fn(&Store),
    cmd: Option<fn() -> Cmd>,
}

fn unreachable_failed<T>(l: &Loadable<T>) -> bool {
    matches!(
        l,
        Loadable::Failed(ApiError {
            kind: ApiErrorKind::Unreachable,
            ..
        })
    )
}

fn slots() -> Vec<Slot> {
    macro_rules! domain {
        ($field:ident, $cmd:expr) => {
            Slot {
                name: stringify!($field),
                failed_unreachable: |s| s.$field.with_untracked(unreachable_failed),
                set_loading: |s| s.$field.set(Loadable::Loading),
                cmd: $cmd,
            }
        };
    }
    vec![
        domain!(providers, Some(|| Cmd::LoadProviders)),
        domain!(profiles, Some(|| Cmd::LoadProfiles)),
        domain!(routes, Some(|| Cmd::LoadRoutes)),
        domain!(users, Some(|| Cmd::LoadUsers)),
        domain!(entities, Some(|| Cmd::LoadEntities)),
        domain!(runtimes, Some(|| Cmd::LoadRuntimes)),
        domain!(runtime_config, Some(|| Cmd::LoadRuntimeConfig)),
        // The runs slot is plane-scoped (it follows the CHOSEN runtime
        // on the Runtimes screen; nothing loads before a choice —
        // operator directive 2026-07-26). This static table cannot
        // know the chosen scope, and retrying the Own lane would load
        // a plane nobody asked for — so: verify + budget-mark only;
        // the Sessions panel's own effect reloads the chosen plane on
        // the next look, and `r` recovers explicitly.
        domain!(runs, None),
        domain!(data_homes, Some(|| Cmd::LoadDataHomes { sizes: false })),
        // Artifacts/logs are PARAMETERIZED loads (page + filters), so their
        // retry is not parameter-free — tracked for health, no blind retry
        // command (the runs slot sets the same precedent).
        domain!(artifacts, None),
        domain!(logs, None),
        domain!(reservations, Some(|| Cmd::LoadReservations)),
        // Action slots: verify + budget-mark, never auto-retry.
        domain!(discover, None),
        domain!(sandbox, None),
        domain!(voices, None),
        domain!(route_test, None),
        domain!(entity_detail, None),
        domain!(entity_policy, None),
        domain!(entity_prompt, None),
        domain!(entity_candidates, None),
    ]
}

/// TRACKED reads of every slot — the effect's subscription surface.
/// (The table's own reads are untracked so settle-time recomputation
/// never subscribes; this function is what makes the trigger effect
/// re-run when any slot changes.)
fn track_all(store: &Store) {
    store.providers.with(|_| ());
    store.profiles.with(|_| ());
    store.routes.with(|_| ());
    store.users.with(|_| ());
    store.entities.with(|_| ());
    store.runtimes.with(|_| ());
    store.runtime_config.with(|_| ());
    store.runs.with(|_| ());
    store.data_homes.with(|_| ());
    store.artifacts.with(|_| ());
    store.logs.with(|_| ());
    store.reservations.with(|_| ());
    store.discover.with(|_| ());
    store.sandbox.with(|_| ());
    store.voices.with(|_| ());
    store.route_test.with(|_| ());
    store.entity_detail.with(|_| ());
    store.entity_policy.with(|_| ());
    store.entity_prompt.with(|_| ());
    store.entity_candidates.with(|_| ());
}

/// Install the trigger: watches every domain slot + the write-failure
/// channel; on a transport-class failure while `Connected` (with at
/// least one unspent budget), flips to `Verifying` and fires the
/// prober. The prober is injected (production: a thread with a fresh
/// client; tests: a recorder) — `settle` is a plain function either
/// calls.
pub fn install(cx: Scope, ctx: &crate::ui::Ctx) {
    let store = ctx.store;
    let prober = ctx.prober.clone();
    let ctx2 = ctx.clone();
    let last_seq = cx.signal(0u64);
    cx.effect(move || {
        let seq = store.net_fail_seq.get();
        track_all(&store);
        // Everything below is untracked: decisions never subscribe.
        let seq_is_new = last_seq.with_untracked(|l| *l != seq);
        let unspent_failure = {
            let spent = store.conn_retry_spent.get_untracked();
            slots()
                .iter()
                .any(|s| (s.failed_unreachable)(&store) && !spent.contains(&s.name))
        };
        if !(seq_is_new || unspent_failure) {
            return;
        }
        let ConnPhase::Connected(id) = store.conn.get_untracked() else {
            // Not connected (or already verifying): the failure wave
            // is either pre-connection or already being verified.
            if seq_is_new {
                last_seq.set(seq);
            }
            return;
        };
        if seq_is_new {
            last_seq.set(seq);
        }
        let gen = store.probe_gen.with_untracked(|g| g + 1);
        store.probe_gen.set(gen);
        store.conn.set(ConnPhase::Verifying(id));
        store.notice.set(Some(
            "network hiccup — verifying the gateway connection…".into(),
        ));
        let (url, token) = ctx2.effective_credentials();
        if let Some(p) = prober.borrow().as_ref() {
            p(url, token, gen);
        }
    });
}

/// Settle a probe outcome. Plain function so the production thread
/// posts it through the wake handle and headless tests call it
/// directly — no network anywhere near the tests.
pub fn settle(store: Store, tx: &Sender<Cmd>, gen: u64, outcome: Result<(), ApiError>) {
    if store.probe_gen.get_untracked() != gen {
        return; // stale settle — a newer probe owns the story
    }
    let ConnPhase::Verifying(id) = store.conn.get_untracked() else {
        return; // a probe/reset settled the phase first
    };
    match outcome {
        Ok(()) => {
            store.conn.set(ConnPhase::Connected(id));
            let spent_before = store.conn_retry_spent.get_untracked();
            let mut newly_spent = Vec::new();
            let mut retried = 0usize;
            for slot in slots() {
                if !(slot.failed_unreachable)(&store) || spent_before.contains(&slot.name) {
                    continue;
                }
                newly_spent.push(slot.name);
                if let Some(cmd) = slot.cmd {
                    (slot.set_loading)(&store);
                    let _ = tx.send(cmd());
                    retried += 1;
                }
            }
            if !newly_spent.is_empty() {
                store.conn_retry_spent.update(|s| s.extend(newly_spent));
            }
            store.notice.set(Some(if retried > 0 {
                format!("✓ connection verified — retrying {retried} failed read(s)…")
            } else {
                "✓ connection verified — the failure did not recur on /ping".to_string()
            }));
        }
        Err(e) => {
            store.notice.set(Some(format!(
                "✗ gateway connection lost: {e} — probe from the Connection screen when it's back"
            )));
            store.conn.set(crate::store::phase_from_probe_error(&e));
        }
    }
}
