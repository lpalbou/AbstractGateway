#!/usr/bin/env python3
"""PTY smoke: drive the REAL binary with real keyboard bytes against a
LIVE gateway, and prove the definition-of-done write path end to end.

The route write is asserted against GATEWAY STATE via HTTP (the
abstractcode-tui lesson: raw-stream pixel grepping is flaky for
full-screen apps — assert side effects out of band; pixels only gate
coarse navigation).

Usage:
  ABSTRACTGATEWAY_AUTH_TOKEN=... python3 scripts/pty_smoke.py [--url URL]

Exit 0 = every gate passed. The script restores the route it writes.
"""

import argparse
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import urllib.request

ANSI = re.compile(rb"\x1b\[[0-9;:?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>]|\x1b\([0-9A-B]")

ROUTE_KEY = "input.music"  # unconfigured on a stock gateway; zero blast radius


def http(url, token, method="GET", body=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data=data, timeout=30) as r:
        return json.loads(r.read().decode())


def route_state(base, token):
    payload = http(f"{base}/api/gateway/config/capability-defaults", token)
    for row in payload.get("routes", []):
        if row.get("key") == ROUTE_KEY:
            return row
    raise SystemExit(f"route {ROUTE_KEY} not in capability-defaults")


class Tui:
    def __init__(self, argv, env, cols=150, rows=42):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        self.raw = bytearray()

    def pump(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if self.master in r:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.raw.extend(chunk)

    def text(self):
        return ANSI.sub(b"", bytes(self.raw)).decode("utf-8", "replace")

    def wait_for(self, needle, timeout, label):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.text():
                print(f"  ✓ {label}")
                return
            self.pump(0.2)
        print(f"  ✗ TIMEOUT waiting for {label!r}")
        print("---- last 2000 chars of stripped output ----")
        print(self.text()[-2000:])
        self.stop()
        sys.exit(1)

    def wait_any(self, needles, timeout, label):
        """Honest OR-gate: any of several truthful renderings passes
        (e.g. a live voice catalog may list voices OR truthfully report
        none — both are correct UI states)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self.text()
            for n in needles:
                if n in got:
                    print(f"  ✓ {label} (saw {n!r})")
                    return
            self.pump(0.2)
        print(f"  ✗ TIMEOUT waiting for any of {needles!r} ({label})")
        print(self.text()[-2000:])
        self.stop()
        sys.exit(1)

    def send(self, data, settle=0.25):
        os.write(self.master, data)
        self.pump(settle)

    def nav(self, data, settle=0.5):
        """Send keys, then request a full repaint (Ctrl+L → the app's
        request_full_redraw): the diff-based presenter re-emits only
        CHANGED cells on a screen switch, fragmenting text needles across
        the byte stream (the abstractcode-tui pty lesson). A full-frame
        emission makes every needle contiguous."""
        self.send(data, settle)
        self.send(b"\x0c", settle=0.3)

    def stop(self):
        if self.proc.poll() is None:
            self.send(b"\x03", settle=0.3)  # Ctrl+C — the app's own quit
        if self.proc.poll() is None:
            time.sleep(0.5)
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
        return self.proc.wait(timeout=5)


def restore_route(base, token, before):
    """Best-effort restore — runs on EVERY exit path after the write
    phase begins (a failed assert must never leave the operator's
    gateway carrying a bogus route)."""
    try:
        now = route_state(base, token)
        if now.get("configured") and not before.get("configured"):
            http(
                f"{base}/api/gateway/config/capability-defaults/input/music",
                token,
                method="DELETE",
            )
            print("[cleanup] restored input.music to unconfigured")
    except Exception as e:  # noqa: BLE001 — cleanup is best-effort, but loud
        print(f"[cleanup] FAILED to restore route: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    args = ap.parse_args()
    token = os.environ.get("ABSTRACTGATEWAY_AUTH_TOKEN", "")
    if not token:
        raise SystemExit("set ABSTRACTGATEWAY_AUTH_TOKEN")

    before = route_state(args.url, token)
    if before.get("configured") and not before.get("covered_by"):
        raise SystemExit(
            f"{ROUTE_KEY} is explicitly configured on this gateway — refusing to touch it"
        )
    print(f"[pre] {ROUTE_KEY}: configured={before.get('configured')}")

    # Needles come from the LIVE gateway, not from one operator's data:
    # the smoke must pass on any gateway with ≥1 profile.
    profiles = http(f"{args.url}/api/gateway/config/provider-endpoint-profiles", token)
    profile_needle = next(
        (p["id"] for p in profiles.get("profiles", []) if p.get("id")), None
    )
    entities = http(f"{args.url}/api/gateway/entities", token)
    entity_needle = next(
        (e["name"] for e in entities.get("entities", []) if e.get("name")), None
    )
    runtimes = http(f"{args.url}/api/gateway/admin/runtimes", token)
    runtime_needle = next(
        (r["runtime_id"] for r in runtimes.get("runtimes", []) if r.get("kind") == "entity"),
        None,
    )
    # Row indexes come from the LIVE payload order (the table renders it
    # verbatim) — never a hardcoded position on someone else's gateway.
    defaults = http(f"{args.url}/api/gateway/config/capability-defaults", token)
    route_keys = [r.get("key") for r in defaults.get("routes", [])]
    idx_voice = route_keys.index("output.voice") if "output.voice" in route_keys else None
    idx_music = route_keys.index(ROUTE_KEY)
    runs_payload = http(f"{args.url}/api/gateway/runs?limit=5&root_only=true", token)
    run_needle = next(
        (r["run_id"][:12] for r in runs_payload.get("items", []) if r.get("run_id")),
        None,
    )

    binary = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "target",
        "debug",
        "abstractgateway-console",
    )
    if not os.path.exists(binary):
        raise SystemExit(f"build first: cargo build ({binary} missing)")

    env = dict(os.environ)
    env["ABSTRACTGATEWAY_AUTH_TOKEN"] = token
    env["TERM"] = "xterm-256color"
    tui = Tui([binary, "--url", args.url], env)

    print("[1] boot + auto-probe (env token)")
    tui.wait_for("AbstractGateway Console", 10, "app painted")
    tui.wait_for("admin@default", 15, "probe connected as admin (live /ping + /me)")

    print("[2] Ctrl+N → providers (live discovery + profiles)")
    tui.nav(b"\x0e")
    tui.wait_for("Available providers", 10, "providers screen (unified list)")
    if profile_needle:
        tui.wait_for(profile_needle, 20, f"live profile row rendered ({profile_needle})")
    tui.wait_for("lmstudio", 20, "live discovered provider rendered")

    print("[3] Ctrl+N → routes (live capability-defaults)")
    tui.nav(b"\x0e")
    tui.wait_for("Routes — which provider", 10, "routes screen")
    tui.wait_for("output.voice", 15, "live route rows rendered")

    if idx_voice is not None:
        print("[3b] output.voice editor: voice picker + Test verb (live catalog)")
        for _ in range(idx_voice):
            tui.send(b"\x1b[B", settle=0.12)
        tui.send(b"e", settle=0.8)
        tui.wait_for("(output.voice)", 10, "voice route editor open")
        # The voice row is fed by GET /voice/voices?provider&model — any
        # truthful state passes: a listed catalog, an honest empty, a
        # per-pair hint (unresolved pair), or a labeled failure.
        tui.wait_any(
            [
                "provider default voice",
                "no voices reported",
                "voices are per-pair",
                "voice catalog failed",
                "loading voices",
            ],
            30,
            "voice row rendered from the live catalog",
        )
        tui.wait_for("Test", 5, "route Test verb present")
        tui.send(b"\x1b", settle=0.5)  # clean editor → closes on one Esc
        tui.send(b"\x0c", settle=0.3)
        tui.wait_for("Routes — which provider", 5, "back on the routes table")
        for _ in range(idx_voice + 2):  # re-anchor selection at the top
            tui.send(b"\x1b[A", settle=0.08)

    print(f"[4] keyboard-drive the {ROUTE_KEY} override (Down×{idx_music}, e, override, pick pair, Save)")
    # Navigate by the live payload's own index — the state assert below
    # catches drift and restores.
    for _ in range(idx_music):
        tui.send(b"\x1b[B", settle=0.15)
    tui.send(b"e", settle=0.8)
    tui.wait_for("Route — Music Input", 10, "route editor open")
    tui.wait_for("nothing configured — engine decides", 5, "honest applies-now line")
    tui.send(b"\x1b[B", settle=0.3)  # mode radio (autofocused) → override
    tui.wait_for("choose a provider…", 5, "placeholder provider (no fabricated pick)")
    tui.send(b"\t", settle=0.3)  # → provider select
    tui.send(b"\r", settle=0.4)  # open popup
    tui.raw.clear()
    # Type-ahead, not a positional walk: the Select popup jumps by label
    # prefix, so this picks lmstudio regardless of the live provider
    # order (a blind Down×N could commit — and SAVE — a different
    # provider on someone else's gateway).
    tui.send(b"lmstudio", settle=0.4)
    tui.send(b"\r", settle=0.6)  # commit provider
    tui.wait_for("lmstudio", 5, "provider committed on the trigger")
    tui.wait_for("choose a model…", 30, "model list discovered (live lmstudio)")
    tui.send(b"\t", settle=0.3)  # → model combobox
    tui.send(b"\r", settle=0.5)  # open
    tui.send(b"\x1b[B", settle=0.2)  # first real model
    tui.send(b"\r", settle=0.5)  # commit model
    for _ in range(3):  # base URL → options → Save
        tui.send(b"\t", settle=0.15)
    tui.send(b"\r", settle=0.5)  # Save override
    tui.send(b"\x0c", settle=0.3)  # editor closes on success → full repaint
    try:
        tui.wait_for(
            "verified: GET shows input.music = lmstudio", 30, "write + verify journaled"
        )
        after = route_state(args.url, token)
        assert after.get("configured") is True, f"gateway state: {after}"
        assert after.get("provider") == "lmstudio", f"gateway state: {after}"
        print(
            f"  ✓ GATEWAY STATE: {ROUTE_KEY} = {after.get('provider')} / {after.get('model')}"
        )
    except BaseException:
        restore_route(args.url, token, before)
        tui.stop()
        raise

    print("[5] keyboard-clear the override (x, confirm)")
    try:
        tui.send(b"x", settle=0.5)
        tui.wait_for("Clear the override on input.music", 10, "confirm prompt")
        tui.send(b"\x1b[A", settle=0.2)  # up to the danger option
        tui.send(b"\r", settle=0.5)
        tui.send(b"\x0c", settle=0.3)
        tui.wait_for("GET shows input.music cleared", 30, "clear + verify journaled")
        restored = route_state(args.url, token)
        assert restored.get("configured") == before.get(
            "configured"
        ), f"restore failed: {restored}"
        print(f"  ✓ GATEWAY STATE restored: configured={restored.get('configured')}")
    except BaseException:
        restore_route(args.url, token, before)
        tui.stop()
        raise

    print("[6] users & entities: roster + manage snapshot + manage menu + reservations")
    tui.nav(b"\x0e")
    tui.wait_for("Users (admin)", 10, "users screen")
    if entity_needle:
        tui.wait_for(entity_needle, 15, f"live entity roster rendered ({entity_needle})")
        # The inline strip became the right Drawer: i toggles the
        # inspector (passive focus; selection-driven detail reads).
        tui.wait_for("i inspects the selected entity", 10, "drawer teaching line")
        tui.send(b"i", settle=0.8)
        tui.wait_for("Entity inspector", 10, "inspector drawer open")
        tui.wait_any(
            ["mind ", "reading", "detail read failed"],
            20,
            "live detail in the drawer",
        )
        tui.send(b"i", settle=0.6)  # toggle closed
        tui.send(b"\x0c", settle=0.3)
        tui.send(b"m", settle=0.8)
        tui.wait_for("Manage entity", 10, "manage menu open")
        # Tool policy editor: live policy + capability-matrix reads.
        for _ in range(5):
            tui.send(b"\x1b[B", settle=0.1)
        tui.send(b"\r", settle=0.8)
        tui.wait_for("Tool policy —", 15, "tool-policy editor open (live reads)")
        tui.wait_any(
            ["visit", "loading tool policy"], 15, "phase grants rendered"
        )
        tui.send(b"\x1b", settle=0.8)  # clean close
        # Fresh-needle discipline + a full frame so the post-close
        # screen state is visible before the next gesture.
        tui.raw.clear()
        tui.send(b"\x0c", settle=0.5)
        tui.wait_for("Users (admin)", 10, "back on the users screen after Esc")
        tui.raw.clear()
        tui.send(b"m", settle=0.8)
        tui.send(b"\x0c", settle=0.3)
        tui.wait_for("Manage entity", 10, "manage menu re-open")
        for _ in range(6):
            tui.send(b"\x1b[B", settle=0.1)
        tui.send(b"\r", settle=0.8)
        tui.wait_for("Prompt overlay —", 15, "prompt editor open (live read)")
        tui.wait_any(
            ["──", "no overlay layers", "loading overlay"],
            15,
            "overlay layers rendered",
        )
        tui.send(b"\x1b", settle=0.5)
        tui.send(b"\x0c", settle=0.3)
    tui.send(b"v", settle=0.8)
    tui.wait_for("Kept data of deleted users", 10, "reservations modal (live read)")
    tui.send(b"\x1b", settle=0.5)
    tui.send(b"\x0c", settle=0.3)

    print("[7] runtimes: inventory, choose-to-inspect tabs (live)")
    tui.nav(b"\x0e")
    tui.wait_for("Runtimes — where each", 10, "runtimes screen")
    if runtime_needle:
        tui.wait_for(runtime_needle, 20, f"live entity runtime row ({runtime_needle})")
    # NOTHING loads before a choice (2026-07-26 directive) — the
    # inspector teaches the gesture instead.
    tui.wait_for("select a runtime above", 10, "inspector teaching line (no eager loads)")
    # Choose the highlighted default plane (Enter on the autofocused
    # inventory) → the Sessions|Data tabs mount and the plane's runs
    # load live.
    tui.send(b"\r", settle=1.0)
    tui.wait_for("Sessions", 10, "inspector tabs mounted after choose")
    tui.wait_for("Data & cache", 5, "data tab present")
    if run_needle:
        tui.wait_for(run_needle, 20, f"live run row rendered ({run_needle})")
    # Knobs live behind a collapsed disclosure now.
    tui.wait_for("Runtime knobs", 5, "knobs disclosure header present (collapsed)")

    print("[8] review: inline sandbox + journal carries the write")
    tui.nav(b"\x0e")
    tui.wait_for("Changes this session", 10, "review screen")
    # The sandbox is INLINE (2026-07-25 redesign): pickers + prompt +
    # Generate live on the screen itself. Presence only — this smoke
    # NEVER runs a generation (a real paid model call).
    tui.wait_for("Live test (sandbox generate)", 5, "inline sandbox block")
    tui.wait_for("Generate (Enter)", 5, "inline Generate affordance")
    tui.wait_for("PUT capability route input.music", 5, "journal carries the write")

    code = tui.stop()
    print(f"[exit] app exited with {code}")
    if code != 0:
        sys.exit(1)
    print("PTY SMOKE: ALL GATES PASSED")


if __name__ == "__main__":
    main()
