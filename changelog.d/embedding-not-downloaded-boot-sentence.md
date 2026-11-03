### Fixed

- **A text embedding model that is not downloaded is one readable sentence per process**, not an error per account
  on every boot: "Semantic memory search is off: the text embedding model Qwen/Qwen3-Embedding-0.6B is not
  downloaded. Download it from the console (Multimodal, Embeddings, Download) or run `abstractcore models download
  huggingface Qwen/Qwen3-Embedding-0.6B`; structured memory keeps working." The model and provider come from
  AbstractCore's typed `ModelNotDownloadedError` (its `model` and `provider` attributes), never from the error's
  wording; this needs the AbstractCore release that adds that error.
