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

from .accounts import EmailPlane, account_store, email_context, email_usable, plane_for_service_config

logger = logging.getLogger("abstractgateway.mail")


def plane_for_host(*, data_root: Path, tenant_id: str, user_id: str, runtime_id: str) -> EmailPlane:
    return plane_for_service_config(
        SimpleNamespace(data_dir=Path(data_root), tenant_id=tenant_id, user_id=user_id, runtime_id=runtime_id)
    )


def make_email_resolver(plane: EmailPlane):
    def resolve(binding: Any):
        ref = str(getattr(binding, "account_ref", "") or "")
        if ref != plane.account_ref:
            # A run bound to another account never resolves here (one runtime = one account).
            raise EmailNotConfigured(
                "This run is bound to an email account that is not this user's.",
                "Start the run again from your own account.",
            )
        return email_context(plane)

    return resolve


def current_binding(plane: EmailPlane) -> Optional[Any]:
    from abstractruntime.email import EmailBinding

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
