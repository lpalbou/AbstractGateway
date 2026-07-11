"""Entity principals (plan item 4, GW-H — phase 2).

Laurent's consequence (b): entities are authenticated USERS of the door —
own principal, own credentials, own runtime, scoped rights, NEVER admin.
These tests pin the door half: the principal is minted at creation (id =
the name, roles = entity-only, scope = its own home), credentials are
door-issued and NEVER travel (nothing in the home carries authority; the
raw token is discarded at mint), re-creates never rotate or widen, and an
admin-shaped name collision is refused loudly instead of adopted.
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

from abstractgateway.entities import EntityRegistry  # noqa: E402
from abstractgateway.users import GatewayUserRegistry  # noqa: E402


def _spark(name: str) -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture(autouse=True)
def _no_env_users_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSTRACTGATEWAY_USERS_FILE", raising=False)


@pytest.fixture()
def registry(tmp_path: Path) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: None)


def _users(registry: EntityRegistry) -> GatewayUserRegistry:
    return GatewayUserRegistry(path=registry.data_dir / "auth" / "users.json")


def test_create_mints_an_entity_principal(registry: EntityRegistry):
    result = registry.create(name="Castor", spark=_spark("Castor"))
    assert result.principal == {"user_id": "castor", "roles": ["entity"], "minted": True}

    record = _users(registry).get_user("castor")
    assert record is not None
    assert list(record.roles) == ["entity"]
    assert list(record.scopes) == ["entity:castor"]
    assert record.enabled is True
    assert record.token_hash  # a credential EXISTS at the door (hashed)...
    principal = record.to_principal(token_fingerprint_value="")
    assert principal.is_admin() is False  # ...and is never admin


def test_recreate_never_rotates_or_widens(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark("Castor"))
    before = _users(registry).get_user("castor")

    again = registry.create(name="Castor", spark=_spark("Castor"))
    assert again.principal == {"user_id": "castor", "roles": ["entity"], "minted": False}
    after = _users(registry).get_user("castor")
    assert after.token_hash == before.token_hash  # no silent credential churn
    assert list(after.roles) == ["entity"]
    assert list(after.scopes) == ["entity:castor"]


def test_credentials_never_travel_with_the_home(registry: EntityRegistry):
    """The copied-home rule (same as the stamp secret): a home directory
    carries ZERO authority — no token, no hash, nothing derived from the
    credential rests under the home. The create result carries no secret
    either (the raw token is discarded at mint)."""
    result = registry.create(name="Castor", spark=_spark("Castor"))
    assert "token" not in json.dumps(result.to_dict()).lower() or result.principal.get("token") is None
    payload = result.to_dict()
    assert set(payload.get("principal", {}).keys()) == {"user_id", "roles", "minted"}

    record = _users(registry).get_user("castor")
    home_dir = registry.entities_dir / "castor"
    hash_bytes = record.token_hash.encode("utf-8")
    for path in sorted(p for p in home_dir.rglob("*") if p.is_file()):
        assert hash_bytes not in path.read_bytes(), f"credential material leaked into {path}"


def test_admin_shaped_collision_is_refused_not_adopted(registry: EntityRegistry, tmp_path: Path):
    users = _users(registry)
    users.create_user(user_id="castor", roles=["admin", "user"])

    result = registry.create(name="Castor", spark=_spark("Castor"))
    assert result.principal is None
    assert any("WITH ADMIN" in w for w in result.warnings)
    # The admin record was not touched, demoted, or adopted.
    record = users.get_user("castor")
    assert "admin" in record.roles


def test_distinct_entities_get_distinct_principals(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark("Castor"))
    registry.create(name="Pollux", spark=_spark("Pollux"))
    users = _users(registry)
    castor, pollux = users.get_user("castor"), users.get_user("pollux")
    assert castor.token_hash != pollux.token_hash
    assert list(castor.scopes) == ["entity:castor"]
    assert list(pollux.scopes) == ["entity:pollux"]
