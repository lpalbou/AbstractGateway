"""Entity homes: create / inspect / verify / list over one data root.

Deliverable 1 of the gateway entity-lifecycle charter (a2a 0004): compose
the shipped pieces — no new engine work. The reference implementation is
`abstractruntime/tests/test_readoption_experiment.py`; these tests cover the
GATEWAY composition around it: homes on disk, the attested spark, engram
idempotency across registry calls, refusal of a drifted spark, pure-read
inspection, and both attestation planes verifying.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("yaml")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402

from abstractgateway.entities import (  # noqa: E402
    EntityRegistry,
    entity_slug,
)


def _spark(name: str = "Castor") -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture()
def registry(tmp_path: Path) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime")


def test_slug_normalization_and_refusal():
    assert entity_slug("Castor") == "castor"
    assert entity_slug("My Entity") == "my-entity"
    with pytest.raises(ValueError):
        entity_slug("")
    with pytest.raises(ValueError):
        entity_slug("../escape")


def test_create_plants_home_and_is_idempotent(registry: EntityRegistry):
    first = registry.create(name="Castor", spark=_spark())
    assert first.created is True
    assert first.entity_id.startswith("entity:castor@home-")

    home_dir = registry.entities_dir / "castor"
    assert (home_dir / "spark.yaml").exists()
    assert (home_dir / "memory.sqlite3").exists()
    assert (home_dir / "home.sqlite3").exists()
    manifest = json.loads((home_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entity_id"] == first.entity_id
    assert manifest["spark_version"] == 1
    assert manifest["public_key_id"] is None  # reserved for the 008 keys work

    # Re-create with the SAME spark: re-adoption, never re-creation.
    second = registry.create(name="Castor", spark=_spark())
    assert second.created is False
    assert second.entity_id == first.entity_id


def test_create_refuses_a_changed_spark(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark())
    drifted = _spark()
    drifted["values"] = list(drifted["values"]) + [
        {"name": "obedience", "class": "revisable", "statement": "Do what you are told without question."}
    ]
    with pytest.raises(ValueError, match="DIFFERENT spark"):
        registry.create(name="Castor", spark=drifted)


def test_create_surfaces_lint_errors_verbatim(registry: EntityRegistry):
    spark = _spark()
    spark["values"] = [v for v in spark["values"] if v.get("name") != "shared_vulnerability"]
    with pytest.raises(ValueError, match="shared_vulnerability"):
        registry.create(name="Castor", spark=spark)


def test_spark_name_mismatch_is_refused(registry: EntityRegistry):
    with pytest.raises(ValueError, match="does not match"):
        registry.create(name="Pollux", spark=_spark("Castor"))


def test_spark_text_is_stored_byte_verbatim(registry: EntityRegistry):
    import yaml

    spark_text = yaml.safe_dump(_spark(), sort_keys=False, allow_unicode=True)
    spark_text = "# attested seed — comment must survive verbatim\n" + spark_text
    result = registry.create(name="Castor", spark_text=spark_text)
    stored = (registry.entities_dir / "castor" / "spark.yaml").read_bytes()
    assert stored == spark_text.encode("utf-8")
    assert result.created is True


def test_inspect_is_a_pure_read_with_identity_core(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark())

    payload = registry.inspect("Castor")
    assert "warnings" not in payload or not any(
        "deposited" in str(w) for w in payload["warnings"]
    ), "inspect must not deposit journal events"

    identity = payload["identity"]
    value_names = [v["name"] for v in identity["values"]]
    assert "shared_vulnerability" in value_names
    # Ordinal precedence: shared_vulnerability is ordinal 0 in the template.
    assert value_names[0] == "shared_vulnerability"
    assert identity["limits"], "honesty items land as limit-traits"
    assert payload["counts"]["identity_records"] == 6  # default template core
    assert payload["diary_tail"] == []  # an empty diary week is a valid diary week

    seq_a = payload["counts"]["memory_seq"]
    seq_b = registry.inspect("Castor")["counts"]["memory_seq"]
    assert seq_a == seq_b, "repeated inspection must not move the journal"


def test_verify_passes_on_a_fresh_home(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark())
    report = registry.verify("Castor")
    assert report["ok"] is True, report
    assert report["checks"]["book_chain"]["ok"] is True
    assert report["checks"]["graph_projection_chain"]["intact"] is True
    assert report["checks"]["spark"]["ok"] is True
    assert report["checks"]["manifest"]["ok"] is True


def test_verify_catches_spark_drift_on_disk(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark())
    spark_path = registry.entities_dir / "castor" / "spark.yaml"
    import yaml

    drifted = _spark()
    drifted["origin"] = "rewritten origin"
    spark_path.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")

    report = registry.verify("Castor")
    assert report["ok"] is False
    assert "drift" in str(report["checks"]["spark"]["error"])


def test_list_entities(registry: EntityRegistry):
    assert registry.list_entities() == []
    registry.create(name="Castor", spark=_spark("Castor"))
    registry.create(name="Pollux", spark=_spark("Pollux"))
    entities = registry.list_entities()
    assert [e["slug"] for e in entities] == ["castor", "pollux"]
    assert all(e["files"]["spark"] and e["files"]["memory"] and e["files"]["book"] for e in entities)


def test_inspect_unknown_entity_raises_key_error(registry: EntityRegistry):
    with pytest.raises(KeyError):
        registry.inspect("nobody")


def test_never_purge_is_structural(registry: EntityRegistry):
    """The registry exposes NO delete/purge/remove surface — never-purge is a
    property of the code that exists (a2a 0004 constraint), so the assertion
    is on the type's surface, not on behavior."""
    forbidden = [n for n in dir(registry) if any(t in n.lower() for t in ("delete", "purge", "remove", "reset", "drop"))]
    assert forbidden == [], f"registry grew a delete-shaped surface: {forbidden}"

    from abstractgateway.entities import EntityHome

    forbidden_home = [n for n in dir(EntityHome) if any(t in n.lower() for t in ("delete", "purge", "remove", "drop"))]
    assert forbidden_home == [], f"EntityHome grew a delete-shaped surface: {forbidden_home}"


def test_registry_embedder_wiring_is_lazy_and_honest(tmp_path: Path):
    """Memory's birth-audit ask (a2a 0003): homes open with the gateway
    embeddings route by default; an unreachable/unconfigured route degrades
    to vectorless with a LABELED warning and never blocks opening a home."""

    class _Embedder:
        calls = 0

        def embed_texts(self, texts):
            _Embedder.calls += 1
            return [[0.1] * 8 for _ in texts]

    # Working factory: homes carry the embedder (resolved once, lazily).
    embedder = _Embedder()
    registry = EntityRegistry(data_dir=tmp_path / "rt-ok", embedder_factory=lambda: embedder)
    registry.create(name="Castor", spark=_spark())
    home = registry.get_home("castor")
    try:
        assert home.memory._embedder is embedder
        assert registry.embedder_warning is None
    finally:
        registry.close_all()

    # Failing factory: vectorless, labeled, never raising.
    registry2 = EntityRegistry(
        data_dir=tmp_path / "rt-fail",
        embedder_factory=lambda: (_ for _ in ()).throw(RuntimeError("embeddings route down")),
    )
    registry2.create(name="Castor", spark=_spark())
    home2 = registry2.get_home("castor")
    try:
        assert home2.memory._embedder is None
        assert "#FALLBACK" in (registry2.embedder_warning or "")
        assert "embeddings route down" in registry2.embedder_warning
        # The vectorless home still answers (exact/keyword recall).
        assert registry2.inspect("castor")["counts"]["identity_records"] == 6
    finally:
        registry2.close_all()


def test_re_adoption_across_registry_instances(tmp_path: Path):
    """The re-adoption property at gateway level: a NEW registry over the
    same data root re-adopts the same identity (bit-identical entity_id,
    created=False) — the directory IS the entity."""
    data_dir = tmp_path / "runtime"
    r1 = EntityRegistry(data_dir=data_dir)
    first = r1.create(name="Castor", spark=_spark())

    r2 = EntityRegistry(data_dir=data_dir)
    second = r2.create(name="Castor", spark=_spark())
    assert second.created is False
    assert second.entity_id == first.entity_id

    inspection = r2.inspect("Castor")
    assert inspection["manifest"]["entity_id"] == first.entity_id
    assert inspection["counts"]["identity_records"] == 6
