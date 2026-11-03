# Catalog loading and compact disclosure — 0986 / 0988

Delivered locally on 2026-10-30. [Machine-readable receipts](validation.json),
[live desktop](models-live.png), [320px light-theme fixture](models-mobile-fixture.png).
Single-agent implementation and visual review; no independent review is claimed.
Chromium ran through the local Playwright harness because the in-app connector
was unavailable.

The repeated costs were measured in AbstractCore: whole-library architecture
membership scans, repeated filesystem resolution of HF cache roots, and repeated
compatibility/provider checks for individual quantizations. Membership answers
now invalidate with library bytes; roots and compatibility/provider facts share
only the existing presence sweep. A new payload rechecks mutable facts.
Concurrent identical authorized HTTP reads share work; completed results are
not retained and a disconnected waiter cannot cancel other readers.

The page requests the existing curated contract for an initial truthful view
while the full extended catalog still runs. Counts say “so far” and a spinner
says “Loading more versions” until the complete result replaces the preview.
No result cap or quant omission was added. A delayed/failed optional installed
inventory does not hide cards. Refresh/enrichment failures preserve visible
results and expose the error. Refreshes preserve current results. Cards show
three versions; the named header-right chevron expands every matching version
and preserves keyboard focus, aria-expanded, theme styling and management actions.

217 targeted tests passed across the 81-test console, 30-test concurrency/endpoint
and 106-test Core gates. Browser assertions: 112 catalog/wizard and 122 Resources,
zero failures. This includes pending enrichment with usable preview cards,
explicit provisional counts, delayed/failed inventory, deduplication, cancellation,
fresh reads after failures, library invalidation, mobile, light/dark and exact
fixture management operations. Live inspection found healthy status, 83 initial
4-bit matches, three-row cards, working header disclosure/details and all shared
widgets, with no page exceptions or management writes.

Timing samples: initial diagnostic catalog 27s; installed inventory 10.8s.
An intermediate live browser waited 54.9s. Final warm live browser: cards 4.279s,
full list 4.484s; warm HTTP full catalog 2.231s. The first final browser after
restart still took 14.169s / 14.995s while workflow startup competed for CPU.
These samples do not establish a p95 SLA. Residual startup contention is
[0990](../../docs/backlog/proposed/0990_console_startup_workflow_contention.md).

The existing gateway was gracefully restarted with zero in-flight runner work.
Core patches were cherry-picked into its clean existing deployment worktree;
no packages were published and no services/persistence installed. Temporary
profiling code was removed before the final redeploy. During investigation,
a signal-based thread capture coincided with native exit 139; the existing
supervisor recovered the gateway. This is retained in
[0989](../../docs/backlog/proposed/0989_native_stack_capture_crash.md); no further
signal captures were attempted. Verdict: catalog/disclosure changes delivered,
with the two measured residual signals retained for follow-up.
