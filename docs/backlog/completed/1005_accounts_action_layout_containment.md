# 1005-abstractgateway: [BUG] Contain Accounts actions inside each row

> Package: abstractgateway
> Type: bug
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
> Priority: P1
> Labels: accounts, ux
> Work-item id: abstractgateway-1005
> Owner: framework
> Thread: local

## Current code reality and scope

The Accounts table reserves 356px for seven 44px actions. Password sign-in added
an eighth action without updating the column budget. The nowrap flex group and
visible cell overflow let Archive escape the table, matching the operator's
screenshot. Percentage text columns also compete with fixed action/Active columns.

Size the column for all current actions, distribute text columns from remaining
space, and use existing flat cards when column minimums cannot fit. Allow safe
action wrapping as a guard, preserve 44px targets, all actions, theme and account
semantics. No credential changes or account migrations. Update stale browser
expectations and verify actual containment on desktop, narrow layouts and touch.

## Validation

Accounts browser layout/action coverage against a disposable Gateway; focused
Accounts/backend gates. Visually verify live dark/light layouts, desktop column
bounds, phone/touch and 44px controls without account mutations. Scoped commit,
authorized idle local restart, served-source and health confirmation.

- [x] Implement and validate responsive containment.
- [x] Commit, redeploy and retain completion evidence.

## Completion report

Gateway `04a3f981` reserves space for eight 44px actions, distributes remaining
text-column width, uses flat cards below the updated container minimum and
allows safe action wrapping. All actions remain available; credentials and
account semantics unchanged. Browser expectations and token login helpers now
match the current password-enabled console.

The Accounts browser suite passes 121 assertions and 19 focused console/workspace
checks pass. One unrelated existing static catalogue tooltip assertion fails
while parsing `mcDeleteButton`; test and catalogue source are unchanged. The
focused rerun excludes that assertion. Live dark/light widths 320–1600px plus
touch show zero overflow, all 44px targets and no account mutations, compared
to the baseline 34px overflow. Idle healthy restart and served version verified.
Two adversarial reviewers' requested checks adopted and passing; integrated
layout verdict approved. [Review and evidence](../../../design/accounts-layout-1005/REVIEW.md).
No new ADR boundary, credential change, Core change, publication, persistence or
required layout follow-up.
