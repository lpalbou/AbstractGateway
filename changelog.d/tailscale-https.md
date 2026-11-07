### Added

- **HTTPS on the Tailscale name, automatic** (`tailscale_https.py`). While it runs, `serve` serves
  `https://<host>.<tailnet>.ts.net` itself (`tailscale serve` started in the foreground as the gateway's
  child), so phones and other computers on the tailnet get voice, camera and the clipboard with no setup.
  The address exists only while the gateway is up: Tailscale removes it when the command ends, and a
  watchdog ends the command within a second when the gateway stops abruptly. When it cannot be served yet
  (Tailscale down, HTTPS certificates off), the gateway checks again every 30 s, so Tailscale coming up
  later needs no restart. An existing `tailscale serve` or `tailscale funnel` on port 443 is never
  replaced. On by default; `serve --tailscale-https off` turns it off. `GET /api/gateway/network` reports
  `tailscale_https` (`off | unavailable | taken | starting | active | failed`, url, target, reason), and the
  console's Network page (web and terminal) shows it instead of the manual `tailscale serve --bg`
  instruction.
