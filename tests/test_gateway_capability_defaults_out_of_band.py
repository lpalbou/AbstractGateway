"""The OTHER entry point writes too: `abstractcore config set-default`.

THE OPERATOR'S RULING has two entry points -- (a) AbstractCore low level and
(b) AbstractGateway high level -- over ONE store. Gateway's PUT/DELETE routes
push the freshly-written payload into the live runtime, so entry point (b) is
effective on the next run. Entry point (a) -- `abstractcore config set-default
<route> --provider ... --model ...`, and AbstractCore's console-TUI, which
shells exactly that command (`console-tui/src/writes.rs::set_route`) -- writes
the same config file with no way to notify a running Gateway.

That was invisible, and TOTALLY so: once the host has pushed a payload, the
runtime stops consulting disk for EVERY route. Unconfigured rows travel in the
payload as an explicit ``source: "not_configured"``, and
`resolve_capability_default_route` treats that as an answer and short-circuits
the config-file fallback it would otherwise take. So a core-side write stayed
unseen until the next Gateway write or a process restart -- the two entry
points disagreeing, which is precisely what the ruling forbids.

The fix is a file fingerprint, not a TTL: one `stat` per run (no parse, no
HTTP), and the payload is re-derived only when a file actually moved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

pytestmark = pytest.mark.basic


class _FakeHost:
    """The two methods under test, bound to a stand-in with the same state.

    Bound off the real class so the logic under test is the shipped logic; the
    surrounding host (bundles, runtime, stores) is not what this file is about.
    """

    def __init__(self, data_dir: Path) -> None:
        import threading

        self.data_dir = str(data_dir)
        self._lock = threading.RLock()
        self._capability_defaults_config_signature = None
        self.refresh_calls = 0
        self.refresh_result: Dict[str, Any] = {"ok": True, "changed": True, "provider": "p", "model": "m"}

    def refresh_capability_defaults(self) -> Dict[str, Any]:
        from abstractgateway.hosts.bundle_host import _capability_defaults_signature

        self.refresh_calls += 1
        with self._lock:
            self._capability_defaults_config_signature = _capability_defaults_signature(Path(self.data_dir))
        return dict(self.refresh_result)

    def refresh_capability_defaults_if_config_changed(self) -> bool:
        from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

        return WorkflowBundleGatewayHost.refresh_capability_defaults_if_config_changed(self)  # type: ignore[arg-type]


@pytest.fixture()
def scoped_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """THE AbstractCore store, watched as the only config file.

    Named through `ABSTRACTCORE_CONFIG_FILE`, so the file the host watches is
    the file `abstractcore config set-default` writes -- which is what makes an
    "out of band" write out of band and not merely a patched path.
    """
    import abstractgateway.core_config as cd

    path = tmp_path / "config" / "abstractcore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")

    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(path))
    monkeypatch.setattr(cd, "core_server_base_url", lambda: None)
    return path


def _write_route(path: Path, provider: str, model: str) -> None:
    """Simulate `abstractcore config set-default output.voice ...` -- a direct
    file write, no Gateway route involved."""
    import os
    import time

    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("capability_defaults", {}).setdefault("routes", {})["output.voice"] = {
        "provider": provider,
        "model": model,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    # st_mtime_ns has coarse granularity on some filesystems; the size changes
    # here anyway, but bump the stamp so the test cannot be flaky either way.
    stamp = time.time() + 1
    os.utime(path, (stamp, stamp))


def test_signature_changes_when_the_config_file_is_written_out_of_band(scoped_config: Path) -> None:
    from abstractgateway.core_config import capability_defaults_config_signature

    before = capability_defaults_config_signature(base_dir=scoped_config.parent.parent)
    assert before, "a file-backed store must produce a signature"
    _write_route(scoped_config, "supertonic", "supertonic-3")
    after = capability_defaults_config_signature(base_dir=scoped_config.parent.parent)
    assert after != before


def test_a_split_core_server_has_no_file_to_watch(
    scoped_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-run HTTP GET: that is the starvation lever the design avoids."""
    import abstractgateway.core_config as cd

    monkeypatch.setattr(cd, "core_server_base_url", lambda: "http://127.0.0.1:8123")
    assert cd.capability_defaults_config_signature(base_dir=scoped_config.parent.parent) is None


def test_an_out_of_band_write_refreshes_the_live_host(scoped_config: Path) -> None:
    host = _FakeHost(scoped_config.parent.parent)
    # Load-time stamp, as `load_from_dir` takes it.
    host.refresh_capability_defaults()
    assert host.refresh_calls == 1

    # Steady state: nothing moved, so nothing is re-derived.
    for _ in range(5):
        assert host.refresh_capability_defaults_if_config_changed() is False
    assert host.refresh_calls == 1, "an unchanged store must cost only the stat"

    # Entry point (a) writes.
    _write_route(scoped_config, "supertonic", "supertonic-3")
    assert host.refresh_capability_defaults_if_config_changed() is True
    assert host.refresh_calls == 2

    # ...and it settles again immediately.
    assert host.refresh_capability_defaults_if_config_changed() is False
    assert host.refresh_calls == 2


def test_a_stat_failure_never_fails_a_run(
    scoped_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import abstractgateway.hosts.bundle_host as bh

    host = _FakeHost(scoped_config.parent.parent)
    host.refresh_capability_defaults()

    def _boom(_data_root: Path) -> Optional[tuple]:
        raise OSError("filesystem said no")

    monkeypatch.setattr(bh, "_capability_defaults_signature", _boom)
    assert host.refresh_capability_defaults_if_config_changed() is False


def test_a_failing_refresh_is_not_retried_every_run(scoped_config: Path) -> None:
    """A broken refresh must not turn into a per-run retry storm."""
    host = _FakeHost(scoped_config.parent.parent)
    host.refresh_capability_defaults()
    _write_route(scoped_config, "supertonic", "supertonic-3")

    def _raise() -> Dict[str, Any]:
        host.refresh_calls += 1
        raise RuntimeError("endpoint profile is broken")

    host.refresh_capability_defaults = _raise  # type: ignore[method-assign]
    assert host.refresh_capability_defaults_if_config_changed() is False
    assert host.refresh_calls == 2
    # The signature was still advanced, so the failure is not retried forever.
    assert host.refresh_capability_defaults_if_config_changed() is False
    assert host.refresh_calls == 2


def test_start_run_consults_the_freshness_check() -> None:
    """The hook must be ON the run path, or the whole mechanism is decorative."""
    import inspect

    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    source = inspect.getsource(WorkflowBundleGatewayHost.start_run)
    assert "refresh_capability_defaults_if_config_changed" in source


def test_the_media_helper_lane_also_consults_it() -> None:
    """Media helper routes bypass `host.start_run` entirely.

    `POST /runs/{id}/images/generate`, `/voice/tts`, `/music/generate`, … reach
    AbstractCore through `_gateway_abstractcore_run_facade`, not through
    `start_run`. Covering only `start_run` would leave every media modality --
    the whole point of this work -- on the stale copy. Caught live: the first
    cut hooked `start_run` only, and an `abstractcore config set-default
    output.image.text_to_image` was still invisible to the next image run.
    """

    import inspect

    from abstractgateway.routes.gateway import _gateway_abstractcore_run_facade

    source = inspect.getsource(_gateway_abstractcore_run_facade)
    assert "refresh_capability_defaults_if_config_changed" in source
