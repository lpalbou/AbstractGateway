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
from typing import Any, Dict, List, Optional, Tuple

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


def address_key(ip: str) -> str:
    """The unit an address limit counts: an IPv4 address, or an IPv6 /64 (one customer's block;
    counting single IPv6 addresses gave an attacker unlimited addresses, review finding)."""
    import ipaddress

    text = str(ip or "unknown").strip().strip("[]")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return text[:64]
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _is_loopback_key(addr: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_network(addr, strict=False).network_address.is_loopback
    except ValueError:
        return False


def _account_key(account: str) -> str:
    # Hashed and fixed-size: a flood of made-up account names costs 16 bytes per entry.
    return hashlib.sha256(str(account or "").encode("utf-8", errors="ignore")).hexdigest()[:24] if account else ""


class SignInLimiter:
    """Refused-attempt counters, checked before verifying. Every counter DECAYS: refusals older
    than `window_s` stop counting, so a lock never lasts forever.

    - address (IPv4, or IPv6 /64): `ip_failures` refusals in `window_s` lock it for `ip_lock_s`;
    - (account, address): from the `account_after`-th consecutive refusal (no quiet `window_s` in
      between) the pair is locked `account_base_s * 2**(n - account_after)` s, capped at
      `account_max_s`; a stranger guessing elsewhere does not lock the owner out this way;
    - account from ALL addresses: every `account_total` refusals in `window_s` lock the account,
      for `ip_lock_s` doubling at each further lock (capped at `account_total_max_s`); the level
      falls back after a quiet `2 * window_s`. An address that signed this account in within
      `trusted_s` is not held by this lock (so a distributed guesser cannot lock the owner out);
    - `global_failures` > 0: that many refusals in `window_s` across all addresses lock the door
      for everyone except callers that pass `exempt_global=True` (a direct local request).
    Memory stays bounded: expired entries go first, a locked entry is never dropped."""

    def __init__(
        self,
        *,
        ip_failures: int = 10,
        window_s: float = 900.0,
        ip_lock_s: float = 900.0,
        account_after: int = 5,
        account_base_s: float = 30.0,
        account_max_s: float = 900.0,
        account_total: int = 20,
        account_total_max_s: float = 86400.0,
        trusted_s: float = 30 * 86400.0,
        global_failures: int = 0,
        max_entries: int = 20_000,
    ) -> None:
        self.ip_failures = max(1, int(ip_failures))
        self.window_s = float(window_s)
        self.ip_lock_s = float(ip_lock_s)
        self.account_after = max(1, int(account_after))
        self.account_base_s = float(account_base_s)
        self.account_max_s = float(account_max_s)
        self.account_total = max(1, int(account_total))
        self.account_total_max_s = float(account_total_max_s)
        self.trusted_s = float(trusted_s)
        self.global_failures = max(0, int(global_failures))
        self._max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._ip: Dict[str, Tuple[List[float], float]] = {}  # addr -> (refusal times, locked until)
        self._pair: Dict[Tuple[str, str], Tuple[int, float, float]] = {}  # (acct, addr) -> (n, locked until, last)
        self._acct: Dict[str, Tuple[List[float], float, int, float]] = {}  # acct -> (times, locked until, level, last)
        self._trusted: Dict[Tuple[str, str], float] = {}  # (acct, addr) -> last success
        self._global: List[float] = []
        self._global_locked = 0.0

    def reset(self) -> None:
        with self._lock:
            for d in (self._ip, self._pair, self._acct, self._trusted):
                d.clear()
            self._global.clear()
            self._global_locked = 0.0

    def _prune(self, now: float) -> None:
        """Expired first; never a locked entry; then the oldest unlocked ones."""
        def shrink(d: dict, live) -> None:
            if len(d) <= self._max_entries:
                return
            for k in [k for k, v in d.items() if not live(v)]:
                d.pop(k, None)
            if len(d) > self._max_entries:
                for k in [k for k, v in d.items() if not self._locked(v, now)][: len(d) - self._max_entries]:
                    d.pop(k, None)

        w = self.window_s
        shrink(self._ip, lambda v: v[1] > now or any(now - t < w for t in v[0]))
        shrink(self._pair, lambda v: v[1] > now or now - v[2] < w)
        shrink(self._acct, lambda v: v[1] > now or now - v[3] < 2 * w)
        shrink(self._trusted, lambda v: now - v < self.trusted_s)

    @staticmethod
    def _locked(v: Any, now: float) -> bool:
        return isinstance(v, tuple) and len(v) > 1 and isinstance(v[1], float) and v[1] > now

    def check(self, *, ip: str, account: str = "", exempt_global: bool = False) -> Optional[Tuple[str, int]]:
        """(`"address"` | `"account"` | `"everyone"`, seconds to wait) when locked, else None."""
        now = time.time()
        addr, acct = address_key(ip), _account_key(account)
        with self._lock:
            if not exempt_global and self._global_locked > now:
                return ("everyone", int(self._global_locked - now) + 1)
            _rows, until = self._ip.get(addr, ([], 0.0))
            if until > now:
                return ("address", int(until - now) + 1)
            if acct:
                _n, p_until, _last = self._pair.get((acct, addr), (0, 0.0, 0.0))
                if p_until > now:
                    return ("account", int(p_until - now) + 1)
                _t, a_until, _lvl, _l = self._acct.get(acct, ([], 0.0, 0, 0.0))
                # Never for loopback: behind a proxy that adds no forwarding headers every caller is 127.0.0.1.
                trusted = not _is_loopback_key(addr) and now - self._trusted.get((acct, addr), -1e18) < self.trusted_s
                if a_until > now and not trusted:
                    return ("account", int(a_until - now) + 1)
        return None

    def failure(self, *, ip: str, account: str = "") -> None:
        now = time.time()
        addr, acct = address_key(ip), _account_key(account)
        with self._lock:
            self._prune(now)
            rows, until = self._ip.get(addr, ([], 0.0))
            rows = [t for t in rows if now - t < self.window_s] + [now]
            if len(rows) >= self.ip_failures:
                until, rows = now + self.ip_lock_s, []
            self._ip[addr] = (rows, until)
            if acct:
                n, _until, last = self._pair.get((acct, addr), (0, 0.0, 0.0))
                if last and now - last >= self.window_s:
                    n = 0  # a quiet window resets the ratchet
                n += 1
                lock_until = 0.0
                if n >= self.account_after:
                    lock_until = now + min(self.account_max_s, self.account_base_s * (2 ** (n - self.account_after)))
                self._pair[(acct, addr)] = (n, lock_until, now)
                times, a_until, level, a_last = self._acct.get(acct, ([], 0.0, 0, 0.0))
                if a_last and now - max(a_last, a_until) >= 2 * self.window_s:
                    level = 0  # a quiet stretch AFTER the last lock lowers the escalation back
                times = [t for t in times if now - t < self.window_s] + [now]
                if len(times) >= self.account_total:
                    a_until = now + min(self.account_total_max_s, self.ip_lock_s * (2 ** level))
                    level, times = level + 1, []
                self._acct[acct] = (times, a_until, level, now)
            if self.global_failures:
                self._global = [t for t in self._global if now - t < self.window_s] + [now]
                if len(self._global) >= self.global_failures:
                    self._global_locked = now + self.ip_lock_s
                    self._global = []

    def success(self, *, ip: str, account: str = "") -> None:
        now = time.time()
        addr, acct = address_key(ip), _account_key(account)
        with self._lock:
            self._ip.pop(addr, None)
            if acct:
                self._pair.pop((acct, addr), None)
                self._trusted[(acct, addr)] = now
                self._prune(now)


#: Password sign-in (`POST /session/login` with a password) and password changes that check
#: the current password: 10 refusals per address (IPv6: per /64) per 15 minutes; 5 in a row for
#: one account from one address lock that pair 30 s, doubling to 15 minutes; every 20 for one
#: account from all addresses lock the account 15 minutes, doubling to 24 hours (an address that
#: signed the account in within 30 days is not held).
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
