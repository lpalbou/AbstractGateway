from __future__ import annotations

import pytest


def test_parse_ioreg_ioaccelerator_output_extracts_device_utilization() -> None:
    from abstractgateway import host_metrics

    sample = """
+-o AGXAcceleratorG16X  <class AGXAcceleratorG16X, id 0x1, registered, matched, active, busy 0, retain 1>
  | {
  |   "model" = "Apple M4 Max"
  |   "PerformanceStatistics" = {"Device Utilization %"=42,"Renderer Utilization %"=50,"Tiler Utilization %"=34}
  | }
""".strip()

    gpus = host_metrics._parse_ioreg_ioaccelerator_output(sample)
    assert len(gpus) == 1
    assert gpus[0]["name"] == "Apple M4 Max"
    assert gpus[0]["utilization_gpu_pct"] == 42.0
    assert gpus[0]["renderer_utilization_pct"] == 50.0
    assert gpus[0]["tiler_utilization_pct"] == 34.0


def test_gpu_metrics_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import host_metrics

    monkeypatch.setenv("ABSTRACTGATEWAY_GPU_METRICS_PROVIDER", "ioreg")
    monkeypatch.setattr(host_metrics, "_read_ioreg_gpu_metrics", lambda **_: {"supported": True, "source": "ioreg", "utilization_gpu_pct": 1.0, "gpus": []})

    out = host_metrics._read_host_gpu_metrics_best_effort(timeout_s=0.01)
    assert out.get("supported") is True
    assert out.get("source") == "ioreg"

