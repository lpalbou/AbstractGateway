from __future__ import annotations

from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from abstractgateway import host_system_metrics as metrics

pytestmark = pytest.mark.basic


def fake_host(monkeypatch, *, fail_cpu=False):
    clock = [100.0]
    counters = {"user": 30, "idle": 70, "read_bytes": 1000, "write_bytes": 2000, "bytes_recv": 3000, "bytes_sent": 4000}
    calls = []
    Times = namedtuple("Times", "user idle iowait guest guest_nice")

    def cpu_times():
        calls.append("cpu")
        if fail_cpu:
            raise OSError("cpu denied")
        return Times(counters["user"], counters["idle"], 0, 0, 0)

    ps = SimpleNamespace(cpu_times=cpu_times, cpu_count=lambda: 8,
                         disk_io_counters=lambda: SimpleNamespace(**counters),
                         net_io_counters=lambda: SimpleNamespace(**counters),
                         swap_memory=lambda: SimpleNamespace(used=0, total=8192))
    monkeypatch.setattr(metrics, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]+1000))
    monkeypatch.setattr(metrics.importlib, "import_module", lambda name: ps)
    monkeypatch.setattr(metrics.shutil, "disk_usage", lambda path: SimpleNamespace(total=10000, used=7000, free=3000))
    return clock, counters, calls


def test_first_interval_is_unknown_then_computes_real_rates(tmp_path, monkeypatch):
    clock, counters, _ = fake_host(monkeypatch)
    sampler = metrics.SystemSampler(ttl_s=0)
    first = sampler.sample(tmp_path)
    assert first["cpu"]["utilization_pct"] is None
    assert first["disk_io"]["read_bytes_per_s"] is None
    assert "first interval" in first["network"]["reason"]
    assert first["swap"]["used_bytes"] == 0  # known zero, not unsupported
    assert first["storage"]["free_bytes"] == 3000
    clock[0] += 2
    counters.update(user=60, idle=80, read_bytes=1512, write_bytes=2256, bytes_recv=4024, bytes_sent=4512)
    second = sampler.sample(tmp_path)
    assert second["cpu"]["utilization_pct"] == 75
    assert second["disk_io"]["read_bytes_per_s"] == 256
    assert second["disk_io"]["write_bytes_per_s"] == 128
    assert second["network"]["read_bytes_per_s"] == 512
    assert second["network"]["write_bytes_per_s"] == 256
    assert second["network"]["scope"].startswith("all host interfaces")


def test_counter_reset_is_not_negative_or_zero_usage(tmp_path, monkeypatch):
    clock, counters, _ = fake_host(monkeypatch)
    sampler = metrics.SystemSampler(ttl_s=0)
    sampler.sample(tmp_path)
    clock[0] += 5
    counters.update(read_bytes=0, write_bytes=0)
    sample = sampler.sample(tmp_path)
    assert sample["disk_io"]["read_bytes_per_s"] is None
    assert "reset" in sample["disk_io"]["reason"]


def test_probe_failure_is_independent_and_concurrent_callers_share_sample(tmp_path, monkeypatch):
    _, _, calls = fake_host(monkeypatch, fail_cpu=True)
    sampler = metrics.SystemSampler()
    with ThreadPoolExecutor(max_workers=6) as pool:
        samples = list(pool.map(lambda _: sampler.sample(tmp_path), range(12)))
    assert len(calls) == 1
    for sample in samples:
        assert sample["cpu"]["supported"] is False
        assert "cpu denied" in sample["cpu"]["reason"]
        assert sample["storage"]["supported"] is True
        assert sample["swap"]["supported"] is True


def test_missing_psutil_still_reports_storage_without_creating_data(tmp_path, monkeypatch):
    def missing(name):
        raise ImportError("not installed")
    monkeypatch.setattr(metrics.importlib, "import_module", missing)
    path = tmp_path / "not-created" / "data"
    sample = metrics.SystemSampler().sample(path)
    assert not path.exists()
    assert sample["storage"]["supported"] is True
    for name in ["cpu", "swap", "disk_io", "network"]:
        assert sample[name]["supported"] is False
        assert "psutil" in sample[name]["reason"]
