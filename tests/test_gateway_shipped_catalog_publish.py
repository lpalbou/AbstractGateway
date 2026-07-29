"""Card 013: boot ensure-publish of shipped catalog bundles (docs-qa).

A fresh gateway's tenant catalog started EMPTY, so the console assistant
drawer (hardwired to docs-qa@…/docsqa001 in tenant_catalog) errored until an
admin ran the documented curl. The ruled shape is (a) — idempotent boot
ensure-publish — with admin authority intact:

- publish IF ABSENT by exact version: restarts never churn records, never
  touch updated_at, never overwrite publisher attribution;
- make_default=False: the store assigns a default only when none exists —
  an admin-moved pointer is never moved back;
- a tombstoned version is never resurrected;
- a sha conflict (rebuilt artifact at the same version) warns loudly and
  never blocks boot;
- ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED=0 restores the curl-only posture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractruntime")

from abstractgateway.shipped_catalog import (  # noqa: E402
    BOOT_PUBLISHER,
    SHIPPED_CATALOG_BUNDLE_IDS,
    _find_shipped_bundle_files,
    ensure_shipped_catalog_bundles,
)
from abstractgateway.workflow_catalog import (  # noqa: E402
    CATALOG_SCOPE_TENANT,
    WorkflowCatalogStore,
    workflow_catalog_path_from_env,
)

_TOKEN = "shipped-catalog-secret"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Ambient escapes stripped (the c568 lesson): an exported provider env
    or remote-core URL in the driving shell must not shape these pins."""
    for key in (
        "ABSTRACTGATEWAY_PROVIDER",
        "ABSTRACTGATEWAY_MODEL",
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTCORE_AUTH_TOKEN",
        "ABSTRACTCORE_SERVER_API_KEY",
        "ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED",
        "ABSTRACTGATEWAY_FLOWS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    yield


def _shipped_docs_qa() -> Path:
    files = _find_shipped_bundle_files("docs-qa")
    assert files, "the repo must carry flows/bundles/docs-qa@<ver>.flow (wheel force-include source)"
    return files[0]


def _store(root: Path) -> WorkflowCatalogStore:
    return WorkflowCatalogStore(root_data_dir=root)


def _docs_qa_record(root: Path, version: str) -> dict:
    rec = _store(root).get_record(
        scope=CATALOG_SCOPE_TENANT, tenant_id="default", bundle_id="docs-qa", bundle_version=version
    )
    assert rec is not None
    return rec


def _shipped_version() -> str:
    name = _shipped_docs_qa().name
    return name[len("docs-qa@") : -len(".flow")]


# ------------------------------------------------------------- fresh install


def test_fresh_root_publishes_docs_qa_and_it_is_startable(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")

    published = [p["bundle_ref"] for p in summary["published"]]
    version = _shipped_version()
    assert f"docs-qa@{version}" in published

    rec = _docs_qa_record(root, version)
    assert rec["status"] == "published"
    assert rec["publisher"] == BOOT_PUBLISHER
    # A fresh catalog had no default: the store assigns one (the drawer pins
    # the exact version anyway, but list/default views must be coherent).
    default = _store(root).get_default_record(scope=CATALOG_SCOPE_TENANT, tenant_id="default", bundle_id="docs-qa")
    assert default is not None and default["bundle_version"] == version

    # The drawer's start tuple resolves: this is the fresh-install receipt.
    from abstractgateway.security.principal import GatewayPrincipal

    principal = GatewayPrincipal(user_id="admin", tenant_id="default", roles=("admin",))
    selection = _store(root).resolve_start(
        principal=principal,
        scope=CATALOG_SCOPE_TENANT,
        tenant_id="default",
        bundle_id="docs-qa",
        bundle_version=version,
        flow_id="docsqa001",
    )
    assert selection.flow_id == "docsqa001" and selection.sha256


def test_restart_is_a_pure_no_op(tmp_path: Path) -> None:
    """Publish-if-absent: the second run must not churn the record — no
    updated_at bump, no publisher overwrite (the install path WOULD stamp a
    passed publisher over an existing record's attribution)."""
    root = tmp_path / "runtime"
    ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    version = _shipped_version()
    before = _docs_qa_record(root, version)

    summary2 = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    assert not summary2["published"]
    assert any(
        s.get("reason") == "already_in_catalog" and s.get("version") == version for s in summary2["skipped"]
    )
    after = _docs_qa_record(root, version)
    assert after == before, "restart must not modify the catalog record at all"


# ------------------------------------------------------------ admin authority


def test_admin_default_pointer_is_never_moved_back(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    version = _shipped_version()

    # An admin publishes another version and points the default at it.
    content = _shipped_docs_qa().read_bytes()
    admin_version = "9.9.9"
    rewritten = _rezip_with_version(content, admin_version)
    store = _store(root)
    store.install_bundle_bytes(
        rewritten, scope=CATALOG_SCOPE_TENANT, tenant_id="default", make_default=True, publisher="default:admin"
    )
    default = store.get_default_record(scope=CATALOG_SCOPE_TENANT, tenant_id="default", bundle_id="docs-qa")
    assert default is not None and default["bundle_version"] == admin_version

    ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    default2 = _store(root).get_default_record(scope=CATALOG_SCOPE_TENANT, tenant_id="default", bundle_id="docs-qa")
    assert default2 is not None and default2["bundle_version"] == admin_version, "boot must not move an admin's default"


def test_tombstoned_version_is_never_resurrected(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    version = _shipped_version()
    _store(root).set_status(
        scope=CATALOG_SCOPE_TENANT,
        tenant_id="default",
        bundle_id="docs-qa",
        bundle_version=version,
        status="tombstoned",
        reason="operator decision",
        updated_by="default:admin",
    )

    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    assert not summary["published"]
    rec = _docs_qa_record(root, version)
    assert rec["status"] == "tombstoned", "a deliberate admin block must survive every boot"


def test_file_restore_preserves_the_existing_publisher(tmp_path: Path) -> None:
    """The one repair case: record present, catalog bundle FILE wiped. The
    restore re-writes bytes but must keep the record's attribution."""
    root = tmp_path / "runtime"
    ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    version = _shipped_version()
    rec = _docs_qa_record(root, version)
    bundle_file = Path(rec["path"])
    assert bundle_file.is_file()
    bundle_file.unlink()

    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    assert any(p.get("action") == "file_restored" for p in summary["published"])
    assert bundle_file.is_file(), "the shipped bytes must be restored"
    assert _docs_qa_record(root, version)["publisher"] == BOOT_PUBLISHER  # unchanged attribution


# ------------------------------------------------------------ failure honesty


def test_sha_conflict_warns_loudly_and_never_raises(tmp_path: Path) -> None:
    """Record lost but a DIVERGENT bundle file survives at the destination
    (the rebuilt-artifact-at-same-version class): the install refuses by
    immutability; ensure must surface a loud warning and keep booting."""
    root = tmp_path / "runtime"
    ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    version = _shipped_version()
    rec = _docs_qa_record(root, version)
    dest = Path(rec["path"])

    # Drop the record (keep the file), then make the file diverge.
    catalog_path = workflow_catalog_path_from_env(root)
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    for catalog in data.get("catalogs", {}).values():
        catalog.get("bundles", {}).pop("docs-qa", None)
    catalog_path.write_text(json.dumps(data), encoding="utf-8")
    dest.write_bytes(b"not the shipped bytes")

    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    assert summary["warnings"], "the conflict must be loud"
    assert any("immutable" in w or "conflicts" in w for w in summary["warnings"])


def test_missing_artifact_is_an_honest_skip(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    summary = ensure_shipped_catalog_bundles(
        root_data_dir=root, tenant_id="default", search_dirs=[tmp_path / "empty"]
    )
    assert not summary["published"]
    assert any(s.get("reason") == "artifact_missing" for s in summary["skipped"])


def test_kill_switch_restores_the_curl_only_posture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTO_PUBLISH_SHIPPED", "0")
    root = tmp_path / "runtime"
    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default")
    assert summary["enabled"] is False
    assert not summary["published"]
    assert (
        _store(root).get_record(
            scope=CATALOG_SCOPE_TENANT, tenant_id="default", bundle_id="docs-qa", bundle_version=_shipped_version()
        )
        is None
    )


def test_publish_is_boot_neutral_for_custom_bundle_deployments(tmp_path: Path) -> None:
    """The gate that keeps boots alive: the bundle host builds an LLM
    runtime whenever ANY loaded flow carries LLM/agent nodes. A deployment
    pointed at a custom flows dir WITHOUT basic-agent may run with no
    provider at all — publishing the llm_call-bearing docs-qa into its
    catalog would turn its next boot into a refusal (26 tests died on the
    first draft of the hook). No basic-agent => honest skip."""
    root = tmp_path / "runtime"
    custom_flows = tmp_path / "custom-flows"
    custom_flows.mkdir()
    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default", flows_dir=custom_flows)
    assert not summary["published"]
    assert any(s.get("reason") == "not_boot_neutral" for s in summary["skipped"])
    assert (
        _store(root).get_record(
            scope=CATALOG_SCOPE_TENANT, tenant_id="default", bundle_id="docs-qa", bundle_version=_shipped_version()
        )
        is None
    )


def test_publish_proceeds_on_the_shipped_default_registry(tmp_path: Path) -> None:
    """The twin pin: a flows dir carrying the REAL basic-agent (the shipped
    default registry — it has an agent node) already requires LLM
    construction — docs-qa rides for free."""
    import shutil

    root = tmp_path / "runtime"
    flows = tmp_path / "flows"
    flows.mkdir()
    shutil.copy2(_shipped_docs_qa().parent / "basic-agent.flow", flows / "basic-agent.flow")
    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default", flows_dir=flows)
    assert any(p.get("action") == "published" for p in summary["published"])


def test_gate_reads_flow_content_not_filenames(tmp_path: Path) -> None:
    """A minimal NON-LLM stand-in named basic-agent.flow (the test-suite
    pattern several gateway suites use) proves nothing about LLM
    requirements — the gate must stay closed on it."""
    import io
    import zipfile

    root = tmp_path / "runtime"
    flows = tmp_path / "flows"
    flows.mkdir()
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "basic-agent",
        "bundle_version": "0.0.0",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "flows": {"root": "flows/root.json"},
        "metadata": {},
    }
    flow = {
        "id": "root",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start"}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end"}},
        ],
        "edges": [],
        "entryNode": "start",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))
    (flows / "basic-agent.flow").write_bytes(buf.getvalue())

    summary = ensure_shipped_catalog_bundles(root_data_dir=root, tenant_id="default", flows_dir=flows)
    assert not summary["published"]
    assert any(s.get("reason") == "not_boot_neutral" for s in summary["skipped"])


# --------------------------------------------------------- boot path (HTTP)


def test_fresh_install_serves_docs_qa_through_the_catalog_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end receipt: a fresh data root boots with the SHIPPED
    default registry (the real basic-agent, copied into a controlled flows
    dir — the repo's live flows/bundles carries dev bundles that would make
    this test hostage to unrelated state), and the catalog list the console
    drawer depends on carries docs-qa without any admin act. The LLM
    runtime is stubbed at the factory seam (the suite's standard move) —
    provider validation against a live server is not this test's subject."""
    import shutil

    flows = tmp_path / "flows"
    flows.mkdir()
    shutil.copy2(_shipped_docs_qa().parent / "basic-agent.flow", flows / "basic-agent.flow")

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractruntime.core.config import RuntimeConfig
    from abstractruntime.core.models import EffectType
    from abstractruntime.core.runtime import EffectOutcome, Runtime
    from abstractruntime.integrations.abstractcore import factory as ac_factory

    def _fake_create_local_runtime(**kwargs):
        def _llm_stub(run, effect, default_next_node):
            del run, effect, default_next_node
            return EffectOutcome.completed({"content": "ok"})

        return Runtime(
            run_store=kwargs["run_store"],
            ledger_store=kwargs["ledger_store"],
            artifact_store=kwargs.get("artifact_store"),
            effect_handlers={EffectType.LLM_CALL: _llm_stub},
            config=RuntimeConfig(provider=kwargs.get("provider"), model=kwargs.get("model")),
        )

    monkeypatch.setattr(ac_factory, "create_local_runtime", _fake_create_local_runtime)

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as client:
        r = client.get("/api/gateway/workflow-catalog")
        assert r.status_code == 200, r.text
        bundles = r.json().get("bundles") or r.json().get("records") or r.json()
        text = json.dumps(bundles)
        assert "docs-qa" in text and "docsqa001" in text


def test_fresh_install_loads_with_no_provider_and_old_runtimes_keep_the_loud_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release-gap fix (delegate order c5863), both halves landed.

    End-to-end with the REAL factory: a bundle with LLM nodes loads and
    registers on a zero-config install — provider resolution defers to run
    time (runtime's blank-pair guard + factory fix, c5894/c5902). Version
    tolerance still pinned: on an older runtime that cannot build without a
    provider (simulated by a raising factory), the load keeps the original
    actionable refusal — never a raw crash."""
    import shutil

    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractruntime.storage.artifacts import FileArtifactStore
    from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore
    from abstractruntime.workflow_bundle.models import WorkflowBundleError

    flows = tmp_path / "flows"
    flows.mkdir()
    shutil.copy2(_shipped_docs_qa(), flows / _shipped_docs_qa().name)
    data = tmp_path / "runtime"
    data.mkdir()
    stores = dict(
        run_store=JsonFileRunStore(str(data / "runs")),
        ledger_store=JsonlLedgerStore(str(data / "ledgers")),
        artifact_store=FileArtifactStore(str(data / "artifacts")),
    )

    # Real factory, no provider anywhere: the load SUCCEEDS and the bundle
    # registers — the fresh-install release blocker, closed end to end.
    host = WorkflowBundleGatewayHost.load_from_dir(bundles_dir=flows, data_dir=data, **stores)
    assert host.specs, "a zero-config install must load and register the shipped bundle"

    # An OLDER runtime (factory refuses without a provider, simulated):
    # the load keeps the original actionable message — never worse than
    # before the deferral existed.
    from abstractruntime.integrations.abstractcore import factory as ac_factory

    def _old_factory(**kwargs):
        raise ValueError(f"Unknown provider: {kwargs.get('provider')!r}")

    monkeypatch.setattr(ac_factory, "create_local_runtime", _old_factory)
    with pytest.raises(WorkflowBundleError) as err:
        WorkflowBundleGatewayHost.load_from_dir(bundles_dir=flows, data_dir=data, **stores)
    assert "no default provider/model is configured" in str(err.value)


# ------------------------------------------------------------------ helpers


def _rezip_with_version(content: bytes, version: str) -> bytes:
    """A second VALID docs-qa bundle at a different version (admin-published
    twin for the default-pointer test) — real zip surgery, not a mock."""
    import io
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(content))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["bundle_version"] = version
                data = json.dumps(manifest).encode("utf-8")
            out.writestr(item, data)
    return out_buf.getvalue()


def test_shipped_list_is_explicit_and_small() -> None:
    """The allowlist is a NAMED decision (card 013): basic-agent and friends
    ride the private runtime registry, not the catalog. Growing this tuple
    is a deliberate act — this pin makes it one."""
    assert SHIPPED_CATALOG_BUNDLE_IDS == ("docs-qa",)
