from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


pytestmark = pytest.mark.basic


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_base_install_is_remote_light_server() -> None:
    data = _pyproject()
    deps = list(data["project"]["dependencies"])
    assert "AbstractRuntime[multimodal,mcp-worker]>=0.4.21" in deps
    assert "abstractagent>=0.3.7" in deps
    assert "abstractflow>=0.3.11" in deps
    assert "AbstractMemory[lancedb]>=0.2.6" in deps
    assert "sentence-transformers<6.0.0,>=5.1.0" in deps
    assert "numpy<3.0.0,>=1.20.0" in deps
    assert "requests<3.0.0,>=2.32.5" in deps
    assert "urllib3<3.0.0,>=2.5.0" in deps
    assert "fastapi<1.0.0,>=0.136.0" in deps
    assert "uvicorn[standard]<1.0.0,>=0.38.0" in deps
    joined = "\n".join(deps)
    assert "abstractcore[" not in joined
    assert "abstractvision" not in joined
    assert "abstractvoice" not in joined
    assert "abstractmusic" not in joined


def test_entrypoint_profiles_cascade_lower_package_extras() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]

    assert extras["http"] == []
    assert extras["server"] == []
    assert extras["multimodal"] == []
    assert extras["memory"] == []
    assert extras["voice"] == []
    assert extras["vision"] == []
    assert extras["visualflow"] == []
    assert extras["telegram"] == []
    assert extras["all"] == []
    assert "apple" in extras
    assert "gpu" in extras
    assert "all-apple" in extras
    assert "all-gpu" in extras

    apple = "\n".join(extras["apple"])
    assert "AbstractRuntime[multimodal,mcp-worker,all-apple]>=0.4.21" in apple
    assert "abstractagent[all-apple]>=0.3.7" in apple
    assert "abstractagent[apple]" not in apple
    assert "AbstractMemory[all-apple]>=0.2.6" in apple
    assert "abstractcore[" not in apple
    assert "abstractvision" not in apple
    assert "abstractvoice" not in apple
    assert "abstractmusic" not in apple
    gpu = "\n".join(extras["gpu"])
    assert "AbstractRuntime[multimodal,mcp-worker,all-gpu]>=0.4.21" in gpu
    assert "abstractagent[all-gpu]>=0.3.7" in gpu
    assert "AbstractMemory[all-gpu]>=0.2.6" in gpu
    assert "abstractcore[" not in gpu
    assert "abstractvision" not in gpu
    assert "abstractvoice" not in gpu
    assert "abstractmusic" not in gpu

    assert extras["all-apple"] == extras["apple"]
    assert extras["all-gpu"] == extras["gpu"]

    nvidia = "\n".join(extras["server-nvidia"])
    assert nvidia == gpu

    assert "AbstractMemory[all-apple]>=0.2.6" in extras["all-apple"]
    assert "AbstractMemory[all-gpu]>=0.2.6" in extras["all-gpu"]
    assert "AbstractMemory[all-gpu]>=0.2.6" in extras["server-nvidia"]


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


def test_default_docker_image_uses_base_server_and_nvidia_uses_gpu_profile() -> None:
    dockerfile = (ROOT / "docker" / "abstractgateway-server" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker" / "abstractgateway-server" / "compose.yml").read_text(encoding="utf-8")
    nvidia_compose = (ROOT / "docker" / "abstractgateway-server" / "compose.nvidia.yml").read_text(
        encoding="utf-8"
    )

    assert "ARG ABSTRACTGATEWAY_EXTRAS=" in dockerfile
    assert "ABSTRACTGATEWAY_EXTRAS: ${ABSTRACTGATEWAY_EXTRAS:-}" in compose
    assert "ABSTRACTGATEWAY_EXTRAS:-gpu" in nvidia_compose
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
