# 0942-gateway: [FEATURE] Personal voices per account, voices an administrator deploys for everyone, and an AI's voice in its profile

> Package: abstractgateway (routes, per-account wiring); abstractvoice (stores, its backlog 0065); abstractruntime (per-account config, voice facade scope)
> Type: feature
> Status: planned
> Created: 2026-10-27
> Priority: P1
> Labels: voice, multi-user, isolation, entities, identity

## Summary

Each account registers the voices it wants (clone from a recording, design from a description) in its own voice store: only that account lists, uses, renames and deletes them. An administrator can still deploy a voice for everyone (a shared library every account reads but cannot change). An account then picks any voice it can see for its assistant, coder or entities, as part of that AI's profile (with its avatar and image: backlog 0938).

## Why

Operator, 2026-10-27: "we would like to have proper isolation between the voices cloned for each user ... users should be allowed to register the voices they want, and this should be personal, not shared by all. An admin should still be able to deploy new voices for all ... I want for entities that they have a full profile: an image/photo, a specific voice, etc."

Today every account shares one voice store (AbstractVoice's machine-user folder), so the voice library's changes are admin-only (0.16.0, `security/authorization.py` resource `voice_library`): one account's change would change every account's voices, and a recording cloned by one account would be available to all.

## Current code reality

- `src/abstractgateway/routes/voice_library.py` (0.16.0): reads open to every account; `POST /voice/clones`, `PATCH`/`DELETE /voice/clones/{id}`, `POST /voice/design` admin-only.
- The gateway builds one runtime (and AbstractCore client) per account plane (`service.get_gateway_service`), so a per-account voice store folder can travel in that runtime's config.
- Entities keep a voice choice in `<home>/voice.yaml` (`entity_voice.py`); the presentation record is proposed in 0938.

## Scope

- In: each account's voice folder in its plane (`<plane>/voices/`), passed to AbstractVoice (0065: `voice_store_dir`, `voice_library_dirs`); the voice library routes act on the caller's personal store for every account (the admin row on `voice_library` relaxed to the library routes only); `POST /voice/library` (admin: deploy a voice for everyone, or promote one of theirs); listings carry `scope: personal|library`; an account's deletion removes its voices; the entity and assistant voice pickers list personal + library voices.
- Out: the avatar/image half of the profile (0938).

## Dependencies

AbstractVoice 0065 released; AbstractRuntime voice facade passing the scope (small additive change); 0938 for the profile record.

## Validation

Two accounts: A designs a voice, speaks with it, B does not see it; the administrator deploys a voice, both see and use it, neither can delete it; deleting A removes A's voices. Non-admin creation allowed, library changes refused (route authorization contract test).
