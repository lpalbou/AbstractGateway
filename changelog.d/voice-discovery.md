### Fixed

- Speech clients can request `GET /api/gateway/discovery/capabilities?scope=voice`
  to check TTS/STT without waiting for unrelated media discovery. The existing
  full discovery response and authentication remain unchanged.
