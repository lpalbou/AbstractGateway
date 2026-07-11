"""Naming pins (plan item 2, GW-B — phase 1 safety rails).

The consensus plan (a2a/fs/plan.md, laurent's consequence (a)): an entity
name is unique per door. That uniqueness is STRUCTURAL — the directory IS
the registry — so these tests pin the structure and add the one refusal the
structure cannot express by itself: a home MOVED or COPIED under a name
that is not its own (manifest slug != directory name) is refused loudly at
lookup and LABELED (never hidden, never served) in listings.

Also pinned: the manifest's random home_id is an INTERNAL BIRTH MARKER —
never a lookup key, never rewritten by a later create. (The "never spoken"
half — new homes engraving entity:<name> without the random suffix — is
phase 2 item 6, deliberately not here.)
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("yaml")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402

from abstractgateway.entities import (  # noqa: E402
    EntityRegistry,
    HomeCollisionError,
    entity_slug,
)


def _spark(name: str) -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture()
def registry(tmp_path: Path) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime")


# ---------------------------------------------------------------------------
# Name unique per door (structural: the directory is the registry)
# ---------------------------------------------------------------------------


def test_name_unique_per_door_is_structural(registry: EntityRegistry):
    """Two entities cannot share a name at one door: the name IS the
    directory, and one directory holds one manifest. A second create under
    the same name with a DIFFERENT spark refuses (identity never drifts);
    with the SAME spark it re-adopts idempotently (created=False)."""
    first = registry.create(name="Castor", spark=_spark("Castor"))
    assert first.created is True

    with pytest.raises(ValueError, match="DIFFERENT spark"):
        other = _spark("Castor")
        other["values"] = [
            dict(v, statement=str(v.get("statement", "")) + " (drifted)")
            for v in other.get("values", [])
        ]
        registry.create(name="Castor", spark=other)

    again = registry.create(name="Castor", spark=_spark("Castor"))
    assert again.created is False
    assert again.entity_id == first.entity_id

    # Distinct names live side by side — uniqueness is per NAME, not global.
    second = registry.create(name="Pollux", spark=_spark("Pollux"))
    assert second.entity_id != first.entity_id
    assert (registry.entities_dir / "castor").is_dir()
    assert (registry.entities_dir / "pollux").is_dir()


def test_reserved_route_segment_names_are_refused(registry: EntityRegistry):
    """An entity named for a literal API path segment (auth, meets) would be
    unreachable behind the literal route — the route-shadowing booby trap.
    entity_slug refuses it, so it can never exist: manifest, home, principal."""
    for reserved in ("meets", "Meets", "auth", "AUTH"):
        with pytest.raises(ValueError, match="reserved"):
            entity_slug(reserved)
        with pytest.raises(ValueError, match="reserved"):
            registry.create(name=reserved, spark=_spark(reserved))
    # A normal name that merely CONTAINS a reserved word is fine.
    ok = registry.create(name="meetsy", spark=_spark("meetsy"))
    assert ok.entity_id == "entity:meetsy"


def test_two_healthy_homes_cannot_claim_one_entity_id(registry: EntityRegistry):
    """Per-door entity_id uniqueness follows from the slug pin: entity_id
    embeds the slug, and a manifest only serves from the directory bearing
    that slug — so two HEALTHY (lookup-servable) homes can never claim the
    same entity_id. The copied-directory case degrades to the moved-home
    collision below, which is refused."""
    registry.create(name="Castor", spark=_spark("Castor"))
    src = registry.entities_dir / "castor"
    copy_dir = registry.entities_dir / "castor-backup"
    shutil.copytree(src, copy_dir)

    listed = registry.list_entities()
    healthy = [e for e in listed if "error" not in e]
    ids = [e["entity_id"] for e in healthy]
    assert len(ids) == len(set(ids)) == 1  # the copy is not healthy-listed
    flagged = [e for e in listed if "error" in e]
    assert any("moved-home collision" in e["error"] for e in flagged)


# ---------------------------------------------------------------------------
# Moved-home collision: loud refusal, labeled listing
# ---------------------------------------------------------------------------


def test_moved_home_under_wrong_name_refuses_at_lookup(registry: EntityRegistry):
    """A home copied/renamed under a name that is not its own must never be
    SERVED as either identity: lookup by the directory name refuses loudly
    (HomeCollisionError names both sides); lookup by the true name still
    resolves the true home."""
    created = registry.create(name="Castor", spark=_spark("Castor"))
    src = registry.entities_dir / "castor"
    stray = registry.entities_dir / "kastor"
    shutil.copytree(src, stray)

    with pytest.raises(HomeCollisionError, match="moved or copied home"):
        registry.manifest_for("kastor")
    # The refusal is also a KeyError, so every existing route's 404 path
    # still applies without new plumbing.
    with pytest.raises(KeyError):
        registry.get_home("kastor")

    # The true home is untouched by the stray copy.
    manifest = registry.manifest_for("Castor")
    assert manifest.entity_id == created.entity_id


def test_moved_home_is_labeled_not_hidden_in_listing(registry: EntityRegistry):
    registry.create(name="Castor", spark=_spark("Castor"))
    shutil.copytree(registry.entities_dir / "castor", registry.entities_dir / "kastor")

    listed = registry.list_entities()
    by_slug = {e["slug"]: e for e in listed}
    assert "castor" in by_slug and "error" not in by_slug["castor"]
    assert "kastor" in by_slug
    assert "moved-home collision" in by_slug["kastor"]["error"]
    assert "castor" in by_slug["kastor"]["error"]  # names the true identity


# ---------------------------------------------------------------------------
# home_id demotion: internal birth marker — never a key, never rewritten
# ---------------------------------------------------------------------------


def test_home_id_is_never_a_lookup_key(registry: EntityRegistry):
    created = registry.create(name="Castor", spark=_spark("Castor"))
    home_id = created.manifest["home_id"]
    assert home_id.startswith("home-")

    # Lookups go by name/slug ONLY; the birth marker resolves nothing.
    with pytest.raises((KeyError, ValueError)):
        registry.manifest_for(home_id)
    with pytest.raises((KeyError, ValueError)):
        registry.manifest_for(created.entity_id)  # the full id is not a key either


def test_new_homes_mint_clean_ids_and_legacy_homes_keep_theirs(registry: EntityRegistry):
    """Item 6 (phase 2): new homes engrave entity:<name>; a legacy home's
    manifest (entity:<slug>@<home_id>) is served unchanged forever —
    re-adoption never rewrites an identity, and verify accepts BOTH
    generations."""
    import json as _json

    created = registry.create(name="Castor", spark=_spark("Castor"))
    assert created.entity_id == "entity:castor"
    assert registry.verify("Castor")["checks"]["manifest"]["ok"] is True

    # A REAL legacy home carries its id from BIRTH: plant the phase-0-shaped
    # manifest BEFORE the first engram (manifest-before-engram is exactly
    # the crash-retry contract), then let create() adopt it.
    import yaml as _yaml

    from abstractmemory import canonical_spark_hash

    legacy_dir = registry.entities_dir / "pollux"
    legacy_dir.mkdir(parents=True)
    doc = _spark("Pollux")
    (legacy_dir / "spark.yaml").write_bytes(
        _yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).encode("utf-8")
    )
    legacy_manifest = {
        "format_version": 1,
        "entity_id": "entity:pollux@home-deadbeef",
        "name": "Pollux",
        "slug": "pollux",
        "home_id": "home-deadbeef",
        "created_at": "2026-07-07T00:00:00+00:00",
        "spark_version": 1,
        "spark_hash": canonical_spark_hash(doc),
        "public_key_id": None,
        "key_registry": None,
    }
    (legacy_dir / "manifest.json").write_text(
        _json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    adopted = registry.create(name="Pollux", spark=_spark("Pollux"))
    assert adopted.entity_id == "entity:pollux@home-deadbeef"  # kept for life, never re-minted
    again = registry.create(name="Pollux", spark=_spark("Pollux"))
    assert again.created is False
    assert again.entity_id == "entity:pollux@home-deadbeef"
    assert registry.verify("Pollux")["checks"]["manifest"]["ok"] is True  # both generations verify


def test_home_id_is_never_rewritten_by_readoption(registry: EntityRegistry):
    """Idempotent re-create (same spark) must keep the SAME manifest —
    home_id is minted once at birth and survives every later create call
    (the manifest-before-engram crash contract depends on this)."""
    first = registry.create(name="Castor", spark=_spark("Castor"))
    manifest_path = registry.entities_dir / "castor" / "manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))

    again = registry.create(name="Castor", spark=_spark("Castor"))
    after = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert again.created is False
    assert before["home_id"] == after["home_id"]
    assert before == after  # nothing in the manifest is rewritten


def test_slug_is_the_only_name_normalization(registry: EntityRegistry):
    """Display name and slug resolve to ONE home ("Castor" and "castor" are
    the same door key) — pinning the normalization the uniqueness rule
    rides on."""
    registry.create(name="Castor", spark=_spark("Castor"))
    assert registry.manifest_for("Castor").slug == "castor"
    assert registry.manifest_for("castor").slug == "castor"
    assert entity_slug("Castor") == entity_slug("castor") == "castor"
