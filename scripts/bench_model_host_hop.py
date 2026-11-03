#!/usr/bin/env python3
"""What the model host hop costs a chat step (ADR 0002, root 1060 latency budget: under 2 ms p50).

Runs the same chat step N times against a local fake OpenAI-compatible server (no model, so the
hop is not hidden behind generation time), first with AbstractRuntime's client in this process,
then through the model host child (`ModelHostClient`), and prints p50/p95 of both and of the
difference. `--stream` measures a streamed step with live token deltas (each delta crosses the
boundary). Run from the repository root:

    python scripts/bench_model_host_hop.py [--n 300] [--stream]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from model_host_fakes import FakeOpenAIServer  # noqa: E402


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def _run(client, n: int, stream: bool) -> list:
    from abstractruntime.core.live_deltas import LiveDeltaEmitter

    out = []
    for i in range(n + 10):
        params = {"stream": True} if stream else {}
        if stream:
            params["_on_delta"] = LiveDeltaEmitter(sink=lambda ev: None, run_id="bench", node_id="n", call_id=f"c{i}",
                                                   flush_interval_s=0.0)
        t = time.perf_counter()
        client.generate(prompt=f"step {i}", params=params)
        if i >= 10:  # warm-up excluded
            out.append((time.perf_counter() - t) * 1000.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--stream", action="store_true")
    args = ap.parse_args()
    from abstractgateway import model_host as mh
    from abstractruntime.integrations.abstractcore.llm_client import LocalAbstractCoreLLMClient

    with FakeOpenAIServer(chunks=8) as srv:
        ctor = dict(provider="openai-compatible", model="fake-model", llm_kwargs={"base_url": srv.base_url, "api_key": "k"})
        local = _run(LocalAbstractCoreLLMClient(**ctor), args.n, args.stream)
        sup = mh.ModelHostSupervisor(Path(tempfile.mkdtemp()), nice=0).start()
        try:
            remote = _run(mh.ModelHostClient(sup, kind="local", ctor=ctor), args.n, args.stream)
        finally:
            sup.stop()
    lp50, rp50 = statistics.median(local), statistics.median(remote)
    print(f"chat step ({'streamed, live deltas' if args.stream else 'plain'}), n={args.n}")
    print(f"  in-process   p50 {lp50:7.3f} ms   p95 {_pct(local, 0.95):7.3f} ms")
    print(f"  model host   p50 {rp50:7.3f} ms   p95 {_pct(remote, 0.95):7.3f} ms")
    print(f"  hop          p50 {rp50 - lp50:+7.3f} ms   p95 {_pct(remote, 0.95) - _pct(local, 0.95):+7.3f} ms   "
          f"(budget: under 2 ms p50)")
    return 0 if rp50 - lp50 < 2.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
