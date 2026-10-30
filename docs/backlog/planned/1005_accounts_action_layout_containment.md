# 1005-abstractgateway: [BUG] Contain Accounts actions inside each row

> Package: abstractgateway
> Type: bug
> Created: 2026-10-30
> Status: planned
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

- [ ] Implement and validate responsive containment.
- [ ] Commit, redeploy and retain completion evidence.
