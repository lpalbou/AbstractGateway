# ADR 0001: AbstractCore access goes through AbstractRuntime facades

Status: Accepted.
Date: 2026-10-27

## Context

The gateway is a host of AbstractRuntime: it starts and replays runs, serves catalogs, manages
accounts and mail. The runtime owns the AbstractCore integration (providers, media, discovery,
mail, voice) and exposes it to hosts through facades (`abstractruntime.integrations.abstractcore.*_facade`).
Importing AbstractCore directly from the gateway couples it to Core's internals, makes the
gateway track Core's version floors itself, and lets a Core change break two packages at once.
The boundary was declared clean in backlog 0050, regressed in 0.2.26/0.2.27, and has been
guarded by `tests/test_gateway_import_boundary.py` since backlog 0059. On 2026-10-27 it regressed
again: the mailbox access code needed AbstractCore's SRV lookup, the runtime's email facade did
not re-export it, and the direct import was written instead of asking the runtime. The test was
red across a whole release line because no written rule said what to do when a facade lacks a
name.

## Decision

1. Gateway source (`src/abstractgateway/`) imports `abstractruntime` and never `abstractcore`.
   The only exemptions are the process-bootstrap ones named one by one in
   `tests/test_gateway_import_boundary.py` (each with the reason it cannot go through the
   runtime); the list does not grow for convenience.
2. When the gateway needs an AbstractCore name no facade carries, the fix is in AbstractRuntime
   (add the re-export or method, cut a patch release: runtime ADR 0008), and the gateway raises
   its runtime floor to that release. The gateway never imports the name directly "for now",
   never copies the code, and never guesses a replacement.
   This covers whole features and capabilities, not only names, and every lower-level package
   (AbstractCore, AbstractVoice, AbstractMemory, their dependencies): a capability the gateway
   lacks is surfaced by the runtime, which does the integration (a facade method, a typed
   refusal such as a 501 with the install hint, or a runtime backlog item). A missing capability
   is never a justification for a direct import of a lower-level package.
3. A gateway change that needs a facade addition ships after the runtime release that carries
   it; its changelog entry names that runtime version, and `pyproject.toml` pins it.

## Consequences

### Positive
- One dependency to reason about: the gateway's AbstractCore behaviour is whatever the pinned
  runtime provides, and Core upgrades land in one place.
- The boundary test is a true release gate.

### Negative
- A feature needing one Core name waits for a runtime patch (about 20 minutes of release work).
- Runtime and gateway releases are sequenced: runtime first.

### Neutral
- The runtime carries thin re-exports on the gateway's behalf (its ADR 0008 accepts that).

## Enforcement
- `tests/test_gateway_import_boundary.py` fails on any `abstractcore` import in gateway
  source; a PR that adds an exemption must cite this ADR and say why the runtime cannot carry
  the name.
- The same test fails on any `abstractmemory` import in gateway source outside
  `_ABSTRACTMEMORY_BURN_DOWN` (file, statement, count), and on an entry whose import is gone,
  so the list only shrinks; AbstractMemory names come from AbstractRuntime's memory facade
  (runtime ADR 0009). Added 2026-10-27 with 34 entries (8 files), burned down to 0 (empty)
  on the same branch (backlog 0943).
- Code review refuses "temporary" direct imports; the reviewer asks for the runtime PR instead.
- Backlog items that touch mail, media or discovery name the runtime facade they use.

## Validation
- The boundary test on every CI run.
- `tests/test_gateway_install_profiles.py`: the gateway's runtime floor is at least the release
  that carries every facade name the gateway imports (the floor moves with the need).

## Backlog links
- 0050 (boundary declared clean), 0059 (the guard test), 1044 (mailbox Read and write access,
  the SRV lookup that needed runtime 0.13.1).

## Related
- AbstractRuntime ADR 0001 (layered coupling), 0005 (discovery queries), 0008 (integration
  facades are the complete host surface).
- `docs/architecture.md` ("AbstractCore, reached through Runtime facades").
