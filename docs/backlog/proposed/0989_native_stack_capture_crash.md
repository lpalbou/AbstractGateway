# 0989-gateway: [IMPROVEMENT] Native thread-stack capture coincided with gateway crash

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: proposed
> Priority: P2
> Labels: reliability, diagnostics, seat-framework
> Work-item id: abstractgateway-0989
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Evidence and proposed work

At 2026-10-30 05:59:26 local time, while investigating catalog latency,
two SIGUSR1 captures used the gateway's registered faulthandler path
(`loop_watchdog.py:LoopWatchdog.start`). The first produced thread stacks;
the second ended with a partial stack and the supervisor recorded exit 139.
The existing supervisor recovered the gateway at 05:59:37. Temporal association
is strong; causality is not proven. No further signal captures were used.

Investigate in an isolated native-runtime reproduction before changing live
watchdog behavior. Check signal dumping with native/GPU threads, partial dump
handling and Python/runtime versions. Preserve watchdog recovery and avoid
reproducing the failure on the operator's host. This is proposed evidence,
not authorization to modify machine services or to blame a particular backend.
ADR impact: assess after diagnosis; none established. Related: completed 0986.
