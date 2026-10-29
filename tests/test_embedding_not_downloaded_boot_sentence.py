"""An embedding default that is not downloaded: one readable sentence per process,
naming the model and the download route, never an error per account per boot.

Linux test server (2026-10-29): `[ERROR] abstractcore.embeddings.manager: Failed to load
embedding model Qwen/Qwen3-Embedding-0.6B: ... not in the local Hugging Face cache ...`
on every boot, once per account.
"""
from __future__ import annotations

import logging

import abstractgateway.memory_store as ms

NOT_CACHED = ("Embedding model 'Qwen/Qwen3-Embedding-0.6B' is not in the local Hugging Face cache "
              "(looked in: /home/u/.cache/huggingface/hub); download it first: "
              "`abstractcore models download huggingface Qwen/Qwen3-Embedding-0.6B`.")


def test_two_accounts_booting_log_one_sentence(monkeypatch, tmp_path, caplog, capsys):
    import abstractgateway.embeddings_config as ec

    monkeypatch.setattr(ms, "_EMBEDDER_NOTICES", set())
    monkeypatch.setattr(ec, "resolve_embedding_config", lambda base_dir: type("R", (), {"model": "qwen3-embedding"})())

    def refuse(*a, **k):
        raise RuntimeError(NOT_CACHED)

    monkeypatch.setattr(ec, "build_embedding_client", refuse)
    caplog.set_level(logging.INFO)
    assert ms.build_gateway_memory_embedder(base_dir=tmp_path / "admin") is None
    assert ms.build_gateway_memory_embedder(base_dir=tmp_path / "bob") is None
    lines = [r for r in caplog.records if "embedding" in r.getMessage().lower()]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    msg = lines[0].getMessage()
    assert lines[0].levelno == logging.WARNING
    assert "Qwen/Qwen3-Embedding-0.6B is not downloaded" in msg
    assert "Models, Defaults, Text Embeddings, Download" in msg and "abstractcore models download huggingface" in msg
    assert "looked in" not in msg and "Traceback" not in msg
    err = capsys.readouterr().err
    assert err.count("[WARN] memory: Semantic memory search is off") == 1, err


def test_other_failures_still_say_why():
    msg = ms.embedder_unavailable_sentence(RuntimeError("sentence-transformers is required\\nmore"))
    assert msg.startswith("Semantic memory search is off") and "sentence-transformers is required" in msg
