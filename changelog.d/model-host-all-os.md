### Changed

- **The model host is the default on macOS and Windows too** (operator ruling 2026-10-29: "BUILD FOR ALL OS ... we must
  be consistent across OS"). `serve` runs the models in the supervised child on Linux, macOS and Windows; `in-process`
  only with `--reload` or when chosen. On a Mac MLX and Metal run in the child (measured with Qwen3.6-27B-4bit and the
  tray running: `kill -9` mid-chat fails the chat in 0.11 s with the sentence, `/api/health` 200 throughout, the child
  back in 0.77 s, warm chats as fast as in-process); the child warms its imports after it reports ready. On Windows the
  child runs without a console window (named-pipe transport); a CI job (`model-host-windows`) runs its life cycle there.
