# 0944-gateway: The OpenAI-compatible endpoint runs as a separately supervised process

> Package: abstractgateway
> Type: reliability / security
> Status: proposed
> Created: 2026-10-27
> Priority: P1
> Labels: resilience, isolation, openai-endpoint, security, load

## Summary

The OpenAI-compatible API (`/v1`, `core_endpoint.py`, mounted in `app.py`) is served by the gateway process
itself: inference traffic, a flood of requests or a misbehaving client shares the event loop, memory and
fate of the gateway that also serves people's sessions, the console and the runner. A denial-of-service
or a provider hang on `/v1` must not take the gateway down.

Spawn the endpoint as its own process controlled by the gateway (like split mode's `abstractgateway runner`
and the push relay): the gateway starts it with its configuration and credentials, proxies or hands the
listener over, watches it (health, restarts with backoff, incident records per 0943), and can stop, start
and rate-limit it from the console and CLI. The gateway keeps authentication, reach rules and the request
log; the child does the serving. Its crash is an incident, not an outage.

## Why

Root 1060 (standing duty): "the openai endpoint that is launchable by the framework should probably be
spawned as a separate service (controllable by gateway) so that if it fails (e.g. ddos), it doesn't crash
the gateway. the gateway is gonna need to be (a) very secured and (b) resilient to failures and heavy
loads."

## Approach

1. `abstractgateway openai-endpoint` subcommand (the child), configured from the same resolved settings
   as today; listens on its own port or an inherited socket.
2. Gateway side: a supervisor (reuse the tray supervisor's restart policy) with health, backoff, max
   restarts per window, memory/CPU ceilings, and the 0943 incident writer; `/v1` on the gateway becomes a
   reverse proxy or a redirect to the child, keeping auth and reach rules in the gateway.
3. Load drill: a reproducible flood of `/v1/chat/completions` (and a hung provider) proves the gateway
   console and sessions stay responsive (p95 under the budget) while the child saturates and restarts.
4. Same pattern offered to other public listeners (triggers ingress, push relay already external).

## Validation

- Tests: child lifecycle (start, health, crash, restart, stop), proxy auth parity with the authorization
  contract test, a load test with numbers in the release report.
- Docs: configuration.md, architecture.md (process layout), troubleshooting.md (the child's incidents).

## Links

- Root 1060; 0943; `core_endpoint.py`, `routes/core_endpoint.py`, `openai_keys.py`, split mode docs;
  framework ADR-0033, Core ADR-0004.
