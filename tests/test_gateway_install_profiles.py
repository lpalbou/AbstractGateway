from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


pytestmark = pytest.mark.basic


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _floor(requirements: list, name: str) -> Version:
    """The `>=` floor a requirement list gives `name` (it must name one)."""
    for raw in requirements:
        req = Requirement(raw)
        if req.name.lower() == name.lower():
            floors = [Version(spec.version) for spec in req.specifier if spec.operator == ">="]
            assert floors, f"{raw!r} has no >= floor"
            return max(floors)
    raise AssertionError(f"{name} is not in {requirements!r}")


def _sibling_pyproject(package_dir: str) -> dict:
    path = WORKSPACE_ROOT / package_dir / "pyproject.toml"
    if not path.exists():
        pytest.skip(f"monorepo sibling package is not available in this checkout: {package_dir}")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_base_install_is_remote_light_server() -> None:
    data = _pyproject()
    deps = list(data["project"]["dependencies"])
    from abstractgateway.live_deltas import ABSTRACTRUNTIME_FLOOR

    assert f"AbstractRuntime>={ABSTRACTRUNTIME_FLOOR}" in deps
    assert "abstractcore>=2.18.0" in deps
    assert "abstractvoice>=0.13.0" in deps
    assert "abstractagent>=0.3.17" in deps
    assert "AbstractMemory[lancedb]>=0.3.0" in deps
    assert "requests<3.0.0,>=2.32.5" in deps
    assert "urllib3<3.0.0,>=2.5.0" in deps
    assert "fastapi<1.0.0,>=0.136.0" in deps
    assert "uvicorn[standard]<1.0.0,>=0.38.0" in deps
    joined = "\n".join(deps)
    assert "mcp-worker" not in joined
    assert "multimodal" not in joined
    assert "sentence-transformers" not in joined
    assert "torch" not in joined
    assert "numpy" not in joined
    assert "abstractcore[" not in joined
    assert "abstractflow" not in joined


def test_base_install_keeps_remote_light_multimodal_plugins_without_local_inferencers() -> None:
    runtime_project = _sibling_pyproject("abstractruntime")["project"]
    core_extras = _sibling_pyproject("abstractcore")["project"]["optional-dependencies"]
    vision_base = "\n".join(_sibling_pyproject("abstractvision")["project"].get("dependencies", []))
    voice_base = "\n".join(_sibling_pyproject("abstractvoice")["project"].get("dependencies", []))
    music_base = "\n".join(_sibling_pyproject("abstractmusic")["project"].get("dependencies", []))

    runtime_base = "\n".join(runtime_project["dependencies"])
    # The runtime's core floor moves with the gateway's own (one wave, one floor).
    core_floor = next(d.split(">=", 1)[1] for d in _pyproject()["project"]["dependencies"] if d.startswith("abstractcore>="))
    assert f"abstractcore[remote,tools,vision,voice,audio,music]>={core_floor}" in runtime_base
    assert "pypdf" in runtime_base
    assert "reportlab" in runtime_base
    assert "pymupdf" not in runtime_base.lower()
    assert "torch" not in runtime_base
    assert "sentence-transformers" not in runtime_base
    assert "mlx" not in runtime_base
    assert "vllm" not in runtime_base

    core_remote = "\n".join(core_extras["remote"])
    assert "openai" in core_remote
    assert "anthropic" in core_remote

    # Floors, not exact strings: the sibling checkouts move ahead of this repo.
    # The voice floor must be at least the gateway's own (it imports
    # abstractvoice directly); vision and music keep their remote-light floors.
    voice_floor = next(d.split(">=", 1)[1] for d in _pyproject()["project"]["dependencies"] if d.startswith("abstractvoice>="))
    assert _floor(core_extras["vision"], "abstractvision") >= Version("0.3.29")
    assert _floor(core_extras["voice"], "abstractvoice") >= Version(voice_floor)
    assert _floor(core_extras["audio"], "abstractvoice") >= Version(voice_floor)
    assert _floor(core_extras["music"], "abstractmusic") >= Version("0.1.15")
    core_light_capabilities = "\n".join(
        [
            *core_extras["vision"],
            *core_extras["voice"],
            *core_extras["audio"],
            *core_extras["music"],
        ]
    )
    assert "omnivoice" not in core_light_capabilities
    assert "torch" not in core_light_capabilities
    assert "torchaudio" not in core_light_capabilities
    assert "sentence-transformers" not in core_light_capabilities

    remote_light_bases = "\n".join([vision_base, voice_base, music_base])
    assert "torch" not in remote_light_bases
    assert "diffusers" not in remote_light_bases
    assert "transformers" not in remote_light_bases
    assert "mlx" not in remote_light_bases
    assert "vllm" not in remote_light_bases
    assert "sentence-transformers" not in remote_light_bases


def test_entrypoint_profiles_cascade_lower_package_extras() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]

    # Legacy compatibility aliases were removed; keep the user-facing install
    # surface minimal and unambiguous.
    for legacy in (
        "http",
        "server",
        "multimodal",
        "memory",
        "voice",
        "vision",
        "visualflow",
        "telegram",
        "all",
        "all-apple",
        "all-gpu",
        "server-nvidia",
    ):
        assert legacy not in extras

    assert "embeddings" in extras
    embeddings = "\n".join(extras["embeddings"])
    assert "abstractcore[embeddings]>=2.18.0" in embeddings

    assert "apple" in extras
    assert "gpu" in extras
    # Tooling extras remain for contributors/CI.
    assert "dev" in extras
    assert "docs" in extras

    apple = "\n".join(extras["apple"])
    assert "AbstractRuntime[apple]>=0.7.0" in apple
    assert "abstractagent[apple]>=0.3.17" in apple
    assert "abstractagent[all-apple]" not in apple
    assert "AbstractMemory[all-apple]>=0.3.0" in apple
    assert "abstractcore[" not in apple
    assert "abstractvision" not in apple
    assert "abstractvoice" not in apple
    assert "abstractmusic" not in apple
    gpu = "\n".join(extras["gpu"])
    assert "AbstractRuntime[gpu]>=0.7.0" in gpu
    assert "abstractagent[gpu]>=0.3.17" in gpu
    assert "AbstractMemory[all-gpu]>=0.3.0" in gpu
    assert "abstractcore[" not in gpu
    assert "abstractvision" not in gpu
    assert "abstractvoice" not in gpu
    assert "abstractmusic" not in gpu


def test_config_entrypoint_is_published() -> None:
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["abstractgateway"] == "abstractgateway.cli:main"
    assert scripts["abstractgateway-config"] == "abstractgateway.config_cli:main"


def test_sdist_excludes_internal_artifacts() -> None:
    sdist = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]
    exclude = set(sdist["exclude"])
    assert "/docs/backlog/**" in exclude
    assert "/flows/**" in exclude
    assert "/tests/**" in exclude


def test_basic_agent_bundle_is_packaged_as_default_gateway_entrypoint() -> None:
    build = _pyproject()["tool"]["hatch"]["build"]["targets"]
    wheel_force = build["wheel"]["force-include"]
    sdist_force = build["sdist"]["force-include"]

    assert wheel_force["flows/bundles/basic-agent.flow"] == "abstractgateway/flows/bundles/basic-agent.flow"
    assert (
        wheel_force["flows/bundles/abstractassistant-orchestrator@0.0.0.flow"]
        == "abstractgateway/flows/bundles/abstractassistant-orchestrator@0.0.0.flow"
    )
    assert (
        wheel_force["flows/bundles/deep-research@0.1.8.flow"]
        == "abstractgateway/flows/bundles/deep-research@0.1.8.flow"
    )
    assert (
        wheel_force["flows/bundles/coding-agent@0.2.8.flow"]
        == "abstractgateway/flows/bundles/coding-agent@0.2.8.flow"
    )
    assert (
        wheel_force["flows/bundles/co-scientist@0.2.1.flow"]
        == "abstractgateway/flows/bundles/co-scientist@0.2.1.flow"
    )
    assert (
        wheel_force["flows/bundles/react-agent@0.1.0.flow"]
        == "abstractgateway/flows/bundles/react-agent@0.1.0.flow"
    )
    assert (
        wheel_force["flows/bundles/codeact-agent@0.1.0.flow"]
        == "abstractgateway/flows/bundles/codeact-agent@0.1.0.flow"
    )
    assert (
        wheel_force["flows/bundles/memact-agent@0.1.0.flow"]
        == "abstractgateway/flows/bundles/memact-agent@0.1.0.flow"
    )
    assert sdist_force["flows/bundles/basic-agent.flow"] == "flows/bundles/basic-agent.flow"
    assert (
        sdist_force["flows/bundles/abstractassistant-orchestrator@0.0.0.flow"]
        == "flows/bundles/abstractassistant-orchestrator@0.0.0.flow"
    )
    assert (
        sdist_force["flows/bundles/deep-research@0.1.8.flow"]
        == "flows/bundles/deep-research@0.1.8.flow"
    )
    assert (
        sdist_force["flows/bundles/coding-agent@0.2.8.flow"]
        == "flows/bundles/coding-agent@0.2.8.flow"
    )
    assert (
        sdist_force["flows/bundles/co-scientist@0.2.1.flow"]
        == "flows/bundles/co-scientist@0.2.1.flow"
    )
    assert (
        sdist_force["flows/bundles/react-agent@0.1.0.flow"]
        == "flows/bundles/react-agent@0.1.0.flow"
    )
    assert (
        sdist_force["flows/bundles/codeact-agent@0.1.0.flow"]
        == "flows/bundles/codeact-agent@0.1.0.flow"
    )
    assert (
        sdist_force["flows/bundles/memact-agent@0.1.0.flow"]
        == "flows/bundles/memact-agent@0.1.0.flow"
    )


def test_docs_qa_bundle_is_packaged_at_the_console_pinned_version() -> None:
    """docs-qa 0.1.1 (session-replayed history, no turn cap) ships in the wheel,
    the sdist and both images, and it is the version both consoles pin."""
    build = _pyproject()["tool"]["hatch"]["build"]["targets"]
    wheel_force = build["wheel"]["force-include"]
    sdist_force = build["sdist"]["force-include"]
    name = "flows/bundles/docs-qa@0.1.1.flow"
    assert (ROOT / name).is_file()
    assert wheel_force[name] == "abstractgateway/" + name
    assert sdist_force[name] == name
    for dockerfile in ("Dockerfile", "Dockerfile.nvidia"):
        assert name in (ROOT / "docker" / "abstractgateway-server" / dockerfile).read_text(encoding="utf-8")
    assert f"!{name}" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    console = (ROOT / "src" / "abstractgateway" / "console.py").read_text(encoding="utf-8")
    assert 'bundle_id: "docs-qa", bundle_version: "0.1.1"' in console
    tui = (ROOT / "console-tui" / "src" / "ui" / "docs.rs").read_text(encoding="utf-8")
    assert 'pub const BUNDLE_VERSION: &str = "0.1.1";' in tui


# Every bundle version a released gateway shipped. Automations, discussion
# roots and catalog records pin the CONCRETE version they were created with,
# so an upgrade that stops shipping one breaks them ("Workflow
# 'coding-agent@0.2.6:coder' not found"). Add a version here when a release
# ships it; never remove one without a migration.
PREVIOUSLY_SHIPPED_BUNDLES = (
    "coding-agent@0.2.6.flow",
    "co-scientist@0.2.0.flow",
    "docs-qa@0.1.0.flow",
)


@pytest.mark.parametrize("name", PREVIOUSLY_SHIPPED_BUNDLES)
def test_versions_shipped_by_0_6_0_are_still_shipped(name: str) -> None:
    build = _pyproject()["tool"]["hatch"]["build"]["targets"]
    rel = f"flows/bundles/{name}"
    assert (ROOT / rel).is_file()
    assert build["wheel"]["force-include"].get(rel) == "abstractgateway/" + rel
    assert build["sdist"]["force-include"].get(rel) == rel
    for dockerfile in ("Dockerfile", "Dockerfile.nvidia"):
        assert rel in (ROOT / "docker" / "abstractgateway-server" / dockerfile).read_text(encoding="utf-8"), dockerfile
    assert f"!{rel}" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_default_docker_image_uses_base_server_and_nvidia_uses_gpu_profile() -> None:
    dockerfile = (ROOT / "docker" / "abstractgateway-server" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker" / "abstractgateway-server" / "compose.yml").read_text(encoding="utf-8")
    nvidia_compose = (ROOT / "docker" / "abstractgateway-server" / "compose.nvidia.yml").read_text(
        encoding="utf-8"
    )

    assert "ARG ABSTRACTGATEWAY_EXTRAS=" in dockerfile
    assert "ABSTRACTGATEWAY_USER_AUTH=1" in dockerfile
    assert "ABSTRACTGATEWAY_DATA_DIR=/data" in dockerfile
    assert "ABSTRACTGATEWAY_FLOWS_DIR=/data/flows" not in dockerfile
    assert "ENTRYPOINT [\"abstractgateway-docker-entrypoint\"]" in dockerfile
    assert "ghcr.io/lpalbou/abstractgateway:${ABSTRACTGATEWAY_IMAGE_TAG:-0.7.0}" in compose
    assert "ABSTRACTGATEWAY_EXTRAS: ${ABSTRACTGATEWAY_EXTRAS:-}" in compose
    assert "ABSTRACTGATEWAY_USER_AUTH: ${ABSTRACTGATEWAY_USER_AUTH:-1}" in compose
    assert "ABSTRACTGATEWAY_EXTRAS:-gpu" in nvidia_compose
    assert "ghcr.io/lpalbou/abstractgateway:${ABSTRACTGATEWAY_NVIDIA_IMAGE_TAG:-0.7.0-gpu}" in nvidia_compose
    assert "context: ../.." in nvidia_compose


def test_nvidia_image_is_documented_as_experimental_while_best_effort() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = (ROOT / ".github" / "workflows" / "publish-ghcr.yml").read_text(encoding="utf-8")
    docs = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"),
            (ROOT / "docker" / "abstractgateway-server" / "README.md").read_text(encoding="utf-8"),
        ]
    ).lower()

    assert "attempt experimental nvidia full server image" in release.lower()
    assert "attempt experimental nvidia full server image" in publish.lower()
    assert "ghcr.io/${{ github.repository_owner }}/abstractgateway:${{ needs.build.outputs.version }}" in release
    assert "ghcr.io/${{ github.repository_owner }}/abstractgateway:${{ steps.meta.outputs.version }}" in publish
    assert "ABSTRACTGATEWAY_INSTALL_MODE=pypi" in release
    assert "ABSTRACTGATEWAY_INSTALL_MODE=pypi" in publish
    assert "continue-on-error: true" in release
    assert "continue-on-error: true" in publish
    assert "experimental" in docs
    assert "cuda build and smoke gate" in docs or "cuda host build/smoke gate" in docs


def test_apple_mlx_docs_use_host_native_endpoint_recipe() -> None:
    docs = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"),
            (ROOT / "docker" / "abstractgateway-server" / "README.md").read_text(encoding="utf-8"),
        ]
    )

    assert "model-runner.docker.internal/engines/v1" in docs
    assert 'pip install "abstractgateway[apple]"' in docs
    assert "not Docker" in docs or "not packaged as a Docker image" in docs
