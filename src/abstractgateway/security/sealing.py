"""Where the gateway's sealed secrets get their key: ONE file per data folder, never a keychain.

Every secret the gateway keeps encrypted is sealed by AbstractCore's `SecretVault` (AES-256-GCM,
reached through AbstractRuntime's email facade) with the data folder's key (round 16, operator
ruling 2026-10-10: no OS keychain, ever; the same on macOS, Linux and Windows):

    <data_dir>/secrets/               0700
    <data_dir>/secrets/sealing.key    0600  base64 of 32 random bytes (os.urandom), made on first use

The stores: each mailbox's credentials (`<plane>/email/account/email/`), the bring-your-own
OAuth clients (`<data_dir>/email/oauth_clients/`), the MCP header values
(`<data_dir>/config/mcp_secrets/`) and the named API keys kept for owner reveal
(`<data_dir>/auth/openai_key_secrets/`). Each seal binds its store's place relative to the data
folder, so a sealed file copied into another store does not open, and a data folder moved or
restored elsewhere — with its `secrets/` — keeps working.

What the key file protects against, plainly: copies of the data folder taken WITHOUT
`secrets/sealing.key`. Code running as the gateway's OS user can read the key file — exactly as
it could read the keychain items of the same user before. Backups must include `secrets/`. On
Windows the 0600/0700 modes are best effort; the folder's NTFS permissions protect it there.

Stores sealed by gateway <= 0.13 with the key in the macOS keychain are NEVER opened (no keychain
call, no password prompt): the vault raises the caller's sentence, and boot marks them
(mail/accounts.py `migrate_keychain_sealed_mailboxes`, `mcp_registry.migrate_legacy_store`,
`openai_keys.migrate_legacy_store`). Nothing here imports `keyring`.
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path
from typing import Any, Optional

SECRETS_DIRNAME = "secrets"
KEY_FILENAME = "sealing.key"


def secrets_dir(data_dir: Path) -> Path:
    return Path(data_dir) / SECRETS_DIRNAME


def sealing_key_path(data_dir: Path) -> Path:
    return secrets_dir(data_dir) / KEY_FILENAME


def ensure_sealing_key(data_dir: Path) -> bytes:
    """The data folder's key, made on first use (`secrets/` 0700, `sealing.key` 0600)."""

    from ..mail.core_mail import ensure_key_file

    return ensure_key_file(sealing_key_path(data_dir))


def read_sealing_key(data_dir: Path) -> Optional[bytes]:
    """The data folder's key, or None when it was never made (nothing is created)."""

    from ..mail.core_mail import read_key_file

    return read_key_file(sealing_key_path(data_dir))


def key_id(key: bytes) -> str:
    """A non-secret fingerprint of the key (the `kid` of each sealed file)."""

    from ..mail.core_mail import key_fingerprint

    return key_fingerprint(key)


def sealed_vault(directory: Path, *, data_dir: Path, **legacy: Any):
    """AbstractCore's `SecretVault` for `directory`, sealed with the data folder's key.
    `legacy` = `legacy_cause` / `legacy_fix` / `legacy_code` (the sentence a store sealed with
    the old keychain key answers) and `reseal_legacy`."""

    from ..mail.core_mail import SecretVault

    return SecretVault(Path(directory), key_file=sealing_key_path(Path(data_dir)), **legacy)


def write_private(path: Path, data: bytes) -> None:
    """Atomic write of a 0600 file (O_EXCL temp file + replace). Not for secrets in clear."""

    tmp = path.with_name(path.name + f".{os.getpid()}-{_secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
