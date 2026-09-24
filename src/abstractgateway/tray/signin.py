"""Signed-in console links for the tray ("Open Console", 2026-09-24).

The console's browser session lasts 8 hours; after that a plain
`/console` URL lands on a token prompt a non-technical user cannot answer.
So the tray opens the console the way `abstractgateway claim` does: it mints a
one-time claim code LOCALLY (a file under `<data>/auth/claims/`, which only
someone who can write the data dir can do) and opens `/console#claim=<code>`;
the console redeems it (loopback peers only, single use) for an admin session.

No HTTP endpoint is involved in minting — there is deliberately no
unauthenticated "sign me in" route on the gateway. When the gateway does not
run with user auth (static token mode) a claim would be refused, so the plain
URL is opened and the result says why.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("abstractgateway.tray")

TRAY_CLAIM_TTL_S = 120  # the browser opens within seconds; a short life is the safer one


@dataclass(frozen=True)
class ConsoleLink:
    url: str
    signed_in: bool  # a one-time claim link (True) or the plain URL (False)
    note: Optional[str] = None  # why it is plain, when it is


def _user_auth_enabled(data_dir: Path) -> Optional[bool]:
    from ..first_run import read_serve_record

    rec = read_serve_record(data_dir) or {}
    auth = rec.get("auth") if isinstance(rec.get("auth"), dict) else None
    if auth is None:
        return None
    return bool(auth.get("user_auth_enabled"))


def _mint_default(data_dir: Path) -> Dict[str, Any]:
    from ..config_cli import ensure_bootstrap_admin_user
    from ..first_run import mint_claim

    # ensure_bootstrap_admin_user resolves the registry from the environment,
    # exactly like `abstractgateway claim`; point it at THIS gateway's data dir.
    os.environ.setdefault("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    admin = ensure_bootstrap_admin_user()
    user = admin.get("user") if isinstance(admin.get("user"), dict) else {}
    return mint_claim(
        data_dir=data_dir,
        tenant_id=str(user.get("tenant_id") or "default"),
        user_id=str(user.get("user_id") or "admin"),
        ttl_s=TRAY_CLAIM_TTL_S,
        created_by="tray",
    )


def console_link(
    base_url: str,
    console_path: str,
    data_dir: Optional[Path],
    *,
    tab: Optional[str] = None,
    mint: Optional[Callable[[Path], Dict[str, Any]]] = None,
    user_auth: Optional[Callable[[Path], Optional[bool]]] = None,
) -> ConsoleLink:
    """The URL "Open Console" should open. Never raises.

    `tab` is carried as `#claim=<code>&tab=<tab>` on a claim link (the console
    reads `claim=` up to the next `&`) and as `#<tab>` on a plain one."""
    base = str(base_url).rstrip("/")
    plain = base + console_path + (f"#{tab}" if tab else "")
    if data_dir is None:
        return ConsoleLink(plain, False, "this tray was started without a data folder, so it cannot mint a sign-in link")
    try:
        enabled = (user_auth or _user_auth_enabled)(Path(data_dir))
    except Exception as exc:  # noqa: BLE001
        enabled = None
        logger.debug("tray: serve record unreadable: %s", exc)
    if enabled is False:
        return ConsoleLink(plain, False, "this gateway uses a static token (no user accounts), so sign-in links are not available")
    try:
        minted = (mint or _mint_default)(Path(data_dir))
    except Exception as exc:  # noqa: BLE001
        return ConsoleLink(plain, False, f"could not create a one-time sign-in link ({type(exc).__name__}: {exc})")
    url = f"{base}{console_path}#claim={minted['code']}" + (f"&tab={tab}" if tab else "")
    return ConsoleLink(url, True, None)
