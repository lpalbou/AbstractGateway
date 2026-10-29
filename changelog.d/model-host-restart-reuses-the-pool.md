### Fixed

- **After a model host restart the next chat answers** (pass three on the OVH box, 2026-10-29). The restarted child
  rebuilt every client it had held: gateway-side clients (a run summary, the sandbox, entity chat) loaded the model as
  instances of their own, and the runtime's pool was rebuilt with its boot-time default and then switched, asking for
  a second copy of the 9B model that the residency failsafe refused ("needs 7.5 GB but only 5.8 GB free"); chat
  failed after every restart until someone unloaded. Now only the runtimes' pool clients are rebuilt at once, with
  their current default as their identity (the switch is not replayed), and a gateway-side client is rebuilt by its
  next call, after the pool holds the model. `tests/test_model_host_restart_chat_e2e.py` boots a real gateway, sets
  the default, chats, kills the child, chats again, and checks the new child built the model no more often than the
  first and nothing else.
