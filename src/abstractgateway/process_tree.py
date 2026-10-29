"""The gateway's process tree, for `abstractgateway service uninstall` (backlog 0934).

`launchctl bootout` and `systemctl --user disable --now` signal the serve process and return.
Several of its children run in their own session (`start_new_session=True`): an entity's own-time
loop, model download jobs, managed processes, the apps the gateway starts, the tray. The service
manager never stops those, and they keep writing into the data folder for seconds after the
command returns. An XDG-autostart gateway (Linux without a systemd user manager) has no manager
to stop it at all.

So the tree is captured BEFORE the service manager is asked to stop anything. While the serve
process lives, every child it started (detached or not) still has it as parent. The tree is:

- the pids the gateway recorded (`<data>/run/gateway-serve.json`, `<data>/run/apps/*.json`),
  each trusted only if it is alive, ours, and its command line names an abstract* program (a
  recycled pid is not);
- the processes running this data folder's app watcher
  (`<data>/apps/_support/parent_watch.cjs`);
- all of their descendants.

This process and its ancestors are never part of it. `stop_tree` then waits for the tree to exit
(bounded), sends SIGTERM to what is left, then SIGKILL, and reports what was running, how each
process ended, and what survived. Not Windows (out of scope).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

# Seams (tests drive them).
_sleep = time.sleep
_monotonic = time.monotonic
_kill = os.kill

POLL_S = 0.25
TERM_WAIT_S = 5.0
KILL_WAIT_S = 3.0


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    stat: str
    args: str

    @property
    def zombie(self) -> bool:
        return self.stat.startswith("Z")


def list_processes() -> List[Proc]:
    """Every process `ps` shows (macOS and Linux). Empty when `ps` cannot run (a minimal container):
    the recorded pids are then all that is known."""
    try:
        cp = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "uid=", "-o", "stat=", "-o", "args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: List[Proc] = []
    for line in (cp.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, uid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        out.append(Proc(pid=pid, ppid=ppid, uid=uid, stat=parts[3], args=parts[4] if len(parts) > 4 else ""))
    return out


_scan: Callable[[], List[Proc]] = list_processes


def recorded_pids(data_dir: Path) -> List[int]:
    """The pids the gateway wrote under `<data>/run/`: the serve record and each app's record."""
    run = Path(data_dir) / "run"
    files = [run / "gateway-serve.json", *sorted((run / "apps").glob("*.json"))] if run.is_dir() else []
    pids: List[int] = []
    for f in files:
        try:
            pid = int(json.loads(f.read_text(encoding="utf-8")).get("pid") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if pid > 1 and pid not in pids:
            pids.append(pid)
    return pids


def app_watcher_marker(data_dir: Path) -> str:
    return str(Path(data_dir) / "apps" / "_support" / "parent_watch.cjs")


def _ancestors(procs: Dict[int, Proc], pid: int) -> Set[int]:
    seen: Set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        p = procs.get(pid)
        if p is None:
            break
        pid = p.ppid
    return seen


def gateway_tree(
    procs: Sequence[Proc],
    *,
    data_dir: Path,
    seeds: Iterable[int] = (),
    uid: Optional[int] = None,
    self_pid: Optional[int] = None,
    verify_seeds: bool = True,
) -> Dict[int, str]:
    """pid -> command line of every live process of this data folder's gateway tree.
    `verify_seeds=False` for pids already verified (the tracking during a stop)."""
    me = os.getuid() if uid is None else int(uid)
    by_pid = {p.pid: p for p in procs if p.uid == me and not p.zombie}
    skip = _ancestors({p.pid: p for p in procs}, os.getpid() if self_pid is None else int(self_pid))
    marker = app_watcher_marker(data_dir)
    tree: Set[int] = set()
    for pid in seeds:
        p = by_pid.get(int(pid))
        if p is not None and (not verify_seeds or "abstract" in p.args.lower()):
            tree.add(p.pid)
    tree |= {p.pid for p in by_pid.values() if marker in p.args}
    tree -= skip
    grew = True
    while grew:
        grew = False
        for p in by_pid.values():
            if p.pid not in tree and p.pid not in skip and p.ppid in tree:
                tree.add(p.pid)
                grew = True
    return {pid: by_pid[pid].args for pid in sorted(tree)}


def snapshot(data_dir: Path) -> Dict[int, str]:
    """The tree now (before anything is asked to stop)."""
    return gateway_tree(_scan(), data_dir=data_dir, seeds=recorded_pids(data_dir))


def _alive(pids: Iterable[int]) -> Set[int]:
    """Those still running. A zombie has exited. Without `ps`, `kill -0` is the only test."""
    wanted = set(pids)
    procs = _scan()
    if procs:
        return {p.pid for p in procs if p.pid in wanted and not p.zombie}
    out: Set[int] = set()
    for pid in wanted:
        try:
            _kill(pid, 0)
            out.add(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            out.add(pid)
    return out


def _wait_gone(pids: Set[int], seconds: float, *, track: Optional[Callable[[], Set[int]]] = None) -> Set[int]:
    deadline = _monotonic() + max(0.0, seconds)
    while True:
        if track is not None:
            pids = pids | track()
        left = _alive(pids)
        if not left or _monotonic() >= deadline:
            return left
        _sleep(POLL_S)


def _signal(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        try:
            _kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def stop_tree(
    tree: Dict[int, str],
    *,
    data_dir: Path,
    wait_s: float,
    echo: Callable[[str], None] = print,
    term_wait_s: Optional[float] = None,
    kill_wait_s: Optional[float] = None,
) -> Dict[str, object]:
    """Wait up to `wait_s` for the tree to exit by itself (the service manager has stopped the serve
    process; 0 when nothing did, as for an XDG-autostart gateway), then SIGTERM, then SIGKILL.
    New children of a tracked process (still linked to it) are tracked too."""
    term_wait = TERM_WAIT_S if term_wait_s is None else term_wait_s
    kill_wait = KILL_WAIT_S if kill_wait_s is None else kill_wait_s
    names = dict(tree)
    tracked: Set[int] = set(tree)

    def track() -> Set[int]:
        found = gateway_tree(_scan(), data_dir=data_dir, seeds=tracked, verify_seeds=False)
        names.update(found)
        tracked.update(found)
        return set(found)

    result: Dict[str, object] = {"running": [], "exited": [], "terminated": [], "killed": [], "survivors": []}
    # Running = what the snapshot saw before the service manager acted, plus what is linked now.
    pids = set(tree) | track()
    if not pids:
        echo("no gateway process is running")
        return result
    result["running"] = [{"pid": p, "args": names.get(p, "")[:160]} for p in sorted(pids)]
    echo("gateway processes running:")
    for p in sorted(pids):
        echo(f"  {p}  {names.get(p, '')[:160]}")
    left = _alive(pids)
    if left and wait_s > 0:
        echo(f"waiting up to {wait_s:.0f} s for them to exit")
        left = _wait_gone(left, wait_s, track=track)
    pids = pids | tracked
    result["exited"] = sorted(pids - left)
    if left:
        echo("still running, sending SIGTERM: " + " ".join(str(p) for p in sorted(left)))
        _signal(left, signal.SIGTERM)
        after_term = _wait_gone(set(left), term_wait)
        result["terminated"] = sorted(left - after_term)
        if after_term:
            echo("still running, sending SIGKILL: " + " ".join(str(p) for p in sorted(after_term)))
            _signal(after_term, signal.SIGKILL)
            survivors = _wait_gone(set(after_term), kill_wait)
            result["killed"] = sorted(after_term - survivors)
            result["survivors"] = [{"pid": p, "args": names.get(p, "")[:160]} for p in sorted(survivors)]
    if not result["survivors"]:
        echo("the gateway process tree has exited")
    return result
