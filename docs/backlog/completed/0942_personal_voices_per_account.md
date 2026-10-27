# 0942-gateway: [FEATURE] Personal voices per account, voices an administrator deploys for everyone, and an AI's voice in its profile

> Package: abstractgateway (routes, per-account wiring); abstractvoice (stores, its backlog 0065); abstractruntime (per-account config, voice facade scope)
> Type: feature
> Status: completed (2026-10-27, 0.16.0)
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

AbstractVoice 0065 released; AbstractRuntime 0851 (the per-account voice settings and the facade's scope); 0938 for the profile record.

## Validation

Two accounts: A designs a voice, speaks with it, B does not see it; the administrator deploys a voice, both see and use it, neither can delete it; deleting A removes A's voices. Non-admin creation allowed, library changes refused (route authorization contract test).

## Report (completed 2026-10-27, 0.16.0)

Built on AbstractVoice 0.19.0 (0065: personal stores in front of read-only libraries) and AbstractRuntime 0.13.0 (0851:
a runtime's voice library and catalog use its synthesis registry and stores). `hosts/bundle_host.py`
`personal_voice_store_dir`: in multi-user mode, every account plane other than the root (the administrator's) gets
`voice_store_dir = <plane>/voices` in its runtime's AbstractCore settings; the library is the machine's shared store
(AbstractVoice's default when no library folder is given), so the administrator's plane IS the library and whatever the
administrator creates is everyone's (no separate deploy route was needed). `routes/voice_library.py`
`_require_change_rights`: an administrator, or an account whose runtime has a personal store, may create, rename,
delete and design; otherwise 403 (single-user mode, an AbstractCore server doing the voice work, AbstractVoice < 0.19.0).
The static admin row was replaced by the in-handler gate (route authorization contract: USER_LEVEL_WRITES).
`language` on `POST /voice/clones`. Not done: entity profiles with avatar and image (0938); a dedicated deploy route
for an account's own voice (the administrator creates library voices directly).

Validation: `tests/test_gateway_voice_library.py` (shared store: 403 for a non-admin; own store: changes allowed,
language forwarded); real weights, a multi-user gateway with two accounts, 11/11 over HTTP (design, personal scope,
invisible to the other account and to the administrator, library visible and read-only, speech, rename, delete,
library unchanged).

