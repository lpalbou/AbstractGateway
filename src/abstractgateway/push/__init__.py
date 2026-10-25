"""Apple push for the watch and the iPhone apps (framework backlog 1049).

- `settings`: the process flag `--push-relay <URL|off>` (CLI only, never an environment variable).
- `store`: the per-plane device registrations, the gateway-wide token owners, the stream cursor and
  the SQLite outbox, all under `<data dir>/push/`.
- `dispatcher`: turns `attention.raised` events of the plane owner's stream into relay requests.
- `relay`: `abstractgateway push-relay`, the stateless publisher-hosted service that holds the APNs
  key and builds a fixed, localized message (no text ever crosses it).
- `routes/push.py`: `PUT/DELETE/GET /api/gateway/me/push/devices…`.
"""
