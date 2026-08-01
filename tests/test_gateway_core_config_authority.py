"""THE AUTHORITY CONTRACT: two entry points, one store, no Gateway copy.

AbstractCore and AbstractGateway are the two entry points to the framework. For
every configuration domain AbstractCore owns, this file proves the three
properties that make "one source of truth" real, parametrized over the domains
rather than restated per domain:

  (a) a Gateway write lands in the AbstractCore store, and the live runtime
      follows it;
  (b) a write made through the AbstractCore entry point -- `abstractcore config
      set-default`, or AbstractCore's console-TUI -- is what the Gateway serves;
  (c) an explicit request pin outranks the default.

It also pins the field-preservation rule that keeps the two entry points from
overwriting each other: the Gateway console edits provider and model, the
AbstractCore CLI sets the reasoning effort, and neither discards the other's
work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.basic


# (route written, storage key in the file, fields the Gateway writes)
_DOMAINS = [
    ("output", "text", None, "input.text", {"provider": "lmstudio", "model": "qwen3-30b"}),
    ("output", "image", "text_to_image", "output.image.text_to_image", {"provider": "mlx-gen", "model": "flux"}),
    ("output", "image", "image_to_image", "output.image.image_to_image", {"provider": "mlx-gen", "model": "kontext"}),
    ("output", "video", "text_to_video", "output.video.text_to_video", {"provider": "hf", "model": "ltx"}),
    ("output", "voice", None, "output.voice", {"provider": "abstractvoice", "model": "supertonic"}),
    ("input", "voice", None, "input.voice", {"provider": "abstractvoice", "model": "whisper-small"}),
    ("output", "music", None, "output.music", {"provider": "abstractmusic", "model": "musicgen"}),
    ("output", "sound", None, "output.sound", {"provider": "abstractsound", "model": "audiogen"}),
    ("embedding", "text", None, "embedding.text", {"provider": "huggingface", "model": "all-minilm-l6-v2"}),
]

_IDS = [key for _kind, _modality, _task, key, _fields in _DOMAINS]


@pytest.fixture()
def scoped_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """THE AbstractCore config file, reached only through the Gateway seam.

    Named by `ABSTRACTCORE_CONFIG_FILE` -- the same override AbstractCore's own
    `resolve_config_file` honours -- rather than by patching the seam's
    internals, so these tests exercise the REAL path resolution. Since the
    one-store ruling (2026-08-01) that is the whole point: a patched path
    would have proved the contract against a store the CLI never opens.
    """
    import abstractgateway.core_config as core_config

    path = tmp_path / "config" / "abstractcore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")

    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(path))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    return path


def _stored_routes(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")).get("capability_defaults", {}).get("routes", {})


def _write_core_side(path: Path, key: str, row: Dict[str, Any]) -> None:
    """Simulate the AbstractCore entry point: a direct write of the same file."""
    import os
    import time

    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("capability_defaults", {}).setdefault("routes", {})[key] = dict(row)
    path.write_text(json.dumps(data), encoding="utf-8")
    stamp = time.time() + 1
    os.utime(path, (stamp, stamp))


def _served_row(path: Path, key: str) -> Dict[str, Any]:
    from abstractgateway.core_config import capability_default_rows

    return capability_default_rows(base_dir=path.parent.parent).get(key, {})


# ---------------------------------------------------------------- (a) writes


@pytest.mark.parametrize(("kind", "modality", "task", "key", "fields"), _DOMAINS, ids=_IDS)
def test_a_gateway_write_lands_in_the_core_store(
    scoped_store: Path, kind: str, modality: str, task: Any, key: str, fields: Dict[str, Any]
) -> None:
    from abstractgateway.core_config import save_gateway_capability_default

    save_gateway_capability_default(
        kind, modality, task=task, base_dir=scoped_store.parent.parent, **fields
    )

    stored = _stored_routes(scoped_store)
    assert key in stored, f"the Gateway write did not reach the AbstractCore store for {key}"
    for name, value in fields.items():
        assert stored[key][name] == value

    served = _served_row(scoped_store, key)
    assert served.get("configured") is True
    for name, value in fields.items():
        assert served.get(name) == value


@pytest.mark.parametrize(("kind", "modality", "task", "key", "fields"), _DOMAINS, ids=_IDS)
def test_a_gateway_clear_removes_the_row_from_the_core_store(
    scoped_store: Path, kind: str, modality: str, task: Any, key: str, fields: Dict[str, Any]
) -> None:
    from abstractgateway.core_config import (
        clear_gateway_capability_default,
        save_gateway_capability_default,
    )

    save_gateway_capability_default(kind, modality, task=task, base_dir=scoped_store.parent.parent, **fields)
    clear_gateway_capability_default(kind, modality, task=task, base_dir=scoped_store.parent.parent)

    assert key not in _stored_routes(scoped_store)


def test_a_gateway_write_refreshes_the_live_runtime() -> None:
    """The write path pushes the new payload onto the running host.

    Without it a saved default would only reach the next process, and the
    operator's setting would not be what the next run used.
    """
    import inspect

    from abstractgateway.routes.gateway import _apply_capability_defaults_to_live_runtime

    source = inspect.getsource(_apply_capability_defaults_to_live_runtime)
    assert "refresh_capability_defaults" in source

    calls: list[str] = []

    class _Host:
        def refresh_capability_defaults(self) -> Dict[str, Any]:
            calls.append("refresh")
            return {"ok": True, "changed": True}

    class _Cfg:
        data_dir = "."

    class _Svc:
        config = _Cfg()
        host = _Host()

    out = _apply_capability_defaults_to_live_runtime(_Svc())
    assert calls == ["refresh"]
    assert out["runtime_refresh"]["ok"] is True


# ------------------------------------------------------- (b) the other entry


@pytest.mark.parametrize(("kind", "modality", "task", "key", "fields"), _DOMAINS, ids=_IDS)
def test_a_core_side_write_is_what_the_gateway_serves(
    scoped_store: Path, kind: str, modality: str, task: Any, key: str, fields: Dict[str, Any]
) -> None:
    _write_core_side(scoped_store, key, fields)

    served = _served_row(scoped_store, key)
    assert served.get("configured") is True, f"{key} written through AbstractCore is not served by the Gateway"
    for name, value in fields.items():
        assert served.get(name) == value


def test_a_core_side_write_moves_the_freshness_signature(scoped_store: Path) -> None:
    """The running host notices a core-side write for the cost of a `stat`."""
    from abstractgateway.core_config import capability_defaults_config_signature

    base = scoped_store.parent.parent
    before = capability_defaults_config_signature(base_dir=base)
    assert before, "a file-backed store must produce a signature"

    _write_core_side(scoped_store, "output.voice", {"provider": "abstractvoice", "model": "supertonic"})
    assert capability_defaults_config_signature(base_dir=base) != before


def test_the_reasoning_effort_survives_the_round_trip(scoped_store: Path) -> None:
    """The AbstractCore CLI sets it; the Gateway serves it and keeps it.

    The console edits provider and model, so a save must merge over the stored
    row. A replacing write would silently discard an effort the operator set
    through the other entry point, and the two entry points would disagree.
    """
    from abstractgateway.core_config import reasoning_default, save_gateway_capability_default

    base = scoped_store.parent.parent
    _write_core_side(scoped_store, "input.text", {"provider": "lmstudio", "model": "qwen3", "reasoning": "high"})
    assert reasoning_default(base_dir=base) == "high"

    save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3-30b", base_dir=base
    )

    stored = _stored_routes(scoped_store)["input.text"]
    assert stored["model"] == "qwen3-30b", "the Gateway write must apply"
    assert stored["reasoning"] == "high", "the Gateway write must not discard the reasoning effort"
    assert reasoning_default(base_dir=base) == "high"


def test_the_gateway_can_set_and_clear_the_reasoning_effort(scoped_store: Path) -> None:
    from abstractgateway.core_config import reasoning_default, save_gateway_capability_default

    base = scoped_store.parent.parent
    save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3", reasoning="medium", base_dir=base
    )
    assert _stored_routes(scoped_store)["input.text"]["reasoning"] == "medium"
    assert reasoning_default(base_dir=base) == "medium"

    # An explicit empty string clears the effort; unset would preserve it.
    save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3", reasoning="", base_dir=base
    )
    assert "reasoning" not in _stored_routes(scoped_store)["input.text"]
    assert reasoning_default(base_dir=base) is None


def test_the_console_save_sends_only_the_fields_it_controls() -> None:
    """The console owns provider, model, the reasoning select and the voice picker.

    A field with no control in that modal must be left unset so the store keeps
    it. Echoing back what the grid last rendered would make the console a writer
    of values nobody edited: a route the operator changed through `abstractcore
    config` between render and save would be rolled back by a save that only
    meant to change the model.
    """
    import abstractgateway.console as console

    source = Path(console.__file__).read_text(encoding="utf-8")
    start = source.index("async function saveDefault()")
    end = source.index("config/capability-defaults/", start)
    handler = source[start:end]

    assert "const body = { provider, model };" in handler
    assert "base_url" not in handler, "the modal has no base_url control, so a save must not send one"
    options_line = handler.index("body.options = options;")
    voice_branch = handler.index("if (isVoiceOutputDefault(row)) {")
    assert voice_branch < options_line, "options travel only on the routes whose picker edits them"


def test_route_options_survive_a_provider_only_save(scoped_store: Path) -> None:
    """Voice/profile/language options are part of the same row."""
    from abstractgateway.core_config import save_gateway_capability_default

    base = scoped_store.parent.parent
    _write_core_side(
        scoped_store,
        "output.voice",
        {"provider": "abstractvoice", "model": "supertonic", "options": {"voice": "aria"}},
    )
    save_gateway_capability_default("output", "voice", provider="abstractvoice", base_dir=base)

    stored = _stored_routes(scoped_store)["output.voice"]
    assert stored["model"] == "supertonic"
    assert stored["options"] == {"voice": "aria"}


# ------------------------------------------------------------- (c) the pins


def test_an_explicit_request_pin_beats_the_text_default(
    scoped_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from abstractgateway.provider_defaults import resolve_gateway_provider_model

    for name in ("ABSTRACTGATEWAY_LLM_PROVIDER", "ABSTRACTGATEWAY_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    base = scoped_store.parent.parent
    _write_core_side(scoped_store, "input.text", {"provider": "lmstudio", "model": "qwen3"})

    default = resolve_gateway_provider_model(base_dir=base)
    assert (default.provider, default.model) == ("lmstudio", "qwen3")

    pinned = resolve_gateway_provider_model(provider="ollama", model="granite", base_dir=base)
    assert (pinned.provider, pinned.model) == ("ollama", "granite")
    assert pinned.source == "request"


def test_an_explicit_reasoning_pin_beats_the_configured_effort() -> None:
    """The execution-side half of the same rule, asserted where it is applied."""
    from abstractruntime.integrations.abstractcore.llm_client import (
        _with_capability_default_reasoning,
    )

    configured = {"output.text": {"key": "output.text", "reasoning": "high"}}
    params: Dict[str, Any] = {"thinking": "low"}
    assert _with_capability_default_reasoning(params, configured) == "low"

    unpinned: Dict[str, Any] = {}
    assert _with_capability_default_reasoning(unpinned, configured) == "high"


def test_a_media_pin_is_never_clobbered_by_a_default() -> None:
    from abstractruntime.integrations.abstractcore.llm_client import (
        _with_capability_default_route,
    )

    configured = {"output.voice": {"key": "output.voice", "provider": "abstractvoice", "model": "supertonic"}}
    routed = _with_capability_default_route({"modality": "voice", "task": "tts", "provider": "openai"}, configured)
    assert routed["provider"] == "openai"
    assert "model" not in routed


# ------------------------------------------------------------ the seam itself


def test_the_seam_exposes_every_core_owned_domain() -> None:
    """The one door must actually carry everything that goes through it."""
    from abstractgateway import core_config

    for name in (
        "gateway_capability_defaults_payload",
        "capability_default_rows",
        "capability_defaults_config_signature",
        "save_gateway_capability_default",
        "clear_gateway_capability_default",
        "apply_recommended_gateway_capability_defaults",
        "capability_default_specs",
        "text_default",
        "reasoning_default",
        "core_config_file",
        "read_core_config_api_key",
        "core_server_base_url",
        "core_server_token",
        "core_server_url",
    ):
        assert callable(getattr(core_config, name)), f"the seam is missing {name}"


def test_a_split_core_server_proxies_writes_instead_of_touching_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With AbstractCore split out, the store lives behind its HTTP boundary."""
    import abstractgateway.core_config as core_config

    sent: list[tuple[str, str, Any]] = []

    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "http://127.0.0.1:8123")
    monkeypatch.setattr(core_config, "_writable_scoped_core_config_path", lambda _base_dir: None)
    monkeypatch.setattr(
        core_config,
        "_core_server_json",
        lambda method, path, body=None: sent.append((method, path, body)) or {"ok": True, "routes": []},
    )

    core_config.save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3", reasoning="high", base_dir=tmp_path
    )

    assert sent, "a split deployment must proxy the write to the AbstractCore server"
    writes = [entry for entry in sent if entry[0] == "PUT"]
    assert writes, "a split deployment must PUT the route to the AbstractCore server"
    method, path, body = writes[0]
    assert method == "PUT"
    assert path == "/config/capability-defaults/output/text"
    assert body["reasoning"] == "high"
    assert core_config.capability_defaults_config_signature(base_dir=tmp_path) is None


# ------------------------------------------------- the split store, for real


@pytest.fixture()
def split_core_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The Gateway seam wired to a REAL AbstractCore server over its config routes.

    Everything downstream of the request line is the shipped AbstractCore server:
    its request model, its handler, its `ConfigurationManager`, its config file.
    Only the socket is replaced, so what this proves about field preservation is
    a property of the code that runs in a split deployment, not of a stand-in.
    """
    import importlib

    from fastapi.testclient import TestClient

    import abstractgateway.core_config as core_config

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ABSTRACTCORE_SERVER_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.delenv("ABSTRACTCORE_AUTH_TOKEN", raising=False)

    server_app = importlib.import_module("abstractcore.server.app")
    client = TestClient(server_app.app)

    def _bridge(method: str, path: str, body: Any = None) -> Dict[str, Any]:
        response = client.request(method.upper(), f"/v1{path}", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "http://abstractcore.test")
    monkeypatch.setattr(core_config, "_writable_scoped_core_config_path", lambda _base_dir: None)
    monkeypatch.setattr(core_config, "_core_server_json", _bridge)

    with client:
        yield core_config


def _split_row(core_config, key: str) -> Dict[str, Any]:
    payload = core_config.gateway_capability_defaults_payload()
    return next((row for row in payload.get("routes", []) if row.get("key") == key), {})


def _core_side_write(**fields: Any) -> None:
    """The AbstractCore entry point writing its own store, behind the boundary."""
    from abstractcore.config.manager import ConfigurationManager

    assert ConfigurationManager().set_capability_default("output", "text", **fields)


def test_a_split_gateway_save_preserves_a_core_set_reasoning_effort(split_core_server) -> None:
    """The flagship rule holds across the HTTP boundary, not only on one disk.

    An operator sets the reasoning effort through AbstractCore and then changes
    the model from the Gateway console. If the boundary replaced the whole row,
    the second action would silently undo the first and the two entry points
    would disagree about the same store.
    """
    core_config = split_core_server
    _core_side_write(provider="lmstudio", model="qwen3", reasoning="high", options={"profile": "local"})

    core_config.save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3-next"
    )

    row = _split_row(core_config, "output.text")
    assert row.get("model") == "qwen3-next"
    assert row.get("reasoning") == "high"
    assert row.get("options") == {"profile": "local"}


def test_a_split_gateway_can_set_and_clear_the_reasoning_effort(split_core_server) -> None:
    """The console's reasoning control is a live control in a split deployment."""
    core_config = split_core_server
    _core_side_write(provider="lmstudio", model="qwen3")

    core_config.save_gateway_capability_default("output", "text", reasoning="medium")
    assert _split_row(core_config, "output.text").get("reasoning") == "medium"
    assert _split_row(core_config, "output.text").get("model") == "qwen3"

    core_config.save_gateway_capability_default("output", "text", reasoning="")
    row = _split_row(core_config, "output.text")
    assert "reasoning" not in row or row.get("reasoning") in (None, "")
    assert row.get("model") == "qwen3", "clearing one field must not clear the row"


def test_a_split_gateway_read_serves_what_the_core_entry_point_wrote(split_core_server) -> None:
    """(b) across the boundary: the AbstractCore side writes, the Gateway serves it."""
    core_config = split_core_server
    _core_side_write(provider="ollama", model="llama4", reasoning="low")

    assert core_config.text_default() == {
        "provider": "ollama",
        "model": "llama4",
        "reasoning": "low",
        "source": "abstractcore.capability_defaults:output.text",
        "key": "output.text",
    }
    assert core_config.reasoning_default() == "low"


# ------------------------------------------- apply-recommended, through the seam
#
# "I asked for qwen3.5-9b everywhere, I see qwen3-0.6b" (2026-08-01). The
# fresh-install seed never touches an existing store -- correct for safety, and
# it left the console's "recommended: N of 3" banner with no action behind it.
# The action exists now; the Gateway must expose the SAME action, with the same
# decision taken once, in AbstractCore.


def test_the_gateway_applies_the_recommendation_to_the_store_it_edits(scoped_store: Path) -> None:
    from abstractgateway import core_config

    payload = core_config.apply_recommended_gateway_capability_defaults()

    report = payload["applied_recommended"]
    assert report["ok"] is True
    stored = _stored_routes(scoped_store)
    assert stored["input.text"]["model"] == "qwen/qwen3.5-9b"
    assert stored["output.voice"]["provider"] == "supertonic"
    assert stored["output.image"]["provider"] == "mlx-gen"
    # The refreshed grid rides along, so the console never renders a stale row.
    assert any(row.get("key") == "output.text" for row in payload.get("routes", []))


def test_the_gateway_keeps_a_route_the_operator_configured(scoped_store: Path) -> None:
    from abstractgateway import core_config

    core_config.save_gateway_capability_default(
        "output", "text", provider="endpoint:airelay", model="gpt-5.4"
    )

    report = core_config.apply_recommended_gateway_capability_defaults()["applied_recommended"]

    row = next(r for r in report["routes"] if r["key"] == "input.text")
    assert row["action"] == "kept"
    assert _stored_routes(scoped_store)["input.text"]["model"] == "gpt-5.4", (
        "a 'recommended' button that silently replaced a deliberate choice would be "
        "the same class of defect it exists to fix"
    )

    # ... and `force` is the explicit overrule.
    core_config.apply_recommended_gateway_capability_defaults(only=["text"], force=True)
    assert _stored_routes(scoped_store)["input.text"]["model"] == "qwen/qwen3.5-9b"


def test_a_gateway_dry_run_writes_nothing(scoped_store: Path) -> None:
    from abstractgateway import core_config

    before = scoped_store.read_text(encoding="utf-8")
    report = core_config.apply_recommended_gateway_capability_defaults(dry_run=True)["applied_recommended"]
    assert report["dry_run"] is True
    assert report["changed"] >= 1
    assert scoped_store.read_text(encoding="utf-8") == before


def test_a_split_core_server_refuses_instead_of_writing_the_wrong_machine(
    split_core_server,
) -> None:
    """With the store behind an HTTP boundary, applying LOCALLY would write to
    the Gateway's own unused store -- the wrong machine, silently."""
    core_config = split_core_server
    with pytest.raises(RuntimeError) as exc:
        core_config.apply_recommended_gateway_capability_defaults()
    assert "abstractcore config apply-recommended" in str(exc.value)
