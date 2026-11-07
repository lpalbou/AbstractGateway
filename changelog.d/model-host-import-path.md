### Fixed

- **The model worker never imports from the data directory, whatever `PYTHONPATH` looks like**
  (`model_host.py`, `CHILD_BOOTSTRAP`). 0.20.2 dropped only the `''` entry from the worker's import
  path, but Python also puts the working directory (the data directory, which holds cache folders
  named `abstractcore/embeddings`) on the path for an empty `PYTHONPATH` entry (`:/x`, `/x::`), for
  `.`, and for the directory or a symlink to it; a cache folder could then shadow AbstractCore again.
  Every entry that resolves to the working directory is now dropped. The stale
  `python -m abstractgateway.model_host_child` mentions (module docstrings, ADR 0002, a test) now
  name the real start command.
