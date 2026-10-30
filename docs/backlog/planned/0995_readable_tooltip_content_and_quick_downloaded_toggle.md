# 0995-abstractgateway: [IMPROVEMENT] Readable tooltips and quick downloaded filter

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: ux, seat-framework
> Work-item id: abstractgateway-0995
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

`mcFitTitle` concatenates every estimator note, including obsolete download-disk
warnings after installation. The shared tooltip only renders plain text, so the
result is a wall of prose. Adopt UIC 0077's safe structured tooltip for a concise
fit verdict, aligned memory/context facts and actionable current warnings. Read
canonical fit fields, never derive operational policy from note wording. Keep
raw estimator notes in build details rather than the hover. Review Models action,
host and residency tooltips for repetition/long paragraphs; use shared layout.

Add a Downloaded only switch beside Fits in the search bar, synchronized with
the existing Browse/Downloaded controls through one handler. Same inventory
behavior (Downloaded clears fit/precision; Browse restores previous filters),
URL/reload truth, keyboard focus and mobile layout. Guide keeps its existing
single Fits toggle. No backend API, notebook, header or theme fork changes.

## Validation

Adversarial fit fixtures (installed with stale disk notes, insufficient current
memory, unknown estimates and uninstalled disk shortage); browser structured
hover, plain multiline/keyboard, dark/light/phone; quick-filter synchronization,
round trips and operations; shared-kit guards and generator drift checks.
Scoped commits and authorized local redeploy with saved visual evidence.
No new ADR: shared primitive ownership and existing fit authority preserved.

- [ ] Implement and validate content/layout and quick filter.
- [ ] Commit, redeploy, verify live and retain evidence.
