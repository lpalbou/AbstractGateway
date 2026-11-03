# Resources dashboard track

Status: completed (0980/0981/0983); 0982 remains proposed. Created: 2026-10-30. Completed: 2026-10-30.

Implemented the accepted Resources design inside the existing console. Preserved the
AbstractUIC theme chooser, doc assistant, About, and clickable account/status menu
in the reserved top-right cluster. Treat unified RAM and dedicated VRAM differently.

1. [0980](0980_resources_dashboard_and_memory_topology.md): layout, topology-aware
   capacity, real browser-local trends, inspectors and existing operations.
2. [0981](0981_host_cpu_storage_and_swap_metrics.md): isolated CPU, storage, swap
   and transfer-rate sampling in the existing host snapshot.
3. [0982](../../proposed/0982_resources_retained_history_and_serving_metrics.md):
   proposed retained history and serving-performance follow-up, outside this delivery.

Dependencies: existing host snapshot/controls and kit islands. ADR 0001 preserves
Core/Runtime memory ownership; ADR 0002 preserves inference process isolation.
No new theme/header system, automatic eviction, machine services or deployment mechanism.
The original [prototype](../../../../design/resources/PROPOSAL.md) is a design
reference; its simulated measurements must never enter the production page.

Implementation: `ee918dc9`. Final validation: 124 passed; 96 browser checks; two baseline Runtime-gated failures recorded in the completion reports.


4. [0983](0983_simplify_memory_inspector.md): operator-requested clarity correction
   to the memory inspector, completed and redeployed 2026-10-30 (`08f963ff`).
   Two cards and three figures replace the legacy panel; hover/keyboard kit
   explanations, 18 focused tests, 122 browser checks and live verification.
