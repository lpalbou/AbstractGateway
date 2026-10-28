#!/usr/bin/env python
"""BEFORE/AFTER benchmark of the gateway replay view against a REAL server (framework 0993).

    cd abstractgateway
    PYTHONPATH=src:<abstractruntime>/src python scripts/bench_replay_view.py [--out PATH]

A gateway process per store backend (file JSONL, then SQLite) is launched by
`_bench_replay_server.py`: uvicorn serving `abstractgateway.app:app` on 127.0.0.1, a fresh data
dir, a seeded delegated run tree (the root waits on `subworkflow:<child>`; the child holds
~3,000 records: LLM calls with usage, tool calls with results, progress events, one `$slim`
pair) and ledger appends made IN THAT PROCESS on command, so they wake the streams through the
observable ledger store exactly as a runner's appends do. Server CPU is the process's CPU time
(psutil, every thread); bytes are what the clients received (decoded, and on the wire: httpx
asks for gzip like a browser does).

BEFORE (what clients do today: fetch the raw ledger, rebuild, follow the raw ledger stream):
  B1   one GET /ledger (child: all records; root) and one GET /history_bundle (default; full)
  Bc   50 clients join: GET /ledger root + child, then follow /ledger/stream for BOTH runs
  B2   the server appends N records at R/s while the 50 clients follow (100 streams)
  B3   the same clients idle; B3p idle while each also polls GET /runs/{root} every 2 s
       client-side: replay() over the raw records one client holds, x50
AFTER (the view):
  A1   GET /view cold (first build), warm (cached), 304 (If-None-Match)
  Ac   50 clients join: /view/stream (`view` frame)
  A2   the same N appends while 50 clients follow: CPU, bytes, append->delivery latency, and
       every client's folded view (`apply_delta` over `view` + `delta`) == a final GET /view
  A3   the 50 clients idle
B0 appends with no client (the cost of the appends alone); B2@1 / A2@1 one client, to see
whether the per-append cost grows with subscribers. In-process microbenchmarks (no server):
replay() of the tree, apply() of one record into the warm view, to_json(), apply_delta().

Prints a table and writes every number to --out (JSON). The gateway processes are killed in
`finally`; their temp data dirs are removed.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import copy
import hashlib
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import psutil

HERE = Path(__file__).resolve().parent
GATEWAY = HERE.parent
sys.path.insert(0, str(HERE))

import _bench_replay_server as tree  # noqa: E402

from abstractruntime.replay import apply_delta, replay, resolve_cursor_token  # noqa: E402

API = "/api/gateway"


# --------------------------------------------------------------------------- small helpers


def _stats(xs: List[float]) -> Dict[str, Optional[float]]:
    xs = sorted(float(x) for x in xs)
    if not xs:
        return {"n": 0, "median": None, "p95": None, "max": None}
    p95 = xs[min(len(xs) - 1, max(0, int(round(0.95 * len(xs) + 0.5)) - 1))]
    return {"n": len(xs), "median": statistics.median(xs), "p95": p95, "max": xs[-1]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _sh(cmd: List[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def machine_info() -> Dict[str, Any]:
    rt_dir = Path(__import__("abstractruntime").__file__).resolve().parent
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "cpu": _sh(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "ncpu": os.cpu_count(),
        "mem_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "gateway_commit": _sh(["git", "-C", str(GATEWAY), "rev-parse", "--short", "HEAD"]),
        "gateway_dirty": bool(_sh(["git", "-C", str(GATEWAY), "status", "--porcelain", "--untracked-files=no"])),
        "runtime_path": str(rt_dir),
        "runtime_commit": _sh(["git", "-C", str(rt_dir), "rev-parse", "--short", "HEAD"]),
        "httpx": httpx.__version__,
        "psutil": psutil.__version__,
    }


# --------------------------------------------------------------------------- the server process


class Server:
    """The gateway process under test (`_bench_replay_server.py`) and its control channel."""

    def __init__(self, backend: str, *, seed_records: int, gateway_src: Optional[str]) -> None:
        self.backend = backend
        self.dir = tempfile.mkdtemp(prefix=f"bench-replay-{backend}-")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        if gateway_src:
            env["PYTHONPATH"] = os.pathsep.join([gateway_src] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep)
                                                                 if p and Path(p).resolve() != (GATEWAY / "src").resolve()])
        self.log_path = os.path.join(self.dir, "server.log")
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "_bench_replay_server.py"), "--data-dir", self.dir, "--backend", backend,
             "--port", str(self.port), "--seed-records", str(seed_records)],
            cwd=str(GATEWAY), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log, text=True)
        self._lock = threading.Lock()
        self.info = self._read()
        if not self.info.get("ready"):
            raise RuntimeError(f"gateway did not start: {self.info}; log: {self.log_path}")
        self.ps = psutil.Process(self.proc.pid)

    def _read(self) -> Dict[str, Any]:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if line.startswith("@@BENCH "):
                return json.loads(line[len("@@BENCH "):])
        raise RuntimeError(f"gateway exited (code {self.proc.poll()}); log: {self.log_path}\n" + self.tail_log())

    def call(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
            return self._read()

    def cpu(self) -> float:
        t = self.ps.cpu_times()
        return float(t.user + t.system)

    def tail_log(self, n: int = 3000) -> str:
        try:
            return Path(self.log_path).read_text(errors="replace")[-n:]
        except OSError:
            return ""

    def stop(self) -> None:
        try:
            if self.proc.poll() is None:
                try:
                    self.call({"cmd": "quit"})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
        finally:
            self._log.close()
            shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------------- SSE followers


class Follower:
    """One SSE stream: receipt time of every frame, the child seq it reached, bytes received.

    `kind="ledger"`: ids are record cursors (the child's cursor == its seq, dense from 1).
    `kind="view"`: ids are cursor tokens; `view`/`delta` frames are kept for the fold."""

    def __init__(self, kind: str, child_run_id: str, run_ids: Tuple[str, ...] = ()) -> None:
        self.kind = kind
        self.child_run_id = child_run_id
        self.run_ids = tuple(run_ids)
        self.status: Optional[int] = None
        self.error: Optional[str] = None
        self.ready = asyncio.Event()
        self.resp: Optional[httpx.Response] = None
        self.task: Optional[asyncio.Task] = None
        self.bytes = 0
        self._buf = b""
        self.seq_t: List[float] = []  # times the child seq advanced
        self.seq_v: List[int] = []  # ... and to what
        self.max_seq = 0
        self.frames: List[Tuple[float, bytes]] = []  # view kind: (t, frame) for view/delta
        self.last_id: Optional[str] = None

    @property
    def wire(self) -> int:
        return int(self.resp.num_bytes_downloaded) if self.resp is not None else 0

    def _seq_of(self, ident: str) -> Optional[int]:
        if self.kind == "ledger":
            try:
                return int(ident)
            except ValueError:
                return None
        m = resolve_cursor_token(ident, self.run_ids) or {}
        return m.get(self.child_run_id)

    def feed(self, t: float, chunk: bytes) -> None:
        self.bytes += len(chunk)
        self._buf += chunk
        while True:
            i = self._buf.find(b"\n\n")
            if i < 0:
                return
            frame, self._buf = self._buf[: i + 2], self._buf[i + 2:]
            if not frame.startswith(b"id: "):
                continue  # keep-alive comments, live token deltas, `done`
            nl = frame.find(b"\n")
            ident = frame[4:nl].decode("utf-8", "replace")
            self.last_id = ident
            if self.kind == "view":
                self.frames.append((t, frame))
            s = self._seq_of(ident)
            if s is not None and s > self.max_seq:
                self.max_seq = s
                self.seq_t.append(t)
                self.seq_v.append(s)

    async def run(self, client: httpx.AsyncClient, url: str, headers: Dict[str, str]) -> None:
        try:
            async with client.stream("GET", url, headers=headers) as r:
                self.resp = r
                self.status = r.status_code
                self.ready.set()
                if r.status_code != 200:
                    self.error = (await r.aread()).decode("utf-8", "replace")[:300]
                    return
                async for chunk in r.aiter_bytes():
                    self.feed(time.time(), chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.error = repr(e)
        finally:
            self.ready.set()

    def start(self, client: httpx.AsyncClient, url: str, headers: Dict[str, str]) -> None:
        self.task = asyncio.ensure_future(self.run(client, url, headers))

    def delivered_at(self, seq: int) -> Optional[float]:
        i = bisect.bisect_left(self.seq_v, seq)
        return self.seq_t[i] if i < len(self.seq_v) else None


async def _close(followers: List[Follower]) -> None:
    for f in followers:
        if f.task is not None:
            f.task.cancel()
    await asyncio.gather(*(f.task for f in followers if f.task is not None), return_exceptions=True)


async def _wait_until(pred: Callable[[], bool], timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def _first_difference(folded: Dict[str, Any], final: Dict[str, Any], keys: List[str]) -> str:
    """The first item and fields where a client's folded view differs from GET /view."""
    for k in keys:
        a, b = folded.get(k), final.get(k)
        if isinstance(a, list) and isinstance(b, list):
            idk = "run_id" if k == "runs" else "id"
            ia = {str(x.get(idk)): x for x in a if isinstance(x, dict)}
            ib = {str(x.get(idk)): x for x in b if isinstance(x, dict)}
            if set(ia) != set(ib):
                return f"{k}: ids only in fold {sorted(set(ia) - set(ib))[:3]}, only in GET {sorted(set(ib) - set(ia))[:3]}"
            for i in ib:
                if ia[i] != ib[i]:
                    fields = {f: (ia[i].get(f), ib[i].get(f)) for f in set(ia[i]) | set(ib[i]) if ia[i].get(f) != ib[i].get(f)}
                    return f"{k}[{i}] fold vs GET: {json.dumps(fields, default=str)[:600]}"
            return f"{k}: same items, different order"
        return f"{k}: fold {json.dumps(a, default=str)[:300]} vs GET {json.dumps(b, default=str)[:300]}"
    return ""


def _latency(appended: List[List[float]], followers: List[Follower]) -> Dict[str, Any]:
    """Append (time before `append()` was called) -> the time the LAST client had a frame whose
    child seq covers it; and the same pooled over every (append, client)."""
    last_client, every_client, missing = [], [], 0
    for seq, t_before, _t_after in appended:
        ts = [f.delivered_at(int(seq)) for f in followers]
        if any(t is None for t in ts):
            missing += sum(1 for t in ts if t is None)
            continue
        every_client.extend(t - t_before for t in ts)
        last_client.append(max(ts) - t_before)
    return {"last_client_ms": {k: (v * 1000 if isinstance(v, float) else v) for k, v in _stats(last_client).items()},
            "per_client_ms": {k: (v * 1000 if isinstance(v, float) else v) for k, v in _stats(every_client).items()},
            "undelivered": missing}


# --------------------------------------------------------------------------- phases


async def _timed_gets(server: Server, client: httpx.AsyncClient, url: str, n: int, *, params=None, headers=None,
                      before: Optional[Callable[[], None]] = None, expect: int = 200) -> Dict[str, Any]:
    lat, cpu, sizes, wire = [], [], [], []
    for _ in range(n):
        if before is not None:
            await asyncio.to_thread(before)
        c0, t0 = server.cpu(), time.perf_counter()
        r = await client.get(url, params=params, headers=headers)
        body = r.content
        lat.append(time.perf_counter() - t0)
        cpu.append(server.cpu() - c0)
        if r.status_code != expect:
            raise RuntimeError(f"GET {url} -> {r.status_code} (expected {expect}): {body[:300]!r}")
        sizes.append(len(body))
        wire.append(int(r.num_bytes_downloaded))
    return {"latency_ms": {k: (v * 1000 if isinstance(v, float) else v) for k, v in _stats(lat).items()},
            "server_cpu_ms_per_req": statistics.median(cpu) * 1000, "server_cpu_ms_total": sum(cpu) * 1000,
            "bytes": sizes[-1], "wire_bytes": wire[-1]}


async def _idle(server: Server, seconds: float, followers: List[Follower]) -> Dict[str, Any]:
    b0 = sum(f.bytes for f in followers)
    c0 = server.cpu()
    await asyncio.sleep(seconds)
    return {"seconds": seconds, "server_cpu_s": server.cpu() - c0, "bytes": sum(f.bytes for f in followers) - b0}


async def _append_phase(server: Server, followers: List[Follower], n: int, rate: float, *, settle_timeout: float = 15.0,
                        hub: bool = False) -> Dict[str, Any]:
    """Append n records at `rate`/s in the server; wait until every follower has the last one."""
    hs0 = await asyncio.to_thread(server.call, {"cmd": "hub_stats"}) if hub else None
    b0, w0 = sum(f.bytes for f in followers), sum(f.wire for f in followers)
    c0, t0 = server.cpu(), time.perf_counter()
    res = await asyncio.to_thread(server.call, {"cmd": "append", "n": n, "interval_s": 1.0 / rate})
    appended = res["appended"]
    t_appended = time.perf_counter()
    last = int(appended[-1][0])
    ok = await _wait_until(lambda: all(f.max_seq >= last for f in followers), settle_timeout) if followers else True
    out: Dict[str, Any] = {
        "appends": n, "rate_per_s": rate, "clients_streams": len(followers),
        "server_cpu_s": server.cpu() - c0, "wall_s": time.perf_counter() - t0,
        "drain_after_last_append_s": time.perf_counter() - t_appended,
        "bytes": sum(f.bytes for f in followers) - b0, "wire_bytes": sum(f.wire for f in followers) - w0,
        "all_delivered": ok, "last_seq": last,
    }
    if followers:
        out["latency"] = _latency(appended, followers)
        out["frames_per_stream"] = statistics.median([sum(1 for v in f.seq_v if v > appended[0][0] - 1) for f in followers])
    if hub:
        hs1 = await asyncio.to_thread(server.call, {"cmd": "hub_stats"})
        out["hub"] = {"folds": (hs1["folds"] or 0) - (hs0["folds"] or 0),
                      "records_folded": (hs1["records_folded"] or 0) - (hs0["records_folded"] or 0),
                      "subscribers": hs1["subscribers"]}
    return out


async def run_backend(backend: str, args: argparse.Namespace, H: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"backend": backend, "anomalies": []}
    server = Server(backend, seed_records=args.seed_records, gateway_src=args.gateway_src)
    try:
        H = {"Authorization": f"Bearer {server.info['token']}"}
        root, child = server.info["root"], server.info["child"]
        tree_ids = (root, child)
        out["server"] = {k: server.info[k] for k in ("seed_s", "ledger_store", "store_bytes", "child_records")}
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
        timeout = httpx.Timeout(60.0, read=None)
        async with httpx.AsyncClient(base_url=server.base, limits=limits, timeout=timeout) as client:
            r = await client.get(f"{API}/runs/{root}", headers=H)
            r.raise_for_status()
            await asyncio.sleep(1.0)  # let boot settle before measuring
            n_clients = args.clients

            # ---------------------------------------------------------------- B1 / A1
            if not args.skip_before:
                b1: Dict[str, Any] = {}
                b1["ledger_child"] = await _timed_gets(server, client, f"{API}/runs/{child}/ledger", args.n_slow,
                                                       params={"limit": 1_000_000}, headers=H)
                b1["ledger_root"] = await _timed_gets(server, client, f"{API}/runs/{root}/ledger", args.n_slow,
                                                      params={"limit": 1_000_000}, headers=H)
                b1["history_bundle"] = await _timed_gets(server, client, f"{API}/runs/{root}/history_bundle", args.n_slow, headers=H)
                b1["history_bundle_full"] = await _timed_gets(server, client, f"{API}/runs/{root}/history_bundle", args.n_slow,
                                                              params={"ledger_mode": "full"}, headers=H)
                out["B1"] = b1
                # Client-side reconstruction: what each client repeats over the raw records it holds.
                recs = {rid: (await client.get(f"{API}/runs/{rid}/ledger", params={"limit": 1_000_000}, headers=H)).json()["items"]
                        for rid in (root, child)}
                snaps = dict(zip((root, child), tree.snapshots()))
                times = []
                for _ in range(5):
                    t0 = time.perf_counter()
                    replay(recs, snaps, root_run_id=root).to_json()
                    times.append(time.perf_counter() - t0)
                out["client_rebuild"] = {"records": sum(len(v) for v in recs.values()),
                                         "replay_to_json_ms": statistics.median(times) * 1000,
                                         "x_clients_s": statistics.median(times) * n_clients}

            view_url = f"{API}/runs/{root}/view"
            a1: Dict[str, Any] = {}
            a1["cold"] = await _timed_gets(server, client, view_url, args.n_slow, headers=H,
                                           before=lambda: server.call({"cmd": "clear_hub"}))
            a1["warm"] = await _timed_gets(server, client, view_url, args.n_fast, headers=H)
            etag = (await client.get(view_url, headers=H)).headers["etag"]
            a1["not_modified"] = await _timed_gets(server, client, view_url, args.n_fast, headers={**H, "If-None-Match": etag},
                                                   expect=304)
            out["A1"] = a1

            # ---------------------------------------------------------------- idle baseline, append baseline
            out["I0"] = await _idle(server, args.idle_short, [])
            out["B0"] = await _append_phase(server, [], args.appends, args.rate)

            # ---------------------------------------------------------------- BEFORE streams
            if not args.skip_before:
                async def ledger_client() -> Tuple[List[Follower], int, int]:
                    fs, nbytes, wire = [], 0, 0
                    for rid in (root, child):
                        rr = await client.get(f"{API}/runs/{rid}/ledger", params={"limit": 1_000_000}, headers=H)
                        nbytes += len(rr.content)
                        wire += int(rr.num_bytes_downloaded)
                        nxt = rr.json()["next_after"]
                        f = Follower("ledger", child if rid == child else "")
                        if rid == child:
                            f.max_seq = int(nxt)
                        f.start(client, f"{API}/runs/{rid}/ledger/stream?after={nxt}&heartbeat_s=5", H)
                        fs.append(f)
                    return fs, nbytes, wire

                one, _nb, _w = await ledger_client()
                await asyncio.gather(*(f.ready.wait() for f in one))
                await asyncio.sleep(0.5)
                out["B2@1"] = await _append_phase(server, [one[1]], args.appends_small, args.rate)
                await _close(one)
                await asyncio.sleep(0.5)

                c0, t0 = server.cpu(), time.perf_counter()
                joined = await asyncio.gather(*(ledger_client() for _ in range(n_clients)))
                t_joined = time.perf_counter() - t0  # every client holds its records; streams requested
                fol = [f for fs, _b, _w in joined for f in fs]
                # Response headers of an SSE stream reach the client with its first frame (the
                # gzip middleware holds the start message): an idle ledger stream answers at its
                # first heartbeat, so `ready` can take up to heartbeat_s. Not part of the join cost.
                await asyncio.gather(*(f.ready.wait() for f in fol))
                bad = [f for f in fol if f.status != 200]
                if bad:
                    out["anomalies"].append(f"B: {len(bad)} ledger streams refused: {bad[0].status} {bad[0].error}")
                out["Bc"] = {"clients": n_clients, "streams": len(fol), "server_cpu_s": server.cpu() - c0,
                             "wall_s": t_joined, "bytes": sum(b for _f, b, _w in joined),
                             "wire_bytes": sum(w for _f, _b, w in joined)}
                await asyncio.sleep(0.5)
                child_streams = [f for f in fol if f.child_run_id]
                b2 = await _append_phase(server, child_streams, args.appends, args.rate)
                b2["bytes_all_streams"] = b2["bytes"]  # the root streams carry nothing (no root appends)
                out["B2"] = b2
                if not b2["all_delivered"]:
                    out["anomalies"].append("B2: a ledger stream missed records")
                out["B3"] = await _idle(server, args.idle, fol)

                async def poller(i: int, stop: asyncio.Event, lat: List[float]) -> None:
                    await asyncio.sleep(2.0 * i / max(1, n_clients))
                    while not stop.is_set():
                        t0p = time.perf_counter()
                        await client.get(f"{API}/runs/{root}", headers=H)
                        lat.append(time.perf_counter() - t0p)
                        try:
                            await asyncio.wait_for(stop.wait(), 2.0)
                        except asyncio.TimeoutError:
                            pass

                stop, plat = asyncio.Event(), []
                pollers = [asyncio.ensure_future(poller(i, stop, plat)) for i in range(n_clients)]
                b3p = await _idle(server, args.idle, fol)
                stop.set()
                await asyncio.gather(*pollers)
                b3p["polls"] = len(plat)
                out["B3p"] = b3p
                await _close(fol)
                await asyncio.sleep(1.0)

            # ---------------------------------------------------------------- AFTER streams
            vurl = f"{API}/runs/{root}/view/stream?heartbeat_s=5"
            f1 = Follower("view", child, tree_ids)
            f1.start(client, vurl, H)
            await f1.ready.wait()
            await _wait_until(lambda: f1.frames != [], 10)
            await asyncio.sleep(0.5)
            out["A2@1"] = await _append_phase(server, [f1], args.appends_small, args.rate, hub=True)
            await _close([f1])
            await asyncio.sleep(0.5)

            c0, t0 = server.cpu(), time.perf_counter()
            vf = [Follower("view", child, tree_ids) for _ in range(n_clients)]
            for f in vf:
                f.start(client, vurl, H)
            await asyncio.gather(*(f.ready.wait() for f in vf))
            got_view = await _wait_until(lambda: all(f.frames for f in vf), 30)
            out["Ac"] = {"clients": n_clients, "streams": len(vf), "server_cpu_s": server.cpu() - c0,
                         "wall_s": time.perf_counter() - t0, "bytes": sum(f.bytes for f in vf),
                         "wire_bytes": sum(f.wire for f in vf), "all_got_view": got_view}
            bad = [f for f in vf if f.status != 200]
            if bad:
                out["anomalies"].append(f"A: {len(bad)} view streams refused: {bad[0].status} {bad[0].error}")
            await asyncio.sleep(0.5)
            a2 = await _append_phase(server, vf, args.appends, args.rate, hub=True)
            out["A2"] = a2
            if not a2["all_delivered"]:
                out["anomalies"].append("A2: a view stream did not reach the last appended seq")
            if a2["hub"]["records_folded"] != args.appends:
                out["anomalies"].append(f"A2: {a2['hub']['records_folded']} records folded for {args.appends} appends "
                                        "(expected one fold per record, whatever the number of clients)")
            out["A3"] = await _idle(server, args.idle, vf)

            # Convergence: every client's fold == a final GET /view.
            final_r = await client.get(view_url, headers=H)
            final = final_r.json()
            # A GET folds what is new and broadcasts it: let such a frame land before comparing.
            await _wait_until(lambda: all(f.last_id == final["cursor"]["token"] for f in vf), 2.0)
            await asyncio.sleep(0.2)
            groups: Dict[str, List[Follower]] = {}
            for f in vf:
                h = hashlib.sha256(b"".join(fr for _t, fr in f.frames)).hexdigest()
                groups.setdefault(h, []).append(f)
            conv: Dict[str, Any] = {"distinct_frame_sequences": len(groups), "clients": len(vf), "converged": 0,
                                    "final_token": final["cursor"]["token"]}
            apply_ms: List[float] = []
            parse_ms: List[float] = []
            for frames_of in groups.values():
                view: Dict[str, Any] = {}
                for _t, fr in frames_of[0].frames:
                    head, _, data = fr.partition(b"\ndata: ")
                    tp = time.perf_counter()
                    payload = json.loads(data)
                    parse_ms.append((time.perf_counter() - tp) * 1000)
                    if b"event: view" in head:
                        view = payload
                    elif b"event: delta" in head:
                        tp = time.perf_counter()
                        view = apply_delta(view, payload)
                        apply_ms.append((time.perf_counter() - tp) * 1000)
                if view == final:
                    conv["converged"] += len(frames_of)
                else:
                    diff = sorted(k for k in set(view) | set(final) if view.get(k) != final.get(k))
                    out["anomalies"].append(f"A2: {len(frames_of)} client(s) folded a view != GET /view (keys {diff}): "
                                            + _first_difference(view, final, diff))
            conv["last_ids_equal_final"] = sum(1 for f in vf if f.last_id == final["cursor"]["token"])
            conv["deltas_per_client"] = len(apply_ms) // max(1, len(groups))
            conv["client_apply_delta_ms"] = _stats(apply_ms)
            conv["client_apply_delta_total_s_x_clients"] = sum(apply_ms) / max(1, len(groups)) / 1000 * n_clients
            conv["client_json_parse_ms"] = _stats(parse_ms)
            out["convergence"] = conv
            if conv["converged"] != len(vf):
                out["anomalies"].append(f"A2: only {conv['converged']}/{len(vf)} clients converged on GET /view")
            await _close(vf)
            await asyncio.sleep(0.5)
        if server.proc.poll() is not None:
            out["anomalies"].append(f"server exited early: {server.tail_log()}")
        return out
    finally:
        server.stop()


# --------------------------------------------------------------------------- in-process microbenchmarks


def microbench(seed_records: int, appends: int) -> Dict[str, Any]:
    root_s, child_s = tree.snapshots()
    seeded = tree.root_records() + list(tree.child_records(0, seed_records))
    recs = tree.as_dicts(seeded)
    by_run: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        by_run.setdefault(r["run_id"], []).append(r)
    snaps = {tree.ROOT: root_s, tree.CHILD: child_s}
    first_block = -(-seed_records // tree.RECORDS_PER_BLOCK)
    more = tree.as_dicts(seeded + list(tree.child_records(first_block, appends)))[len(seeded):]

    t_replay = []
    for _ in range(5):
        t0 = time.perf_counter()
        v = replay(by_run, snaps, root_run_id=tree.ROOT)
        t_replay.append(time.perf_counter() - t0)
    t_json, t_dumps = [], []
    for _ in range(5):
        t0 = time.perf_counter()
        j = v.to_json()
        t_json.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        body = json.dumps(j, ensure_ascii=False, separators=(",", ":"))
        t_dumps.append(time.perf_counter() - t0)
    t_apply, deltas = [], []
    for rec in more:
        t0 = time.perf_counter()
        d = v.apply([rec])
        t_apply.append(time.perf_counter() - t0)
        deltas.append(d)
    t_ad = []
    base = v.to_json()
    for d in deltas[:50]:
        t0 = time.perf_counter()
        apply_delta(base, d)
        t_ad.append(time.perf_counter() - t0)
    t_copy = []
    for _ in range(5):
        t0 = time.perf_counter()
        copy.deepcopy(base)
        t_copy.append(time.perf_counter() - t0)
    ms = lambda xs: {k: (x * 1000 if isinstance(x, float) else x) for k, x in _stats(xs).items()}  # noqa: E731
    return {
        "records": len(recs), "view_bytes": len(body.encode("utf-8")), "activities": len(j["activities"]),
        "replay_ms": ms(t_replay), "to_json_ms": ms(t_json), "json_dumps_ms": ms(t_dumps),
        "apply_one_record_ms": ms(t_apply),
        "delta_bytes_median": statistics.median(len(json.dumps(d, separators=(",", ":"))) for d in deltas),
        "delta_key_bytes_median": {k: statistics.median(len(json.dumps(d.get(k), separators=(",", ":"))) for d in deltas)
                                   for k in sorted({k for d in deltas for k in d})},
        "record_bytes_median": statistics.median(len(json.dumps(r, separators=(",", ":"))) for r in more),
        "apply_delta_ms": ms(t_ad), "deepcopy_view_ms": ms(t_copy),
    }


# --------------------------------------------------------------------------- report


def _f(x: Any, nd: int = 1) -> str:
    if x is None:
        return "-"
    if isinstance(x, bool):
        return "yes" if x else "NO"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _kb(x: Any) -> str:
    return "-" if x is None else f"{x / 1024:.0f} KB" if x < 10 * 2**20 else f"{x / 2**20:.1f} MB"


def table(results: Dict[str, Any]) -> str:
    bks = [b for b in results["backends"]]
    R = results["backends"]

    def g(b: str, *path: str) -> Any:
        cur: Any = R[b]
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return cur

    def per_append(b: str, phase: str) -> Optional[float]:
        ph, base = g(b, phase), g(b, "B0")
        if not ph or not base:
            return None
        return (ph["server_cpu_s"] - base["server_cpu_s"] * ph["appends"] / base["appends"]) / ph["appends"] * 1000

    rows: List[Tuple[str, Callable[[str], str]]] = [
        ("child records seeded / store bytes", lambda b: f"{g(b, 'server', 'child_records')} / {_kb(g(b, 'server', 'store_bytes'))}"),
        ("BEFORE", lambda b: ""),
        ("B1 GET /ledger child: median ms | server CPU ms | body | wire",
         lambda b: f"{_f(g(b, 'B1', 'ledger_child', 'latency_ms', 'median'))} | {_f(g(b, 'B1', 'ledger_child', 'server_cpu_ms_per_req'))} | {_kb(g(b, 'B1', 'ledger_child', 'bytes'))} | {_kb(g(b, 'B1', 'ledger_child', 'wire_bytes'))}"),
        ("B1 GET /ledger root: median ms | server CPU ms",
         lambda b: f"{_f(g(b, 'B1', 'ledger_root', 'latency_ms', 'median'))} | {_f(g(b, 'B1', 'ledger_root', 'server_cpu_ms_per_req'))}"),
        ("B1 GET /history_bundle (tail 2000): median ms | CPU ms | body",
         lambda b: f"{_f(g(b, 'B1', 'history_bundle', 'latency_ms', 'median'))} | {_f(g(b, 'B1', 'history_bundle', 'server_cpu_ms_per_req'))} | {_kb(g(b, 'B1', 'history_bundle', 'bytes'))}"),
        ("B1 GET /history_bundle full: median ms | CPU ms | body",
         lambda b: f"{_f(g(b, 'B1', 'history_bundle_full', 'latency_ms', 'median'))} | {_f(g(b, 'B1', 'history_bundle_full', 'server_cpu_ms_per_req'))} | {_kb(g(b, 'B1', 'history_bundle_full', 'bytes'))}"),
        ("client rebuild: replay()+to_json() ms per client | x clients s",
         lambda b: f"{_f(g(b, 'client_rebuild', 'replay_to_json_ms'))} | {_f(g(b, 'client_rebuild', 'x_clients_s'), 2)}"),
        ("Bc clients join (2 GET /ledger + 2 streams each): server CPU s | wall s | wire",
         lambda b: f"{_f(g(b, 'Bc', 'server_cpu_s'), 2)} | {_f(g(b, 'Bc', 'wall_s'), 2)} | {_kb(g(b, 'Bc', 'wire_bytes'))}"),
        ("B2@1 server CPU ms per append above B0 (1 client)", lambda b: _f(per_append(b, "B2@1"), 2)),
        ("B2 appends, 50 clients/100 streams: server CPU s (B0 alone)",
         lambda b: f"{_f(g(b, 'B2', 'server_cpu_s'), 2)} ({_f(g(b, 'B0', 'server_cpu_s'), 2)})"),
        ("B2 server CPU ms per append above B0", lambda b: _f(per_append(b, "B2"), 2)),
        ("B2 bytes to clients | wire", lambda b: f"{_kb(g(b, 'B2', 'bytes'))} | {_kb(g(b, 'B2', 'wire_bytes'))}"),
        ("B2 append->50th client ms: median | p95",
         lambda b: f"{_f(g(b, 'B2', 'latency', 'last_client_ms', 'median'))} | {_f(g(b, 'B2', 'latency', 'last_client_ms', 'p95'))}"),
        ("B3 idle 10 s, streams open: server CPU s", lambda b: _f(g(b, "B3", "server_cpu_s"), 3)),
        ("B3p idle 10 s + GET /runs every 2 s: server CPU s", lambda b: _f(g(b, "B3p", "server_cpu_s"), 3)),
        ("AFTER", lambda b: ""),
        ("A1 GET /view cold: median ms | p95 | server CPU ms | body | wire",
         lambda b: f"{_f(g(b, 'A1', 'cold', 'latency_ms', 'median'))} | {_f(g(b, 'A1', 'cold', 'latency_ms', 'p95'))} | {_f(g(b, 'A1', 'cold', 'server_cpu_ms_per_req'))} | {_kb(g(b, 'A1', 'cold', 'bytes'))} | {_kb(g(b, 'A1', 'cold', 'wire_bytes'))}"),
        ("A1 GET /view warm: median ms | p95 | server CPU ms",
         lambda b: f"{_f(g(b, 'A1', 'warm', 'latency_ms', 'median'), 2)} | {_f(g(b, 'A1', 'warm', 'latency_ms', 'p95'), 2)} | {_f(g(b, 'A1', 'warm', 'server_cpu_ms_per_req'), 2)}"),
        ("A1 GET /view 304: median ms | p95 | server CPU ms",
         lambda b: f"{_f(g(b, 'A1', 'not_modified', 'latency_ms', 'median'), 2)} | {_f(g(b, 'A1', 'not_modified', 'latency_ms', 'p95'), 2)} | {_f(g(b, 'A1', 'not_modified', 'server_cpu_ms_per_req'), 2)}"),
        ("Ac clients join (/view/stream): server CPU s | wall s | wire",
         lambda b: f"{_f(g(b, 'Ac', 'server_cpu_s'), 2)} | {_f(g(b, 'Ac', 'wall_s'), 2)} | {_kb(g(b, 'Ac', 'wire_bytes'))}"),
        ("A2@1 server CPU ms per append above B0 (1 client)", lambda b: _f(per_append(b, "A2@1"), 2)),
        ("A2 appends, 50 clients: server CPU s (B0 alone)",
         lambda b: f"{_f(g(b, 'A2', 'server_cpu_s'), 2)} ({_f(g(b, 'B0', 'server_cpu_s'), 2)})"),
        ("A2 server CPU ms per append above B0", lambda b: _f(per_append(b, "A2"), 2)),
        ("A2 records folded | folds (for N appends, 50 clients)",
         lambda b: f"{g(b, 'A2', 'hub', 'records_folded')} | {g(b, 'A2', 'hub', 'folds')}"),
        ("A2 bytes to clients | wire", lambda b: f"{_kb(g(b, 'A2', 'bytes'))} | {_kb(g(b, 'A2', 'wire_bytes'))}"),
        ("A2 append->50th client ms: median | p95",
         lambda b: f"{_f(g(b, 'A2', 'latency', 'last_client_ms', 'median'))} | {_f(g(b, 'A2', 'latency', 'last_client_ms', 'p95'))}"),
        ("A2 converged clients (fold == GET /view)",
         lambda b: f"{g(b, 'convergence', 'converged')}/{g(b, 'convergence', 'clients')}"),
        ("A2 client apply_delta ms: median | p95 (x clients total s)",
         lambda b: f"{_f(g(b, 'convergence', 'client_apply_delta_ms', 'median'), 2)} | {_f(g(b, 'convergence', 'client_apply_delta_ms', 'p95'), 2)} ({_f(g(b, 'convergence', 'client_apply_delta_total_s_x_clients'), 1)})"),
        ("A3 idle 10 s, streams open: server CPU s", lambda b: _f(g(b, "A3", "server_cpu_s"), 3)),
        ("I0 idle 5 s, no client: server CPU s", lambda b: _f(g(b, "I0", "server_cpu_s"), 3)),
    ]
    w0 = max(len(r[0]) for r in rows)
    lines = [f"{'metric'.ljust(w0)}  " + "  ".join(b.ljust(34) for b in bks)]
    lines.append("-" * len(lines[0]))
    for label, fn in rows:
        if fn(bks[0]) == "":
            lines.append(f"-- {label}")
            continue
        lines.append(f"{label.ljust(w0)}  " + "  ".join(fn(b).ljust(34) for b in bks))
    mb = results.get("microbench") or {}
    if mb:
        lines.append("")
        lines.append(f"in-process ({mb['records']} records, view {_kb(mb['view_bytes'])}, {mb['activities']} activities): "
                     f"replay() {_f(mb['replay_ms']['median'])} ms | apply(1 record) median {_f(mb['apply_one_record_ms']['median'], 3)} ms "
                     f"p95 {_f(mb['apply_one_record_ms']['p95'], 3)} ms | to_json() {_f(mb['to_json_ms']['median'])} ms | "
                     f"json.dumps {_f(mb['json_dumps_ms']['median'])} ms | apply_delta() {_f(mb['apply_delta_ms']['median'])} ms "
                     f"(deepcopy of the view {_f(mb['deepcopy_view_ms']['median'])} ms) | delta {mb['delta_bytes_median']} B")
    for b in bks:
        for a in R[b].get("anomalies") or []:
            lines.append(f"ANOMALY [{b}]: {a}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="BEFORE/AFTER benchmark of the gateway replay view (real uvicorn server).")
    ap.add_argument("--out", default=None, help="JSON results path (default: <gateway>/untracked/bench_replay_view-<ts>.json)")
    ap.add_argument("--backends", default="file,sqlite")
    ap.add_argument("--clients", type=int, default=50)
    ap.add_argument("--seed-records", type=int, default=3000)
    ap.add_argument("--appends", type=int, default=300)
    ap.add_argument("--appends-small", type=int, default=100, help="appends of the 1-client phases")
    ap.add_argument("--rate", type=float, default=20.0, help="appends per second")
    ap.add_argument("--idle", type=float, default=10.0)
    ap.add_argument("--idle-short", type=float, default=5.0)
    ap.add_argument("--n-slow", type=int, default=5, help="requests per cold / full-ledger measurement")
    ap.add_argument("--n-fast", type=int, default=30, help="requests per warm / 304 measurement")
    ap.add_argument("--skip-before", action="store_true", help="AFTER only")
    ap.add_argument("--gateway-src", default=None, help="serve another abstractgateway src tree (e.g. an older commit)")
    args = ap.parse_args()

    t_start = time.perf_counter()
    results: Dict[str, Any] = {"machine": machine_info(), "params": vars(args), "command": " ".join([sys.executable] + sys.argv),
                               "backends": {}}
    print(f"machine: {results['machine']['cpu']} ({results['machine']['ncpu']} cpu), Python {results['machine']['python']}, "
          f"gateway {results['machine']['gateway_commit']}, runtime {results['machine']['runtime_commit']}", flush=True)
    results["microbench"] = microbench(args.seed_records, args.appends)
    print("microbench done", flush=True)
    for b in [x.strip() for x in args.backends.split(",") if x.strip()]:
        t0 = time.perf_counter()
        results["backends"][b] = asyncio.run(run_backend(b, args, {}))
        print(f"{b}: done in {time.perf_counter() - t0:.0f} s", flush=True)
    results["total_s"] = time.perf_counter() - t_start
    end = machine_info()
    for k in ("gateway_commit", "runtime_commit"):
        if end[k] != results["machine"][k]:
            for b in results["backends"].values():
                b["anomalies"].append(f"{k} changed during the run ({results['machine'][k]} -> {end[k]}): "
                                      "the server processes and this client may run different code")
    out = Path(args.out) if args.out else GATEWAY / "untracked" / f"bench_replay_view-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str))
    print()
    print(table(results))
    print(f"\ntotal {results['total_s']:.0f} s; results: {out}")
    return 1 if any(results["backends"][b].get("anomalies") for b in results["backends"]) else 0


if __name__ == "__main__":
    sys.exit(main())
