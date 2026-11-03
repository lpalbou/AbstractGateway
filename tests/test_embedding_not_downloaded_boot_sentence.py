"""An embedding default that is not downloaded: one readable sentence per process,
naming the model and the download route, never an error per account per boot.

Linux test server (2026-10-29): `[ERROR] abstractcore.embeddings.manager: Failed to load
embedding model Qwen/Qwen3-Embedding-0.6B: ... not in the local Hugging Face cache ...`
on every boot, once per account.
"""
from __future__ import annotations

import logging

import pickle

from abstractcore.exceptions import ModelNotDownloadedError, ModelNotFoundError

import abstractgateway.memory_store as ms

REPO = "Qwen/Qwen3-Embedding-0.6B"
NOT_CACHED = (f"Embedding model '{REPO}' is not in the local Hugging Face cache "
              "(looked in: /home/u/.cache/huggingface/hub); download it first: "
              f"`abstractcore models download huggingface {REPO}`.")


def not_downloaded(message: str = NOT_CACHED) -> ModelNotDownloadedError:
    return ModelNotDownloadedError(message, model=REPO, provider="huggingface")


def test_two_accounts_booting_log_one_sentence(monkeypatch, tmp_path, caplog, capsys):
    import abstractgateway.embeddings_config as ec

    monkeypatch.setattr(ms, "_EMBEDDER_NOTICES", set())
    monkeypatch.setattr(ec, "resolve_embedding_config", lambda base_dir: type("R", (), {"model": "qwen3-embedding"})())

    def refuse(*a, **k):
        raise not_downloaded()

    monkeypatch.setattr(ec, "build_embedding_client", refuse)
    caplog.set_level(logging.INFO)
    assert ms.build_gateway_memory_embedder(base_dir=tmp_path / "admin") is None
    assert ms.build_gateway_memory_embedder(base_dir=tmp_path / "bob") is None
    lines = [r for r in caplog.records if "embedding" in r.getMessage().lower()]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    msg = lines[0].getMessage()
    assert lines[0].levelno == logging.WARNING
    assert "Qwen/Qwen3-Embedding-0.6B is not downloaded" in msg
    assert "(Multimodal, Embeddings, Download)" in msg and f"`abstractcore models download huggingface {REPO}`" in msg
    assert "looked in" not in msg and "Traceback" not in msg
    err = capsys.readouterr().err
    assert err.count("[WARN] memory: Semantic memory search is off") == 1, err


def test_other_failures_still_say_why():
    msg = ms.embedder_unavailable_sentence(RuntimeError("sentence-transformers is required\\nmore"))
    assert msg.startswith("Semantic memory search is off") and "sentence-transformers is required" in msg


def test_the_service_boot_path_says_it_too():
    """The per-account service build (service.py) is where the boot error came from."""
    import inspect
    import abstractgateway.service as service

    src = inspect.getsource(service)
    i = src.index("embeddings_client = build_embedding_client(")
    assert "say_embedder_unavailable_once(e)" in src[i:i + 1200], "MUTANT: the service path stays silent -> RED"


def test_no_route_configured_says_nothing(monkeypatch, tmp_path, capsys):
    import abstractgateway.embeddings_config as ec

    monkeypatch.setattr(ms, "_EMBEDDER_NOTICES", set())

    def unconfigured(base_dir):
        raise ValueError("no embedding route configured")

    monkeypatch.setattr(ec, "resolve_embedding_config", unconfigured)
    assert ms.build_gateway_memory_embedder(base_dir=tmp_path) is None
    assert "[WARN] memory" not in capsys.readouterr().err


def test_the_model_comes_from_the_typed_error_not_its_wording():
    """AbstractCore's message is for people; the gateway reads `model` and `provider`."""
    msg = ms.embedder_unavailable_sentence(ModelNotDownloadedError("reworded entirely", model=REPO, provider="huggingface"))
    assert f"the text embedding model {REPO} is not downloaded" in msg
    assert f"`abstractcore models download huggingface {REPO}`" in msg
    # The same words on another error type are not read as "not downloaded".
    other = ms.embedder_unavailable_sentence(ModelNotFoundError(NOT_CACHED))
    assert "is not downloaded." not in other and other.startswith("Semantic memory search is off: the text embedding model could not be loaded")


def test_the_typed_error_crosses_the_model_host_unchanged():
    """With a model host (ADR 0002) the embedding client is built in the child; its error comes back
    through `_portable_exception` and a pickle, and must still be the typed error with its attributes."""
    from abstractgateway import model_host

    back = pickle.loads(pickle.dumps(model_host._portable_exception(not_downloaded())))
    assert isinstance(back, ModelNotDownloadedError) and (back.model, back.provider) == (REPO, "huggingface")
