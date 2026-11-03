"""Persisted endpoint switches; safe to read before any optional HTTP imports."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

ACCESS_MODES = ("token", "open")
REACH_MODES = ("machine", "network", "tailnet", "anywhere")
GUEST_ACCOUNT = "guest"


@dataclass(frozen=True)
class EndpointSettings:
    enabled: bool = False
    access: str = "token"
    reach: str = "machine"
    token: str = field(default="", repr=False)
    open_account: str = GUEST_ACCOUNT
    # "API keys can be revealed by their owner" (round 16 keys fix): on, each named API key is
    # also kept encrypted so its owner can reveal it again; off, a key is shown once and the
    # gateway keeps only its hash (`abstractgateway.openai_keys`).
    owner_reveal: bool = True
    direct_enabled: bool = False


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "config" / "core_endpoint.json"


def read_settings(data_dir: Path) -> EndpointSettings:
    path = _settings_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EndpointSettings()
    if not isinstance(raw, dict):
        raise ValueError("Invalid Core endpoint settings; restore config/core_endpoint.json")
    # A 0.12.0 file has no `reach`: it served every client the listener reached.
    reach = raw.get("reach", "network")
    open_account = raw.get("open_account", GUEST_ACCOUNT)
    owner_reveal = raw.get("owner_reveal", True)
    if (not isinstance(open_account, str) or not open_account.strip()
            or type(owner_reveal) is not bool
            or type(raw.get("enabled")) is not bool
            or type(raw.get("direct_enabled", False)) is not bool
            or raw.get("access") not in ACCESS_MODES
            or reach not in REACH_MODES
            or not isinstance(raw.get("token"), str)
            or (raw["enabled"] and not raw["token"].strip())):
        raise ValueError("Invalid Core endpoint settings; restore config/core_endpoint.json")
    return EndpointSettings(enabled=raw["enabled"], access=raw["access"], reach=reach, token=raw["token"],
                            open_account=open_account.strip(), owner_reveal=owner_reveal,
                            direct_enabled=raw.get("direct_enabled", False))

