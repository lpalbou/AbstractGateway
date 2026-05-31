from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


pytestmark = pytest.mark.basic


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _sibling_pyproject(package_dir: str) -> dict:
    path = WORKSPACE_ROOT / package_dir / "pyproject.toml"
    if not path.exists():
        pytest.skip(f"monorepo sibling package is not available in this checkout: {package_dir}")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_base_install_is_remote_light_server() -> None:
    data = _pyproject()
    deps = list(data["project"]["dependencies"])
    assert "AbstractRuntime>=0.4.26" in deps
    assert "abstractagent>=0.3.10" in deps
    assert "AbstractMemory[lancedb]>=0.2.6" in deps
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
    assert "abstractcore[remote,media,tools,vision,voice,audio,music]>=2.13.31" in runtime_base
    assert "torch" not in runtime_base
    assert "sentence-transformers" not in runtime_base
    assert "mlx" not in runtime_base
    assert "vllm" not in runtime_base

    core_remote = "\n".join(core_extras["remote"])
    assert "openai" in core_remote
    assert "anthropic" in core_remote

    assert "abstractvision>=0.3.18" in "\n".join(core_extras["vision"])
    assert "abstractvoice>=0.10.17" in "\n".join(core_extras["voice"])
    assert "abstractvoice>=0.10.17" in "\n".join(core_extras["audio"])
    assert "abstractmusic>=0.1.12" in "\n".join(core_extras["music"])
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
    assert "abstractcore[embeddings]>=2.13.31" in embeddings

    assert "apple" in extras
    assert "gpu" in extras
    # Tooling extras remain for contributors/CI.
    assert "dev" in extras
    assert "docs" in extras

    apple = "\n".join(extras["apple"])
    assert "AbstractRuntime[apple]>=0.4.26" in apple
    assert "abstractagent[apple]>=0.3.10" in apple
    assert "abstractagent[all-apple]" not in apple
    assert "AbstractMemory[all-apple]>=0.2.6" in apple
    assert "abstractcore[" not in apple
    assert "abstractvision" not in apple
    assert "abstractvoice" not in apple
    assert "abstractmusic" not in apple
    gpu = "\n".join(extras["gpu"])
    assert "AbstractRuntime[gpu]>=0.4.26" in gpu
    assert "abstractagent[gpu]>=0.3.10" in gpu
    assert "AbstractMemory[all-gpu]>=0.2.6" in gpu
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
    assert wheel_force["flows/bundles/basic-agent@0.0.1.flow"] == "abstractgateway/flows/bundles/basic-agent@0.0.1.flow"
    assert sdist_force["flows/bundles/basic-agent.flow"] == "flows/bundles/basic-agent.flow"
    assert sdist_force["flows/bundles/basic-agent@0.0.1.flow"] == "flows/bundles/basic-agent@0.0.1.flow"


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
    assert "ghcr.io/lpalbou/abstractgateway:${ABSTRACTGATEWAY_IMAGE_TAG:-0.2.24}" in compose
    assert "ABSTRACTGATEWAY_EXTRAS: ${ABSTRACTGATEWAY_EXTRAS:-}" in compose
    assert "ABSTRACTGATEWAY_USER_AUTH: ${ABSTRACTGATEWAY_USER_AUTH:-1}" in compose
    assert "ABSTRACTGATEWAY_EXTRAS:-gpu" in nvidia_compose
    assert "ghcr.io/lpalbou/abstractgateway:${ABSTRACTGATEWAY_NVIDIA_IMAGE_TAG:-0.2.24-gpu}" in nvidia_compose
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
