"""ADR 0002 on every OS (operator ruling 2026-10-29): the model host's life cycle with only portable
process calls (`Popen.kill`: SIGKILL on POSIX, TerminateProcess on Windows). CI runs this file on
Windows too (`model-host-windows` job), where the Unix-signal tests in test_model_host_rpc.py skip.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from abstractgateway import model_host as mh
from model_host_fakes import FakeOpenAIServer


def _until(pred, timeout_s=30.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_the_model_host_answers_dies_mid_request_restarts_and_answers_again(tmp_path: Path) -> None:
    with FakeOpenAIServer(chunks=4) as fake:
        sup = mh.ModelHostSupervisor(tmp_path / "data", nice=0, backoff_s=(0.2,)).start()
        try:
            assert sup.generation_alive() and sup.pid
            client = mh.ModelHostClient(sup, kind="local", ctor=dict(
                provider="openai-compatible", model="fake-model",
                llm_kwargs={"base_url": fake.base_url, "api_key": "fake-key"}))
            assert "hello" in client.generate(prompt="hello")["content"]
            fake.delay_s = 1.0
            pid, proc = sup.pid, sup._proc
            threading.Timer(0.6, proc.kill).start()
            t0 = time.monotonic()
            with pytest.raises(mh.ModelHostCrashed) as info:
                client.generate(prompt="victim")
            assert time.monotonic() - t0 < 5.0
            assert "stopped while" in str(info.value) and info.value.status_code == 424
            assert mh.read_incidents(tmp_path / "data"), "no incident file"
            assert _until(lambda: sup.generation_alive() and sup.pid not in (None, pid))
            fake.delay_s = 0.0
            assert "after" in client.generate(prompt="after")["content"]
        finally:
            sup.stop()
    assert mh.default_mode() == mh.MODE_PROCESS
