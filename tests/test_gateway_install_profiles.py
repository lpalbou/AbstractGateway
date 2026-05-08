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


def test_base_install_stays_runtime_only() -> None:
    data = _pyproject()
    deps = list(data["project"]["dependencies"])

    assert deps == ["AbstractRuntime>=0.4.8"]


def test_entrypoint_profiles_cascade_lower_package_extras() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]

    assert "memory" in extras
    assert "apple" in extras
    assert "gpu" in extras
    assert "all-apple" in extras
    assert "all-gpu" in extras

    server = "\n".join(extras["server"])
    assert "AbstractRuntime[multimodal]>=0.4.8" in server
    assert "abstractcore[remote,media,tools,tokens,compression,vision,voice,audio]>=2.13.12" in server
    assert "abstractvision>=0.3.3" in server
    assert "abstractvoice>=0.9.2" in server

    assert extras["memory"] == ["AbstractMemory[lancedb]>=0.2.4"]
    apple = "\n".join(extras["apple"])
    assert "AbstractRuntime[multimodal,all-apple]>=0.4.8" in apple
    assert "abstractagent[all-apple]>=0.3.2" in apple
    assert "abstractcore[all-apple]>=2.13.12" in apple
    assert "abstractvision[all-apple]>=0.3.3" in apple
    assert "abstractvoice[all-apple]>=0.9.2" in apple
    assert "abstractmusic[all-apple]>=0.1.1" in apple
    assert "AbstractMemory[all-apple]>=0.2.4" in apple
    gpu = "\n".join(extras["gpu"])
    assert "AbstractRuntime[multimodal,all-gpu]>=0.4.8" in gpu
    assert "abstractagent[all-gpu]>=0.3.2" in gpu
    assert "abstractcore[all-gpu]>=2.13.12" in gpu
    assert "abstractvision[all-gpu]>=0.3.3" in gpu
    assert "abstractvoice[all-gpu]>=0.9.2" in gpu
    assert "abstractmusic[all-gpu]>=0.1.1" in gpu
    assert "AbstractMemory[all-gpu]>=0.2.4" in gpu

    nvidia = "\n".join(extras["server-nvidia"])
    assert "abstractcore[all-gpu,vision-diffusers]>=2.13.12" in nvidia
    assert "abstractvision[diffusers]>=0.3.3" in nvidia
    assert "abstractvoice[local]>=0.9.2" in nvidia

    assert "AbstractMemory[all-apple]>=0.2.4" in extras["all-apple"]
    for name in ("all-gpu", "server-nvidia", "dev", "all"):
        expected = "AbstractMemory[all-gpu]>=0.2.4" if name == "all-gpu" else "AbstractMemory[lancedb]>=0.2.4"
        assert expected in extras[name]


def test_config_entrypoint_is_published() -> None:
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["abstractgateway"] == "abstractgateway.cli:main"
    assert scripts["abstractgateway-config"] == "abstractgateway.config_cli:main"


def test_default_docker_image_composes_server_and_memory_profiles() -> None:
    dockerfile = (ROOT / "docker" / "abstractgateway-server" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker" / "abstractgateway-server" / "compose.yml").read_text(encoding="utf-8")
    nvidia_compose = (ROOT / "docker" / "abstractgateway-server" / "compose.nvidia.yml").read_text(
        encoding="utf-8"
    )

    assert "ARG ABSTRACTGATEWAY_EXTRAS=server,memory" in dockerfile
    assert "ABSTRACTGATEWAY_EXTRAS:-server,memory" in compose
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
    assert 'pip install "abstractgateway[all-apple]"' in docs
    assert "not Docker" in docs or "not packaged as a Docker image" in docs
