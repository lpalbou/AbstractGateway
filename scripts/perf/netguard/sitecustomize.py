"""Benchmark-gateway guard, loaded through PYTHONPATH by scripts/perf/bench_gateway.sh (never by a real gateway).

1. Network: every connect/connect_ex/sendto to a non-loopback address and every name lookup of a non-local
   name is REFUSED (OSError / gaierror) and written to $PERF_GUARD_LOG, so the fixture cannot reach a mail
   server, a feed, a push relay or a model provider.
2. Garbage collection: with $PERF_GC_LOG set, every collection taking >= 5 ms is appended to that file as one
   JSON line {t, gen, dt_s, collected}: a collection holds the GIL, so its duration is a whole-process pause.
"""

from __future__ import annotations

import gc
import json
import os
import socket
import threading
import time

_GUARD_LOG = os.environ.get("PERF_GUARD_LOG") or ""
_GC_LOG = os.environ.get("PERF_GC_LOG") or ""
_lock = threading.Lock()


def _append(path: str, rec: dict) -> None:
    if not path:
        return
    try:
        with _lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _local_host(host) -> bool:
    if host is None:
        return True
    h = host.decode() if isinstance(host, bytes) else str(host)
    h = h.strip("[]").lower()
    return h in ("localhost", "::1", "0.0.0.0", "::", "") or h.startswith("127.") or h.startswith("::ffff:127.")


def _refuse(api: str, target) -> None:
    _append(_GUARD_LOG, {"t": time.time(), "api": api, "target": repr(target)[:200]})
    raise OSError(111, f"perf netguard: outbound network refused ({api} {target!r})")


def _inet(sock, address) -> bool:
    return getattr(sock, "family", None) in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)


_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_sendto = socket.socket.sendto
_orig_getaddrinfo = socket.getaddrinfo


def _connect(self, address):
    if _inet(self, address) and not _local_host(address[0]):
        _refuse("connect", address)
    return _orig_connect(self, address)


def _connect_ex(self, address):
    if _inet(self, address) and not _local_host(address[0]):
        _refuse("connect_ex", address)
    return _orig_connect_ex(self, address)


def _sendto(self, data, *args):
    address = args[-1] if args else None
    if _inet(self, address) and not _local_host(address[0]):
        _refuse("sendto", address)
    return _orig_sendto(self, data, *args)


def _getaddrinfo(host, *args, **kwargs):
    if not _local_host(host):
        _append(_GUARD_LOG, {"t": time.time(), "api": "getaddrinfo", "target": repr(host)[:200]})
        raise socket.gaierror(socket.EAI_NONAME, f"perf netguard: name lookup refused ({host!r})")
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.socket.sendto = _sendto
socket.getaddrinfo = _getaddrinfo

if _GC_LOG:
    # The callback only appends to a list (no lock, no I/O: a collection can start inside any allocation,
    # including one made while a lock is held); a daemon thread writes the records out every second.
    _gc_start = [0.0]
    _gc_pending: list = []

    def _gc_cb(phase, info):
        if phase == "start":
            _gc_start[0] = time.perf_counter()
            return
        dt = time.perf_counter() - _gc_start[0]
        if dt >= 0.005:
            _gc_pending.append((time.time(), info.get("generation"), round(dt, 4), info.get("collected")))

    def _gc_flush():
        while True:
            time.sleep(1.0)
            if not _gc_pending:
                continue
            batch = _gc_pending[:]
            del _gc_pending[: len(batch)]
            try:
                with open(_GC_LOG, "a", encoding="utf-8") as fh:
                    for t, gen, dt, col in batch:
                        fh.write(json.dumps({"t": t, "gen": gen, "dt_s": dt, "collected": col}) + "\n")
            except Exception:
                pass

    gc.callbacks.append(_gc_cb)
    threading.Thread(target=_gc_flush, name="perf-gc-log", daemon=True).start()
