"""The creation knob for the embedder pin (plan item 3 — gateway half).

Memory M1 owns the pin itself (persistence, mismatch refusals, first-write
fallback); these tests pin the DOOR's half: `entity create` accepts the
birth choice, the pin derives HONESTLY when the operator does not choose
(the resolved embedder's identity — never a hardcoded name the route might
not serve), a conflicting explicit choice refuses at creation, and the pin
is visible to the operator through inspect.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.embedding_pin")
pytest.importorskip("yaml")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402

from abstractgateway.entities import EntityRegistry  # noqa: E402


def _spark(name: str = "Castor") -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


class _NamedEmbedder:
    """Duck-typed like the engine expects: embed_texts + a `model` identity."""

    def __init__(self, model: str = "test-embedder-3", dimension: int = 8) -> None:
        self.model = model
        self._dimension = int(dimension)

    def embed_texts(self, texts):
        return [[0.1] * self._dimension for _ in texts]


def _registry(tmp_path: Path, embedder) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: embedder)


def _stored_pin(registry: EntityRegistry, name: str):
    home = registry.open(name)
    try:
        reader = getattr(home.store, "embedding_pin", None)
        return reader() if callable(reader) else None
    finally:
        home.close()


def test_explicit_birth_choice_is_pinned(tmp_path: Path):
    registry = _registry(tmp_path, _NamedEmbedder(model="test-embedder-3", dimension=8))
    result = registry.create(
        name="Castor", spark=_spark(), embedding_model="test-embedder-3", embedding_dimension=8
    )
    assert result.created is True
    pin = _stored_pin(registry, "Castor")
    assert pin is not None
    assert pin["model_id"] == "test-embedder-3"
    assert pin["dimension"] == 8
    assert pin["source"] == "creation"


def test_default_pin_derives_from_resolved_embedder(tmp_path: Path):
    """No explicit choice: the pin is the identity the home will ACTUALLY
    live with — model from the embedder's declared attribute, dimension
    probed with one embed call."""
    registry = _registry(tmp_path, _NamedEmbedder(model="route-model-x", dimension=6))
    registry.create(name="Castor", spark=_spark())
    pin = _stored_pin(registry, "Castor")
    assert pin is not None
    assert pin["model_id"] == "route-model-x"
    assert pin["dimension"] == 6
    assert pin["source"] == "creation"


def test_conflicting_birth_choice_refuses_loudly(tmp_path: Path):
    """An explicit model the door's resolved embedder does NOT serve would
    brick the home at its next open (identity-vs-pin refusal) — creation
    refuses instead, naming both sides."""
    registry = _registry(tmp_path, _NamedEmbedder(model="route-model-x"))
    with pytest.raises(ValueError, match="does not match the door's resolved embedder"):
        registry.create(name="Castor", spark=_spark(), embedding_model="some-other-model")
    # Nothing half-created that blocks the corrected retry.
    result = registry.create(name="Castor", spark=_spark())
    assert result.created is True


def test_no_embedder_no_choice_is_unpinned_and_labeled(tmp_path: Path):
    registry = _registry(tmp_path, None)
    result = registry.create(name="Castor", spark=_spark())
    assert result.created is True
    assert any("without an embedding pin" in w for w in result.warnings)
    pin = _stored_pin(registry, "Castor")
    assert pin is None  # memory's labeled first-write fallback owns it from here


def test_inspect_surfaces_the_pin(tmp_path: Path):
    registry = _registry(tmp_path, _NamedEmbedder(model="route-model-x", dimension=6))
    registry.create(name="Castor", spark=_spark())
    payload = registry.inspect("Castor")
    pin = payload.get("embedding_pin")
    assert pin is not None
    assert pin["model_id"] == "route-model-x"
    assert pin["dimension"] == 6
