# 0938-abstractgateway: [FEATURE] An AI's identity presentation (avatar, voice, image) defined once, shared by every client

> Package: abstractgateway (contract and route), abstractruntime (the identity record), clients adopt (AbstractAssistant desktop, iPhone, Watch; AbstractNote web)
> Type: feature
> Created: 2026-10-26
> Priority: P2
> Labels: identity, entities, avatar, voice, clients, settings

## Summary

Let the runtime hold, for the Assistant and for each entity, how it presents itself: an avatar
(today the orb style, a named design the clients draw themselves), a voice (a TTS voice, per
language where the engine has them) and, optionally, a still image. The gateway serves it to
every client, so the same AI looks and sounds the same on the Mac, the iPhone, the watch face and
the web, and a person who changes it in one place changes it everywhere.

## Why

Operator, 2026-10-26: reinforce the identity of an AI with a specific voice and a specific
image or avatar, defined at the runtime/gateway level so they are shared across apps. Today each
client keeps its own: the watch's orb style is a per-app `AppStorage` value
(`abstractassistant/watch/apple/Sources/App/AbstractAssistantWatchApp.swift:10`, drawn into the
watch face by `FaceSnapshot.drawAvatar`), the desktop's voice is a per-app route override
(`output.voice` in its preferences), the iPhone has its own orb. An entity summoned on the
desktop and visited from the watch has no shared face and no shared voice: the identity lives in
its memory and persona, not in how it appears.

## Current code reality

- `abstractruntime/src/abstractruntime/identity/`: entities have a runtime (`entity_runtime.py`),
  a diary, a fingerprint, communities; no presentation fields (no avatar, voice or image) were
  found in that package.
- Gateway entity routes (`src/abstractgateway/routes/session_tools.py`, `entity_replay.py`) serve
  sessions, visits, tools and replays; nothing about presentation. The voice routes resolve a
  TTS voice per request (`/voice/tts`, the capability contract's `assistant.voice.tts`).
- Clients: orb designs are an enum in each Swift app (`OrbDesign`, 20-odd styles, drawn
  procedurally); the desktop tray has its own icon set. Voices are chosen per client in settings.

## Proposed direction

- **One record per principal AI** (the Assistant of a user plane, and each entity), stored by
  the runtime beside the entity's identity: `presentation: {avatar: {kind: "orb", design:
  "<name>"} | {kind: "image", artifact: "<$artifact>"}, voice: {provider?, model?, voice,
  language?}, image?: "<$artifact>" (a still portrait, for places that cannot animate),
  updated_at}`. Unset fields fall back to the gateway's defaults, then the client's.
- **Served by the gateway**: `GET /api/gateway/me/presentation` (the Assistant) and
  `GET /api/gateway/entities/{name}/presentation`, `PUT` on both (the owner, or an admin for
  shared entities), in the capability contract so a client knows the route exists. The TTS
  routes take the record's voice when the request names none.
- **Clients adopt, do not fork**: the Swift apps keep drawing the orb designs (the design name is
  the contract, the drawing is the client's), the watch face follows the record, the desktop
  tray too; a client-local choice remains possible only as "override on this device", shown as
  such. AbstractNote's web app reads the same record for the Assistant's face in its chat.
- **Entities first-class**: an entity's presentation travels with it when it is shared or
  summoned elsewhere; a person meeting an entity on another device sees and hears the same one.

## Non-goals

No voice cloning, no generated portraits in this item (an image is an uploaded artifact). No
per-conversation presentation. No change to what the orb designs look like.

## Promotion criteria

- The runtime seat agrees on where the record lives (the entity's identity store, or the plane's
  settings for the Assistant) and the gateway seat on the two routes.
- One client (the watch, whose face already draws the avatar from a snapshot) adopts it as the
  proof.

## Validation ideas

- A PUT from one client is read by another within one poll; the watch face redraws.
- An entity summoned on a second plane carries its presentation.
- TTS without a voice in the request speaks with the record's voice.
