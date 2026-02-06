"""AbstractGateway.

AbstractGateway is a deployable Run Gateway host for AbstractRuntime:
- durable command inbox (start/resume/pause/cancel/emit_event)
- ledger replay + SSE streaming (replay-first)
- security middleware for network-safe deployments
"""

__version__ = "0.2.0"
