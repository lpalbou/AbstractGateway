"""Wiring each principal's Runtime to that principal's email account (AbstractRuntime 0.8 seams).

Called by the bundle host every time it builds a Runtime (boot, workflow reload, publish), so a
rebuilt runtime is never left without its account:

- `runtime.set_event_inbox(JsonFileEventInbox(<plane runtime dir>/event_inbox))`: the durable
  inbox the watcher appends received mail to and the `email.received@1` trigger reads;
- `runtime.set_email_context_resolver(fn)`: in memory only; `fn(binding)` answers ONLY for this
  plane's `account_ref` and builds the `EmailContext` from this plane's own store at the moment
  of use (recipient policy, send limits, the user's and the admin's switches);
- `runtime.set_email_binding(...)`: the account automation occurrences are bound to (None while
  the account is not connected or email is turned off), refreshed by the email worker and by
  the settings routes.

Root runs are bound at the door (`bundle_host.start_run`): client-supplied email keys are
popped, then `_runtime.email_account` is SET from the runtime's binding.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .core_mail import EmailError, EmailNotConfigured

from .accounts import EmailPlane, account_store, email_context, email_usable, plane_for_service_config, require_agent_tools

logger = logging.getLogger("abstractgateway.mail")


def plane_for_host(*, data_root: Path, tenant_id: str, user_id: str, runtime_id: str) -> EmailPlane:
    return plane_for_service_config(
        SimpleNamespace(data_dir=Path(data_root), tenant_id=tenant_id, user_id=user_id, runtime_id=runtime_id)
    )


def make_email_resolver(plane: EmailPlane):
    def resolve(binding: Any, *, use: str = "agent_tool"):
        ref = str(getattr(binding, "account_ref", "") or "")
        if ref != plane.account_ref:
            # A run bound to another account never resolves here (one runtime = one account).
            raise EmailNotConfigured(
                "This run is bound to an email account that is not this user's.",
                "Start the run again from your own account.",
            )
        # Execution-time gate (defence in depth; the agents' toolsets are gated at build time
        # too). `use` comes from the runtime: "action" is the runtime's own send-email action
        # (user-authored templates) — the account being connected, enabled and allowed is
        # enough; anything else is an agent/workflow tool call and also needs "Agent email
        # tools" (available from the administrator AND switched on by the user).
        if use != "action":
            require_agent_tools(plane)
        return email_context(plane)

    return resolve


def current_binding(plane: EmailPlane) -> Optional[Any]:
    from abstractruntime.email import EmailBinding

    # The binding follows the ACCOUNT (connected + enabled + allowed), never the agent-tools
    # choice: user-authored send actions work with agent tools off.
    if not email_usable(plane):
        return None
    try:
        st = account_store(plane).settings()
    except EmailError:
        return None
    if st.account is None:
        return None
    return EmailBinding(account_ref=plane.account_ref, address=st.account.address)


def refresh_runtime_binding(runtime: Any, plane: EmailPlane) -> None:
    try:
        runtime.set_email_binding(current_binding(plane))
    except Exception:  # noqa: BLE001 - never breaks the caller (a route, a tick)
        logger.warning("email binding refresh failed for %s", plane.key, exc_info=True)


def wire_runtime_email(runtime: Any, plane: EmailPlane) -> None:
    from abstractruntime.email import JsonFileEventInbox

    runtime.set_event_inbox(JsonFileEventInbox(plane.runtime_data_dir / "event_inbox"))
    runtime.set_email_context_resolver(make_email_resolver(plane))
    refresh_runtime_binding(runtime, plane)


def refresh_cached_service_binding(plane: EmailPlane) -> None:
    """After a settings change: re-bind the plane's live runtime, if its service is built."""

    try:
        from .. import service as service_mod

        with service_mod._service_lock:
            candidates = [service_mod._service] + list(service_mod._services_by_principal.values())
        for svc in candidates:
            if svc is None:
                continue
            worker = getattr(svc, "email_worker", None)
            if worker is not None and worker.plane.root == plane.root:
                refresh_runtime_binding(svc.host.runtime, plane)
    except Exception:  # noqa: BLE001
        logger.warning("email binding refresh (cached services) failed", exc_info=True)


def refresh_all_cached_bindings() -> None:
    """After a gateway-wide capability change: re-bind every live runtime."""

    try:
        from .. import service as service_mod

        with service_mod._service_lock:
            candidates = [service_mod._service] + list(service_mod._services_by_principal.values())
        for svc in candidates:
            worker = getattr(svc, "email_worker", None) if svc is not None else None
            if worker is not None:
                refresh_runtime_binding(svc.host.runtime, worker.plane)
    except Exception:  # noqa: BLE001
        logger.warning("email binding refresh (all services) failed", exc_info=True)


def email_tools_for_data_dir(data_dir: Any) -> bool:
    """Agent email tools active for the plane whose runtime data dir is `data_dir` (a built
    service's), else False — for checks that know only the data dir."""

    try:
        from .. import service as service_mod
        from .accounts import agent_tools_active

        target = Path(data_dir).resolve()
        with service_mod._service_lock:
            candidates = [service_mod._service] + list(service_mod._services_by_principal.values())
        for svc in candidates:
            if svc is None or getattr(svc, "email_worker", None) is None:
                continue
            if Path(svc.config.data_dir).resolve() == target:
                return agent_tools_active(svc.email_worker.plane)
    except Exception:  # noqa: BLE001
        return False
    return False
