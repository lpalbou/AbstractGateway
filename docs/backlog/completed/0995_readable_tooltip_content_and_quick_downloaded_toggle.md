# 0995-abstractgateway: [IMPROVEMENT] Readable tooltips and quick downloaded filter

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
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

- [x] Implement and validate content/layout and quick filter.
- [x] Commit, redeploy, verify live and retain evidence.

## Completion report

Implemented `15a26edc`, adopting shared UIC `3833355` (0077). Fit, host and
residency tooltips now have concise titles, aligned facts and relevant actions.
Installed models suppress stale download warnings; raw estimator diagnostics
remain in expandable build details. Downloaded only is beside Fits, sharing tab,
inventory, restore and URL behavior. Guide retains its single switch.

182 catalog and 122 Resources browser assertions pass. Focused pytest: 53 passed
with one selective-export fixture failure, corrected and verified in a passing
34-check follow-up gate. Shared tooltip/filter/theme/build guards pass. Healthy
graceful redeploy and live dark/light/320px interaction, reload and keyboard
checks pass; no page errors or model-management writes. Real reported installed
model tooltip is now about 166px tall with four facts and no stale disk warning.
[Review and screenshots](../../../design/model-tooltips-0995/REVIEW.md),
[validation](../../../design/model-tooltips-0995/validation.json).
No new residual work or ADR impact; no registry publication. Agora unavailable.
