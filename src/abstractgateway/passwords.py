"""Account passwords: hashing, the strength rule, and the sign-in attempt limits.

A password is a second way into an account, next to its gateway token (which stays for
automation and the apps). It is stored on the account's registry record (`password_hash`,
users.py) and never anywhere else.

- **Hash**: `hashlib.scrypt` (stdlib; no argon2/bcrypt dependency exists in this package),
  N=2**16, r=8, p=1, a 16-byte salt and a 32-byte key, written
  `scrypt$<N>$<r>$<p>$<salt hex>$<key hex>` so the cost can rise later without breaking
  existing hashes. Verification is constant-time.
- **Strength rule** (`password_problem`): at least 10 characters, at least 5 different
  characters, not the account name, not a well-known password. Passphrases are welcome.
- **Attempt limits** (`SignInLimiter`): per client address, per (account, address) and per account, decaying, process-local
  (a restart clears them, like the token lockout in security/gateway_security.py). A locked
  account or address is refused with 429 and `retry_after_s` even when the password is right:
  otherwise the lock would not stop guessing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_MAXMEM = 256 * 1024 * 1024

MIN_LENGTH = 10
MIN_DISTINCT = 5
MAX_LENGTH = 256

PASSWORD_RULE = (
    f"At least {MIN_LENGTH} characters with at least {MIN_DISTINCT} different ones; not your account name "
    "and not a well-known password. A few words make a good one."
)

# The most common leaked passwords that pass the length rule (all compared lower-case).
_COMMON = frozenset(
    {
        "1234567890", "12345678910", "0123456789", "qwertyuiop", "1q2w3e4r5t", "password1", "password12",
        "password123", "password1234", "passw0rd123", "iloveyou12", "qwerty1234", "qwerty12345",
        "abc1234567", "abcd123456", "letmein123", "welcome123", "admin12345", "administrator",
        "adminadmin", "changeme123", "trustno1234", "football123", "baseball123", "superman123",
        "1qaz2wsx3edc", "zaq12wsxcde", "qazwsxedc123", "asdfghjkl1", "zxcvbnm123", "princess123",
        "sunshine123", "monkey12345", "dragon12345", "master12345", "starwars123", "whatever123",
        "abstractgateway", "abstractframework", "gatewayadmin", "mypassword", "mypassword1",
        "secretpassword", "passwordpassword", "correcthorsebatterystaple",
    }
)


class PasswordError(ValueError):
    """A password that is refused. `reason_code` is stable; `message` is one sentence."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def password_problem(password: str, *, account: str = "") -> Optional[str]:
    """None when `password` meets the strength rule, else the sentence that says why not."""
    pw = str(password or "")
    if len(pw) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(pw) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    if len(set(pw)) < MIN_DISTINCT:
        return f"Use at least {MIN_DISTINCT} different characters."
    low = pw.lower()
    acct = str(account or "").strip().lower()
    if acct and len(acct) >= 3 and acct in low and len(low) - len(acct) < MIN_LENGTH:
        return "Don't build the password around the account name."
    if low.replace(" ", "") in _COMMON:
        return "That password is too well known; choose another."
    return None


def require_strong(password: str, *, account: str = "") -> str:
    problem = password_problem(password, account=account)
    if problem:
        raise PasswordError("password_too_weak", problem)
    return str(password)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        str(password).encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=_MAXMEM, dklen=_DKLEN
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, key_hex = str(password_hash or "").split("$", 5)
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if n < 2**14 or n > 2**20 or r < 1 or r > 32 or p < 1 or p > 16:
            return False
        expected = bytes.fromhex(key_hex)
        actual = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=n,
            r=r,
            p=p,
            maxmem=_MAXMEM,
            dklen=len(expected) or _DKLEN,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


_DUMMY_HASH: Optional[str] = None
_DUMMY_LOCK = threading.Lock()


def burn_equivalent_time(password: str) -> None:
    """Spend one verification's cost when there is no hash to check (unknown account, or an
    account without a password), so the answer time does not tell accounts apart."""
    global _DUMMY_HASH
    with _DUMMY_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY_HASH)


class SignInLimiter:
    """Refused-attempt counters, checked before verifying. Every counter DECAYS: failures older
    than `window_s` stop counting, so nobody can keep a lock alive forever with one guess per
    window (review finding, 0.18.0).

    - address: `ip_failures` refusals from one address in `window_s` lock that address for `ip_lock_s`;
    - account from ONE address: from the `account_after`-th consecutive refusal (with no quiet
      `window_s` in between) that (account, address) pair is locked `account_base_s * 2**(n -
      account_after)` seconds, capped at `account_max_s`. Keyed on the pair, so a stranger guessing
      from elsewhere does not lock the owner out;
    - account from ALL addresses: `account_total` refusals in `window_s` lock the account for
      `ip_lock_s` (the backstop against guessing from many addresses; the account's token still
      signs in);
    - `global_failures` > 0: that many refusals in `window_s` across all addresses lock the door for
      everyone except callers that pass `exempt_global=True` (a direct local request).
    A success clears the address's and the pair's counters."""

    def __init__(
        self,
        *,
        ip_failures: int = 10,
        window_s: float = 900.0,
        ip_lock_s: float = 900.0,
        account_after: int = 5,
        account_base_s: float = 30.0,
        account_max_s: float = 900.0,
        account_total: int = 50,
        global_failures: int = 0,
        max_entries: int = 10_000,
    ) -> None:
        self.ip_failures = max(1, int(ip_failures))
        self.window_s = float(window_s)
        self.ip_lock_s = float(ip_lock_s)
        self.account_after = max(1, int(account_after))
        self.account_base_s = float(account_base_s)
        self.account_max_s = float(account_max_s)
        self.account_total = max(1, int(account_total))
        self.global_failures = max(0, int(global_failures))
        self._max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._ip: Dict[str, List[float]] = {}
        self._ip_locked: Dict[str, float] = {}
        self._pair: Dict[Tuple[str, str], Tuple[int, float, float]] = {}  # (acct, ip) -> (n, locked until, last)
        self._acct: Dict[str, List[float]] = {}
        self._acct_locked: Dict[str, float] = {}
        self._global: List[float] = []
        self._global_locked = 0.0

    def reset(self) -> None:
        with self._lock:
            for d in (self._ip, self._ip_locked, self._pair, self._acct, self._acct_locked):
                d.clear()
            self._global.clear()
            self._global_locked = 0.0

    def _prune(self) -> None:
        for d in (self._ip, self._ip_locked, self._pair, self._acct, self._acct_locked):
            if len(d) > self._max_entries:
                for k in list(d)[: self._max_entries // 10]:
                    d.pop(k, None)

    def check(self, *, ip: str, account: str = "", exempt_global: bool = False) -> Optional[Tuple[str, int]]:
        """(`"address"` | `"account"` | `"everyone"`, seconds to wait) when locked, else None."""
        now = time.time()
        with self._lock:
            if not exempt_global and self._global_locked > now:
                return ("everyone", int(self._global_locked - now) + 1)
            until = self._ip_locked.get(ip, 0.0)
            if until > now:
                return ("address", int(until - now) + 1)
            if account:
                n, p_until, _last = self._pair.get((account, ip), (0, 0.0, 0.0))
                if p_until > now:
                    return ("account", int(p_until - now) + 1)
                a_until = self._acct_locked.get(account, 0.0)
                if a_until > now:
                    return ("account", int(a_until - now) + 1)
        return None

    def failure(self, *, ip: str, account: str = "") -> None:
        now = time.time()
        with self._lock:
            self._prune()
            rows = [t for t in self._ip.get(ip, []) if now - t < self.window_s]
            rows.append(now)
            self._ip[ip] = rows
            if len(rows) >= self.ip_failures:
                self._ip_locked[ip] = now + self.ip_lock_s
                self._ip[ip] = []
            if account:
                n, _until, last = self._pair.get((account, ip), (0, 0.0, 0.0))
                if last and now - last >= self.window_s:
                    n = 0  # a quiet window resets the ratchet
                n += 1
                lock_until = 0.0
                if n >= self.account_after:
                    lock_until = now + min(self.account_max_s, self.account_base_s * (2 ** (n - self.account_after)))
                self._pair[(account, ip)] = (n, lock_until, now)
                arows = [t for t in self._acct.get(account, []) if now - t < self.window_s]
                arows.append(now)
                self._acct[account] = arows
                if len(arows) >= self.account_total:
                    self._acct_locked[account] = now + self.ip_lock_s
                    self._acct[account] = []
            if self.global_failures:
                self._global = [t for t in self._global if now - t < self.window_s]
                self._global.append(now)
                if len(self._global) >= self.global_failures:
                    self._global_locked = now + self.ip_lock_s
                    self._global = []

    def success(self, *, ip: str, account: str = "") -> None:
        with self._lock:
            self._ip.pop(ip, None)
            self._ip_locked.pop(ip, None)
            if account:
                self._pair.pop((account, ip), None)


#: Password sign-in (`POST /session/login` with a password) and password changes that check
#: the current password: 10 refusals per address per 15 minutes; 5 in a row for one account from
#: one address lock that pair 30 s, doubling to 15 minutes; 50 for one account from all addresses
#: lock the account 15 minutes.
PASSWORD_LIMITER = SignInLimiter()

#: The first-run onboarding form: 5 refused setup codes per address per 15 minutes, and 20
#: across every address lock the form for everyone for 15 minutes.
ONBOARDING_LIMITER = SignInLimiter(ip_failures=5, account_after=10**6, account_total=10**6, global_failures=20)


def limit_message(scope: str, retry_after_s: int) -> str:
    minutes = max(1, (int(retry_after_s) + 59) // 60)
    when = f"{minutes} minute" + ("s" if minutes != 1 else "")
    if scope == "account":
        return f"Too many wrong passwords for this account. Try again in {when}."
    if scope == "everyone":
        return f"Too many refused attempts on this gateway. Try again in {when}."
    return f"Too many refused attempts from this address. Try again in {when}."
